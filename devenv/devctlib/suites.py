"""`devctl test`: run a repo's test suite inside the environment.

The point is a first-class, allowlistable entry point for "run the tests" that
doesn't require knowing each repo's runner. api/inference/frontend/bridgeu run
in the live container, where their dependencies are already installed; ingex
is Go with no long-running service, so it gets an ephemeral container over the
same bind-mounted source and shared caches the ingest uses. `devctl exec`
covers anything finer (a single file, a -k filter, -v).
"""

from __future__ import annotations

import sys

from .bridgeu import BRIDGEU_REPO, clone_hint, have_bridgeu
from .compose import exec_argv, service_running
from .ctx import Ctx, die, warn
from .instance import GO_IMAGE, check_docker, ensure_go_caches

TEST_SUITES = ("api", "inference", "ingex", "frontend", "bridgeu")

# The container each in-stack suite runs in, and how.
IN_CONTAINER = {
    "api": ("api", "pipenv", "run", "pytest"),
    "inference": ("inference", "pipenv", "run", "pytest"),
    "frontend": ("frontend", "npm", "run", "test:unit"),
    "bridgeu": ("bridgeu-api", "pipenv", "run", "pytest"),
}


def suite_argv(ctx: Ctx, suite: str) -> list[str]:
    if suite == "ingex":
        tty = ["-t"] if sys.stdout.isatty() else []
        return [
            "docker",
            "run",
            "--rm",
            *tty,
            "-v",
            f"{ctx.repo_root}/ingex/ingest:/src",
            "-w",
            "/src",
            "-v",
            "ge-dev-gomod:/go/pkg/mod",
            "-v",
            "ge-dev-gocache:/root/.cache/go-build",
            GO_IMAGE,
            "go",
            "test",
            "-race",
            "./...",
        ]
    return exec_argv(ctx, *IN_CONTAINER[suite])


def cmd_test(ctx: Ctx, args: list[str]) -> None:
    #   devctl test              # every suite
    #   devctl test api          # just one
    #   devctl --name alpha test api
    check_docker(ctx)
    ensure_go_caches(ctx)

    suites = list(args)
    if not suites:
        suites = list(TEST_SUITES)
        # "Every suite" means every suite this checkout can run.
        if not have_bridgeu(ctx):
            suites.remove("bridgeu")
            warn(f"devctl: skipping bridgeu ({BRIDGEU_REPO} not checked out)")

    for suite in suites:
        if suite not in TEST_SUITES:
            choices = " ".join(TEST_SUITES)
            die(f"unknown test suite: {suite} (choose from: {choices}, or omit for all)")

    # The container suites need the stack up. Check them all first, so a bare
    # `devctl test` with nothing running says so once rather than dying on the
    # first suite after ingex has already run.
    need_up = []
    for suite in suites:
        if suite == "bridgeu" and not have_bridgeu(ctx):
            die(f"the bridgeu suite needs {BRIDGEU_REPO} checked out:\n        {clone_hint(ctx)}")
        if suite in IN_CONTAINER and not service_running(ctx, IN_CONTAINER[suite][0]):
            need_up.append(suite)
    if need_up:
        die(
            f"these need the stack running: {' '.join(need_up)}\n"
            f"        start it:  devctl up{ctx.name_flag()}\n"
            "        (ingex tests need nothing running: devctl test ingex)"
        )

    failed = []
    for suite in suites:
        print()
        print(f"devctl: ===== {suite} =====")
        if not ctx.run(suite_argv(ctx, suite), check=False).ok:
            failed.append(suite)

    print()
    if failed:
        die(f"test failures: {' '.join(failed)}")
    print(f"devctl: all passed ({' '.join(suites)})")
