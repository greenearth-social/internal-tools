# Green Earth dev environment

A cohesive, agent-friendly local development environment for the Green Earth
serving stack ([api#268](https://github.com/greenearth-social/api/issues/268)).
One Docker Compose project runs Elasticsearch, the Firebase emulators, the api,
the frontend,
and (for now) an inference stub, seeded with representative Bluesky data — no
GCP, Firebase, or production credentials required.

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
  internal-tools/    this repo
  inference-service/ optional (milestone 2)
  frontend/          optional
```

If your layout differs, set `GE_DEV_REPO_ROOT=<parent>` in
`devenv/devenv.local.env`.

## Quick start

```bash
cd internal-tools/devenv

./devctl setup           # checks prerequisites, builds/pulls images
./devctl fetch-fixture   # downloads the published sample (~170MB)
./devctl up              # ES + Firebase emulators + inference stub + api + frontend
./devctl seed            # rebase timestamps, ingest posts, load likes
./devctl feed popularity
```

`devctl feed <feed>` shows a feed in the terminal: it requests
`getFeedSkeleton` from the local api as the seeded dev persona, hydrates each
post from this environment's Elasticsearch (no AppView call, so it works
entirely against fixture data), and prints author, timestamp, like count, text,
and per-post pipeline detail (which generator retrieved it, its rank and ranker
score). Defaults to the `popularity` feed and the seeded persona.

```bash
./devctl feed popularity                 # the seeded persona's popularity feed
./devctl feed your-feed --user did:plc:… # a different feed as a chosen persona
./devctl feed random --limit 50 --pages 2  # page through more of the feed
./devctl feed popularity --json          # the raw getFeedSkeleton response
```

The persona defaults to the fixture manifest's DID (via `.runtime/probe.env`)
and the request is a development session (see below), so the api runs the full
signed-in path and records a snapshot. The viewer is a read-only client — it
never posts, likes, or writes anything. `--no-pipeline` skips the snapshot
lookup and shows the feed as a plain client would.

Other commands: `devctl status`, `devctl logs [service]`, `devctl down`
(stop, keep data), `devctl nuke` (delete everything). Data is disposable by
design — when in doubt, `nuke` and re-`seed`.

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

An earlier version pseudonymized liker DIDs. That was removed deliberately,
because the protection wasn't real: Graze's turbostream/megastream archives
already publish the complete Bluesky event history — every like, by every DID,
going back over a year — alongside hydrated posts. A fixture built from a few
hours of that window exposes nothing the archives don't already hand out in
full.

Meanwhile the costs were concrete. Synthetic DIDs resolve to nobody, so
`followed_users` and `network_likes` returned empty, ranking couldn't be
exercised against the identities that produced the likes, and no fixture DID
could be looked up for debugging.

The trade being accepted: a published fixture is a real slice of public
activity and can't honor later deletions the way prod's tombstones do — the
same trade the upstream archives already make.

### Generating your own

`fixtures/generate_sample.py` is the committed, parameterized generator — what
makes a good sample is encoded in it (contiguous window, cohort-densified
likes, hydrated danglers, dev personas; see its docstring). Output goes to
`fixtures/data/` (gitignored): megastream-format `.db.zip` post chunks,
`likes.jsonl.gz`, `like_counts.jsonl.gz`, and `manifest.json`. Publish a fresh
one for the team with `fixtures/publish_fixture.sh`.

- `--source prod-es` samples a real cluster. A read-only key is enough. For
  prod, port-forward first
  (`kubectl port-forward svc/greenearth-es-internal-lb 9200:9200 -n greenearth-prod`)
  and set `GE_ELASTICSEARCH_URL=https://localhost:9200` plus
  `GE_ELASTICSEARCH_API_KEY`. A default 48h run takes ~25 min over the
  tunnel and yields roughly 130k posts / 210k likes (~170 MB on disk),
  which seeds in about 10 minutes. Posts are sampled per hour bucket (half
  by like_count, half arbitrary) so coverage spans the whole window rather
  than the first sliver of it.
- `--source megastream-files` builds from existing megastream `.db.zip` files
  (default: `ingex/ingest/test_data/megastream`) and synthesizes the like
  graph — no credentials, deterministic under `--seed`. The checked-in test
  files only contain a handful of posts; point `--input` at a real megastream
  archive for a meatier fixture.

## Seeding and time rebasing

`devctl seed` runs three one-shot containers:

1. **seed-rebase** — copies fixtures into `.runtime/seed/` with every
   timestamp shifted forward by one uniform delta so the capture window ends
   an hour before now. Code anchored to `now` (recency windows, popularity
   decay) sees a full window of data; relative structure (inter-post spacing,
   like-after-post ordering) is preserved exactly.
2. **seed-megastream** — runs the real `megastream_ingest` binary from your
   `ingex` checkout (`go run`, byte-identical code path to prod) against the
   rebased fixtures. Post-tower embeddings come from the inference stub.
3. **seed-likes** — bulk-loads likes (prod document identity: `_id=at_uri`,
   routing=`author_did`) and applies per-post `like_count`, which the
   popularity generator ranks on.

A seeded window drifts stale after a day or two and recency-anchored feeds go
empty — that's expected; just re-run `devctl seed`.

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
devctl logs firebase
```

`authBluesky` names the variable it's missing (`APP_ORIGIN not configured`,
`BLUESKY_OAUTH_CLIENT_KID not configured`); the others report generically, so
for those an error means "check the emulator logs" rather than pointing at a
specific variable.

### Seeing real data in the transparency UI

The frontend is a feed-*transparency* UI: it reports on feeds the api has
already served to you. To put something in it, generate a feed:

```bash
devctl feed popularity     # then reload the frontend
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
  you're working on. `your-feed` won't run here — it needs the Perspective API
  and the trained ranking models — but `popularity`, `random` and
  `post-similarity` all work.

Snapshots are only kept for 15 minutes, so an untouched tab goes empty again;
run `devctl feed` and reload.

## Service endpoints (defaults)

| Service | Address | Notes |
| --- | --- | --- |
| api | `http://127.0.0.1:8300` | first start runs `pipenv install` into a cached volume — watch `devctl logs api` |
| Elasticsearch | `http://127.0.0.1:9201` | user `elastic` / `ge-dev-elastic`, or the minted keys |
| frontend | `http://127.0.0.1:3000` | Vite dev server; first start runs `npm install` |
| Firestore emulator | `127.0.0.1:8080` | project `greenearth-471522` |
| Firebase Auth emulator | `127.0.0.1:9099` | |
| Functions emulator | `127.0.0.1:5001` | |
| Firebase Emulator UI | `http://127.0.0.1:4000` | browse Firestore data, auth users, function logs |

Override ports/heap/etc. in `devenv.local.env` (gitignored): `GE_DEV_PORT_API`,
`GE_DEV_PORT_ES`, `GE_DEV_PORT_FIRESTORE`, `GE_DEV_PORT_FRONTEND`,
`GE_DEV_PORT_FIREBASE_AUTH`, `GE_DEV_PORT_FUNCTIONS`, `GE_DEV_PORT_FIREBASE_UI`,
`GE_DEV_ES_HEAP`, `GE_DEV_NAME`
(compose project name), `GE_DEV_API_RELOAD=1` (uvicorn --reload watch mode).

## Current limitations (by milestone)

- **Inference is a stub** ([api#269](https://github.com/greenearth-social/api/issues/269)):
  `/models/post-tower/predict` returns deterministic pseudo-embeddings so
  ingest works; ML rankers (`heavy_ranker`, `two_tower` feeds) won't produce
  meaningful order until the real inference-service + published models land.
  Use `random` / popularity-driven feeds meanwhile.
- Feeds ranked with the `perspective` model call the external Perspective API;
  without `GE_PERSPECTIVE_API_KEY` those rankers fail — stick to feeds that
  don't use it (e.g. `random`).
- **Follow-driven generators need a fixture generated after real IDs landed.**
  `followed_users` and `network_likes` resolve the requesting user's follows
  from the live AT Protocol network. Fixtures generated before identities went
  real carry synthetic persona DIDs that resolve to nobody, so those
  generators come back empty — check with
  `curl -o /dev/null -w '%{http_code}' https://plc.directory/<persona-did>`
  (404 means synthetic). Regenerate from prod ES to fix it. Even then, yield
  depends on whether the accounts a persona follows have posts inside the
  sampled window; `random`, `popularity`, and `post_similarity` don't have
  that dependency and exercise the retrieve → rank path fully.
- Multi-instance (`--name`), `devctl exec`, and local/live backend switching
  are milestone 3 ([api#283](https://github.com/greenearth-social/api/issues/283)).
