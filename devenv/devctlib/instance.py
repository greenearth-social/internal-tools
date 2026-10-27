"""Instances (issue api#283), their recorded state, and their ports.

`--name <n>` runs a second, fully independent environment: its own compose
project (so its own containers, network and volumes), its own runtime
directory, and its own block of published ports. Two agents can then work on
two branches at once without either noticing the other.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import ports
from .bridgeu import BRIDGEU_REPO, have_bridgeu
from .ctx import DEFAULT_INSTANCE, Ctx, die, warn
from .envfiles import read_env_file, write_env_file
from .runner import Result

INSTANCE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Required to run the environment at all; the rest are optional.
REQUIRED_REPOS = ("api", "ingex", "frontend", "inference-service")

# The Go module and build caches are declared external so that they survive
# one instance's `nuke` and are shared by all of them; compose won't create an
# external volume, so we do. Also used directly by `devctl test ingex`, whose
# ephemeral container isn't a compose service.
GO_IMAGE = "golang:1.25"
GO_CACHE_VOLUMES = ("ge-dev-gomod", "ge-dev-gocache")


def set_instance(ctx: Ctx, name: str) -> None:
    """Select the instance every later step acts on.

    Compose project names accept lowercase alphanumerics, dashes and
    underscores; ours also becomes part of a runtime directory name and a
    config filename.
    """
    if not INSTANCE_NAME.match(name):
        die(f"invalid instance name: '{name}' (lowercase letters, digits, - and _)")
    ctx.instance = name
    ctx.export("GE_DEV_NAME", f"ge-{name}")
    # Relative, because compose resolves it against --project-directory.
    if name == DEFAULT_INSTANCE:
        ctx.export("GE_DEV_RUNTIME", "./.runtime")
        ctx.export("GE_DEV_FIREBASE_CONFIG", "firebase.devenv.json")
    else:
        ctx.export("GE_DEV_RUNTIME", f"./.runtime.{name}")
        ctx.export("GE_DEV_FIREBASE_CONFIG", f"firebase.devenv.{name}.json")


def load_state(ctx: Ctx) -> None:
    """Apply what this instance has recorded: its repo root, its live-service
    choice and whether it shares Elasticsearch.

    `up` is the only command that decides these; it records the result in the
    runtime directory and every other command reads the record, so `seed` and
    `status` act on the environment that's actually running. What the caller
    configured is kept on ctx first, because `up` starts from that instead.
    """
    ctx.repo_root_configured = ctx.get("GE_DEV_REPO_ROOT")
    ctx.env.update(read_env_file(ctx.repo_root_state))
    ctx.export("GE_DEV_REPO_ROOT", ctx.get("GE_DEV_REPO_ROOT", str(ctx.default_repo_root)))

    ctx.live_configured = ctx.get("GE_DEV_LIVE")
    ctx.live_env_configured = ctx.get("GE_DEV_LIVE_ENV", "stage")
    ctx.env.update(read_env_file(ctx.live_state))
    ctx.export("GE_DEV_LIVE", ctx.get("GE_DEV_LIVE"))
    ctx.export("GE_DEV_LIVE_ENV", ctx.get("GE_DEV_LIVE_ENV", "stage"))

    ctx.env.update(read_env_file(ctx.share_state))


def record_repo_root(ctx: Ctx) -> None:
    write_env_file(ctx.repo_root_state, {"GE_DEV_REPO_ROOT": str(ctx.repo_root)})


def resolve_ports(ctx: Ctx, mode: str = "read") -> None:
    """Published ports for this instance. `allocate` claims a free block on
    first use and remembers it; `read` refuses to invent one, so a mistyped
    --name is an error rather than a second empty environment."""
    if ctx.offset is not None:
        return
    bases = ports.resolve_bases(ctx.env)
    resolve = ports.allocate if mode == "allocate" else ports.read_only
    offset, allocated = resolve(ctx.devenv, ctx.instance, bases)
    ctx.offset = offset
    ctx.ports = allocated
    ctx.export("GE_DEV_PORT_OFFSET", str(offset))
    for name, port in allocated.items():
        ctx.export(f"GE_DEV_PORT_{name}", str(port))


def runtime_instance(runtime: Path) -> str:
    """Instance name from a runtime directory path (.runtime -> dev)."""
    return ports.instance_name(runtime)


def runtime_dirs(devenv: Path) -> list[Path]:
    return sorted(p for p in devenv.glob(".runtime*") if p.is_dir())


# --- prerequisites -----------------------------------------------------------


def check_docker(ctx: Ctx) -> None:
    if shutil.which("docker", path=ctx.env.get("PATH")) is None:
        die("docker is required (https://docs.docker.com/get-docker/)")
    if not ctx.run(["docker", "info"], check=False, quiet=True, quiet_err=True).ok:
        die("docker daemon isn't running")
    if not ctx.run(["docker", "compose", "version"], check=False, quiet=True, quiet_err=True).ok:
        die("docker compose v2 is required")


def have_repo(ctx: Ctx, repo: str) -> bool:
    return (ctx.repo_root / repo / ".git").exists()


def missing_repos(ctx: Ctx) -> list[str]:
    return [repo for repo in REQUIRED_REPOS if not have_repo(ctx, repo)]


def check_repos(ctx: Ctx) -> None:
    root = ctx.repo_root
    missing = missing_repos(ctx)
    for repo in missing:
        warn(f"devctl: required repo missing: {root}/{repo}")
        warn(
            f"        clone it: git clone git@github.com:greenearth-social/{repo}.git"
            f' "{root}/{repo}"'
        )
    if missing:
        die("missing required repos (or set GE_DEV_REPO_ROOT to the directory that contains them)")
    # Only needed to retrain or republish models, not to run the environment.
    if not have_repo(ctx, "engagement-prediction"):
        warn(f"devctl: note: optional repo not checked out: {root}/engagement-prediction")
    if not have_bridgeu(ctx):
        warn(
            f"devctl: note: {BRIDGEU_REPO} not checked out, so the BridgEU services won't run:"
            f" {root}/{BRIDGEU_REPO}"
        )


def ensure_go_caches(ctx: Ctx) -> None:
    for volume in GO_CACHE_VOLUMES:
        inspected: Result = ctx.run(
            ["docker", "volume", "inspect", volume], check=False, quiet=True, quiet_err=True
        )
        if not inspected.ok:
            ctx.run(["docker", "volume", "create", volume], quiet=True)


def ensure_runtime(ctx: Ctx) -> None:
    ensure_go_caches(ctx)
    (ctx.runtime / "seed").mkdir(parents=True, exist_ok=True)
    models = ctx.devenv / "models" / "data"
    models.mkdir(parents=True, exist_ok=True)
    # compose env_file entries must exist before any `compose` invocation.
    for path in (
        ctx.runtime / "es_key_ro.env",
        ctx.runtime / "es_key_rw.env",
        ctx.runtime / "probe.env",
        models / "models.env",
    ):
        path.touch()
