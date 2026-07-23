# Green Earth dev environment

A cohesive, agent-friendly local development environment for the Green Earth
serving stack ([api#268](https://github.com/greenearth-social/api/issues/268)).
One Docker Compose project runs Elasticsearch, the Firestore emulator, the api,
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
./devctl up              # ES + Firestore emulator + inference stub + api
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
Releases and unpacks it into `fixtures/data/`. It needs the `gh` CLI and
access to this repo, since it's private.

Fixtures ride as release assets rather than being committed: they're large
compressed binaries that get regenerated periodically, and git can't delta
them, so each generation would add ~170 MB to every clone forever. Releases
can be deleted when they go stale; git history can't.

A seeded environment keeps working indefinitely no matter how old the release
is — seeding rebases every timestamp so the window always ends an hour before
now (see below). Replace the published fixture when its *content* gets stale
enough to matter.

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

## Service endpoints (defaults)

| Service | Address | Notes |
| --- | --- | --- |
| api | `http://127.0.0.1:8300` | first start runs `pipenv install` into a cached volume — watch `devctl logs api` |
| Elasticsearch | `http://127.0.0.1:9201` | user `elastic` / `ge-dev-elastic`, or the minted keys |
| Firestore emulator | `127.0.0.1:8086` | project `demo-no-project` |

Override ports/heap/etc. in `devenv.local.env` (gitignored): `GE_DEV_PORT_API`,
`GE_DEV_PORT_ES`, `GE_DEV_PORT_FIRESTORE`, `GE_DEV_ES_HEAP`, `GE_DEV_NAME`
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
- `followed_users` candidates resolve follows via the live AT Protocol network;
  synthetic personas (`megastream-files` fixtures) have no real follows.
- Multi-instance (`--name`), `devctl exec`, and local/live backend switching
  are milestone 3 ([api#283](https://github.com/greenearth-social/api/issues/283)).
- A proper terminal feed viewer is [api#285](https://github.com/greenearth-social/api/issues/285);
  `devctl feed` is the minimal stand-in.
