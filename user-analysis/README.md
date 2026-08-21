# user-analysis

Bot/inauthentic-user analysis for the 08-18 growth spike (api#426). Scores
the 129,235 DIDs exported from Posthog (`data/*-users.csv`, gitignored)
against public Bluesky API profile signals, a skywatch.blue labeler
cross-check, and tiered prod Firestore activity data.

See `../docs/superpowers/specs/2026-08-20-user-analysis-design.md` for the
full design.

This directory's name has a hyphen, so it is **not** an importable Python
package: every module uses bare top-level imports (`import dids`, not
`from . import dids`), matching `../devenv/`'s existing convention for
standalone script directories. Run scripts directly (`python
user-analysis/run_pipeline.py`), never via `python -m`.

## Usage

```bash
# From the internal-tools repo root:
pipenv run pytest user-analysis

# Validate end-to-end on a small slice first (skips Firestore by default
# unless you have gcloud auth login'd against prod):
pipenv run python user-analysis/run_pipeline.py --sample 500 --skip-firestore

# Full run, public API + labeler only:
pipenv run python user-analysis/run_pipeline.py

# Full run including tiered Firestore data (needs `gcloud auth login` and
# read access to greenearth-471522/greenearth-prod):
pipenv run python user-analysis/run_pipeline.py --tier-b-top-n 500 --tier-b-control-n 500
```

Outputs land in `data/`: `user_analysis.csv` (per-DID signals/flags/score),
`summary.md` (aggregate findings), `profiles_checkpoint.jsonl` (resumable
Stage 1 progress — safe to delete to force a full re-fetch).
