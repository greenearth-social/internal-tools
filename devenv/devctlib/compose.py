"""docker compose, as this instance's project."""

from __future__ import annotations

import json
import sys

from .ctx import Ctx
from .runner import Result

COMPOSE_FILES = ("docker-compose.yml", "docker-compose.bridgeu.yml")
RANKER_OVERRIDE = "ranker.compose.json"


def compose_argv(ctx: Ctx, *args: str) -> list[str]:
    """`docker compose ...` over both halves of the stack, as one project:
    mysky (docker-compose.yml) and BridgEU (docker-compose.bridgeu.yml), which
    only reaches mysky through its api. See the header of the BridgEU file."""
    argv = ["docker", "compose"]
    for name in COMPOSE_FILES:
        argv += ["-f", str(ctx.devenv / name)]
    # A selected ranker belongs to this instance and survives fetching a new
    # shared model bundle. Its environment overrides the bundle's env_file.
    override = ctx.runtime / RANKER_OVERRIDE
    if override.is_file():
        argv += ["-f", str(override)]
    return [*argv, "--project-directory", str(ctx.devenv), *args]


def compose(ctx: Ctx, *args: str, **kwargs) -> Result:
    return ctx.run(compose_argv(ctx, *args), **kwargs)


def exec_argv(ctx: Ctx, *args: str, tty: bool | None = None) -> list[str]:
    """compose exec with a TTY when stdout is one (so pytest and rich colorize
    and ^C reaches the process) and -T when piped, where allocation would fail.
    PIPENV_VERBOSITY silences pipenv's "existing virtualenv" warning per call."""
    if tty is None:
        tty = sys.stdout.isatty()
    flags = [] if tty else ["-T"]
    return compose_argv(ctx, "exec", *flags, "-e", "PIPENV_VERBOSITY=-1", *args)


def compose_exec(ctx: Ctx, *args: str, **kwargs) -> Result:
    # A captured exec never has a terminal on the other end.
    tty = None if not kwargs.get("capture") else False
    return ctx.run(exec_argv(ctx, *args, tty=tty), **kwargs)


def service_running(ctx: Ctx, service: str) -> bool:
    result = compose(
        ctx, "ps", "--status", "running", service, check=False, capture=True, quiet_err=True
    )
    return service in result.stdout


def running_services(ctx: Ctx) -> list[str]:
    result = compose(
        ctx,
        "ps",
        "--status",
        "running",
        "--format",
        "{{.Service}}",
        check=False,
        capture=True,
        quiet_err=True,
    )
    return sorted(line for line in result.stdout.splitlines() if line.strip())


def oom_killed_services(ctx: Ctx) -> list[str]:
    """Services whose last exit was 137 — SIGKILL, which here almost always
    means the kernel reclaimed memory rather than anything about the service."""
    result = compose(
        ctx,
        "ps",
        "-a",
        "--format",
        "{{.Service}} {{.Status}}",
        check=False,
        capture=True,
        quiet_err=True,
    )
    return [
        line.split()[0]
        for line in result.stdout.splitlines()
        if "Exited (137)" in line and line.split()
    ]


def running_projects(ctx: Ctx) -> set[str]:
    """The compose projects docker reports as running ("ge-dev", "ge-alpha"...)."""
    result = ctx.run(
        ["docker", "compose", "ls", "--format", "json"],
        check=False,
        capture=True,
        quiet_err=True,
    )
    try:
        return {p["Name"] for p in json.loads(result.stdout)}
    except (ValueError, TypeError, KeyError):
        return set()
