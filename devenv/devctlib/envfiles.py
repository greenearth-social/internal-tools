"""KEY=value files: devenv.local.env and the per-instance state records.

devenv.local.env is the developer's own overrides (ports, ES heap, live keys).
It's dotenv format: `NAME=value` lines, optionally quoted, `#` comments. Its
values win over the process environment, as they did when the bash version
sourced it with `set -a`.

The state files under .runtime/ (live.env, share.env, repo_root.env, bsky.env,
eurosky.env, es_key_*.env, probe.env) are written by devctl or by the
containers, one KEY=value per line.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

LOCAL_ENV_FILE = "devenv.local.env"


def read_env_file(path: Path) -> dict[str, str]:
    """Every KEY=value in path; empty when the file doesn't exist."""
    if not path.is_file():
        return {}
    return {k: v or "" for k, v in dotenv_values(path, interpolate=False).items()}


def env_file_value(path: Path, key: str) -> str:
    """One value from an env file, or "" — including when the file is missing,
    which must read as empty rather than as an error."""
    return read_env_file(path).get(key, "")


def nonempty(path: Path) -> bool:
    """True for a file with something in it — how a minted key's presence
    shows."""
    return path.is_file() and path.stat().st_size > 0


def write_env_file(path: Path, values: Mapping[str, str]) -> None:
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))


def load_local_env(devenv: Path, env: dict[str, str]) -> None:
    """Apply devenv.local.env over env, if the file exists."""
    path = devenv / LOCAL_ENV_FILE
    if not path.is_file():
        return
    # Interpolation sees the process environment, so `${HOME}` and friends
    # work the way they did under `source`.
    for key, value in dotenv_values(path).items():
        env[key] = value or ""
