"""Test support: a recording Runner, and a sandboxed devenv to run devctl in.

Not imported by devctl itself.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NoReturn

from .ctx import Ctx
from .runner import CommandFailed, Result, Runner

DEVENV_DIR = Path(__file__).resolve().parent.parent

# How the launcher starts devctl, minus the venv: tests run it with their own
# interpreter, which has the same dependencies.
BOOT = (
    "import sys\n"
    "devenv = sys.argv.pop(1)\n"
    "sys.path.insert(0, devenv)\n"
    "from devctlib.cli import main\n"
    "sys.exit(main(sys.argv[1:], devenv))\n"
)


def devctl_argv(devenv: Path, *args: str) -> list[str]:
    return [sys.executable, "-I", "-c", BOOT, str(devenv), *args]


# --- unit tests --------------------------------------------------------------


@dataclass
class Call:
    argv: list[str]
    env: dict[str, str]
    options: dict

    @property
    def line(self) -> str:
        return " ".join(self.argv)


class Exec(Exception):
    """Raised by FakeRunner.exec in place of replacing the process."""

    def __init__(self, argv: Sequence[str]):
        super().__init__(" ".join(argv))
        self.argv = list(argv)


Response = Result | int | str | None


class FakeRunner(Runner):
    """Records every command instead of running it.

    respond(argv) decides what a command returns: a Result, an exit code, the
    stdout of a successful run, or None for success with no output.
    """

    def __init__(self, respond: Callable[[list[str]], Response] | None = None):
        self.calls: list[Call] = []
        self.respond = respond

    def run(self, argv, *, env, check=True, **options) -> Result:
        argv = list(argv)
        self.calls.append(Call(argv, dict(env), options))
        response = self.respond(argv) if self.respond else None
        if response is None:
            result = Result(0)
        elif isinstance(response, int):
            result = Result(response)
        elif isinstance(response, str):
            result = Result(0, response)
        else:
            result = response
        if check and not result.ok:
            raise CommandFailed(argv, result.returncode)
        return result

    def exec(self, argv, *, env) -> NoReturn:
        self.calls.append(Call(list(argv), dict(env), {"exec": True}))
        raise Exec(argv)

    @property
    def lines(self) -> list[str]:
        return [c.line for c in self.calls]

    def compose_lines(self) -> list[str]:
        """Compose invocations with the `docker compose -f ... --project-directory
        <dir>` prefix dropped, so tests read as the subcommand alone."""
        out = []
        for call in self.calls:
            argv = call.argv
            if argv[:2] == ["docker", "compose"] and "--project-directory" in argv:
                i = argv.index("--project-directory")
                out.append(" ".join(argv[i + 2 :]))
        return out


def fake(ctx: Ctx) -> FakeRunner:
    """The FakeRunner behind a test's ctx."""
    assert isinstance(ctx.runner, FakeRunner)
    return ctx.runner


def make_ctx(
    tmp_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    runner: Runner | None = None,
    instance: str = "dev",
    ports: Mapping[str, int] | None = None,
) -> Ctx:
    """A Ctx over a scratch devenv at tmp_path/internal-tools/devenv."""
    from . import ports as port_table

    devenv = tmp_path / "internal-tools" / "devenv"
    devenv.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(
        devenv=devenv,
        env={"PATH": os.environ["PATH"], **(env or {})},
        runner=runner or FakeRunner(),
        instance=instance,
    )
    ctx.offset = 0
    ctx.ports = dict(ports or port_table.BASE_PORTS)
    ctx.env.setdefault("GE_DEV_REPO_ROOT", str(tmp_path))
    ctx.runtime.mkdir(parents=True, exist_ok=True)
    return ctx


# --- sandboxed end-to-end runs -----------------------------------------------

# Not copied into a sandbox: generated, large, or per-developer.
_SKIP = shutil.ignore_patterns(
    ".runtime",
    ".runtime.*",
    ".venv-devctl",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "devenv.local.env",
    "data",
    "data.staging",
)

FAKE_TOOL = """\
import json, os, re, sys
name = os.path.basename(sys.argv[0])
argv = [name, *sys.argv[1:]]
env = {k: v for k, v in os.environ.items() if re.match(os.environ["FAKE_ENV_KEYS"], k)}
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"argv": argv, "env": env}) + "\\n")
line = " ".join(argv)
for rule in json.loads(open(os.environ["FAKE_RULES"]).read()):
    if re.search(rule["match"], line):
        sys.stdout.write(rule.get("stdout", ""))
        sys.stderr.write(rule.get("stderr", ""))
        sys.exit(rule.get("rc", 0))
sys.exit(int(os.environ.get("FAKE_DEFAULT_RC", "0")))
"""

# The environment that reaches docker and decides what the stack does.
ENV_KEYS = r"^(GE_|BRIDGEU|NGROK|BLUESKY_OAUTH|OAUTH_STATE)"
# Compose reads this one as `${VAR-default}`, so empty and unset differ.
EMPTY_MATTERS = {"GE_DEV_BRIDGEU_SESSION_SECRET"}
# devctl's own bookkeeping, which no compose file reads: the bash version only
# exported these when a live.env existed, the port always does.
NOT_COMPARED = {"GE_DEV_LIVE", "GE_DEV_LIVE_ENV"}


@dataclass
class Rule:
    """A canned response from a fake tool: the first rule whose regex matches
    the joined command line wins."""

    match: str
    stdout: str = ""
    rc: int = 0
    stderr: str = ""


@dataclass
class Outcome:
    returncode: int
    stdout: str
    stderr: str
    calls: list[tuple[list[str], dict[str, str]]]
    requests: list[tuple[str, str]]
    files: dict[str, str]

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


class _Recorder(BaseHTTPRequestHandler):
    def _answer(self) -> None:
        server = self.server
        assert isinstance(server, _FakeHTTP)
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        server.requests.append((self.command, self.path))
        status, body = server.respond(self.command, self.path)
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = _answer

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass


class _FakeHTTP(ThreadingHTTPServer):
    def __init__(self, port: int, respond: Callable[[str, str], tuple[int, str]]):
        super().__init__(("127.0.0.1", port), _Recorder)
        self.requests: list[tuple[str, str]] = []
        self.respond = respond


@dataclass
class Sandbox:
    """A copy of devenv, with fake sibling repos and fake docker/kubectl/gcloud
    on PATH, to run devctl end to end with nothing real behind it.

    Ports: the sandbox gives the default instance base ports well clear of a
    real environment's, so a fake Elasticsearch (http=...) can listen on them.
    """

    root: Path
    rules: list[Rule] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    http: Callable[[str, str], tuple[int, str]] | None = None
    repos: Sequence[str] = ("api", "ingex", "frontend", "inference-service")

    def __post_init__(self) -> None:
        self.devenv = self.root / "internal-tools" / "devenv"
        shutil.copytree(DEVENV_DIR, self.devenv, ignore=_SKIP)
        for repo in self.repos:
            (self.root / repo / ".git").mkdir(parents=True, exist_ok=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        tool = self.bin / "fake_tool.py"
        tool.write_text(f"#!{sys.executable}\n{FAKE_TOOL}")
        tool.chmod(0o755)
        for name in ("docker", "kubectl", "gcloud", "ngrok", "openssl"):
            (self.bin / name).symlink_to(tool)
        self.log = self.root / "calls.jsonl"
        self.baseline = self._snapshot()

    @staticmethod
    def port_env_once() -> dict[str, str]:
        from .ports import BASE_PORTS

        # One block of free ports for the default instance's bases, spaced so
        # named instances' offsets don't collide with anything real.
        base = _free_port_block(len(BASE_PORTS))
        return {f"GE_DEV_PORT_{name}": str(base + i) for i, name in enumerate(BASE_PORTS)}

    def run(self, *args: str, legacy: bool = False, stdin: str = "") -> Outcome:
        rules = self.root / "rules.json"
        rules.write_text(json.dumps([r.__dict__ for r in self.rules]))
        self.log.unlink(missing_ok=True)
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "HOME": str(self.root),
            "FAKE_LOG": str(self.log),
            "FAKE_RULES": str(rules),
            "FAKE_ENV_KEYS": ENV_KEYS,
            **self.env,
        }
        # The fake Elasticsearch answers on the default instance's port and on
        # the first named instance's (offset 10), for one with its own cluster.
        servers: list[_FakeHTTP] = []
        requests: list[tuple[str, str]] = []
        if self.http is not None:
            for offset in (0, 10):
                server = _FakeHTTP(int(env.get("GE_DEV_PORT_ES", "9201")) + offset, self.http)
                server.requests = requests
                threading.Thread(target=server.serve_forever, daemon=True).start()
                servers.append(server)
        try:
            argv = (
                [str(self.devenv / "devctl-legacy"), *args]
                if legacy
                else devctl_argv(self.devenv, *args)
            )
            proc = subprocess.run(
                argv, env=env, text=True, capture_output=True, input=stdin, timeout=60
            )
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
        calls = []
        if self.log.exists():
            for line in self.log.read_text().splitlines():
                record = json.loads(line)
                calls.append((self._norm_argv(record["argv"]), self._norm_env(record["env"])))
        return Outcome(
            proc.returncode,
            self._norm(proc.stdout),
            self._norm(proc.stderr),
            calls,
            list(requests),
            self._changed_files(),
        )

    # --- normalization: compare two sandboxes as if they were one ------------

    def _norm(self, text: str) -> str:
        return text.replace(str(self.root), "<ROOT>")

    def _norm_argv(self, argv: list[str]) -> list[str]:
        return [self._norm(a) for a in argv]

    def _norm_env(self, env: dict[str, str]) -> dict[str, str]:
        return {
            k: self._norm(v)
            for k, v in sorted(env.items())
            if (v != "" or k in EMPTY_MATTERS) and k not in NOT_COMPARED
        }

    def _snapshot(self) -> dict[str, str]:
        out = {}
        for path in self.root.rglob("*"):
            # Git's own bookkeeping carries hashes and timestamps; what pull did
            # to a repo shows in its output instead.
            skip = {"__pycache__", ".git", "remotes"}
            parts = set(path.relative_to(self.root).parts)
            if not path.is_file() or skip & parts or path == self.log:
                continue
            rel = str(path.relative_to(self.root))
            if rel.startswith(("bin/", "rules.json")):
                continue
            try:
                out[rel] = self._norm(path.read_text())
            except UnicodeDecodeError:
                out[rel] = "<binary>"
        return out

    def _changed_files(self) -> dict[str, str]:
        now = self._snapshot()
        changed = {k: v for k, v in now.items() if self.baseline.get(k) != v}
        changed.update({k: "<deleted>" for k in self.baseline if k not in now})
        return changed


def _free_port_block(size: int) -> int:
    """A base port P with P..P+size-1 and the first named instance's block
    (P+10...) all free right now."""
    import socket

    for base in range(20000, 60000, 200):
        ports = [base + i for i in range(size)] + [base + 10 + i for i in range(size)]
        ok = True
        for port in ports:
            with socket.socket() as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    ok = False
                    break
        if ok:
            return base
    raise RuntimeError("no free port block")


def assert_parity(legacy: Outcome, new: Outcome, *, phrases: Sequence[str] = ()) -> None:
    """The ported command did what the bash one did: the same commands with the
    same environment, the same requests to Elasticsearch, the same files, and
    the same exit status. Output may be worded differently, but must carry the
    given phrases."""
    assert new.returncode == legacy.returncode, _diff("output", legacy.output, new.output)
    assert new.calls == legacy.calls, _diff("calls", legacy.calls, new.calls)
    assert new.requests == legacy.requests, _diff("requests", legacy.requests, new.requests)
    assert new.files == legacy.files, _diff("files", legacy.files, new.files)
    for phrase in phrases:
        assert re.search(phrase, legacy.output), f"legacy lacks {phrase!r}:\n{legacy.output}"
        assert re.search(phrase, new.output), f"port lacks {phrase!r}:\n{new.output}"


def parity(
    tmp_path: Path,
    *args: str,
    rules: Sequence[Rule] = (),
    env: Mapping[str, str] | None = None,
    http: Callable[[str, str], tuple[int, str]] | None = None,
    setup: Callable[[Sandbox], None] | None = None,
    phrases: Sequence[str] = (),
    repos: Sequence[str] = ("api", "ingex", "frontend", "inference-service"),
) -> tuple[Outcome, Outcome]:
    """Run `devctl <args>` through the bash original and the port, each in its
    own identical sandbox, and assert they behaved the same."""
    shared = {**Sandbox.port_env_once(), **(env or {})}
    outcomes = []
    for name, legacy in (("legacy", True), ("port", False)):
        sandbox = Sandbox(tmp_path / name, rules=list(rules), env=shared, http=http, repos=repos)
        if setup is not None:
            setup(sandbox)
            sandbox.baseline = sandbox._snapshot()
        outcomes.append(sandbox.run(*args, legacy=legacy))
    assert_parity(outcomes[0], outcomes[1], phrases=phrases)
    return outcomes[0], outcomes[1]


def _diff(what: str, legacy: object, new: object) -> str:
    import difflib
    import pprint

    def lines(value: object) -> list[str]:
        return (value if isinstance(value, str) else pprint.pformat(value, width=100)).splitlines()

    body = "\n".join(difflib.unified_diff(lines(legacy), lines(new), "legacy", "port", lineterm=""))
    return f"{what} differ:\n{body}"


@contextmanager
def serve(respond: Callable[[str, str], tuple[int, str]]) -> Iterator[_FakeHTTP]:
    """A local HTTP server answering with respond(method, path); its port is
    server.server_address[1] and what it was asked is server.requests."""
    server = _FakeHTTP(0, respond)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
