# Runbooks: regenerating fixtures and models

Maintainer procedures. Day-to-day use never needs these — `devctl bootstrap`
downloads published artifacts. These are for when the *published* artifacts
need replacing.

## When to regenerate what

| Situation | Action |
| --- | --- |
| Fixture content is stale enough to matter (months old, missing new fields) | Publish a new fixture |
| Index mappings changed in `ingex` | Publish a new fixture (generated against the new mappings) |
| New models trained and blessed for serving | Publish a new model bundle, bump `MODELS_TAG` |
| Content encoder or embedding dims changed | Publish **both** — model/fixture compatibility is checked and enforced |
| Someone joined the team, or their account is missing from `fixtures/dev_users.json` | Add them, then publish a new fixture |

A seeded environment never *requires* a fresh fixture — seeding rebases
timestamps so any fixture stays usable indefinitely. Replace the published
one when its content, not its age, is the problem.

## Publish a new fixture

Needs: org membership (to cut a release on `internal-tools`), and read-only
prod Elasticsearch access via kubectl (internal engineers only).

1. **Port-forward prod ES** in its own terminal:

   ```bash
   kubectl port-forward svc/greenearth-es-internal-lb 9200:9200 -n greenearth-prod
   ```

2. **Generate the sample** (~25 min for the default 48h window over the
   tunnel; ~130k posts / ~210k likes, ~170MB on disk):

   ```bash
   cd internal-tools/devenv
   GE_ELASTICSEARCH_URL=https://localhost:9200 \
   GE_ELASTICSEARCH_API_KEY=<read-only key> \
   python3 fixtures/generate_sample.py --source prod-es
   ```

   What makes a good sample is encoded in the generator — contiguous window,
   per-hour-bucket post sampling (half by like_count, half arbitrary),
   cohort-densified likes, hydrated danglers, dev personas. See its
   docstring before changing parameters. Output lands in `fixtures/data/`.

   The default production window is 48 hours ending two hours ago, rounded
   down to the hour. The generator preserves Elasticsearch `topic_scores`
   as `inferences.text["message.commit.record.text"].topic`, which ingex
   converts back to scores when seeding. Older hydrated history posts can
   legitimately lack scores.

   Verify a fresh production capture before seeding or publishing:

   ```bash
   python3 fixtures/verify_fixture.py fixtures/data --require-topic-scores
   ```

   Verification reports the percentage of posts with scores and rejects
   malformed scores or zero coverage. Omit `--require-topic-scores` when
   checking legacy or offline captures that may have no scores; invalid
   values still fail. `--offline` skips only persona DID lookups.
   `fixtures/regen_fixture.sh` automates production capture into
   `fixtures/data.staging` and requires score coverage during verification.

   Watch the dev-user lines near the end: the run reports how many of the
   accounts in `fixtures/dev_users.json` contributed likes, and warns about
   any that contributed none. Those accounts will get an empty, unpersonalized
   feed when their owner signs into a devenv frontend as themselves, which is
   the thing this list exists to prevent. Widening
   `--dev-user-lookback-days` helps only if they've been active at all;
   otherwise tell them to go like some posts before the next regeneration.

   Without prod access, `--source megastream-files` builds a fixture from
   megastream `.db.zip` archives and synthesizes the like graph —
   deterministic under `--seed`, no credentials. Dev accounts get a
   synthesized history there too, so signing in as yourself still works.

3. **Try it locally** before publishing:

   ```bash
   ./devctl seed && ./devctl feed
   ```

   You want a feed with sensible pipeline detail, not just a doc count —
   thin retrieval here means a thin fixture for everyone.

4. **Publish**:

   ```bash
   fixtures/publish_fixture.sh
   ```

   This cuts a `fixture-<date>` release on `internal-tools` with the archive
   as an asset. `devctl fetch-fixture` always takes the newest `fixture-*`
   release, so publishing *is* shipping — there's no tag to bump. Identities
   in the fixture are real (see the README); don't add pseudonymization back.

5. Stale releases can be deleted from GitHub once nobody needs them; that's
   the point of releases over git history.

## Capture a graph alongside a fixture

Graph capture is optional and requires SSH access with sudo to the BridgEU
Postgres host. It runs separately from ordinary `devctl fetch-fixture` and
`devctl seed`, which need no production access. From `internal-tools/devenv`,
extend an existing `prod-es` fixture:

```bash
python3 fixtures/generate_graph.py --fixture-dir fixtures/data \
  --ssh-host bridgeu-prod-bridgeu-postgres-hetzner-fsn1-01.tailc2fe33.ts.net
```

To capture both parts in one regeneration, pass the graph option through the
existing wrapper. Graph extraction runs after the base sample is written:

```bash
fixtures/regen_fixture.sh -- \
  --graph-ssh-host bridgeu-prod-bridgeu-postgres-hetzner-fsn1-01.tailc2fe33.ts.net
```

The wrapper stages both parts in `fixtures/data.staging`. The same
`--graph-ssh-host` option works directly with `generate_sample.py --source
prod-es`; it is rejected with the synthetic `megastream-files` source.
Neither command publishes a release. Publishing remains the separate,
explicit step above.

The standalone options are `--database` (default `bridgeu`), `--batch-size`
(100), `--page-size` (10,000), and `--statement-timeout-ms` (30,000). When
using `generate_sample.py` or the wrapper, prefix each with `--graph-`, as in
`--graph-page-size`. Batch size, page size and timeout must be positive.
Batching and pagination bound individual queries; they do not truncate the
captured graph. Every selected user's active follows are paged through,
without a per-user follow cap.
Each SSH query also has a wall-clock deadline of its statement timeout plus
30 seconds, so a stalled connection fails instead of waiting indefinitely.

Follow roots are the union of all liker DIDs in the base likes file and every
DID in the base manifest's `personas` and `dev_users`. The selection comes
from the fixture, not a fixed user count. Extraction first looks up actor
metadata for all base users, including post authors, then fetches current
active outgoing follows only for the selected roots. Actor lookup progress
therefore covers more users than follow extraction progress.

Post authors and all captured follow targets remain in the actor file. Their
own follow lists are not expanded unless they are also selected roots, and
incoming follows are not fetched. An actor's `is_root` flag identifies the
selected follow roots, not every author or neighbor. Missing base DIDs remain
in `actors.jsonl.gz` with `in_graph` false. The output is:

- `graph/actors.jsonl.gz` and `graph/follows.jsonl.gz` for the captured graph.
- `graph/manifest.json` with the follow-root selection policy, capture
  metadata and fingerprints tying it to the exact base fixture.
- The existing root `likes.jsonl.gz` remains the shared like dataset; graph
  capture does not take a separate sample of likes.

The follow graph reflects the time of extraction, which may be much later
than the base fixture's post/like window. The manifest records that
distinction; this is not a reconstruction of follows at the historical
window. Each short query reads committed data at its own start; the manifest
records the capture's start and finish rather than claiming one atomic
snapshot. This avoids a long production transaction holding back vacuum.
Regenerating the base fixture removes any old `graph/` directory so
unrelated captures cannot be bundled together.

Every SSH query switches to `bridgeu_readonly` inside a read-only transaction
and verifies both before reading. Extraction does not write production data,
and no credentials are saved in the artifacts. The query interface leaves
room for other sources or direct database authentication later.

Capture generates files only. With the BridgEU checkout present, run
`devctl seed` to validate and load them into local Postgres alongside the
ordinary Elasticsearch seed. Validation checks the original graph against
its base fixture before any seeded data is wiped. No production access is
needed for seeding.

The seed rebases graph actor/follow timestamps from the graph capture finish
time to the same effective window end as the post/like seed. This uses a
separate delta from the historical base window; likes themselves are the
same rebased rows loaded into Elasticsearch. Local actor IDs are assigned
afresh and all follow/like references are resolved by DID. Missing captured
actors become placeholders with status `unknown` and `first_seen_at` equal
to that effective end. Like sequences are `0`, and no ingestion cursor is
invented.

Graph replacement runs in one transaction: repeated seeds replace prior raw
graph data and preserve migrations, and a failed load rolls back. Choosing
a fixture without `graph/` clears the local graph rather than retaining an
old capture. Instances sharing Elasticsearch still cannot run `devctl seed`;
use `--dedicated-es` when seeding a named instance's graph. Neither live graph
ingestion nor graph export starts as part of seeding.

## Publish a new model bundle

Needs: `gcloud` with read access to the prod model bucket.

1. **Mirror and stage** the newest artifacts:

   ```bash
   models/publish_models.sh --dry-run
   ```

   This pulls both TorchScript towers, the ranker, the author-index maps and
   serving manifests, rewrites the manifests to local paths, and stages the
   bundle without uploading. Inspect it.

2. **Check `max_history_len`.** It's the one value not derivable from the
   artifacts (it isn't in the serving manifest — it's how far back the user
   tower and ranker look, mirrored from how prod deploys the same models).
   Getting it wrong degrades embeddings silently. `publish_models.sh`
   carries the command to re-check it against the prod deployment.

3. **Publish** (drop `--dry-run`). The release tag names the two training
   runs in the bundle (`models-<two-tower>-<ranker>`), so re-publishing the
   same artifacts is idempotent.

4. **Pin it**: set `MODELS_TAG` in `devctlib/assets.py` to the tag the script prints,
   and commit. Unlike fixtures, models are pinned — everyone runs the same
   bundle until this line moves, because seeded embeddings and serving
   towers have to agree.

5. **Tell everyone to re-seed.** Post embeddings are stamped with the post
   tower's ID and the ranker filters on it; environments seeded under the
   old models serve thin feeds until they re-seed. `devctl` refuses `up`
   and `seed` outright if the new bundle's content encoder disagrees with
   the fixture's — if it does, publish a new fixture too (its embeddings
   come from the ingest path, so regenerate after the model release exists).

## Testing a candidate bundle before pinning

`GE_DEV_MODELS_TAG=<tag> ./devctl fetch-models` fetches any published tag
into place without touching the pin, and `devctl up` serves it. Re-seed,
compare feeds, then move `MODELS_TAG` when satisfied.
