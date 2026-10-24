"""`devctl eurosky`: serve a local BridgEU feed on the real network (bridg-eu#23).

BridgEU's counterpart to `bsky`: one ngrok tunnel into the gateway, which here
sends everything to bridgeu-api, and BridgEU's feeds published as records on a
dev account with bridg-eu's own scripts/publish_feeds.py. A feed record isn't
tied to an AppView, so the published feed loads in Skeets and mu (on either
AppView) as well as the Bluesky app. `eurosky down` deletes exactly the records
`up` published and returns bridgeu-api to its local defaults.

The two commands share the tunnel and gateway — work is on mysky or on
BridgEU, never both — so each refuses while the other's session exists.

Unlike mysky's dev account, the BridgEU one is its own (GE_DEV_EUROSKY_HANDLE):
`bsky down` deletes every feed record on its account, which on a shared one
would take BridgEU's records with it.
"""

from __future__ import annotations

from .compose import service_running
from .ctx import Ctx
from .envfiles import env_file_value


def export_env(ctx: Ctx, host: str, publisher: str) -> None:
    """What bridgeu-api runs with while it's the tunnel's feed generator. The
    dev session goes off: bridg-eu refuses it on any hostname but localhost."""
    ctx.export("GE_DEV_BRIDGEU_HOSTNAME", host)
    ctx.export("GE_DEV_BRIDGEU_PUBLISHER_DID", publisher)
    ctx.export("GE_DEV_BRIDGEU_SESSION_SECRET", "")


def export_session_env(ctx: Ctx) -> None:
    """Re-apply an active session's environment, for resolve_live. Only while
    the tunnel runs: after a `devctl down` (or a reboot) it's gone, and a free
    ngrok domain won't come back the same, so bridgeu-api goes back to local
    and the session is only owed a `eurosky down`."""
    if not ctx.eurosky_state.is_file() or not service_running(ctx, "ngrok"):
        return
    export_env(
        ctx,
        env_file_value(ctx.eurosky_state, "GE_EUROSKY_HOST"),
        env_file_value(ctx.eurosky_state, "GE_EUROSKY_PUBLISHER_DID"),
    )
