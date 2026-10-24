"""`devctl doctor`: check everything this environment needs and name the fix.

The first thing to run (human or agent) when the environment misbehaves, and
the thing the troubleshooting doc points at before its symptom list.
"""

from __future__ import annotations

import io
import shutil
from contextlib import redirect_stderr

from . import http
from .assets import fixture_counts, have_fixtures, have_models, models_agree, models_tag
from .bridgeu import BRIDGEU_REPO, clone_hint, have_bridgeu
from .compose import oom_killed_services, running_services, service_running
from .ctx import Ctx, die
from .envfiles import nonempty
from .es import (
    ES_OWNER,
    STALE_DATA_HOURS,
    check_memory_budget,
    data_age_hours,
    es_count,
    es_health,
    owner_es_port,
    owner_ro_key,
    seed_fix,
    share_es,
)
from .instance import missing_repos


class Report:
    def __init__(self) -> None:
        self.fails = 0
        self.warns = 0

    def ok(self, message: str) -> None:
        print(f"  ok    {message}")

    def warn(self, message: str, fix: str = "") -> None:
        print(f"  warn  {message}")
        if fix:
            print(f"        fix: {fix}")
        self.warns += 1

    def fail(self, message: str, fix: str = "") -> None:
        print(f"  FAIL  {message}")
        if fix:
            print(f"        fix: {fix}")
        self.fails += 1

    def summary(self) -> None:
        print()
        if self.fails:
            warns = f" and {self.warns} warning(s)" if self.warns else ""
            die(f"{self.fails} problem(s){warns} found")
        if self.warns:
            print(f"devctl: {self.warns} warning(s), nothing fatal")
        else:
            print("devctl: everything checks out")


def cmd_doctor(ctx: Ctx, args: list[str]) -> None:
    print(f"devctl doctor{ctx.label()}")
    report = Report()
    docker_up = check_host(ctx, report)
    if docker_up and check_stack(ctx, report):
        check_data(ctx, report)
    report.summary()


def check_host(ctx: Ctx, report: Report) -> bool:
    """Docker, the checkouts, models and fixture. True when docker is usable."""
    docker_up = False
    if shutil.which("docker", path=ctx.env.get("PATH")) is None:
        report.fail("docker isn't installed", "https://docs.docker.com/get-docker/")
    elif not ctx.run(["docker", "info"], check=False, quiet=True, quiet_err=True).ok:
        report.fail("the docker daemon isn't running", "start Docker Desktop (or dockerd)")
    elif not ctx.run(["docker", "compose", "version"], check=False, quiet=True, quiet_err=True).ok:
        report.fail("docker compose v2 is missing", "install the compose plugin")
    else:
        docker_up = True
        server = ctx.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            check=False,
            capture=True,
            quiet_err=True,
        ).stdout.strip()
        compose_version = ctx.run(
            ["docker", "compose", "version", "--short"], check=False, capture=True, quiet_err=True
        ).stdout.strip()
        report.ok(f"docker {server}, compose {compose_version}")

    missing = missing_repos(ctx)
    if missing:
        report.fail(
            f"sibling repos missing from {ctx.repo_root}: {' '.join(missing)}",
            "clone them next to internal-tools (see docs/onboarding.md) or point at\n"
            "             a working copy that has them:"
            f" devctl up{ctx.name_flag()} --repo-root <dir>",
        )
    else:
        report.ok(f"sibling repos present under {ctx.repo_root}")
    if have_bridgeu(ctx):
        report.ok(f"{BRIDGEU_REPO} present")
    else:
        report.warn(
            f"{BRIDGEU_REPO} not checked out — the BridgEU services won't run",
            f"{clone_hint(ctx)} (optional)",
        )

    if have_models(ctx):
        report.ok(f"models {models_tag(ctx)}")
    else:
        report.fail("no model artifacts in models/data/", "devctl fetch-models (or devctl setup)")
    if have_fixtures(ctx):
        report.ok(f"fixture: {fixture_counts(ctx)}")
    else:
        report.fail("no fixture set in fixtures/data/", "devctl fetch-fixture")
    if have_models(ctx) and have_fixtures(ctx):
        if models_agree(ctx):
            report.ok("models and fixture agree on the content encoder")
        else:
            report.fail(
                "models and fixture disagree on the content encoder",
                "devctl fetch-models && devctl fetch-fixture, then devctl seed",
            )
    return docker_up


def check_stack(ctx: Ctx, report: Report) -> bool:
    """The running containers. True when there's local data worth checking."""
    running = running_services(ctx)
    if not running:
        report.warn(f"no containers running{ctx.label()}", f"devctl up{ctx.name_flag()}")
        return False
    report.ok(f"running: {' '.join(running)}")

    killed = oom_killed_services(ctx)
    if killed:
        report.fail(
            f"killed by the kernel, almost certainly out of memory: {' '.join(killed)}",
            f"devctl up{ctx.name_flag()} to restart;"
            " give Docker more memory or stop an instance (devctl ls)",
        )
    budget = io.StringIO()
    with redirect_stderr(budget):
        check_memory_budget(ctx)
    for line in budget.getvalue().splitlines():
        print(f"        {line}")

    if ctx.live_has("es"):
        report.ok(f"the api reads LIVE Elasticsearch ({ctx.live_env}) — local data checks skipped")
        return False
    return True


def check_data(ctx: Ctx, report: Report) -> None:
    """Elasticsearch reachable, keys minted, holding data; the apis answering."""
    if share_es(ctx):
        code = http.status(
            f"http://127.0.0.1:{owner_es_port(ctx)}/_cluster/health",
            headers={"Authorization": f"ApiKey {owner_ro_key(ctx)}"},
        ).code
        if code == "200":
            report.ok(
                f"shared Elasticsearch (the '{ES_OWNER}' instance's,"
                f" port {owner_es_port(ctx)}) is answering"
            )
        else:
            report.fail(
                f"the '{ES_OWNER}' instance's Elasticsearch isn't answering (HTTP {code})",
                "start the owner: devctl up"
                f"   (or run standalone: devctl up{ctx.name_flag()} --dedicated-es)",
            )
    elif not nonempty(ctx.runtime / "es_key_ro.env"):
        report.warn("no minted Elasticsearch keys yet", f"devctl up{ctx.name_flag()}")
    else:
        health = es_health(ctx)
        if health in ("green", "yellow"):
            report.ok(f"elasticsearch is {health}")
        else:
            report.fail(
                f"elasticsearch isn't answering on port {ctx.port('ES')}",
                "devctl logs elasticsearch — if it exited 137, that's memory",
            )

    if service_running(ctx, "api"):
        posts = es_count(ctx, "posts", quiet=True)
        if posts in ("", "0"):
            report.warn("no posts indexed — feeds come back empty, not as errors", seed_fix(ctx))
        else:
            report.ok(f"{posts} posts indexed")

        age = data_age_hours(ctx)
        if age is not None and age >= STALE_DATA_HOURS:
            report.warn(
                f"newest post {age}h old — outside the api's 24h recency window,"
                " so recency feeds are empty",
                seed_fix(ctx),
            )
        elif age is not None:
            report.ok(f"newest post {age}h old")

        check_answering(ctx, report, "api", ctx.port("API"))
    if service_running(ctx, "bridgeu-api"):
        check_answering(ctx, report, "bridgeu-api", ctx.port("BRIDGEU_API"))


def check_answering(ctx: Ctx, report: Report, service: str, port: int) -> None:
    code = http.status(f"http://127.0.0.1:{port}/health").code
    if code == "200":
        report.ok(f"{service} answering on port {port}")
    else:
        report.warn(
            f"{service} isn't answering on port {port} (HTTP {code})",
            f"first start installs dependencies — watch: devctl logs -t {service}",
        )
