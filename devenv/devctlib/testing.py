"""Test support: a recording Runner, a scratch Ctx, and a local HTTP server.

Not imported by devctl itself.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
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


# --- a local HTTP server ---------------------------------------------------


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
