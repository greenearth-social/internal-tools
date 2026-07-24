"""Published-port allocation for named dev environment instances (api#283).

Docker Compose already keeps named instances apart everywhere it can: the
project name namespaces containers, networks and volumes. Published host ports
are the one shared resource it can't namespace, because their whole purpose is
to be reachable from outside Docker.

So each instance gets an offset added to every published port. The default
instance ("dev") is always offset 0, which is what makes `devctl up` behave
exactly as it did before any of this existed. Named instances get the first
free offset, allocated once and then remembered in the instance's runtime
directory, so an instance's ports don't move between restarts — an agent (or a
shell alias, or a browser tab) can rely on the number it was given.

Offsets step by STRIDE rather than 1 so the last digits identify the instance:
with dev on 8300/9201/3000, the first named instance is on 8310/9211/3010.
STRIDE also keeps the series from colliding with each other — the gaps between
base ports (e.g. 51, between the Auth emulator and the Emulator UI websocket)
are not multiples of it, so one instance's port can never land on another's.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

# Port name -> default. The env var for each is GE_DEV_PORT_<name>, which is
# also what docker-compose.yml reads, and what devenv.local.env overrides.
BASE_PORTS = {
    "API": 8300,
    "ES": 9201,
    "FRONTEND": 3000,
    "FIRESTORE": 8080,
    "FIREBASE_AUTH": 9099,
    "FUNCTIONS": 5001,
    "FIREBASE_UI": 4000,
    "FIREBASE_UI_WS": 9150,
}

STRIDE = 10
MAX_OFFSET = 90

DEFAULT_INSTANCE = "dev"


def runtime_dir(devenv_dir: Path, name: str) -> Path:
    """Where an instance keeps its minted keys, seed state and port record."""
    if name == DEFAULT_INSTANCE:
        return devenv_dir / ".runtime"
    return devenv_dir / f".runtime.{name}"


def instance_name(runtime: Path) -> str:
    """Inverse of runtime_dir, for listing what exists on disk."""
    if runtime.name == ".runtime":
        return DEFAULT_INSTANCE
    return runtime.name[len(".runtime.") :]


def list_instances(devenv_dir: Path) -> list[tuple[str, int]]:
    """Every instance with a runtime directory, as (name, offset) pairs."""
    found = []
    for path in sorted(devenv_dir.glob(".runtime*")):
        if not path.is_dir():
            continue
        name = instance_name(path)
        found.append((name, read_offset(path) or 0))
    return found


def read_offset(runtime: Path) -> int | None:
    record = runtime / "ports.json"
    if not record.exists():
        return None
    try:
        return int(json.loads(record.read_text())["offset"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def port_free(port: int) -> bool:
    """True if nothing is listening on this port on the loopback address.

    Deliberately without SO_REUSEADDR: we want to know whether *binding* would
    succeed for real, which is the question docker publish asks a moment later.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def ports_for(offset: int, bases: dict[str, int]) -> dict[str, int]:
    return {name: base + offset for name, base in bases.items()}


def allocate(devenv_dir: Path, name: str, bases: dict[str, int]) -> tuple[int, dict[str, int]]:
    """Return this instance's (offset, ports), allocating one if it has none.

    A previously allocated offset is returned as-is without re-checking that
    the ports are free: a running instance is holding its own ports, and
    "your ports are in use" is exactly the wrong thing to say about that. A
    genuine conflict surfaces a moment later as compose's own bind error,
    which names the port and is clear enough.
    """
    if name == DEFAULT_INSTANCE:
        return 0, ports_for(0, bases)

    runtime = runtime_dir(devenv_dir, name)
    existing = read_offset(runtime)
    if existing is not None:
        return existing, ports_for(existing, bases)

    claimed = {offset for _, offset in list_instances(devenv_dir)}
    for offset in range(STRIDE, MAX_OFFSET + 1, STRIDE):
        if offset in claimed:
            continue
        ports = ports_for(offset, bases)
        if all(port_free(port) for port in ports.values()):
            runtime.mkdir(parents=True, exist_ok=True)
            (runtime / "ports.json").write_text(
                json.dumps({"offset": offset, "ports": ports}, indent=2) + "\n"
            )
            return offset, ports

    raise SystemExit(
        f"devctl: no free port block for instance '{name}' — "
        f"tried offsets {STRIDE}..{MAX_OFFSET}.\n"
        f"        Free one up with: devctl nuke --name <other>"
    )


def resolve_bases(env: dict) -> dict[str, int]:
    """Base ports, honouring GE_DEV_PORT_* overrides from devenv.local.env."""
    return {
        name: int(env.get(f"GE_DEV_PORT_{name}", default))
        for name, default in BASE_PORTS.items()
    }


def read_only(devenv_dir: Path, name: str, bases: dict[str, int]) -> tuple[int, dict[str, int]]:
    """This instance's ports, without claiming a block it doesn't already have.

    Every command except `up` uses this, so that a mistyped `--name` reports an
    unknown instance instead of quietly allocating ports for a new one and
    then reporting an empty environment.
    """
    if name == DEFAULT_INSTANCE:
        return 0, ports_for(0, bases)
    offset = read_offset(runtime_dir(devenv_dir, name))
    if offset is None:
        raise SystemExit(
            f"devctl: no such instance: '{name}'\n"
            f"        start it with: devctl up --name {name}\n"
            f"        or list what exists: devctl ls"
        )
    return offset, ports_for(offset, bases)


def main(argv: list[str], env: dict) -> int:
    if len(argv) != 4 or argv[3] not in ("allocate", "read"):
        print("usage: ports.py <devenv-dir> <instance-name> allocate|read", file=sys.stderr)
        return 2
    devenv_dir, name, mode = Path(argv[1]), argv[2], argv[3]

    bases = resolve_bases(env)
    offset, ports = (allocate if mode == "allocate" else read_only)(devenv_dir, name, bases)
    # Shell-sourceable: devctl evaluates this and re-exports, so the values
    # reach docker-compose.yml as GE_DEV_PORT_*.
    print(f"GE_DEV_PORT_OFFSET={offset}")
    for port_name, port in ports.items():
        print(f"GE_DEV_PORT_{port_name}={port}")
    return 0


if __name__ == "__main__":
    import os

    raise SystemExit(main(sys.argv, dict(os.environ)))
