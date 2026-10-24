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

import json
import secrets
from pathlib import Path

from . import incontainer, tunnel
from .compose import compose, compose_exec, require_running, service_running
from .ctx import Ctx, die, warn
from .envfiles import env_file_value, nonempty, write_env_file

FUNCTIONS_TARGET = "firebase:15001"


def oauth_dir(ctx: Ctx) -> Path:
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


# --- the session ---------------------------------------------------------------

# A dev OAuth keypair generated in the firebase container with Node's built-in
# EC-to-JWK export — no dependency on jose or an ESM entry point. The dev client
# is separate from prod by construction (client_id derives from the tunnel
# origin), so this never touches prod keys.
KEYGEN_JS = (
    'const c=require("crypto");'
    'const{publicKey,privateKey}=c.generateKeyPairSync("ec",{namedCurve:"P-256"});'
    'const pub=publicKey.export({format:"jwk"});const priv=privateKey.export({format:"jwk"});'
    "const kid=c.randomUUID();"
    'pub.kid=kid;pub.use="sig";pub.alg="ES256";priv.kid=kid;priv.alg="ES256";'
    "process.stdout.write(JSON.stringify({kid:kid,privateJwk:priv,publicJwks:{keys:[pub]}}));"
)

# What to route to the api when the api can't be asked (it is running by the
# time `bsky up` gets here, so this is a backstop, not the normal path).
# Serving the feed at all needs the first two; the rest is what the api
# exposed when this was written.
GATEWAY_FALLBACK_PATHS = (
    "/.well-known/did.json /xrpc /xrpc/* /admin /admin/* /api /api/* "
    "/candidates /candidates/* /diversify /docs /docs/* /health /openapi.json /r /r/* /rank "
    "/rank/* /redoc /skylight /skylight/*"
)

FEEDS_TSV = "bsky_feeds.tsv"
OAUTH_FILES = ("private-key.json", "public-jwks.json", "kid", "state-key")
OAUTH_ENV = (
    "BLUESKY_OAUTH_CLIENT_KID",
    "BLUESKY_OAUTH_CLIENT_PRIVATE_KEY",
    "BLUESKY_OAUTH_PUBLIC_JWKS",
    "OAUTH_STATE_ENCRYPTION_KEY",
)


def cmd_bsky(ctx: Ctx, args: list[str]) -> None:
    sub, rest = (args[0], args[1:]) if args else ("", [])
    commands = {"up": bsky_up, "down": bsky_down, "status": bsky_status}
    if sub not in commands:
        die("usage: devctl bsky <up|down|status>")
    if rest:
        die(f"usage: devctl bsky {sub}")
    commands[sub](ctx)


def require_tunnel_free(ctx: Ctx) -> None:
    """The one tunnel and gateway serve either a bsky or a eurosky session."""
    flag = ctx.name_flag()
    if ctx.bsky_state.is_file():
        die(
            "a bsky session holds the tunnel for this instance — tear it down first:\n"
            f"        devctl bsky down{flag}"
        )
    if ctx.eurosky_state.is_file():
        die(
            "a eurosky session holds the tunnel for this instance — tear it down first:\n"
            f"        devctl eurosky down{flag}"
        )


def require_ngrok_token(ctx: Ctx) -> None:
    # ngrok runs as a container (no host install); it only needs an authtoken.
    if not ctx.get("NGROK_AUTHTOKEN"):
        die(
            "set NGROK_AUTHTOKEN in devenv.local.env — copy it from"
            " https://dashboard.ngrok.com/get-started/your-authtoken\n"
            "        (a free ngrok account is enough)"
        )


def bsky_up(ctx: Ctx) -> None:
    require_running(ctx, "api", "the api isn't running")
    require_ngrok_token(ctx)
    handle = ctx.get("GE_DEV_BSKY_HANDLE")
    app_password = ctx.get("GE_BSKY_APP_PASSWORD")
    if not handle:
        die(
            "set GE_DEV_BSKY_HANDLE in devenv.local.env"
            " (your dev bsky account, e.g. caterpie-internal.bsky.social)"
        )
    if not app_password:
        die(f"set GE_BSKY_APP_PASSWORD in devenv.local.env (an app password for {handle})")
    if ctx.bsky_state.is_file():
        flag = ctx.name_flag()
        die(
            "a bsky session is already active for this instance — tear it down first:\n"
            f"        devctl bsky down{flag}   (inspect it with: devctl bsky status{flag})"
        )
    require_tunnel_free(ctx)

    # 1. Dev OAuth keypair. Best-effort: real frontend sign-in needs it, feed
    #    serving doesn't, so a failure here is a warning, not a stop.
    oauth_ok = ensure_oauth_keys(ctx)

    # 2. The public endpoint: the gateway plus one tunnel into it.
    api_paths = api_gateway_paths(ctx)
    if not api_paths:
        warn(
            "devctl: warning — couldn't read the api's routes; routing the paths devctl knows about"
        )
        api_paths = GATEWAY_FALLBACK_PATHS
    write_gateway_config(ctx.runtime / "Caddyfile", api_paths)
    if not tunnel.start(ctx):
        die("the public endpoint didn't start (see the error above)")
    host = tunnel.host(ctx)
    if not host:
        tunnel.stop(ctx)
        die("no usable tunnel — devctl logs ngrok")
    did = f"did:web:{host}"

    # 3. Recreate the api as the tunnel's feed generator. Recreating drops the
    #    environment `up` injected, so re-supply it the way `restart` does —
    #    without the ES key the api crash-loops. The DID is set for this one
    #    call so it doesn't leak into later restarts.
    print(f"devctl: pointing the api at the tunnel ({did}) and restarting it")
    recreate_env(ctx)
    with ctx.scoped_env(GE_DEV_FEED_GENERATOR_DID=did):
        compose(ctx, "up", "-d", "--no-deps", "--force-recreate", "api", quiet=True)
    if not tunnel.wait_did(host, did):
        warn(
            "devctl: warning — did.json hasn't confirmed yet at"
            f" https://{host}/.well-known/did.json (bsky may need a minute)"
        )

    # 4. Frontend / OAuth wiring, on that same origin — the gateway keeps the
    #    two apart by path, so the browser and the AppView share one hostname.
    wire_frontend(ctx, host, oauth_ok)

    # 5. Publish the feeds, pointing them at this tunnel's DID.
    print(f"devctl: publishing feeds to {handle}")
    published = compose_exec(
        ctx,
        "api",
        "pipenv",
        "run",
        "python",
        "scripts/publish_feed.py",
        "--sync",
        "--environment",
        "dev",
        "--handle",
        handle,
        "--app-password",
        app_password,
        "--generator-did",
        did,
        check=False,
    )
    if not published.ok:
        tunnel.stop(ctx)
        restore_stack(ctx)
        die("publishing feeds failed — the tunnel and api change have been rolled back")

    # 6. Persist the session and print where to find everything. Capturing the
    #    feed names only affects the printed labels, so it never aborts after a
    #    successful publish.
    capture_feeds(ctx)
    write_env_file(
        ctx.bsky_state,
        {
            "GE_BSKY_HANDLE": handle,
            "GE_BSKY_HOST": host,
            "GE_BSKY_DID": did,
            "GE_BSKY_OAUTH_OK": "1" if oauth_ok else "0",
        },
    )
    print()
    print(f"Serving this environment's feed on Bluesky as {handle} ({did}).")
    print()
    print("Published feeds:")
    print_feed_links(ctx, handle)
    print()
    print_endpoint(host, oauth_ok)
    print()
    print("This tunnel is on the public internet. Tear it down when you're done:")
    print(f"  devctl bsky down{ctx.name_flag()}")


def recreate_env(ctx: Ctx) -> None:
    """The environment `up` gave the api, for recreating it here: its live
    targets (which re-apply this session) and how it reaches Elasticsearch —
    the owner's cluster, on a sharing instance."""
    from .es import export_api_env
    from .live import resolve_live  # live re-applies this module's session

    resolve_live(ctx)
    export_api_env(ctx)


def ensure_oauth_keys(ctx: Ctx) -> bool:
    """Generate (once) and cache a dev ES256 keypair and AES state key, unless
    the developer exported an explicit BLUESKY_OAUTH_* set (an escape hatch for
    externally-managed keys). False — the caller warns — if the keys can't be
    produced, e.g. the firebase container isn't up."""
    if all(ctx.get(name) for name in OAUTH_ENV):
        return True
    keys = oauth_dir(ctx)
    keys.mkdir(parents=True, exist_ok=True)
    if all(nonempty(keys / name) for name in OAUTH_FILES):
        return True
    if not service_running(ctx, "firebase"):
        warn("devctl: firebase container isn't running — skipping real OAuth setup")
        return False
    generated = compose_exec(
        ctx, "firebase", "node", "-e", KEYGEN_JS, check=False, capture=True, quiet_err=True
    )
    if not generated.ok:
        warn("devctl: dev OAuth key generation failed — skipping real OAuth setup")
        return False
    try:
        pair = json.loads(generated.stdout)
        (keys / "private-key.json").write_text(json.dumps(pair["privateJwk"]))
        (keys / "public-jwks.json").write_text(json.dumps(pair["publicJwks"]))
        (keys / "kid").write_text(pair["kid"])
    except (ValueError, KeyError, TypeError, OSError):
        warn("devctl: could not write dev OAuth keys — skipping real OAuth setup")
        return False
    (keys / "state-key").write_text(secrets.token_hex(32) + "\n")
    return True


def firebase_has_env(ctx: Ctx, origin: str, kid: str) -> bool:
    """True when the running firebase already has this APP_ORIGIN and client
    kid loaded — its functions are configured for this tunnel, so it can be
    reused rather than recreated (which would wipe the emulator's data)."""
    if not kid or not service_running(ctx, "firebase"):
        return False

    def loaded(name: str) -> str:
        return compose(
            ctx,
            "exec",
            "-T",
            "firebase",
            "printenv",
            name,
            check=False,
            capture=True,
            quiet_err=True,
        ).stdout.strip()

    return loaded("APP_ORIGIN") == origin and loaded("BLUESKY_OAUTH_CLIENT_KID") == kid


def wire_frontend(ctx: Ctx, host: str, oauth_ok: bool) -> None:
    """Recreate the frontend (and, only when needed, firebase) with the tunnel
    as the public origin. With OAuth keys, also take the dev-auth shim out so
    real Bluesky sign-in runs; without, serve the app but leave the shim in."""
    print(f"devctl: wiring the frontend for https://{host}")
    ctx.export("GE_DEV_FRONTEND_ALLOWED_HOSTS", f"localhost,{host}")
    # Only the frontend (stateless Vite) restarts by default. The firebase
    # emulator has no import/export configured, so recreating it wipes its
    # data (feed snapshots, auth sessions) — touch it only when we must: the
    # functions read APP_ORIGIN and the OAuth keys at container start, so
    # enabling *real* sign-in needs a firebase restart.
    services = ["frontend"]
    if oauth_ok:
        ctx.export("GE_DEV_APP_ORIGIN", f"https://{host}")
        export_oauth_env(ctx)
        ctx.export("GE_DEV_FUNCTIONS_TARGET", FUNCTIONS_TARGET)
        # `bsky down` leaves the tunnel APP_ORIGIN and keys loaded in the
        # functions (only the frontend reverts to the shim), so a repeat `bsky
        # up` at the same origin — a stable ngrok domain plus the cached
        # keypair — can reuse the running emulator, keeping its data across
        # the up/down/up cycle.
        if firebase_has_env(ctx, f"https://{host}", ctx.get("BLUESKY_OAUTH_CLIENT_KID")):
            print("devctl: firebase already has this origin and keys — reusing its emulator state")
        else:
            services.append("firebase")
            warn(
                "devctl: restarting the firebase emulator to enable real OAuth"
                " (its emulator data resets)"
            )
    else:
        warn(
            "devctl: real sign-in disabled (no OAuth keys) —"
            " the app is served but /auth uses the local shim"
        )
    # --no-deps so recreating the frontend can't bounce the api (reverting the
    # tunnel DID) or the firebase emulator we're deliberately leaving alone.
    compose(ctx, "up", "-d", "--no-deps", "--force-recreate", *services, quiet=True)


def api_gateway_paths(ctx: Ctx) -> str:
    """The api's public paths, asked of the running api (see
    incontainer/api_gateway_paths.py); "" when it can't be asked."""
    result = compose_exec(
        ctx,
        "api",
        "pipenv",
        "run",
        "python",
        "-c",
        incontainer.source("api_gateway_paths"),
        check=False,
        capture=True,
        quiet_err=True,
    )
    return result.stdout.replace("\r", "").strip() if result.ok else ""


def write_gateway_config(path: Path, api_paths: str) -> None:
    """The api's own paths go to the api; everything else — the app, its
    assets, the OAuth metadata and callback the Firebase functions serve
    through Vite — to the frontend. The frontend is the catch-all because Vite
    serves an open-ended set of paths (/@vite, /@fs, /node_modules/.vite, the
    SPA's own routes) that can't be enumerated, while the api's can."""
    tunnel.write_caddyfile(
        path,
        f"@api path {api_paths}\nreverse_proxy @api api:8000\nreverse_proxy frontend:3000",
    )


def capture_feeds(ctx: Ctx) -> None:
    """Each published feed as "rkey<TAB>label", for the printed links."""
    result = compose_exec(
        ctx,
        "api",
        "pipenv",
        "run",
        "python",
        "-c",
        incontainer.source("api_feeds"),
        check=False,
        capture=True,
        quiet_err=True,
    )
    (ctx.runtime / FEEDS_TSV).write_text(result.stdout)


def print_feed_links(ctx: Ctx, handle: str) -> None:
    """Aligned "<name>:  https://bsky.app/.../feed/<rkey>" lines."""
    tsv = ctx.runtime / FEEDS_TSV
    if not nonempty(tsv):
        return
    feeds = []
    for line in tsv.read_text().splitlines():
        rkey, _, name = line.replace("\r", "").partition("\t")
        feeds.append((rkey, name or rkey))
    width = max(len(name) for _, name in feeds) + 1
    for rkey, name in feeds:
        print(f"  {name + ':':<{width}}  https://bsky.app/profile/{handle}/feed/{rkey}")


def print_endpoint(host: str, oauth_ok: bool) -> None:
    """The one public URL and what answers on it."""
    print(f"Public URL:  https://{host}")
    print("  /xrpc/* and /.well-known/did.json reach the api (the feed generator);")
    print("  everything else reaches the frontend.")
    if oauth_ok:
        print("  (sign in with real Bluesky there — browse that URL, not localhost)")
    else:
        print("  (real sign-in disabled until OAuth keys are set)")


def restore_stack(ctx: Ctx) -> None:
    """Recreate api and frontend at their local defaults (no tunnel DID, no
    public origin, dev-auth shim back). Used by `down` and by `up`'s rollback.
    Re-supplies the api's ES key — a bare recreate would drop it and leave the
    api crash-looping."""
    print("devctl: restoring the api and frontend to local defaults")
    ctx.unset("GE_DEV_APP_ORIGIN", "GE_DEV_FRONTEND_ALLOWED_HOSTS", "GE_DEV_FUNCTIONS_TARGET")
    recreate_env(ctx)
    # Deliberately not firebase: reverting the frontend to the dev-auth shim
    # already disables real OAuth, and recreating firebase would needlessly
    # wipe its emulator data. A stale APP_ORIGIN left in the functions is
    # unused once the shim is back.
    compose(
        ctx,
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "api",
        "frontend",
        check=False,
        quiet=True,
        quiet_err=True,
    )


def bsky_down(ctx: Ctx) -> None:
    if not ctx.bsky_state.is_file():
        die(f"no active bsky session for this instance{ctx.label()} — nothing to tear down")
    handle = env_file_value(ctx.bsky_state, "GE_BSKY_HANDLE") or ctx.get("GE_DEV_BSKY_HANDLE")
    app_password = ctx.get("GE_BSKY_APP_PASSWORD")
    if handle and app_password and service_running(ctx, "api"):
        print(f"devctl: deleting feed records from {handle}")
        deleted = compose_exec(
            ctx,
            "api",
            "pipenv",
            "run",
            "python",
            "scripts/publish_feed.py",
            "--delete-all",
            "--handle",
            handle,
            "--app-password",
            app_password,
            check=False,
        )
        if not deleted.ok:
            warn("devctl: warning — feed deletion failed; check with publish_feed.py --list")
    else:
        warn(
            "devctl: warning — can't delete feed records (api down or no app password);"
            " do it manually:"
        )
        warn(f"        publish_feed.py --delete-all --handle {handle}")

    tunnel.stop(ctx)
    ctx.bsky_state.unlink(missing_ok=True)
    (ctx.runtime / FEEDS_TSV).unlink(missing_ok=True)
    restore_stack(ctx)
    print("devctl: bsky session torn down — back to a local-only feed.")


def bsky_status(ctx: Ctx) -> None:
    flag = ctx.name_flag()
    if not ctx.bsky_state.is_file():
        print(f"no active bsky session{ctx.label()}.")
        print(f"start one with: devctl bsky up{flag}")
        return
    handle = env_file_value(ctx.bsky_state, "GE_BSKY_HANDLE")
    did = env_file_value(ctx.bsky_state, "GE_BSKY_DID")
    # The DID is the hostname, so it stands in for a state file written before
    # the two tunnels became one (that one recorded GE_BSKY_API_HOST instead).
    host = env_file_value(ctx.bsky_state, "GE_BSKY_HOST") or did.removeprefix("did:web:")
    oauth_ok = env_file_value(ctx.bsky_state, "GE_BSKY_OAUTH_OK") == "1"
    # The tunnel containers stop with the rest of the stack (`devctl down`, a
    # reboot), but the published records don't go anywhere — so distinguish a
    # session that's serving from one that's only still owed a cleanup.
    if service_running(ctx, "ngrok"):
        print(f"bsky session active{ctx.label()} as {handle or '?'} ({did or '?'}).")
    else:
        print(
            f"bsky session recorded{ctx.label()} as {handle or '?'} ({did or '?'}),"
            " but its tunnel isn't running."
        )
        # `devctl up` doesn't reopen the tunnel (and a free ngrok URL wouldn't
        # come back the same), so the way forward is down and, if wanted, up.
        print("The feeds below are still published and now resolve to nothing:")
        print(f"  devctl bsky down{flag} deletes them; devctl bsky up{flag} starts a new session.")
    print()
    print("Published feeds:")
    print_feed_links(ctx, handle)
    print()
    print_endpoint(host or "?", oauth_ok)
    print()
    print(f"tear down with: devctl bsky down{flag}")
