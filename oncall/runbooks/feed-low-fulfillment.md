---
alert_id: feed-low-fulfillment
severity: warning
---

## Low Feed Fulfillment

This means that the ratio of total posts returned vs posts requested has dropped too low.

**Trigger:** Real production `your-feed` traffic: the median `feed.snapshot.posts_fulfilled_ratio`, pooled across instances using 5-minute histogram deltas, stays below 0.5 for 10 minutes.

**Investigate:** Filter API metrics to `namespace=prod`, `traffic=real`, `feed_name=your-feed`, `endpoint=get_feed_skeleton`.

1. Check `candidates.generate.fill_share` by `generator_name`. If retrieval fell, check generator errors, Elasticsearch freshness/health, and ingestion checkpoints/OOM errors.
2. Compare `feed.slate.cutoff_count` (`reason=rank_score`) with the sum of `feed.mmr.input_size` observations over the same interval. Increased cuts with healthy retrieval suggest ranking; compare model/score-scale and `min_rank_score` deployments.
3. Inspect `generator_diagnostics` in `greenearth-prod` Firestore: `users/{user_id}/feed_snapshots/{request_id}`; strip `did:plc:` from the DID for `user_id`.
4. From `api/`: `pipenv run python scripts/feed_debug.py HANDLE --show REQUEST_ID --environment prod` shows captured scores. Requires debugging enabled before generation; records expire after 7 days and omit cache-only loads.

**Recover:** Restore the failing dependency or roll back the confirmed regression. Verify fulfillment above 0.5 with continuing traffic; missing data is not recovery.
