# Bot/inauthentic-user analysis for the 08-18 growth spike (api#426)

## Context

Posthog showed ~130k new users beginning 2026-08-18 14:00 UTC. api#426's most
recent comment (mjmor) concluded the spike is almost certainly inorganic
(impression/like counts didn't scale with it) and asked for next-step 1: user
analysis of the affected accounts — are they real people, when were the
accounts created, do they show interaction sequences like real clients,
and how do they compare against a third-party labeling service.

`internal-tools/user-analysis/data/` holds three CSVs exported from Posthog,
one `distinct_id` (a `did:plc:...`) per row: 129,251 rows, 129,235 unique
DIDs after dropping 16 duplicates in batch 3.

ingex#466 built and validated a set of reference scripts for exactly this
kind of investigation (labeler enumeration via wholesale `queryLabels`,
Jetstream like sampling, modlist retrieval via `getRepo`) as scaffolding for
ingex#474 (filtering inauthentic accounts at ingest). This analysis reuses
those techniques rather than re-deriving them, applied to a fixed DID list
instead of live traffic.

## Goals

- Produce a bot-rate estimate and per-DID evidence for the 129,235 DIDs in
  the growth spike.
- Answer, per next-step 1: are these real accounts, when were they created,
  do they show real-client-like interaction sequences, and what fraction
  are already flagged by a third-party labeler.
- Leave behind rerunnable scripts (this data will need re-measuring if the
  question comes up again), not a one-off notebook.

## Non-goals

- Filtering/blocking inauthentic accounts in ingest — that's ingex#474.
- A production Firestore readonly access path — reuses the same
  application-default-credentials pattern `scripts/apikeys.py` /
  `scripts/feed_debug.py` already use.

## Architecture

New `internal-tools/user-analysis/` package, following the `velocity/` split
of a tested library plus thin script entrypoints:

```
user-analysis/
  data/                       # gitignored: input CSVs, checkpoints, outputs
  __init__.py
  dids.py            (+test)  # load/dedupe DIDs from the 3 CSVs
  scoring.py         (+test)  # pure functions: signals -> flags -> composite score
  profiles.py                 # Stage 1: batched getProfiles against public appview
  labeler.py                  # Stage 2: wholesale labeler enumeration + intersect
  firestore_tiers.py          # Stage 4: tier A (bulk) + tier B (sampled subcollections)
  run_pipeline.py             # orchestrates stages 1-4, writes final CSV + summary
  README.md
```

### Stage 1 — public API account signals (`profiles.py`)

Batches DIDs 25 at a time into `app.bsky.actor.getProfiles`
(`public.api.bsky.app`, unauthenticated) — ~5,170 calls for 129,235 DIDs.
Captures `createdAt`, `followersCount`/`followsCount`/`postsCount`,
`displayName`/`description`/`avatar` presence, self-declared `labels[]`.

Retry/backoff mirrors `enumerate_labeler.py`'s `fetch_json` in ingex. Progress
checkpoints to `data/profiles_checkpoint.jsonl` every N batches (append-only,
one JSON line per profile) so an interrupted run resumes instead of
restarting; `run_pipeline.py` skips DIDs already present in the checkpoint.

### Stage 2 — labeler cross-check (`labeler.py`)

Ports `enumerate_labeler.py`'s wholesale-enumeration approach: pulls
skywatch.blue's full account-label set once via `queryLabels`, then
intersects locally against the 129,235-DID set. This is O(labeler size), not
O(users) — the ingex docstring notes per-DID lookups get rate-limited
(403s) past ~45k lookups against a similar labeler.

### Stage 3 — heuristic scoring (`scoring.py`, pure/tested)

Combines Stage 1 + Stage 2 output into per-DID flags and a composite score:

- `created_during_spike`: `createdAt` within the spike window
- `no_profile_content`: no avatar, no bio, zero posts
- `handle_looks_random`: heuristic on handle character distribution
- `labeler_flagged`: present in the skywatch.blue set, which label(s)
- `self_declared_bot`: `bot` (or equivalent) in self-labels

This is the part getting real unit test coverage — pure functions over
fixture profile dicts, no network.

### Stage 4 — Firestore behavioral data (`firestore_tiers.py`), tiered by cost

- **Tier A (all 129,235 DIDs):** one batched `get_all()` pass over
  `users/{doc_id}` (DID with `did:plc:` prefix stripped per
  `user_doc_id()`) for `created_at` (first seen by our API),
  `last_seen_at`, `social_radius`. ~129k document reads (~$0.08 at Firestore
  pricing) — cheap enough to run against the full set.
- **Tier B (sampled):** for the top N (default 500) highest-scoring DIDs
  from Stage 3 plus an equal-size random control sample, pulls
  `feed_activity`, `interactions`, and `seen_posts` subcollections to
  examine actual request sequences and timing — this is what answers
  "do they load the feed repeatedly like a real client." Scoped to a sample
  because `interactions` isn't indexed for a `user_did`-filtered
  collection-group query today, and per-user subcollection reads at 129k
  scale would be far more expensive than Tier A for a question that a
  sample already answers.

Uses `GE_FIRESTORE_PROJECT=greenearth-471522`,
`GE_FIRESTORE_DATABASE=greenearth-prod`, application-default credentials —
same pattern as `scripts/apikeys.py`. Requires an interactive
`gcloud auth login` if the current ADC token has expired.

### Orchestration (`run_pipeline.py`)

CLI with `--sample N` (run stages 1-4 against the first N DIDs only, for
end-to-end validation before committing to a 129k-DID run) and
`--resume` (skip DIDs already checkpointed). Writes:

- `data/user_analysis.csv` — one row per DID: all raw signals + flags +
  composite score
- `data/summary.md` — aggregate stats (bot-rate estimate, breakdown by
  flag, account-age histogram, labeler overlap, Tier B sequence findings)

## Testing

- `dids.py`, `scoring.py`: real unit tests, fixtures for profile/label
  dicts, no network — run via `pipenv run pytest user-analysis`.
- `profiles.py`, `labeler.py`, `firestore_tiers.py`: thin I/O scripts against
  live external services, tested at the parsing/backoff-logic level only
  (consistent with `enumerate_labeler.py` itself having no test suite) —
  validated end-to-end via `run_pipeline.py --sample` against a small DID
  count before the full run.
- `ruff check` / `ruff format --check` / `pyright` per `internal-tools`
  CI (`pyproject.toml` `testpaths`/`include` need `user-analysis` added).

## Rollout

1. Implement stages with TDD, `--sample`-test each stage as it lands.
2. Run `run_pipeline.py --sample 500` end-to-end (all 4 stages, including a
   real Tier B pull) and review results before committing to the full run.
3. Run the full 129,235-DID pipeline.
4. Share `data/summary.md` + top findings in this conversation for review.
5. Only after that review, post a summary comment to api#426 (not automatic).
6. Commit scripts (not `data/`) and open a draft PR against
   `internal-tools` main.
