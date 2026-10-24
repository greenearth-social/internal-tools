"""Argument handling and dispatch."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import assets, doctor, info, live, pull, stack, suites
from .ctx import DEFAULT_INSTANCE, Ctx, Die, die
from .envfiles import load_local_env
from .instance import load_state, resolve_ports, set_instance
from .runner import CommandFailed, Runner
from .usage import usage

# Commands not yet ported are handed, argv untouched, to the bash original.
LEGACY = "devctl-legacy"

HELP_FLAGS = ("-h", "--help")


@dataclass(frozen=True)
class Command:
    run: Callable[[Ctx, list[str]], int | None]
    # How ports are resolved before the command runs. "read" for nearly
    # everything: an instance with no ports recorded doesn't exist, and saying
    # so beats acting on the wrong one. None for commands that work without an
    # instance (or, like `up`, claim ports themselves after parsing flags).
    ports: str | None = "read"
    # Refuse any argument rather than ignore it.
    no_args: bool = False
    # The underlying tool answers --help, so it isn't usage for us.
    passthrough: bool = False


def cmd_help(ctx: Ctx, args: list[str]) -> None:
    usage()


COMMANDS: dict[str, Command] = {
    "": Command(cmd_help, ports=None),
    "help": Command(cmd_help, ports=None),
    "-h": Command(cmd_help, ports=None),
    "--help": Command(cmd_help, ports=None),
    "ports": Command(info.cmd_ports),
    "ls": Command(info.cmd_ls, ports=None, no_args=True),
    "instances": Command(info.cmd_ls, ports=None, no_args=True),
    "es": Command(info.cmd_es, passthrough=True),
    "logs": Command(info.cmd_logs),
    "status": Command(info.cmd_status, no_args=True),
    "ps": Command(info.cmd_status, no_args=True),
    "doctor": Command(doctor.cmd_doctor, no_args=True),
    "exec": Command(info.cmd_exec, passthrough=True),
    "test": Command(suites.cmd_test),
    "login": Command(info.cmd_login),
    "pull": Command(pull.cmd_pull, ports=None),
    "fetch-fixture": Command(assets.cmd_fetch_fixture, ports=None, no_args=True),
    "fetch-models": Command(assets.cmd_fetch_models, ports=None, no_args=True),
    "use-ranker": Command(assets.cmd_use_ranker, ports=None),
    "setup": Command(stack.cmd_setup, ports=None, no_args=True),
    "tunnel": Command(live.cmd_tunnel),
    # up and bootstrap claim their ports themselves, after parsing flags.
    "up": Command(stack.cmd_up, ports=None),
    "bootstrap": Command(stack.cmd_bootstrap, ports=None),
    "seed": Command(stack.cmd_seed, no_args=True),
    "restart": Command(stack.cmd_restart),
    "down": Command(stack.cmd_down, no_args=True),
    "nuke": Command(stack.cmd_nuke, no_args=True),
}


@dataclass
class Invocation:
    instance: str
    command: str
    args: list[str]


def parse_global(argv: Sequence[str], default_instance: str) -> Invocation:
    """Strip `--name <n>` from anywhere in argv.

    The flag is global rather than per-command — every command has to act on
    the same instance. The exception is what follows `exec`'s service: that
    belongs to the command being run inside the container and must reach it
    untouched. So after `exec`, only `--name` pairs straight after it are
    taken (`devctl exec --name alpha api ...`), and everything from the
    service on passes through.
    """
    instance = default_instance
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--name":
            if i + 1 >= len(argv) or not argv[i + 1]:
                die("--name needs an instance name")
            instance = argv[i + 1]
            i += 2
        elif arg == "exec":
            rest.append(arg)
            i += 1
            while i < len(argv) and argv[i] == "--name":
                if i + 1 >= len(argv) or not argv[i + 1]:
                    die("--name needs an instance name")
                instance = argv[i + 1]
                i += 2
            rest.extend(argv[i:])
            break
        else:
            rest.append(arg)
            i += 1
    command = rest[0] if rest else ""
    return Invocation(instance, command, rest[1:])


def hand_off_to_legacy(devenv: Path, argv: Sequence[str], environ: Mapping[str, str]) -> None:
    legacy = str(devenv / LEGACY)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(legacy, [legacy, *argv], dict(environ))


def run(
    argv: Sequence[str],
    *,
    devenv: Path,
    environ: Mapping[str, str],
    runner: Runner | None = None,
) -> int:
    env = dict(environ)
    load_local_env(devenv, env)
    inv = parse_global(argv, env.get("GE_DEV_INSTANCE") or DEFAULT_INSTANCE)

    command = COMMANDS.get(inv.command)
    if command is None:
        hand_off_to_legacy(devenv, argv, environ)
        return 0  # not reached

    ctx = Ctx(devenv=devenv, env=env, runner=runner or Runner())
    # Everything after this — the live-mode record, the minted keys, the
    # compose project — is per instance, so it settles first.
    set_instance(ctx, inv.instance)
    load_state(ctx)

    # A command must never act on an argument it doesn't read: `seed --help`
    # used to go ahead and wipe and reload Elasticsearch (issue #42). So --help
    # means usage everywhere except the passthrough commands, whose underlying
    # tool answers it — and before port resolution, so it works on an instance
    # that doesn't exist yet.
    if not command.passthrough and any(a in HELP_FLAGS for a in inv.args):
        usage()
        return 0
    if command.no_args and inv.args:
        die(f"{inv.command} takes no arguments, got: {' '.join(inv.args)} (try: devctl help)")

    if command.ports is not None:
        resolve_ports(ctx, command.ports)
    return command.run(ctx, inv.args) or 0


def main(argv: Sequence[str] | None = None, devenv: str | None = None) -> int:
    """Entry point. devenv is the launcher's own directory, as the shell
    spells it (symlinks unresolved), so derived paths read the way they did."""
    devenv_dir = Path(devenv) if devenv else Path(__file__).resolve().parent.parent
    try:
        return run(
            list(sys.argv[1:] if argv is None else argv), devenv=devenv_dir, environ=os.environ
        )
    except Die as err:
        sys.stdout.flush()
        print(f"devctl: {err}", file=sys.stderr)
        return 1
    except CommandFailed as err:
        return err.returncode
    except KeyboardInterrupt:
        return 130
