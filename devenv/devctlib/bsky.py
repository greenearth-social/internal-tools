"""`devctl bsky`: serve a local feed on real Bluesky (issue #19).

`bsky up` opens one ngrok tunnel into a small path-routing gateway and points
real Bluesky at this environment. That single public origin is both the feed
generator (its host is the did:web the AppView resolves, reached via /xrpc and
/.well-known/did.json) and the app the browser does real OAuth against
(everything else). `bsky down` deletes the feed records it created and returns
the stack to a purely local feed. It never runs by default — an open tunnel to
your laptop is on the public internet — so it's an explicit, opt-in pair you
tear down when you're done.
"""

from __future__ import annotations

from .compose import service_running
from .ctx import Ctx, warn
from .envfiles import env_file_value

FUNCTIONS_TARGET = "firebase:15001"


def oauth_dir(ctx: Ctx):
    return ctx.runtime / "oauth"


def export_oauth_env(ctx: Ctx) -> None:
    """The BLUESKY_OAUTH_* / OAUTH_STATE_ENCRYPTION_KEY a firebase restart
    needs: an explicit set from the environment if there is one, else the
    cached dev keypair."""
    if ctx.get("BLUESKY_OAUTH_CLIENT_KID"):
        return
    keys = oauth_dir(ctx)
    ctx.export("BLUESKY_OAUTH_CLIENT_KID", (keys / "kid").read_text().rstrip("\n"))
    ctx.export(
        "BLUESKY_OAUTH_CLIENT_PRIVATE_KEY", (keys / "private-key.json").read_text().rstrip("\n")
    )
    ctx.export("BLUESKY_OAUTH_PUBLIC_JWKS", (keys / "public-jwks.json").read_text().rstrip("\n"))
    ctx.export("OAUTH_STATE_ENCRYPTION_KEY", (keys / "state-key").read_text().rstrip("\n"))


def export_session_env(ctx: Ctx) -> None:
    """Re-apply an active session's environment, for resolve_live: the api as
    the tunnel's feed generator, and the frontend and functions on the tunnel
    origin. Without it, `devctl restart api` puts the api back on
    did:web:localhost, so every feed record points at a generator that no
    longer answers as itself. Only while the tunnel runs."""
    if not ctx.bsky_state.is_file() or not service_running(ctx, "ngrok"):
        return
    host = env_file_value(ctx.bsky_state, "GE_BSKY_HOST")
    if not host:
        return
    ctx.export("GE_DEV_FEED_GENERATOR_DID", f"did:web:{host}")
    ctx.export("GE_DEV_FRONTEND_ALLOWED_HOSTS", f"localhost,{host}")
    if env_file_value(ctx.bsky_state, "GE_BSKY_OAUTH_OK") == "1":
        ctx.export("GE_DEV_APP_ORIGIN", f"https://{host}")
        ctx.export("GE_DEV_FUNCTIONS_TARGET", FUNCTIONS_TARGET)
        export_oauth_env(ctx)


def warn_if_published(ctx: Ctx, mode: str = "stop") -> None:
    """Warn that a session's feed records are still on a real account, for the
    commands that take the tunnel down without deleting them. The session file
    records feed records published to a real Bluesky account — state that
    outlives every container here, and that only `bsky down` cleans up — so
    `down` and `nuke` warn rather than remove it. `nuke` also destroys the
    session file, so it gets the manual incantation rather than `bsky down`."""
    if not ctx.bsky_state.is_file():
        return
    handle = env_file_value(ctx.bsky_state, "GE_BSKY_HANDLE") or ctx.get(
        "GE_DEV_BSKY_HANDLE", "your dev account"
    )
    flag = ctx.name_flag()
    if mode == "nuke":
        warn(
            f"devctl: note — this environment's feeds are still published on {handle}, and\n"
            "        nuke removes the record of that session along with everything else.\n"
            "        Delete them afterwards with:\n"
            f"          devctl up{flag} && devctl exec api pipenv run \\\n"
            f"            python scripts/publish_feed.py --delete-all --handle {handle}"
        )
    else:
        warn(
            f"devctl: note — this environment's feeds are still published on {handle},\n"
            "        and the tunnel serving them is now closed.\n"
            f"        Delete them with: devctl up{flag} && devctl bsky down{flag}"
        )
