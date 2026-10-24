"""The state every command works from.

In the bash version this was a few dozen globals, some exported (and so seen by
docker compose) and some not. Here the exported ones live in `Ctx.env` — the
environment every child process gets — and the rest are attributes.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

from .runner import Result, Runner

DEFAULT_INSTANCE = "dev"


class Die(Exception):
    """Stop with `devctl: <message>` on stderr and exit status 1."""


def die(message: str) -> NoReturn:
    raise Die(message)


def warn(message: str) -> None:
    # Flushed first so the two streams interleave as written when both go to
    # the same pipe (stdout is block-buffered there).
    sys.stdout.flush()
    print(message, file=sys.stderr)


@dataclass
class Ctx:
    devenv: Path
    env: dict[str, str]
    runner: Runner = field(default_factory=Runner)
    instance: str = DEFAULT_INSTANCE
    # Set by instance.resolve_ports; GE_DEV_PORT_* in env carry the same values.
    offset: int | None = None
    ports: dict[str, int] = field(default_factory=dict)
    # What the caller configured, captured before the recorded state files are
    # applied over it — `up` decides from these, every other command from the
    # record (see instance.load_state).
    repo_root_configured: str = ""
    live_configured: str = ""
    live_env_configured: str = "stage"
    # "Next: ..." guidance is suppressed inside bootstrap, where the next step
    # is about to run anyway.
    in_bootstrap: bool = False

    # --- environment ---------------------------------------------------------

    def get(self, name: str, default: str = "") -> str:
        """An environment value, with unset and empty both reading as default."""
        return self.env.get(name) or default

    def export(self, name: str, value: str) -> None:
        self.env[name] = value

    def unset(self, *names: str) -> None:
        for name in names:
            self.env.pop(name, None)

    @contextmanager
    def scoped_env(self, **values: str) -> Iterator[None]:
        """Export values for the commands run inside the block only."""
        saved = {name: self.env.get(name) for name in values}
        self.env.update(values)
        try:
            yield
        finally:
            for name, value in saved.items():
                if value is None:
                    self.env.pop(name, None)
                else:
                    self.env[name] = value

    def run(self, argv: Sequence[str], **kwargs) -> Result:
        return self.runner.run(argv, env=self.env, **kwargs)

    def exec(self, argv: Sequence[str]) -> NoReturn:
        self.runner.exec(argv, env=self.env)

    def hint(self, message: str) -> None:
        if not self.in_bootstrap:
            print(message)

    # --- instance ------------------------------------------------------------

    @property
    def is_default(self) -> bool:
        return self.instance == DEFAULT_INSTANCE

    @property
    def runtime(self) -> Path:
        """This instance's runtime directory: minted keys, seed state, sessions."""
        if self.is_default:
            return self.devenv / ".runtime"
        return self.devenv / f".runtime.{self.instance}"

    def label(self) -> str:
        """ " [name]" for a named instance. Not shown for the default one: there
        it's noise, since those are the numbers in the README and in everybody's
        bookmarks."""
        return "" if self.is_default else f" [{self.instance}]"

    def name_flag(self) -> str:
        """ " --name <n>" when a hint needs the flag spelled out; empty on the
        default instance, whose commands work bare."""
        return "" if self.is_default else f" --name {self.instance}"

    def port(self, name: str) -> int:
        return self.ports[name]

    # --- repo root -----------------------------------------------------------

    @property
    def default_repo_root(self) -> Path:
        # Normalized without resolving symlinks, like `cd ../.. && pwd`.
        return Path(os.path.normpath(self.devenv / ".." / ".."))

    @property
    def repo_root(self) -> Path:
        return Path(self.get("GE_DEV_REPO_ROOT", str(self.default_repo_root)))

    # --- live services -------------------------------------------------------

    @property
    def live(self) -> str:
        return self.get("GE_DEV_LIVE")

    @property
    def live_env(self) -> str:
        return self.get("GE_DEV_LIVE_ENV", "stage")

    def live_has(self, service: str) -> bool:
        return service in self.live.split(",")

    # --- state files under the runtime directory ----------------------------

    @property
    def repo_root_state(self) -> Path:
        return self.runtime / "repo_root.env"

    @property
    def live_state(self) -> Path:
        return self.runtime / "live.env"

    @property
    def share_state(self) -> Path:
        return self.runtime / "share.env"

    @property
    def bsky_state(self) -> Path:
        return self.runtime / "bsky.env"

    @property
    def eurosky_state(self) -> Path:
        return self.runtime / "eurosky.env"
