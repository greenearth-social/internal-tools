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

from pathlib import Path

from . import incontainer, tunnel
from .bridgeu import BRIDGEU_REPO, clone_hint, have_bridgeu
from .compose import compose, compose_exec, require_running, service_running
from .ctx import Ctx, die, warn
from .envfiles import env_file_value, nonempty, write_env_file
from .runner import Result


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


def warn_if_published(ctx: Ctx, mode: str = "stop") -> None:
    """Like bsky.warn_if_published: this session's records outlive the stack,
    and only `eurosky down` removes them."""
    if not ctx.eurosky_state.is_file():
        return
    handle = env_file_value(ctx.eurosky_state, "GE_EUROSKY_HANDLE") or "your dev account"
    flag = ctx.name_flag()
    if mode == "nuke":
        warn(
            f"devctl: note — BridgEU's feeds are still published on {handle}, and\n"
            "        nuke removes the record of that session. Delete them from its profile in\n"
            "        a client, or with bridg-eu's scripts/publish_feeds.py --delete."
        )
    else:
        warn(
            f"devctl: note — BridgEU's feeds are still published on {handle},\n"
            "        and the tunnel serving them is now closed.\n"
            f"        Delete them with: devctl up{flag} && devctl eurosky down{flag}"
        )


# --- the session ---------------------------------------------------------------


def feeds_tsv(ctx: Ctx) -> Path:
    return ctx.runtime / "eurosky_feeds.tsv"


def cmd_eurosky(ctx: Ctx, args: list[str]) -> None:
    sub, rest = (args[0], args[1:]) if args else ("", [])
    commands = {"up": eurosky_up, "down": eurosky_down, "status": eurosky_status}
    if sub not in commands:
        die("usage: devctl eurosky <up|down|status>")
    if rest:
        die(f"usage: devctl eurosky {sub}")
    commands[sub](ctx)


def publish_feeds(ctx: Ctx, *args: str, env: dict[str, str] | None = None, **kwargs) -> Result:
    """bridg-eu's scripts/publish_feeds.py in bridgeu-api. Secrets go in the
    environment, not argv, where anything on the host could read them."""
    flags = [f for name, value in (env or {}).items() for f in ("-e", f"{name}={value}")]
    return compose(
        ctx,
        "exec",
        "-T",
        *flags,
        "bridgeu-api",
        "pipenv",
        "run",
        "python",
        "scripts/publish_feeds.py",
        *args,
        **kwargs,
    )


def eurosky_up(ctx: Ctx) -> None:
    from .bsky import require_ngrok_token, require_tunnel_free

    flag = ctx.name_flag()
    if not have_bridgeu(ctx):
        die(
            f"eurosky serves BridgEU's feeds and needs {BRIDGEU_REPO} checked out:"
            f" {clone_hint(ctx)}"
        )
    require_running(ctx, "bridgeu-api")
    require_ngrok_token(ctx)
    handle = ctx.get("GE_DEV_EUROSKY_HANDLE")
    if not handle:
        die(
            "set GE_DEV_EUROSKY_HANDLE in devenv.local.env"
            " (your dev BridgEU publisher account, e.g. you-beu-dev.eurosky.social)"
        )
    if not ctx.get("GE_DEV_EUROSKY_APP_PASSWORD"):
        die(f"set GE_DEV_EUROSKY_APP_PASSWORD in devenv.local.env (an app password for {handle})")
    if ctx.eurosky_state.is_file():
        die(
            "a eurosky session is already active for this instance — tear it down first:\n"
            f"        devctl eurosky down{flag}   (inspect it with: devctl eurosky status{flag})"
        )
    require_tunnel_free(ctx)

    # 1. The publisher: getFeedSkeleton only serves feeds under its DID, so
    #    bridgeu-api has to be told it before anything is published.
    publisher = resolve_did(ctx, handle)
    if not publisher:
        die(f"couldn't resolve {handle} to a DID")

    # 2. One tunnel into the gateway, which sends every path to bridgeu-api.
    #    There's no frontend or OAuth to split off, unlike bsky's gateway.
    tunnel.write_caddyfile(ctx.runtime / "Caddyfile", "reverse_proxy bridgeu-api:8000")
    if not tunnel.start(ctx):
        die("the public endpoint didn't start (see the error above)")
    host = tunnel.host(ctx)
    if not host:
        tunnel.stop(ctx)
        die("no usable tunnel — devctl logs ngrok")

    # 3. Recreate bridgeu-api as the tunnel's feed generator. Its first start
    #    installs dependencies, so give did.json longer than bsky does.
    print(f"devctl: pointing bridgeu-api at the tunnel (did:web:{host}) and restarting it")
    resolve_live_env(ctx)
    export_env(ctx, host, publisher)
    compose(ctx, "up", "-d", "--no-deps", "--force-recreate", "bridgeu-api", quiet=True)
    if not tunnel.wait_did(host, f"did:web:{host}", 90):
        tunnel.stop(ctx)
        restore(ctx)
        die(
            f"bridgeu-api didn't answer at https://{host}/.well-known/did.json"
            " — devctl logs bridgeu-api"
        )

    # 4. Publish, with the script's dev naming.
    configured = compose_exec(
        ctx,
        "bridgeu-api",
        "pipenv",
        "run",
        "python",
        "scripts/publish_feeds.py",
        "--configured",
        "--environment",
        "dev",
        check=False,
        capture=True,
        quiet_err=True,
    )
    feeds_tsv(ctx).write_text(configured.stdout.replace("\r", ""))
    print(f"devctl: publishing BridgEU's feeds to {handle}")
    published = publish_feeds(
        ctx,
        "--environment",
        "dev",
        "--handle",
        handle,
        env={"BRIDGEU_PUBLISHER_APP_PASSWORD": ctx.get("GE_DEV_EUROSKY_APP_PASSWORD")},
        check=False,
    )
    if not published.ok:
        tunnel.stop(ctx)
        feeds_tsv(ctx).unlink(missing_ok=True)
        restore(ctx)
        die("publishing feeds failed — the tunnel and bridgeu-api change have been rolled back")

    write_env_file(
        ctx.eurosky_state,
        {
            "GE_EUROSKY_HANDLE": handle,
            "GE_EUROSKY_PUBLISHER_DID": publisher,
            "GE_EUROSKY_HOST": host,
        },
    )
    print()
    print(f"Serving BridgEU's feeds as {handle} ({publisher}), from https://{host}.")
    print()
    print_feeds(ctx, handle)
    print()
    print("This tunnel is on the public internet. Tear it down when you're done:")
    print(f"  devctl eurosky down{flag}")


def resolve_live_env(ctx: Ctx) -> None:
    from .live import resolve_live  # live re-applies this module's session

    resolve_live(ctx)


def resolve_did(ctx: Ctx, handle: str) -> str:
    """handle's DID, resolved inside bridgeu-api; "" if it can't be."""
    result = compose_exec(
        ctx,
        "bridgeu-api",
        "pipenv",
        "run",
        "python",
        "-c",
        incontainer.source("bridgeu_resolve_did"),
        handle,
        check=False,
        capture=True,
        quiet_err=True,
    )
    did = result.stdout.replace("\r", "").strip()
    return did if result.ok and did.startswith("did:") else ""


def restore(ctx: Ctx) -> None:
    """Back to local defaults. Callers have removed the session state first,
    or never wrote it, so resolve_live exports nothing of the session's."""
    print("devctl: restoring bridgeu-api to local defaults")
    ctx.unset(
        "GE_DEV_BRIDGEU_HOSTNAME", "GE_DEV_BRIDGEU_PUBLISHER_DID", "GE_DEV_BRIDGEU_SESSION_SECRET"
    )
    resolve_live_env(ctx)
    compose(
        ctx,
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "bridgeu-api",
        check=False,
        quiet=True,
        quiet_err=True,
    )


def print_feeds(ctx: Ctx, handle: str) -> None:
    print(
        "Published feeds (open in the Bluesky app, or find them on"
        f" {handle}'s profile in Skeets or mu):"
    )
    tsv = feeds_tsv(ctx)
    if not nonempty(tsv):
        return
    for line in tsv.read_text().splitlines():
        rkey, _, name = line.partition("\t")
        print(f"  {name or rkey}:  https://bsky.app/profile/{handle}/feed/{rkey}")


def eurosky_down(ctx: Ctx) -> None:
    if not ctx.eurosky_state.is_file():
        die(f"no active eurosky session for this instance{ctx.label()} — nothing to tear down")
    handle = env_file_value(ctx.eurosky_state, "GE_EUROSKY_HANDLE")
    publisher = env_file_value(ctx.eurosky_state, "GE_EUROSKY_PUBLISHER_DID")
    tsv = feeds_tsv(ctx)
    rkeys = [line.split("\t")[0] for line in tsv.read_text().splitlines()] if tsv.is_file() else []
    rkeys = [r for r in rkeys if r]
    # Exactly the records `up` published, by rkey: nothing else on the
    # account. BRIDGEU_PUBLISHER_DID is the session's, since bridgeu-api may
    # already be back on the local placeholder (after a `devctl down`).
    password = ctx.get("GE_DEV_EUROSKY_APP_PASSWORD")
    if rkeys and password and service_running(ctx, "bridgeu-api"):
        print(f"devctl: deleting feed records from {handle}")
        deleted = publish_feeds(
            ctx,
            "--handle",
            handle,
            "--delete",
            *rkeys,
            env={"BRIDGEU_PUBLISHER_APP_PASSWORD": password, "BRIDGEU_PUBLISHER_DID": publisher},
            check=False,
        )
        if not deleted.ok:
            die(
                "deleting the feed records failed — the session is left in place;"
                f" retry devctl eurosky down{ctx.name_flag()}"
            )
    else:
        warn(
            "devctl: warning — can't delete the feed records"
            " (bridgeu-api down, no app password, or\n"
            "        no record of what was published). Delete them by hand:\n"
            "          devctl exec bridgeu-api pipenv run python scripts/publish_feeds.py --list\n"
            f"        with BRIDGEU_PUBLISHER_DID={publisher}, then --delete <rkey>"
        )

    tunnel.stop(ctx)
    ctx.eurosky_state.unlink(missing_ok=True)
    tsv.unlink(missing_ok=True)
    restore(ctx)
    print("devctl: eurosky session torn down — bridgeu-api is local-only again.")


def eurosky_status(ctx: Ctx) -> None:
    flag = ctx.name_flag()
    if not ctx.eurosky_state.is_file():
        print(f"no active eurosky session{ctx.label()}.")
        print(f"start one with: devctl eurosky up{flag}")
        return
    handle = env_file_value(ctx.eurosky_state, "GE_EUROSKY_HANDLE")
    publisher = env_file_value(ctx.eurosky_state, "GE_EUROSKY_PUBLISHER_DID")
    host = env_file_value(ctx.eurosky_state, "GE_EUROSKY_HOST")
    if service_running(ctx, "ngrok"):
        print(
            f"eurosky session active{ctx.label()} as {handle or '?'} ({publisher or '?'}),"
            f" from https://{host or '?'}."
        )
    else:
        print(
            f"eurosky session recorded{ctx.label()} as {handle or '?'},"
            " but its tunnel isn't running."
        )
        print(
            "The feeds below are still published and resolve to nothing:"
            f" devctl eurosky down{flag} deletes them."
        )
    print()
    print_feeds(ctx, handle)
    print()
    print(f"tear down with: devctl eurosky down{flag}")
