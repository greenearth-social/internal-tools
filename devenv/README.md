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

`devctl feed <rkey>` fetches `getFeedSkeleton` from the local api as the
seeded dev persona (JWT auth is bypassed with the `X-Probe-Secret` header; the
persona DID comes from the fixture manifest via `.runtime/probe.env`).

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

### Pseudonymized liker identities

**Post content and post author DIDs in the fixture are real. Liker DIDs are
not** — every liker is replaced by a synthetic `did:plc`, consistently across
the like documents, the like `at_uri`s, and the manifest personas.

Why: cohort densification deliberately captures each cohort member's *complete*
like history, so a real-DID fixture would be a per-person interest profile —
and this one is a public download that can't honor later deletions the way
prod's tombstones do. Individually public likes become a different artifact
when aggregated, frozen, and published.

The like graph is preserved exactly (who liked what, how densely, in what
order), which is everything the dev environment needs. Personas behave
identically; the api just can't resolve their handles, exactly as with
synthetic fixtures.

**The mapping is irreversible on purpose** — SHA-256 under a random salt
generated per run and never stored. A plain hash would be worthless here,
since `did:plc` identifiers are public and enumerable from the firehose:
anyone could hash the whole DID space and look ours up. Discarding the salt
is what prevents that.

**Consequence, worth knowing before you need it:** you cannot map a fixture
DID back to a real user, ever — including for legitimate work like "whose
likes are these?" or investigating a specific author's like behavior. If you
need real identities, generate a private fixture with `--no-pseudonymize`,
use it locally, and don't publish it. `publish_fixture.sh` refuses to upload
one, and its `manifest.json` records `pseudonymized: false` so the file itself
declares what it is. A durable answer for that use case — a held mapping
table, or querying prod directly — isn't built, and would need designing
along with its own handling of the deletion problem.

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

`VITE_USE_MOCK_SERVICES=true` is the frontend's own default and what devenv
uses: the full UI with hardcoded data, no backend needed. To drive the seeded
api instead, set `GE_DEV_FRONTEND_MOCK=false` in `devenv.local.env`, then:

```bash
./devctl login          # prints a sign-in URL for the seeded persona
```

Real sign-in means completing Bluesky OAuth, which needs private keys this
environment deliberately doesn't carry. The Auth emulator accepts *unsigned*
custom tokens, so `devctl login` mints one for the seeded persona and hands it
to the app's own `#/auth/finish` route — the same entry point the real OAuth
callback uses. The token lasts an hour; re-run for a fresh one. It only works
against an emulator.

### What the frontend can and can't show

The frontend is a feed-*transparency* UI. Its feed list reads `feed_snapshots`
for the **`your-feed`** feed from the last 15 minutes, and that pipeline runs
the `perspective` ranker, which hard-fails without `GE_PERSPECTIVE_API_KEY`
(Google's Perspective API). So in real mode the app authenticates and reaches
the api correctly, but the list is empty until either:

- you set `GE_PERSPECTIVE_API_KEY` in `devenv.local.env`, or
- the real inference service lands
  ([api#269](https://github.com/greenearth-social/api/issues/269)) and the
  feed's other ML rankers work.

Separately, `_record_session` in the api abandons its user/activity upsert when
it can't resolve the caller's handle on the live network — which pseudonymized
personas never can (see Pseudonymization above). So no `users` document is
created automatically, and `scripts/feed_debug.py --enable` has nothing to
attach to until one exists.

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
- **Feeds needing a real network identity return nothing.** `followed_users`
  and `network_likes` resolve the requesting user's follows from the live AT
  Protocol network, and personas are pseudonymized (see above), so their
  follow set comes back empty. This is a deliberate trade — those two
  generators are what pseudonymizing personas costs us. `random`,
  `popularity`, and `post_similarity` are unaffected and exercise the
  retrieve → rank path fully. To work on follow-driven generators, generate a
  local `--no-pseudonymize` fixture (and don't publish it) so personas are
  real users with real follows.
- Multi-instance (`--name`), `devctl exec`, and local/live backend switching
  are milestone 3 ([api#283](https://github.com/greenearth-social/api/issues/283)).
- A proper terminal feed viewer is [api#285](https://github.com/greenearth-social/api/issues/285);
  `devctl feed` is the minimal stand-in.
