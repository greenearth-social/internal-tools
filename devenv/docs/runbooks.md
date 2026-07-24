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

   Without prod access, `--source megastream-files` builds a fixture from
   megastream `.db.zip` archives and synthesizes the like graph —
   deterministic under `--seed`, no credentials.

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

4. **Pin it**: set `MODELS_TAG` in `devctl` to the tag the script prints,
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
