"""Commands that build, start, load, restart and remove the stack."""

from __future__ import annotations

from .assets import cmd_fetch_models, fixtures_hint, have_fixtures, have_models
from .bridgeu import have_bridgeu
from .compose import compose
from .ctx import Ctx, die
from .instance import check_docker, check_repos, ensure_runtime

# --- setup -------------------------------------------------------------------

BUILT = ("es-init", "api", "inference", "firebase", "frontend", "seed-firestore-users")
PULLED = ("elasticsearch", "perspective-stub", "dev-auth", "seed-rebase", "seed-megastream")


def cmd_setup(ctx: Ctx, args: list[str]) -> None:
    check_docker(ctx)
    check_repos(ctx)
    ensure_runtime(ctx)
    print("devctl: pulling and building images (first run takes a few minutes)...")
    builds = [*BUILT, *(["bridgeu-api"] if have_bridgeu(ctx) else [])]
    compose(ctx, "--profile", "tools", "build", *builds)
    compose(ctx, "--profile", "tools", "pull", *PULLED, "--ignore-buildable")

    if not have_models(ctx):
        cmd_fetch_models(ctx, [])

    # Install inference-service's dependencies now rather than on first `up`.
    # torch is a few hundred MB; doing it here, where a wait is expected, keeps
    # `up` from looking hung the first time. It also keeps `up` runnable
    # offline, which is true of everything else here once setup has run.
    print("devctl: preparing inference-service dependencies (one time, ~1 min)...")
    installed = compose(
        ctx,
        "run",
        "--rm",
        "--no-deps",
        "inference",
        "bash",
        "/inference/start.sh",
        "--install-only",
        check=False,
    )
    if not installed.ok:
        die("failed to install inference-service dependencies")

    print("devctl: setup complete.")
    if not ctx.in_bootstrap and not have_fixtures(ctx):
        fixtures_hint(ctx)
    ctx.hint("Next: devctl up && devctl seed")
