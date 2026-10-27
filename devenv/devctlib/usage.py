"""The text `devctl help` prints."""

USAGE = """\
devctl — Green Earth local dev environment

Usage: devctl [--name <instance>] <command> [args]

Setup (first run):
  bootstrap        fresh clone to a working, seeded environment in one command
                   (setup + fetch-fixture + up + seed; resumes if interrupted)
  setup            check prerequisites, build/pull images, download models,
                   install inference-service's dependencies
  fetch-fixture    download the published fixture set (no account or auth needed)
  fetch-models     download the pinned trained models (no account or auth needed)
  use-ranker <gs://run-or-manifest>
                   download and select a ranker for this instance (needs gcloud)
                   history length is required in the training configuration
                     --reset                         return to the bundle's ranker
                   prepares files only; never starts or restarts a service

Run the stack:
  up               start the stack (ES + Firebase emulators + inference + api,
                   and the BridgEU services when bridg-eu is checked out)
                     --live <svc>[,<svc>...]          use deployed services
                       (es, inference, perspective; mysky for bridgeu-api)
                     --live-env stage|prod            which deployment (default stage)
                     --with-ingest                    also ingest live posts+likes
                       (--with-megastream / --with-jetstream for one half)
                     --watch                          reload api/inference/bridgeu-api on edit
                     --dedicated-es                   own Elasticsearch, not shared
                     --repo-root <dir>                mount checkouts from another
                       working copy (sticks per instance; default: this repo's parent)
  seed             load fixtures: rebase timestamps, ingest posts, load likes
  restart <svc>    recreate one service, picking up code and config changes
  down             stop containers (data volumes survive)
  nuke             stop and delete everything (volumes, minted keys, seed state)

Work with it:
  test [svc]       run a repo's tests inside the env, or all with no arg
                     (api, inference, ingex, frontend, bridgeu)
  exec <svc> ...   run any command in a service (e.g. exec api pipenv run pytest)
  feed [feed]      view a hydrated feed in the terminal (default: your-feed;
                   --user <did>, --limit, --pages, --json for raw skeleton;
                   --bridgeu for a BridgEU feed, default: passthrough)
  es <path>        query this instance's Elasticsearch (read-only key, e.g.
                   devctl es /posts/_count)
  login [did]      print a sign-in URL for the frontend as the seeded persona
  status           container status + index doc counts + seed age
  doctor           check everything and name the fix — run this first when
                   the environment misbehaves
  logs [svc...]    print recent logs and exit (-t follows instead)
  pull             fetch + fast-forward every sibling checkout (bridg-eu too) and
                   flag any on a branch other than main (--repo-root <dir> for
                   another copy)

Serve on real Bluesky or Eurosky (opt-in; ngrok runs as a container — just an authtoken):
  bsky up          tunnel this env to Bluesky and publish its feeds; the same
                   public URL serves the frontend for real sign-in. Prints the
                   feed + sign-in links
  bsky down        delete those feed records and close the tunnel
  bsky status      show the active session's URL, sign-in note and feed links
       (config: NGROK_AUTHTOKEN, GE_DEV_BSKY_HANDLE, GE_BSKY_APP_PASSWORD; see README)
  eurosky up       the same for BridgEU: tunnel bridgeu-api and publish its feeds
                   to your dev Eurosky account, to load in Skeets, mu or Bluesky
  eurosky down     delete those feed records and close the tunnel
  eurosky status   show the active session's URL and feed links
       (config: NGROK_AUTHTOKEN, GE_DEV_EUROSKY_HANDLE, GE_DEV_EUROSKY_APP_PASSWORD)
       bsky and eurosky share one tunnel, so only one runs at a time.

Instances & ports:
  ls               every instance on this machine, and whether it's running
  ports [svc]      this instance's published ports (one number with a service;
                   --ssh prints port-forwarding flags for remote use)
  tunnel [env]     port-forward a deployed Elasticsearch, for --live es

Instances
  --name <n> runs a second environment with its own containers, volumes and
  ports; it works on every command and defaults to "dev". A named instance
  reads dev's Elasticsearch (the memory-hungry part), so it's read-only against
  that data — seed dev, or pass --dedicated-es to give it its own cluster.
  Give each instance its own working copy (e.g. git worktrees) with
  'up --repo-root <dir>' so parallel agents edit parallel code — the choice is
  remembered per instance and shown by devctl ls. See docs/agents.md.

Fixtures & models
  fetch-fixture and fetch-models download published sets — no account needed.
  Roll your own with fixtures/generate_sample.py (two modes) and
  fixtures/publish_fixture.sh; maintainers publish models with
  models/publish_models.sh (from the prod bucket).

Configuration (devenv.local.env, gitignored — see devenv.local.env.example)
  Live-service keys:   GE_DEV_ES_API_KEY, GE_DEV_INFERENCE_API_KEY,
                       GE_PERSPECTIVE_API_KEY
  Stick a live choice: GE_DEV_LIVE, GE_DEV_LIVE_ENV (else a bare 'up' is all-local)
  Serve on bsky:       NGROK_AUTHTOKEN, GE_DEV_BSKY_HANDLE, GE_BSKY_APP_PASSWORD,
                       GE_DEV_NGROK_DOMAIN (bsky up/down)
  Serve on eurosky:    NGROK_AUTHTOKEN, GE_DEV_EUROSKY_HANDLE,
                       GE_DEV_EUROSKY_APP_PASSWORD (eurosky up/down)
  Other overrides:     ports (GE_DEV_PORT_*), ES heap (GE_DEV_ES_HEAP),
                       repo root (GE_DEV_REPO_ROOT)
"""


def usage() -> None:
    print(USAGE, end="")
