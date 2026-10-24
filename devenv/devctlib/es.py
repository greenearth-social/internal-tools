"""Elasticsearch: this instance's cluster, or the one it shares.

Shared Elasticsearch (issue api#283): Elasticsearch is ~1.9GB of an instance's
~3GB, so by default a *named* instance doesn't run one — it reads the default
instance's cluster. A second environment then costs about a gigabyte instead of
three, which is the difference between "two fit" and "the first one's ES gets
OOM-killed".

The default instance ("dev") always owns a cluster. A named instance shares it
unless `--dedicated-es` asks for its own — the one case that needs it is
working on seeding or the index setup itself, since a sharer is read-only.

Read-only is enforced the way live mode is: seed, nuke-of-data and live ingest
refuse from a sharing instance, naming the owner. The api only reads, so it
gets the owner's read-only key and reaches the cluster the same way `--live es`
reaches a deployed one — through the host, at the owner's port.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from . import http
from .compose import running_projects
from .ctx import DEFAULT_INSTANCE, Ctx, Die, die, warn
from .envfiles import env_file_value, write_env_file
from .instance import runtime_dirs, runtime_instance

ES_OWNER = DEFAULT_INSTANCE
API_KEY = "GE_ELASTICSEARCH_API_KEY"


def share_es(ctx: Ctx) -> bool:
    """True when this instance reads the owner's cluster instead of its own.
    The owner never shares, whatever a stray GE_DEV_SHARE_ES says."""
    return ctx.instance != ES_OWNER and ctx.get("GE_DEV_SHARE_ES") == "1"


def owner_runtime(ctx: Ctx) -> Path:
    return ctx.devenv / ".runtime"


def owner_es_port(ctx: Ctx) -> int:
    """The owner's published ES port. Every instance's ES port is base+offset,
    so subtracting this instance's own offset lands on the base."""
    assert ctx.offset is not None
    return ctx.port("ES") - ctx.offset


def owner_ro_key(ctx: Ctx) -> str:
    return env_file_value(owner_runtime(ctx) / "es_key_ro.env", API_KEY)


def own_ro_key(ctx: Ctx) -> str:
    return env_file_value(ctx.runtime / "es_key_ro.env", API_KEY)


def es_endpoint(ctx: Ctx) -> tuple[str, str]:
    """(base URL, read-only key) for the cluster this instance reads."""
    if share_es(ctx):
        # A sharing instance has no cluster of its own; read the owner's.
        key = owner_ro_key(ctx)
        if not key:
            die(
                f"shared Elasticsearch not available — the '{ES_OWNER}' instance owns it: devctl up"
            )
        return f"http://127.0.0.1:{owner_es_port(ctx)}", key
    key = own_ro_key(ctx)
    if not key:
        die(f"no ES key yet — run: devctl up{ctx.name_flag()}")
    return f"http://127.0.0.1:{ctx.port('ES')}", key


def es_curl(ctx: Ctx, path: str, *curl_args: str) -> None:
    """curl this instance's Elasticsearch with its read-only key; extra
    arguments go to curl."""
    base, key = es_endpoint(ctx)
    ctx.run(["curl", "-s", "-H", f"Authorization: ApiKey {key}", f"{base}{path}", *curl_args])


def es_query(ctx: Ctx, path: str, body: str | None = None, *, quiet: bool = False) -> str:
    """The body of a read against this instance's cluster, or "" when it can't
    be had. Without a key that's said on stderr unless quiet; a cluster that
    doesn't answer is silent, as `curl -s` was."""
    try:
        base, key = es_endpoint(ctx)
    except Die as err:
        if not quiet:
            warn(f"devctl: {err}")
        return ""
    headers = {"Authorization": f"ApiKey {key}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    response = http.request(
        f"{base}{path}", method="POST" if body is not None else "GET", headers=headers, body=body
    )
    return response.body


def es_count(ctx: Ctx, alias: str, *, quiet: bool = False) -> str:
    """Documents behind an alias, as text; "" when unknown. The posts alias
    doesn't exist until the first seed (the ingest binary creates it, as in
    prod), and that reads as unknown too."""
    match = re.search(r'"count":(\d+)', es_query(ctx, f"/{alias}/_count", quiet=quiet))
    return match.group(1) if match else ""


def es_health(ctx: Ctx) -> str:
    match = re.search(r'"status":"([a-z]+)"', es_query(ctx, "/_cluster/health", quiet=True))
    return match.group(1) if match else ""


# --- data staleness ----------------------------------------------------------
# The rebase leaves the fixture window ending an hour before seed time, and the
# api's popularity generator only looks back 24h (RECENCY_WINDOW) — so from ~23
# hours after a seed, recency-anchored feeds (popularity, your-feed) come back
# thin or empty with nothing visibly wrong. That's the single most misleading
# failure this environment has, so it's warned about wherever the symptom would
# otherwise surface.
STALE_DATA_HOURS = 23

NEWEST_POST_QUERY = '{"size":0,"aggs":{"newest":{"max":{"field":"created_at"}}}}'


def data_age_hours(ctx: Ctx) -> int | None:
    """Hours since the newest indexed post — the ground truth for recency
    drift, read from Elasticsearch rather than a seed-time marker so it holds
    however the data got there. None when ES is unreachable or the index is
    empty: callers treat "unknown" as "don't warn", not as "fresh"."""
    try:
        newest = json.loads(es_query(ctx, "/posts/_search", NEWEST_POST_QUERY, quiet=True))
        newest_ms = int(newest["aggregations"]["newest"]["value"])
    except (ValueError, KeyError, TypeError):
        return None
    # Clamped at 0: right after a seed the newest post can sit a touch ahead
    # of now, and a negative "age" reads as broken rather than fresh.
    return max((int(time.time()) - newest_ms // 1000) // 3600, 0)


def seed_fix(ctx: Ctx) -> str:
    if share_es(ctx):
        return f"devctl seed   (on the '{ES_OWNER}' instance, which owns the data)"
    return "devctl seed"


def warn_stale_data(ctx: Ctx) -> None:
    age = data_age_hours(ctx)
    if age is None or age < STALE_DATA_HOURS:
        return
    warn(
        f"devctl: the newest indexed post is {age}h old — the data has drifted out of\n"
        "        the api's 24h recency window, so recency-anchored feeds come back thin\n"
        f"        or empty from here. Re-seed: {seed_fix(ctx)}"
    )


# --- memory ------------------------------------------------------------------
# Each instance costs roughly this much resident memory, measured with `docker
# stats` on a seeded environment: Elasticsearch ~1.9G (1g heap plus JVM
# overhead), the Firebase emulators ~350M, inference ~250M, and ~200M across
# the api, frontend and the two small stand-ins. A seed transiently adds more.
INSTANCE_MEMORY_GB = 3
# On top of the per-instance figure: a seed runs an extra Go build and the
# rebase alongside everything else, and the machine needs room that isn't ours.
# Without this the arithmetic said two instances fit in 7.7GB, on the very
# machine where that combination got an Elasticsearch OOM-killed.
MEMORY_HEADROOM_GB = 2


def is_sharer(runtime: Path) -> bool:
    return env_file_value(runtime / "share.env", "GE_DEV_SHARE_ES") == "1"


def check_memory_budget(ctx: Ctx) -> None:
    """Warn when the running Elasticsearch-bearing instances won't fit.

    Starting an instance on a machine that can't hold its Elasticsearch
    doesn't fail here — it gets *another* instance's ES OOM-killed some
    minutes later, which reads as "the environment broke" with nothing
    pointing at the cause. Only clusters count: a sharing instance adds ~1GB,
    not a JVM. Warn rather than refuse — the estimate is approximate, and a
    smaller heap makes it wrong in the safe direction.

    Runs after the share decision, so share_es reflects this instance.
    """
    info = ctx.run(
        ["docker", "info", "--format", "{{.MemTotal}}"], check=False, capture=True, quiet_err=True
    )
    try:
        total_gb = int(info.stdout.strip()) // 1024**3
    except ValueError:
        total_gb = 0
    if total_gb <= 0:
        return

    running = running_projects(ctx)
    # Running instances that carry their own cluster (i.e. aren't sharers),
    # plus this one if it bears a cluster and isn't already counted.
    bearers = sum(
        1
        for runtime in runtime_dirs(ctx.devenv)
        if f"ge-{runtime_instance(runtime)}" in running and not is_sharer(runtime)
    )
    if not share_es(ctx) and f"ge-{ctx.instance}" not in running:
        bearers += 1
    if bearers <= 1:
        return

    needed = bearers * INSTANCE_MEMORY_GB + MEMORY_HEADROOM_GB
    if needed <= total_gb:
        return
    warn(
        f"devctl: WARNING — {bearers} instances run their own Elasticsearch, needing roughly\n"
        f"        {needed}GB, and Docker has {total_gb}GB. The usual symptom is one of\n"
        "        them being OOM-killed mid-run (exit 137), not a failure here. Give\n"
        "        Docker more memory, set GE_DEV_ES_HEAP=512m in devenv.local.env, share\n"
        "        a cluster (named instances do by default — drop --dedicated-es), or\n"
        "        stop one: devctl ls"
    )


# --- the api's view of Elasticsearch -------------------------------------------


def record_share_choice(ctx: Ctx) -> None:
    write_env_file(ctx.share_state, {"GE_DEV_SHARE_ES": ctx.get("GE_DEV_SHARE_ES")})


def resolve_shared_es(ctx: Ctx) -> None:
    """Point the api at the owner's cluster and serve the owner's persona
    (since it's the owner's data). Reachable via host.docker.internal, exactly
    as a deployed cluster is under `--live es`."""
    if not share_es(ctx):
        return
    key = owner_ro_key(ctx)
    if not key:
        die(
            f"shared Elasticsearch needs the '{ES_OWNER}' instance up first:\n"
            "        devctl up\n"
            "    Or give this instance its own cluster:\n"
            f"        devctl up --name {ctx.instance} --dedicated-es"
        )
    ctx.export("GE_DEV_ES_API_KEY", key)
    ctx.export("GE_DEV_ES_URL", f"http://host.docker.internal:{owner_es_port(ctx)}")
    try:
        shutil.copyfile(owner_runtime(ctx) / "probe.env", ctx.runtime / "probe.env")
    except OSError:
        pass


def preflight_shared_es(ctx: Ctx) -> None:
    """Reach the owner's cluster from the host before the api depends on it,
    so an owner that isn't running is a clear message here rather than a feed
    that 500s later."""
    if not share_es(ctx):
        return
    port = owner_es_port(ctx)
    code = http.status(
        f"http://127.0.0.1:{port}/_cluster/health",
        headers={"Authorization": f"ApiKey {ctx.get('GE_DEV_ES_API_KEY')}"},
    ).code
    if code == "200":
        print(
            f"devctl: sharing the '{ES_OWNER}' instance's Elasticsearch"
            f" (127.0.0.1:{port}, read-only)"
        )
    elif code == "000":
        die(
            f"the '{ES_OWNER}' instance's Elasticsearch isn't reachable on port {port}.\n"
            "        Start it:              devctl up\n"
            f"        Or run standalone:     devctl up --name {ctx.instance} --dedicated-es"
        )
    else:
        die(f"shared Elasticsearch returned HTTP {code} from the '{ES_OWNER}' instance")


def export_api_es_env(ctx: Ctx) -> None:
    """Supply the api's Elasticsearch key. The api reads its ES endpoint from
    the compose `environment` block rather than an env_file, so that it can be
    pointed at a deployed cluster (an `environment` entry always wins over
    env_file, so the file can't be the fallback). That makes supplying the
    local key devctl's job."""
    if not ctx.get("GE_DEV_ES_API_KEY"):
        key = own_ro_key(ctx)
        if not key:
            die(
                f"no Elasticsearch key in {ctx.get('GE_DEV_RUNTIME')}/es_key_ro.env"
                f" — run: devctl up{ctx.name_flag()}"
            )
        ctx.export("GE_DEV_ES_API_KEY", key)


def sharers_of_owner(ctx: Ctx) -> list[str]:
    """Named instances that read the owner's cluster — checked before nuking or
    stopping the owner, whose volumes hold data they're serving."""
    return [
        runtime_instance(runtime)
        for runtime in runtime_dirs(ctx.devenv)
        if runtime.name != ".runtime" and is_sharer(runtime)
    ]


def export_api_env(ctx: Ctx) -> None:
    """Everything the api needs to reach the cluster it reads, for commands
    that recreate it outside `up`: the owner's endpoint and key on a sharing
    instance, else this instance's key."""
    resolve_shared_es(ctx)
    export_api_es_env(ctx)
