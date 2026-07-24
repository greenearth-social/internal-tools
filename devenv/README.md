# Green Earth dev environment

A cohesive, agent-friendly local development environment for the Green Earth
serving stack ([api#268](https://github.com/greenearth-social/api/issues/268)).
One Docker Compose project runs Elasticsearch, the Firebase emulators, the api,
the frontend, and the real inference-service over the real trained models,
seeded with representative Bluesky data — no GCP, Firebase, or production
credentials required. Feeds come back in the order the model would actually put
them ([Models](#models)).

Code stays on your host filesystem: the sibling `api` and `ingex` checkouts are
bind-mounted into containers, so you edit with your normal editor and commit
with your normal git, while everything executes inside containers.

## Prerequisites

- Docker (with Compose v2)
- The sibling repos checked out next to this one (`devctl` verifies this):

```text
<parent>/
  api/               required
  ingex/             required (index templates + megastream_ingest)
  inference-service/ required (serves the trained models)
  internal-tools/    this repo
  frontend/          required
```

If your layout differs, set `GE_DEV_REPO_ROOT=<parent>` in
`devenv/devenv.local.env`. A single instance can also mount a different
working copy with `up --repo-root <dir>` — see
[Running more than one environment](#running-more-than-one-environment).

**New here?** [docs/onboarding.md](docs/onboarding.md) is the full
walkthrough, from clone to a ranked feed. The rest of the docs directory:
[troubleshooting](docs/troubleshooting.md) (symptoms → fixes),
[runbooks](docs/runbooks.md) (regenerating fixtures and models),
[remote](docs/remote.md) (running this on another machine), and
[agents](docs/agents.md) (using it as a coding-agent sandbox).

## Quick start

```bash
cd internal-tools/devenv
./devctl bootstrap       # setup + fetch fixture + up + seed, resumable
./devctl feed
```

Bootstrap runs the whole first-time sequence — image builds, the pinned
models (~33MB), the published data sample (~170MB), start, seed. Budget
20–30 minutes, most of it the seed. The individual steps are also commands
(`setup`, `fetch-fixture`, `up`, `seed`) when you want just one; after
setup, everything but the downloads works offline.

`devctl feed <feed>` shows a feed in the terminal: it requests
`getFeedSkeleton` from the local api as the seeded dev persona, hydrates each
post from this environment's Elasticsearch (no AppView call, so it works
entirely against fixture data), and prints author, timestamp, like count, text,
and per-post pipeline detail (which generator retrieved it, its rank and ranker
score). Defaults to `your-feed` and the seeded persona.

```bash
./devctl feed                            # the seeded persona's your-feed
./devctl feed your-feed --user did:plc:… # a different feed as a chosen persona
./devctl feed random --limit 50 --pages 2  # page through more of the feed
./devctl feed --json                     # the raw getFeedSkeleton response
```

The persona defaults to the fixture manifest's DID (via `.runtime/probe.env`)
and the request is a development session (see below), so the api runs the full
signed-in path and records a snapshot. The viewer is a read-only client — it
never posts, likes, or writes anything. `--no-pipeline` skips the snapshot
lookup and shows the feed as a plain client would.

Other commands: `devctl status`, `devctl doctor` (check everything, name
the fix — run it first when the environment misbehaves), `devctl logs
[service]` (add `-t` to follow), `devctl es <path>` (query Elasticsearch),
`devctl exec <service> <cmd>` (run something inside a container), `devctl
restart <service>`, `devctl down` (stop, keep data), `devctl nuke` (delete
everything). Data is disposable by design — when in doubt, `nuke` and
re-`seed`.

## Running commands inside the environment

Dependencies are installed in containers, not on your host — there is no
`node_modules`, no `.venv` and no Go toolchain in your checkouts. Everything
runs through `devctl`.

`devctl test` runs a repo's test suite with the right runner, or all of them
with no argument:

```bash
./devctl test               # api, inference, ingex, frontend
./devctl test api           # one repo
./devctl test ingex         # Go tests, in a throwaway container — no stack needed
```

`api`, `inference` and `frontend` run in the live containers (so the stack has
to be up); `ingex` gets an ephemeral Go container over the same bind-mounted
source the ingest uses. It exits nonzero and names the suite if anything fails.

For anything finer — a single file, a `-k` filter, a linter, a one-off script —
`devctl exec` runs any command inside a service, with the right interpreter and
environment already in place:

```bash
./devctl exec api pipenv run pytest src/app/lib/firestore_test.py -v
./devctl exec api pipenv run ruff check .
./devctl exec frontend npm run typecheck
./devctl es /posts/_count
```

This is the point of having one wrapper: a coding agent's sandbox can allow
`devctl` and get all of the above, rather than being granted `docker`,
`pipenv`, `npm` and `go` separately.

### Pointing a coding agent at this

This is the point of having one wrapper: a sandbox that allows `devctl` gets
tests, execs, logs, feeds, and diagnosis without being granted `docker`,
`pipenv`, `npm` and `go` separately — and everything that could touch a
deployed system is enforced read-only in code. **[docs/agents.md](docs/agents.md)**
covers the sandbox recipe, what an unattended agent can and can't damage, and
running one instance per agent. The skill at
`internal-tools/.claude/skills/devenv/` teaches the working loop; Claude Code
finds it automatically in this repo or the parent directory, or via
`claude --add-dir ../internal-tools` from a sibling repo.

### Editing code

Source is bind-mounted, so edits are visible inside the containers
immediately. What isn't automatic is restarting the process:

```bash
./devctl restart api        # after an api change
./devctl up --watch         # or run api + inference under --reload instead
```

Reload is off by default. Under `--watch` a save mid-request restarts the
service underneath it, and for inference each reload re-loads the TorchScript
towers and fails readiness while it does — which is disruptive when an agent is
editing and running requests in the same loop. The frontend always hot-reloads
(Vite). The Go ingest services run under `go run`, so they recompile whenever
they're restarted.

`devctl restart` recreates the container rather than just restarting the
process, so it picks up changed configuration too — a re-minted ES key, a
different `--live` target.

## Running more than one environment

`--name` gives you a second environment: its own containers, volumes, minted
keys and published ports. Two agents (or two branches) can run at once without
either noticing the other.

```bash
./devctl up --name featurework      # first use allocates a free port block
./devctl --name featurework exec api pipenv run pytest
./devctl ports --name featurework   # what it got
./devctl ls                         # every instance, and whether it's running
./devctl nuke --name featurework    # destroys only this one
```

`--name` works on every command and defaults to `dev`, which is the environment
the rest of this README describes — on the documented ports, with no offset.
Named instances take the next free block of ports: the first gets `+10` (api
8310, frontend 3010, Elasticsearch 9211), the second `+20`, and so on. The
allocation is made once and remembered in the instance's runtime directory, so
an instance's ports don't move between restarts.

### Each instance can mount its own working copy

By default every instance bind-mounts the same sibling checkouts. That's fine
for one task at a time, but the point of instances is parallel work — and
parallel work wants parallel code. `up --repo-root <dir>` points an instance at
a different directory of checkouts (a second clone, or git worktrees — see the
recipe in [docs/agents.md](docs/agents.md)):

```bash
./devctl up --name agent1 --repo-root ~/src/ge-agent1
```

The choice is remembered per instance, exactly like the `--live` and
`--dedicated-es` choices: every later command (`exec`, `test`, `nuke`) acts on
the recorded working copy, a later bare `up` recreates against it rather than
reverting to the default, and `devctl ls` shows which instance mounts what.
Pass `--repo-root` again to move an instance; `nuke` forgets it.

### A named instance shares `dev`'s Elasticsearch

Elasticsearch is the expensive part — ~1.9GB of an instance's ~3GB, and an hour
to seed. So a named instance doesn't run one. It reads the `dev` instance's
cluster, which makes it cost about a gigabyte, start in ~90 seconds with no
seed, and immediately see the same data. `devctl status`, `devctl es` and
`devctl feed` on the named instance all report against `dev`'s cluster; it even
borrows `dev`'s seeded persona, since it's serving `dev`'s data.

The catch is that a sharer is **read-only** against that data — enforced, the
same way live mode is. `devctl seed`, `devctl up --with-ingest`, and anything
else that would write refuse from a named instance and point you at `dev`:

```bash
./devctl up --name featurework      # ~90s, no ES, reads dev's data
./devctl --name featurework feed    # works immediately
./devctl seed                       # (re)seed the shared data on dev
```

This requires `dev` to be up and seeded — it owns the cluster. `devctl nuke`
and `devctl down` on `dev` warn (nuke refuses) while any sharer still exists,
so you don't pull the data out from under them.

### When you need your own cluster

`--dedicated-es` gives a named instance its own Elasticsearch, which is the
point only when you're changing seeding or the index setup itself — a sharer
can't exercise that. It then behaves like a fully independent environment: its
own empty cluster to seed.

```bash
./devctl up --name schematest --dedicated-es
./devctl --name schematest seed     # ~an hour; its own data, isolated
```

Now you're paying for a second Elasticsearch. Measured with `docker stats`, a
dedicated instance holds ~3GB (ES ~1.9GB of it), so two want ~6GB steady and
more during a seed. Docker's default allocation on macOS (~8GB) isn't enough:
the symptom is one instance's ES being OOM-killed (exit 137) mid-seed, which
looks like the environment breaking rather than a memory limit. `devctl up`
warns when the number of *cluster-bearing* instances outruns the memory Docker
reports (sharers don't count), and `devctl status` names any container the
kernel killed. To actually run two clusters, give Docker ~12GB (Docker Desktop
→ Settings → Resources) or set `GE_DEV_ES_HEAP=512m` in `devenv.local.env`.

### What's shared, and why

- **The `dev` cluster**, by default (above).
- **Fixtures and models** — downloaded once, read-only, identical for everyone.
- **The Go module and build caches** (`ge-dev-gomod`, `ge-dev-gocache`, ~2GB
  together). Content-addressed and safe for concurrent use, so sharing them
  saves a dedicated instance a full module download before it can seed. They're
  declared external, which means `devctl nuke --name x` leaves them alone.
- **The frontend checkout, between instances on the same repo root**:
  `functions/lib` is compiled into it by whichever instance starts. Instances
  with their own `--repo-root` don't share it. `node_modules` is a per-instance
  volume, and the derived Firebase config is named per instance
  (`firebase.devenv.<name>.json`) so one instance's `down` can't delete
  another's.

Everything else — data volumes, minted keys, seeded personas, ports — is per
instance.

## Fixtures

Normally you don't generate a fixture — `devctl fetch-fixture` downloads the
published one (a real ~130k-post prod sample) from this repo's GitHub
Releases and unpacks it into `fixtures/data/`. It's a plain HTTPS download
from a public repo: no GitHub account, no auth, no extra tooling.

Fixtures ride as release assets rather than being committed: they're large
compressed binaries that get regenerated periodically, and git can't delta
them, so each generation would add ~170 MB to every clone forever. Releases
can be deleted when they go stale; git history can't.

A seeded environment keeps working indefinitely no matter how old the release
is — seeding rebases every timestamp so the window always ends an hour before
now (see below). Replace the published fixture when its *content* gets stale
enough to matter.

### Identities in the fixture are real

Posts, post authors, and liker DIDs are all kept exactly as they appear in the
source data. Personas are therefore real accounts: their handles resolve,
their follow graphs exist, and you can look any fixture DID up against the
Bluesky API.

An earlier version pseudonymized liker DIDs, but that isn't necessary since
complete ATProto archives exist elsewhere already.

### Generating your own

`fixtures/generate_sample.py` is the committed, parameterized generator — what
makes a good sample is encoded in it (contiguous window, cohort-densified
likes, hydrated danglers, dev personas; see its docstring). `--source prod-es`
samples a real cluster through a port-forward; `--source megastream-files`
builds from megastream archives with a synthesized like graph, no credentials
needed. Output goes to `fixtures/data/` (gitignored).

The full procedure — generating, sanity-checking, and publishing a fixture
for the team — is in [docs/runbooks.md](docs/runbooks.md).

## Models

The environment runs the real `inference-service` from your checkout, serving
the same trained towers prod serves, on CPU. There's no separate mode to turn
on and nothing to opt into: `devctl setup` downloads the models and `devctl up`
starts the service.

It costs about 465MB of RAM and an 893MB venv volume. It does not cost you
speed — the post tower embeds ~6,000 posts/sec, so seeding is no slower than
with a fake, and ranking adds ~90ms to a ~700ms feed request.

### Model and fixture have to agree

`megastream_ingest` computes each post's `ge_post_embedding` by calling the
inference service at seed time, and the api's `two_tower` generator runs kNN
over that field *filtered by the model UUID that produced it*. Change the
models and the seeded embeddings no longer match: the feed comes back thin
rather than wrong, which is the safe failure but an easy one to misread. **Any
model change needs a re-seed.**

`devctl` also compares the model bundle's `manifest.json` against the fixture's
before `up` and `seed`, and refuses to run if the content encoder or its
dimensions differ. Embeddings of the same width from different encoders
multiply together perfectly well and produce confident nonsense, so that one is
worth failing loudly over.

### Where the models come from

`devctl fetch-models` (run for you by `devctl setup`) downloads a GitHub
release — the same public channel the fixtures use, for the same reason: a
fresh clone should need no Google account. The bundle lands in `models/data/`
(gitignored) and holds both TorchScript towers, the ranker, the author-index
maps, and serving manifests rewritten to local paths. inference-service then
loads plain files: no GCS, no ClearML, no network.

Unlike the fixture, the model release is **pinned** — `MODELS_TAG` in `devctl`,
overridable with `GE_DEV_MODELS_TAG`. The tag names the two training runs it
holds (`models-<two-tower>-<ranker>`), so it says what's in it and re-publishing
the same artifacts is idempotent.

Maintainers publish a new bundle with `models/publish_models.sh` and move the
pin — the procedure, including the `max_history_len` check that fails silently
when skipped, is in [docs/runbooks.md](docs/runbooks.md).

## Live ingestion (optional)

`devctl up --with-ingest` keeps the local indexes moving with real Bluesky
data, using the same two binaries as prod:

| | Source | Writes | Credentials |
| --- | --- | --- | --- |
| `megastream_ingest` | Megastream archives (S3) | posts | S3 key required |
| `jetstream_ingest` | Jetstream firehose | likes | none — public |

Both are off by default: they're the only parts of this environment that need
the internet and never finish. `--with-megastream` and `--with-jetstream` start
one half each, which is worth doing when you're debugging that binary — but for
anything else you want both, since likes arriving for posts the index will
never hold aren't much use, and posts without likes leave the like graph
frozen. `devctl up` warns when only one is running.

Posts are embedded on the way in by the same inference service the seed uses,
so anything arriving live is immediately reachable by the `two_tower`
generator's kNN query.

Megastream needs credentials for the archive bucket, in `devenv.local.env`:

```bash
GE_AWS_S3_ACCESS_KEY=<key>
GE_AWS_S3_SECRET_KEY=<secret>
```

Everything else about the bucket is defaulted to what prod reads. `devctl`
checks for these before starting anything, because without them the container
just fails validation and restarts, which looks like a broken environment
rather than a missing setting.

Live ingestion keeps its own cursor, separate from the seed's, so re-seeding
doesn't rewind it and it doesn't make the next re-seed skip the fixture. The
indexes grow for as long as it runs; `devctl nuke` resets everything.

## Live services

Three dependencies can be pointed at deployed instances instead of the local
ones, individually, for when a local stand-in can't reproduce what you're
chasing:

```bash
./devctl up --live perspective              # real Perspective API
./devctl up --live inference                # deployed inference-service
./devctl up --live es,inference             # several at once
./devctl up --live inference --live-env prod
```

`--live-env` picks stage (default) or prod; `devctl` knows the endpoints, so
you don't have to. Each service needs its key in `devenv.local.env`
(gitignored):

| `--live` | Key | Notes |
| --- | --- | --- |
| `es` | `GE_DEV_ES_API_KEY` | must be read-only; this is enforced |
| `inference` | `GE_DEV_INFERENCE_API_KEY` | |
| `perspective` | `GE_PERSPECTIVE_API_KEY` | real quota, be careful |

`devctl up` checks each one is reachable and the key works before starting
anything, so a bad key fails immediately instead of becoming a 500 in a
container log. Set `GE_DEV_LIVE=inference` in `devenv.local.env` to make a
choice stick without passing `--live` every time; otherwise a bare `devctl up`
always returns to all-local.

### What live mode will not do

Nothing here writes to a deployed backend, and a few things enforce that
rather than relying on it:

- **The live ES key has to be read-only.** `devctl` asks the cluster what the
  key can do (`_has_privileges`) and refuses to start if it can write. Prod
  publishes a read-only key for exactly this — the same one the deployed api
  reads with — and pasting the read-write one instead is an easy mistake with
  no symptom, since reading works identically either way. Override with
  `GE_DEV_ALLOW_ES_WRITE_KEY=1` if you genuinely mean it.
- **Seeding and ingestion are always local.** `devctl seed`,
  `megastream-ingest` and `jetstream-ingest` address the local cluster by
  container name and never read `GE_DEV_ES_URL`, so there's no combination of
  flags that points them at a deployed one. With `--live es`, `seed` says so
  out loud: the data it loads won't appear in feeds, because the api is
  reading somewhere else.
- **Firestore is always the emulator.** The api writes user state there, and
  there is no flag that changes it.

### Elasticsearch needs a tunnel

The cluster sits behind an internal load balancer, so `--live es` reaches it
through a port-forward. Run one in its own terminal:

```bash
./devctl tunnel stage     # or: prod
```

It reconnects on its own — `kubectl port-forward` drops connections routinely,
and a silently dead tunnel looks like a broken environment.

**Seeding always writes to the local cluster**, never to a live one: the seed
services address Elasticsearch directly and ignore the live setting. So with
`--live es` the data you seed is not the data the api reads, and `devctl
status` counts the local cluster. `devctl seed` says so when it notices.

### Live inference and your seeded embeddings

A deployed service serves whatever models it was deployed with, while your
seeded posts were embedded by whatever ran at seed time. If those disagree the
`two_tower` generator returns little or nothing, because it filters kNN by the
model UUID that produced the field. Re-seeding while pointed at the live
service keeps them consistent — but that sends every post in the fixture
through it.

**Bluesky OAuth** is covered separately under "Working on real Bluesky auth" —
it needs a public tunnel, not just a key.

## Seeding and time rebasing

`devctl seed` runs three one-shot containers:

1. **seed-rebase** — copies fixtures into `.runtime/seed/` with every
   timestamp shifted forward by one uniform delta so the capture window ends
   an hour before now. Code anchored to `now` (recency windows, popularity
   decay) sees a full window of data; relative structure (inter-post spacing,
   like-after-post ordering) is preserved exactly.
2. **seed-megastream** — runs the real `megastream_ingest` binary from your
   `ingex` checkout (`go run`, byte-identical code path to prod) against the
   rebased fixtures. Post-tower embeddings come from whichever inference
   service is running, which is why switching between them needs a re-seed.
3. **seed-likes** — bulk-loads likes (prod document identity: `_id=at_uri`,
   routing=`author_did`) and applies per-post `like_count`, which the
   popularity generator ranks on.

A seeded window drifts stale as real time moves on: the api's recency windows
are anchored to *now* (popularity looks back 24h), so from about a day after a
seed, recency-anchored feeds go empty — expected, not a fault. `devctl
status`, `devctl feed` and `devctl doctor` all warn once the seed is old
enough for this; the fix is just `devctl seed` again.

## How ES mirrors prod

`es-init` reads the *same* index-template ConfigMaps prod uses (from
`ingex/index/deploy/k8s/base/templates/`), applies them with dev-sized
settings (1 shard, 0 replicas), creates the bootstrap indexes/aliases, and
mints two ES API keys mirroring prod's split: read-only for the api,
read-write for ingest/seed (written to `.runtime/es_key_*.env`). The api
authenticates to ES exactly as it does in prod.

## The frontend and Firebase

`devctl up` runs the frontend's Vite dev server and the Firebase emulator suite
(Firestore, Auth, Functions). The emulators are started **from the frontend
checkout**, so the rules and function code under test are the ones that repo
deploys — the api repo's Firebase config is being retired
([frontend#42](https://github.com/greenearth-social/frontend/issues/42)) and
isn't used here.

Two adaptations are made at startup, both in `firebase/`:

- **A derived emulator config.** The frontend declares `firestore` as an array
  of named databases for deployment. The Firestore emulator doesn't support
  multiple databases and silently ignores that shape — it logs "Did not find a
  Cloud Firestore rules file" and then *allows all reads and writes*. So
  `firebase/derive-config.mjs` collapses it to the single database the
  emulator supports, still pointing at the frontend's own `firestore.rules`.
  The result is written to `frontend/firebase.devenv.json` (the CLI insists
  referenced paths sit beside the config), regenerated on every start and
  removed by `devctl down`/`nuke`.
- **Port bridging.** The emulators bind loopback inside their container, which
  Docker can't publish. socat re-exposes them, and the *host* ports are
  exactly 8080/9099/5001 because the frontend's Firebase SDK hardcodes
  `127.0.0.1` at those numbers and runs in your browser.

Firestore rules are genuinely enforced here (an unauthenticated read gets a
403), unlike the bare emulator this replaced, which allowed everything.
Indices are **not** applied: they haven't moved out of the api repo yet, so a
query needing a composite index can still pass locally and fail deployed.

### Signing in

Just click **Sign in with Bluesky**. It works with no credentials and logs you
in as the seeded persona. (The feed list starts empty — see "Seeing real data
in the transparency UI" below.)

Real sign-in starts a Bluesky OAuth handshake in the `authBluesky` Cloud
Function, which needs private keys this environment deliberately doesn't
carry — so the button would otherwise fail, which is the first thing a new
engineer is likely to try. The `dev-auth` service sits where the Functions
emulator does, answers that one call with a redirect to the app's own
`#/auth/finish?token=...` route (the same place the real OAuth callback sends
the browser), and passes every other function through untouched.

`devctl login` prints the same URL if you'd rather paste it, or want to sign
in as a different persona: `devctl login did:plc:...`.

Both mint an unsigned custom token, which only an emulator will accept — the
Auth emulator ignores the signature, real Firebase would reject it outright.
Tokens last an hour.

### Working on real Bluesky auth

The shim above is for *using* the app. To work on the OAuth flow itself, take
`dev-auth` out of the path so `/auth/bluesky` reaches the real function again:

```bash
# devenv.local.env
GE_DEV_FUNCTIONS_TARGET=firebase:15001
```

The functions read six variables, all passed straight through from
`devenv.local.env` to the emulator:

| Variable | Purpose |
| --- | --- |
| `APP_ORIGIN` | Derives `client_id` (`$APP_ORIGIN/.well-known/oauth-client-metadata`) and `redirect_uri` (`$APP_ORIGIN/oauth/callback`) |
| `BLUESKY_OAUTH_CLIENT_KID` | Key id in the client assertion |
| `BLUESKY_OAUTH_CLIENT_PRIVATE_KEY` | Signs the client assertion (prod key) |
| `BLUESKY_OAUTH_CLIENT_PRIVATE_KEY_STAGE` | Same, for the `*Stage` function variants |
| `BLUESKY_OAUTH_PUBLIC_JWKS` | Served by `oauthJwks` for Bluesky to verify the assertion |
| `OAUTH_STATE_ENCRYPTION_KEY` | Encrypts the OAuth state parameter |

**`APP_ORIGIN` has to be publicly reachable.** Bluesky's authorization server
fetches your client metadata from it and redirects the browser back to it, so
the default `http://localhost:3000` cannot work — the handshake fails at the
authorization server, not in our code. Point it at a tunnel, and tell Vite to
accept that hostname:

```bash
# devenv.local.env
GE_DEV_APP_ORIGIN=https://your-subdomain.ngrok.dev
GE_DEV_FRONTEND_ALLOWED_HOSTS=localhost,your-subdomain.ngrok.dev
GE_DEV_FUNCTIONS_TARGET=firebase:15001
```

Then `devctl up` and browse the tunnel URL rather than localhost. Any origin
change is also a fresh Firebase auth origin, so you'll be signed out — expected,
not a bug.

Sanity checks:

```bash
# Confirms APP_ORIGIN: the client_id and redirect_uris it returns are exactly
# what Bluesky will be asked to fetch and redirect to. If these say
# "localhost", the handshake cannot work.
curl -s localhost:5001/greenearth-471522/us-central1/oauthClientMetadata

# Confirms BLUESKY_OAUTH_PUBLIC_JWKS — unset, it answers {"error":"Failed to
# load JWKS"}.
curl -s localhost:5001/greenearth-471522/us-central1/oauthJwks

# Per-request function logs, including thrown errors.
devctl logs -t firebase
```

`authBluesky` names the variable it's missing (`APP_ORIGIN not configured`,
`BLUESKY_OAUTH_CLIENT_KID not configured`); the others report generically, so
for those an error means "check the emulator logs" rather than pointing at a
specific variable.

### Seeing real data in the transparency UI

The frontend is a feed-*transparency* UI: it reports on feeds the api has
already served to you. To put something in it, generate a feed:

```bash
devctl feed                # then reload the frontend
```

Two things make that work, both of which are otherwise dead ends locally:

- **`devctl feed` requests are development sessions.** `getFeedSkeleton`
  normally authenticates an AT Protocol JWT signed by your Bluesky identity,
  which nothing local can mint. `GE_DEV_SESSION_SECRET` lets the api accept an
  explicit stand-in (`X-Dev-Session` + `X-Dev-Session-DID`) and treat it as a
  genuine signed-in user, so the full session path runs and a feed snapshot is
  recorded. It is deliberately separate from the Cloud Scheduler probe bypass,
  which is monitoring traffic and is excluded from user data — a probe request
  writes `feed_cache` but never `feed_snapshots`, which is what this UI reads.
  The api refuses to start with `GE_DEV_SESSION_SECRET` set in a deployed
  environment.
- **The UI reports on every feed you've loaded**, so generate whichever one
  you're working on. Every feed runs here, `your-feed` included — the
  Perspective API and the ML rankers are both served by local stubs (see
  below).

Snapshots are only kept for 15 minutes, so an untouched tab goes empty again;
run `devctl feed` and reload.

## Service endpoints (defaults)

| Service | Address | Notes |
| --- | --- | --- |
| api | `http://127.0.0.1:8300` | first start runs `pipenv install` into a cached volume — watch `devctl logs -t api` |
| Elasticsearch | `http://127.0.0.1:9201` | user `elastic` / `ge-dev-elastic`, or the minted keys |
| frontend | `http://127.0.0.1:3000` | Vite dev server; first start runs `npm install` |
| Firestore emulator | `127.0.0.1:8080` | project `greenearth-471522` |
| Firebase Auth emulator | `127.0.0.1:9099` | |
| Functions emulator | `127.0.0.1:5001` | |
| Firebase Emulator UI | `http://127.0.0.1:4000` | browse Firestore data, auth users, function logs |
| inference | internal only | real inference-service over the trained towers |
| perspective-stub | internal only | stands in for Google's Perspective API |
| megastream-ingest | internal only | `--with-ingest`; live posts from the archives |
| jetstream-ingest | internal only | `--with-ingest`; live likes from the public firehose |

These are the default instance's ports; a named instance's are these plus its
offset (`devctl ports --name <n>`). Everything binds `127.0.0.1` only —
nothing is reachable from the network. To use the environment from another
machine, forward the ports over SSH (`devctl ports --ssh` prints the flags):
see [docs/remote.md](docs/remote.md).

Override the bases in `devenv.local.env` (gitignored): `GE_DEV_PORT_API`,
`GE_DEV_PORT_ES`, `GE_DEV_PORT_FIRESTORE`, `GE_DEV_PORT_FRONTEND`,
`GE_DEV_PORT_FIREBASE_AUTH`, `GE_DEV_PORT_FUNCTIONS`, `GE_DEV_PORT_FIREBASE_UI`.
Other settings in the same file: `GE_DEV_ES_HEAP`, `GE_DEV_INSTANCE` (default
instance name, same as passing `--name`), `GE_DEV_API_RELOAD=1` /
`GE_DEV_INFERENCE_RELOAD=1` (what `--watch` sets),
`GE_DEV_MODELS_TAG` (use a model release other than the pinned one),
`GE_DEV_BIND` (published-port bind address — loopback unless you've read
[docs/remote.md](docs/remote.md)),
`GE_DEV_LIVE` / `GE_DEV_LIVE_ENV` (see [Live services](#live-services)).

## Current limitations (by milestone)

- **The model release is a hard dependency.** `devctl setup` needs to reach
  GitHub Releases to download it; there's no offline fallback any more now that
  the stub is gone. Once fetched, everything runs offline.
- **Perspective is stubbed.** The `perspective` ranker (used by `your-feed`)
  calls Google's Perspective API and hard-fails without a key, so
  `perspective-stub` answers it locally with deterministic hash-derived
  scores — stable per post, but not content analysis. Use
  `devctl up --live perspective` to score against the real API.
- Feeds can reference posts outside the fixture — `your-feed` pins a specific
  post, and a cached feed can outlive a re-seed — so `devctl feed` may show
  "(not in Elasticsearch)" for an item. That's the viewer reporting a real
  dangling reference, not a failure.
- **Follow-driven generators work, but yield depends on the sample.**
  `followed_users` and `network_likes` resolve the requesting user's follows
  from the live AT Protocol network, so they need a persona whose DID is real
  — every fixture generated since identities went real qualifies. Check a
  persona with
  `curl -o /dev/null -w '%{http_code}' https://plc.directory/<persona-did>`;
  404 means it came from an old pseudonymized fixture, so refetch or
  regenerate.

  Even with real DIDs, these only return what the fixture happens to contain.
  `followed_users` finds posts by accounts the persona follows, and usually
  produces some. `network_likes` needs those accounts to have *liked*
  something inside the sampled window, which is much rarer — an empty
  `network-likes` feed is normally the sample, not a fault. The api says which
  in its logs ("No liked posts found for followed users of ..."). `random`,
  `popularity`, and `post_similarity` have no such dependency.
- **Instances on the same repo root share the frontend checkout.**
  `functions/lib` is compiled into it by whichever instance starts, so two
  such instances on two different frontend branches will overwrite each
  other's build. Give each its own working copy (`up --repo-root <dir>`) and
  the collision disappears. Everything that can be per instance is.
- **A named instance shares `dev`'s Elasticsearch** (see [Running more than one
  environment](#running-more-than-one-environment)), so it's read-only against
  that data and depends on `dev` being up. `--dedicated-es` opts out at the
  cost of a second cluster. On a native-Linux host, sharing needs one extra
  setting (`GE_DEV_BIND`) — see [docs/remote.md](docs/remote.md).
- **`--name` is not remembered between commands.** Each invocation needs it
  (`devctl --name x exec ...`); without it you act on the default instance. Set
  `GE_DEV_INSTANCE` in your shell if you're staying in one for a while.
