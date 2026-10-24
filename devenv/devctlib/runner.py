"""Running external commands.

Everything devctl does to the outside world — docker, curl-equivalents aside,
git, kubectl, gcloud — goes through a Runner, so tests can swap in one that
records what would have run (see fakes.py) instead of patching subprocess.

A failed command raises CommandFailed by default, the equivalent of the bash
script's `set -e`: the command stops there and devctl exits with the failed
command's status, having printed nothing more (the command already said why).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import NoReturn


class CommandFailed(Exception):
    """An external command exited non-zero where that should stop devctl."""

    def __init__(self, argv: Sequence[str], returncode: int):
        super().__init__(f"{argv[0]} exited {returncode}")
        self.argv = list(argv)
        self.returncode = returncode


@dataclass
class Result:
    returncode: int
    stdout: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Runner:
    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        check: bool = True,
        capture: bool = False,
        quiet: bool = False,
        quiet_err: bool = False,
        input: str | None = None,
        cwd: str | os.PathLike[str] | None = None,
    ) -> Result:
        """Run argv to completion.

        capture: return stdout as text instead of letting it through.
        quiet / quiet_err: discard stdout / stderr.
        check: raise CommandFailed on a non-zero exit.
        """
        # Whatever devctl has printed so far comes before the command's output.
        sys.stdout.flush()
        stdout = subprocess.PIPE if capture else (subprocess.DEVNULL if quiet else None)
        stderr = subprocess.DEVNULL if quiet_err else None
        try:
            proc = subprocess.run(
                list(argv),
                env=dict(env),
                stdout=stdout,
                stderr=stderr,
                input=input,
                text=True,
                cwd=cwd,
            )
        except FileNotFoundError:
            # What a shell reports for a missing command.
            proc = subprocess.CompletedProcess(list(argv), 127, "" if capture else None)
        result = Result(proc.returncode, proc.stdout or "")
        if check and not result.ok:
            raise CommandFailed(argv, result.returncode)
        return result

    def exec(self, argv: Sequence[str], *, env: Mapping[str, str]) -> NoReturn:
        """Replace devctl with argv, so its TTY, signals and exit status are the
        command's own."""
        sys.stdout.flush()
        sys.stderr.flush()
        os.execvpe(argv[0], list(argv), dict(env))
