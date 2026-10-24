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

from .ctx import DEFAULT_INSTANCE, Ctx, die
from .envfiles import env_file_value

ES_OWNER = DEFAULT_INSTANCE
API_KEY = "GE_ELASTICSEARCH_API_KEY"


def share_es(ctx: Ctx) -> bool:
    """True when this instance reads the owner's cluster instead of its own.
    The owner never shares, whatever a stray GE_DEV_SHARE_ES says."""
    return ctx.instance != ES_OWNER and ctx.get("GE_DEV_SHARE_ES") == "1"


def owner_runtime(ctx: Ctx):
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
