"""Commands that build, start, load, restart and remove the stack."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import bsky, eurosky
from .assets import (
    check_model_fixture_compat,
    cmd_fetch_fixture,
    cmd_fetch_models,
    fixtures_hint,
    have_fixtures,
    have_models,
    models_hint,
    models_tag,
)
from .bridgeu import BRIDGEU_REPO, have_bridgeu, issue_bridgeu_key
from .compose import compose, service_running
from .ctx import DEFAULT_INSTANCE, Ctx, die, warn
from .envfiles import env_file_value
from .es import (
    API_KEY,
    ES_OWNER,
    check_memory_budget,
    es_count,
    export_api_es_env,
    owner_es_port,
    preflight_shared_es,
    record_share_choice,
    resolve_shared_es,
    share_es,
    sharers_of_owner,
    warn_stale_data,
)
from .instance import (
    check_docker,
    check_repos,
    ensure_runtime,
    record_repo_root,
    resolve_ports,
)
from .live import preflight_live, record_live_choice, resolve_live

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


# --- up ----------------------------------------------------------------------


@dataclass
class UpOptions:
    jetstream: bool = False
    megastream: bool = False
    dedicated_es: bool = False
    repo_root: str = ""


def parse_up(ctx: Ctx, args: list[str]) -> UpOptions:
    opts = UpOptions()
    # Start from what's configured, not from what the last `up` recorded, so
    # this command fully determines the result.
    ctx.export("GE_DEV_LIVE", ctx.live_configured)
    ctx.export("GE_DEV_LIVE_ENV", ctx.live_env_configured)
    i = 0

    def value() -> str:
        if i + 1 >= len(args) or not args[i + 1]:
            die(f"{args[i]} needs a value")
        return args[i + 1]

    while i < len(args):
        arg = args[i]
        if arg == "--with-jetstream":
            opts.jetstream = True
        elif arg == "--with-megastream":
            opts.megastream = True
        # Posts and likes are two halves of the same firehose; one without the
        # other is rarely what anyone wants.
        elif arg == "--with-ingest":
            opts.jetstream = opts.megastream = True
        # Give this named instance its own Elasticsearch instead of reading the
        # owner's. The reason to want it is a change to seeding or the index
        # setup, which a sharer (read-only) can't exercise. Costs ~1.9GB more.
        elif arg == "--dedicated-es":
            opts.dedicated_es = True
        # Mount a different set of checkouts (a second working copy, a set of
        # git worktrees) into this instance, so parallel instances can work on
        # parallel copies of the code. Recorded per instance: later commands
        # and later `up`s keep it until another --repo-root (or nuke).
        elif arg == "--repo-root":
            if i + 1 >= len(args) or not args[i + 1]:
                die("--repo-root needs a directory")
            opts.repo_root = args[i + 1]
            i += 1
        elif arg == "--live":
            ctx.export("GE_DEV_LIVE", value())
            i += 1
        elif arg == "--live-env":
            ctx.export("GE_DEV_LIVE_ENV", value())
            i += 1
        # Off by default (issue api#283): a rebuild triggered by an editor
        # autosave is wasted work at best, and at worst it restarts the api
        # underneath an agent that's mid-request. Agents rebuild deliberately
        # with `devctl restart <service>`; people who want a reload loop ask.
        elif arg == "--watch":
            ctx.export("GE_DEV_API_RELOAD", "1")
            ctx.export("GE_DEV_INFERENCE_RELOAD", "1")
        else:
            die(f"unknown argument to up: {arg} (try: devctl help)")
        i += 1
    return opts


def decide_repo_root(ctx: Ctx, flag: str) -> None:
    """`up` decides the repo root: the flag, else an explicit GE_DEV_REPO_ROOT
    from the caller, else whatever this instance already mounts. Absolute,
    because compose resolves bind mounts against --project-directory, not
    against the shell's cwd."""
    want = flag or ctx.repo_root_configured or str(ctx.repo_root)
    if not Path(want).is_dir():
        die(f"repo root doesn't exist: {want}")
    ctx.export("GE_DEV_REPO_ROOT", os.path.abspath(want))


def cmd_up(ctx: Ctx, args: list[str]) -> None:
    opts = parse_up(ctx, args)
    decide_repo_root(ctx, opts.repo_root)

    check_docker(ctx)
    check_repos(ctx)
    resolve_ports(ctx, "allocate")
    ensure_runtime(ctx)
    record_repo_root(ctx)
    resolve_live(ctx)
    record_live_choice(ctx)

    # A named instance shares the owner's cluster by default; --dedicated-es
    # or its own --live es opt out. `--live es` points somewhere else
    # entirely, so there's nothing to share. Decided here and recorded, so
    # seed/status/es on this instance all agree about where its data lives.
    share = ctx.instance != ES_OWNER and not opts.dedicated_es and not ctx.live_has("es")
    ctx.export("GE_DEV_SHARE_ES", "1" if share else "")
    record_share_choice(ctx)
    check_memory_budget(ctx)
    resolve_shared_es(ctx)

    if share_es(ctx) and (opts.megastream or opts.jetstream):
        die(
            "live ingestion writes to Elasticsearch, but this instance shares the\n"
            f"        '{ES_OWNER}' instance's cluster (read-only here). Run ingest on the owner,\n"
            f"        or give this one its own: devctl up --name {ctx.instance} --dedicated-es"
        )

    # One list, built from what's staying local. A live service's container
    # isn't just skipped but stopped: it's still a defined service, so
    # --remove-orphans leaves it running, and it would go on answering
    # nothing. A sharing instance runs no Elasticsearch at all.
    local_services = ["firebase", "dev-auth"]
    if not share_es(ctx):
        local_services.append("elasticsearch")
    for service in ("inference", "perspective-stub"):
        if ctx.live_has(service.removesuffix("-stub")):
            compose(ctx, "stop", service, check=False, quiet=True, quiet_err=True)
        else:
            local_services.append(service)

    if not ctx.live_has("inference"):
        if not have_models(ctx):
            models_hint(ctx)
            raise SystemExit(1)
        check_model_fixture_compat(ctx)
    if ctx.live_has("mysky") and not have_bridgeu(ctx):
        die(
            f"--live mysky points bridgeu-api at a deployed api, but {BRIDGEU_REPO}"
            f" isn't checked out:\n        {ctx.repo_root}/{BRIDGEU_REPO}"
        )
    preflight_live(ctx)

    # Checked before anything starts: without these the container just fails
    # its config validation and restarts, which reads as a broken environment
    # rather than a missing setting. Jetstream needs no equivalent — it's a
    # public firehose.
    if opts.megastream and not (
        ctx.get("GE_AWS_S3_ACCESS_KEY") and ctx.get("GE_AWS_S3_SECRET_KEY")
    ):
        die(
            "--with-megastream needs GE_AWS_S3_ACCESS_KEY and GE_AWS_S3_SECRET_KEY\n"
            "        in devenv.local.env — the post archives are a private bucket, unlike\n"
            "        the public Jetstream firehose."
        )

    # --remove-orphans clears containers for services the compose files no
    # longer define (e.g. the standalone `firestore` emulator the Firebase
    # suite replaced, which would otherwise linger holding its old port).
    # --wait because the api doesn't depends_on the swappable ones, so this is
    # what guarantees they're serving before it starts.
    compose(ctx, "up", "-d", "--remove-orphans", "--wait", *local_services)
    # Before the api starts: on a first run both would otherwise install the
    # api's dependencies into the same volume at once.
    issue_bridgeu_key(ctx)

    # A sharing instance runs no cluster of its own, so there's nothing to
    # initialize and the api's key is the owner's (already exported). Its api
    # also can't `depends_on: elasticsearch` — that service isn't started here
    # — so it starts without deps; what it truly needs (firebase) is up.
    api_up = ["up", "-d"]
    if share_es(ctx):
        preflight_shared_es(ctx)
        api_up.append("--no-deps")
    else:
        print("devctl: initializing Elasticsearch (templates, indexes, API keys)...")
        compose(ctx, "run", "--rm", "es-init")
        export_api_es_env(ctx)
    # BridgEU doesn't depend_on the mysky api: it talks to it over HTTP, the
    # way it will across clouds, so it has to cope with the api not being up.
    app_services = ["api", "frontend"]
    if have_bridgeu(ctx):
        app_services.append("bridgeu-api")
    else:
        # A container left from a repo root that had the checkout would
        # otherwise go on serving code this instance no longer mounts.
        compose(ctx, "stop", "bridgeu-api", check=False, quiet=True, quiet_err=True)
    compose(ctx, *api_up, *app_services)

    print_up_summary(ctx, opts)
    start_ingest(ctx, opts)
    if ctx.instance != DEFAULT_INSTANCE:
        name = ctx.instance
        print(f"  ** instance '{name}' — every other devctl command needs --name {name}")

    # The posts alias doesn't exist until the first seed (the ingest binary
    # creates it, as in prod) — a missing alias reads like an empty index. A
    # sharer can't seed, so an empty owner is the owner's problem, not a
    # prompt to run something here.
    if es_count(ctx, "posts") in ("", "0"):
        if share_es(ctx):
            print(f"The shared '{ES_OWNER}' cluster has no posts yet — seed it: devctl seed")
        elif have_fixtures(ctx):
            print("Elasticsearch is empty — run: devctl seed")
        else:
            fixtures_hint(ctx)
    elif not ctx.live_has("es"):
        # Data present but possibly aged past the api's recency window. Not
        # under --live es, where local data age is moot.
        warn_stale_data(ctx)


def print_up_summary(ctx: Ctx, opts: UpOptions) -> None:
    p = ctx.port
    print(f"devctl: environment is up{ctx.label()}.")
    print(
        f"  frontend      http://127.0.0.1:{p('FRONTEND')}"
        "    (first start installs deps; watch: devctl logs -t frontend)"
    )
    print(
        f"  api           http://127.0.0.1:{p('API')}"
        "    (first start installs deps; watch: devctl logs -t api)"
    )
    if ctx.repo_root != ctx.default_repo_root:
        print(f"  code          mounted from {ctx.repo_root}")
    if share_es(ctx):
        print(
            f"  elasticsearch shared from '{ES_OWNER}' (http://127.0.0.1:{owner_es_port(ctx)})"
            "    read-only here"
        )
    else:
        password = ctx.get("GE_DEV_ES_PASSWORD", "ge-dev-elastic")
        print(f"  elasticsearch http://127.0.0.1:{p('ES')}    (user elastic / {password})")
    print(
        f"  firestore     http://127.0.0.1:{p('FIRESTORE')}"
        "    (emulator, from the frontend checkout)"
    )
    print(f"  firebase auth http://127.0.0.1:{p('FIREBASE_AUTH')}")
    print(f"  functions     http://127.0.0.1:{p('FUNCTIONS')}")
    print(
        f"  emulator UI   http://127.0.0.1:{p('FIREBASE_UI')}"
        "    (browse Firestore data, auth users, function logs)"
    )
    if ctx.live_has("inference"):
        print(f"  inference     LIVE {ctx.get('GE_DEV_INFERENCE_URL')}")
    else:
        print(f"  inference     {models_tag(ctx)}    (watch: devctl logs -t inference)")
    if have_bridgeu(ctx):
        print(
            f"  bridgeu-api   http://127.0.0.1:{p('BRIDGEU_API')}"
            "    (BridgEU; reaches the api as http://mysky-api:8000)"
        )
    if ctx.live_has("mysky"):
        url = ctx.get("GE_DEV_BRIDGEU_MYSKY_API_URL")
        print(f"  ** bridgeu-api is calling the LIVE mysky api: {url}")
    if ctx.live_has("es"):
        print(f"  ** the api is reading LIVE Elasticsearch: {ctx.get('GE_DEV_ES_URL')}")
    if ctx.live_has("perspective"):
        print("  ** the api is calling the LIVE Perspective API")


def start_ingest(ctx: Ctx, opts: UpOptions) -> None:
    if opts.megastream:
        compose(ctx, "--profile", "megastream", "up", "-d", "megastream-ingest")
        print("  megastream    streaming live posts from the archives (needs internet)")
    if opts.jetstream:
        compose(ctx, "--profile", "jetstream", "up", "-d", "jetstream-ingest")
        print("  jetstream     streaming live likes from the public firehose (needs internet)")
    # Likes arriving for posts the index will never hold is a confusing way to
    # run, and the reverse leaves the like graph frozen.
    if opts.jetstream != opts.megastream:
        warn("  ** only half of live ingestion is on; --with-ingest runs both")


# --- bootstrap -----------------------------------------------------------------


def cmd_bootstrap(ctx: Ctx, args: list[str]) -> None:
    # Fresh clone to working, seeded environment in one command (issue
    # api#284): setup, fetch the fixture, start the stack, seed it. Each step
    # skips work that's already done, so an interrupted bootstrap resumes
    # where it left off rather than starting over. Flags pass through to `up`.
    ctx.in_bootstrap = True
    # `up` parses and records --repo-root, but setup runs first and looks at
    # the same checkouts (check_repos, the inference dependency install), so
    # the flag's value has to apply before it — cmd_up validates and records it.
    for flag, value in zip(args, args[1:], strict=False):
        if flag == "--repo-root":
            ctx.export("GE_DEV_REPO_ROOT", value)
    cmd_setup(ctx, [])
    if not have_fixtures(ctx):
        cmd_fetch_fixture(ctx, [])
    cmd_up(ctx, args)
    if share_es(ctx):
        print(
            f"devctl: bootstrap complete{ctx.label()} — serving the '{ES_OWNER}' instance's data."
        )
    else:
        posts = es_count(ctx, "posts")
        if posts in ("", "0"):
            cmd_seed(ctx, [])
        else:
            print(f"devctl: already seeded ({posts} posts) — a fresh load is: devctl seed")
        print(f"devctl: bootstrap complete{ctx.label()}.")
    print(f"Try it:  devctl feed{ctx.name_flag()}     (a ranked feed, in the terminal)")
    print(f"         open http://127.0.0.1:{ctx.port('FRONTEND')}")


# --- seed ----------------------------------------------------------------------

# In order: each step needs what the one before it wrote.
SEED_STEPS = (
    ("wiping previously seeded data...", ("seed-wipe",)),
    ("rebasing fixture timestamps...", ("seed-rebase",)),
    # Firestore UserDocuments for every fixture persona (not just the probe
    # persona seed-rebase pointed .runtime/probe.env at) — so a freshly seeded
    # instance has more than one Firestore-tracked user for things like ingex's
    # followed_users_backfill job to walk. Needs /runtime/seed/manifest.json,
    # which seed-rebase just wrote.
    ("seeding Firestore users for fixture personas...", ("seed-firestore-users",)),
    (
        "ingesting posts via megastream_ingest (first run compiles the Go binary)...",
        ("seed-megastream",),
    ),
    ("loading likes and like counts...", ("seed-likes",)),
    ("backfilling the two-tower quality corpus...", ("seed-quality",)),
    (
        "publishing recent-post aliases...",
        ("seed-likes", "python", "/seedscripts/load_likes.py", "--aliases-only"),
    ),
)


def cmd_seed(ctx: Ctx, args: list[str]) -> None:
    check_docker(ctx)
    ensure_runtime(ctx)
    # A sharing instance has no cluster of its own and only a read-only key to
    # the owner's, so there is nothing here it may seed. Point at the owner
    # rather than fail obscurely on a missing key.
    if share_es(ctx):
        owner_flag = "" if ES_OWNER == DEFAULT_INSTANCE else f" --name {ES_OWNER}"
        die(
            f"this instance shares the '{ES_OWNER}' instance's Elasticsearch,"
            " which is read-only here.\n"
            f"        Seed the owner:                 devctl seed{owner_flag}\n"
            f"        Or give this one its own:       devctl up --name {ctx.instance}"
            f" --dedicated-es, then devctl --name {ctx.instance} seed"
        )
    if not have_fixtures(ctx):
        fixtures_hint(ctx)
        raise SystemExit(1)
    if not env_file_value(ctx.runtime / "es_key_rw.env", API_KEY):
        die("no ES key yet — run: devctl up")
    check_model_fixture_compat(ctx)

    resolve_live(ctx)

    # Seeding always writes to the *local* cluster — the seed services address
    # it directly and ignore GE_DEV_ES_URL, so there is no way for this to
    # touch a deployed one. Worth saying out loud, because the api is
    # meanwhile reading somewhere else and the data about to be loaded won't
    # show up in a feed.
    if ctx.live_has("es"):
        warn("devctl: note — seeding local Elasticsearch, but the api is reading")
        warn(f"        {ctx.get('GE_DEV_ES_URL')}, so this data won't appear in feeds.")

    # megastream_ingest computes each post's ge_post_embedding by calling the
    # inference service, so the seed is only meaningful once that service has
    # actually loaded its models. seed-megastream can't depend_on it — whether
    # it runs at all is decided here, not in the compose file — so wait
    # explicitly. --wait blocks on the healthcheck, which probes /ready.
    if not ctx.live_has("inference"):
        print("devctl: waiting for inference to be ready...")
        if not compose(ctx, "up", "-d", "--wait", "inference", check=False).ok:
            die("inference never became ready — see: devctl logs inference")

    for message, run in SEED_STEPS:
        print(f"devctl: {message}")
        compose(ctx, "run", "--rm", *run)

    persona = env_file_value(ctx.runtime / "probe.env", "GE_PROBE_USER_DID")
    # Restart the api so it picks up the new persona from .runtime/probe.env —
    # but only if it was already running, so `seed` never starts a service the
    # developer deliberately stopped.
    api_running = service_running(ctx, "api")
    if api_running:
        export_api_es_env(ctx)
        compose(ctx, "up", "-d", "--force-recreate", "api")

    print(f"devctl: seed complete (persona: {persona or 'unset'}).")
    if api_running:
        print("Try a feed:")
        print("  devctl feed")
    else:
        # Don't suggest a feed request with nothing to serve it.
        print("The api isn't running — start it with: devctl up")


# --- restart, down, nuke -------------------------------------------------------


def cmd_restart(ctx: Ctx, args: list[str]) -> None:
    # Recreate rather than `compose restart`, so a service picks up changed
    # environment (a new ES key, a different live target) and not just
    # changed code. --no-deps keeps it to the named services: restarting the
    # api should not bounce Elasticsearch underneath everything else.
    if not args:
        die("usage: devctl restart <service>... (whole stack: devctl up)")
    check_docker(ctx)
    resolve_live(ctx)
    export_api_es_env(ctx)
    if "inference" in args:
        check_model_fixture_compat(ctx)
    compose(ctx, "up", "-d", "--no-deps", "--force-recreate", *args)
    # A recreated emulator has forgotten the key.
    if "firebase" in args:
        issue_bridgeu_key(ctx)
    print(f"devctl: restarted{ctx.label()}: {' '.join(args)}")


def remove_firebase_config(ctx: Ctx) -> None:
    (ctx.repo_root / "frontend" / ctx.get("GE_DEV_FIREBASE_CONFIG")).unlink(missing_ok=True)


def cmd_down(ctx: Ctx, args: list[str]) -> None:
    # Stopping the owner leaves any sharer's api reading a cluster that's no
    # longer there. Data survives (down keeps volumes), but say so.
    if ctx.instance == ES_OWNER:
        sharers = sharers_of_owner(ctx)
        if sharers:
            warn(
                f"devctl: note — these instances read this one's Elasticsearch: {' '.join(sharers)}"
            )
    compose(ctx, "down")
    remove_firebase_config(ctx)
    bsky.warn_if_published(ctx)
    eurosky.warn_if_published(ctx)


def cmd_nuke(ctx: Ctx, args: list[str]) -> None:
    # The owner's cluster holds the data every sharer is serving, so
    # destroying it out from under them would look like several environments
    # breaking at once. Refuse while any sharer's runtime is still on disk;
    # GE_DEV_FORCE_NUKE is the escape hatch for when that's the intent.
    if ctx.instance == ES_OWNER and not ctx.get("GE_DEV_FORCE_NUKE"):
        sharers = sharers_of_owner(ctx)
        if sharers:
            die(
                f"these instances share this one's Elasticsearch: {' '.join(sharers)}\n"
                "        Nuking it takes their data with it. Either nuke them first\n"
                "        (devctl nuke --name <name>), give each its own cluster\n"
                "        (devctl up --name <name> --dedicated-es), or force it:\n"
                "        GE_DEV_FORCE_NUKE=1 devctl nuke"
            )
    bsky.warn_if_published(ctx, "nuke")
    eurosky.warn_if_published(ctx, "nuke")
    compose(ctx, "down", "-v", "--remove-orphans")
    # Volumes are namespaced by compose project, and the runtime directory and
    # derived Firebase config are named after the instance — so this destroys
    # only this instance's data, whatever else is running.
    shutil.rmtree(ctx.runtime, ignore_errors=True)
    remove_firebase_config(ctx)
    print(f"devctl: all containers, data volumes, and runtime state removed{ctx.label()}.")
