---
alert_id: low-candidate-generator-fill-share
severity: warning
---

## Low Candidate Generator Fill Share

**Trigger:** Any generator's 1-hour average `candidates.generate.fill_share` stays below 0.6 for 30 minutes, evaluated every 2 minutes. Filters: `namespace=prod`, `traffic=real`, `endpoint=get_feed_skeleton`; pooled across feeds and API instances.

**Meaning:** Each completed primary generator call contributes returned/requested candidates, capped at 1, before deduplication and ranking. Empty results count as zero; errors, timeouts, and infill calls have no fill-share observation.

**Investigate:** Use the incident's `generator_name` and the same filters.

1. Sum 1-hour increases in `candidates.generate.fill_share_count`. No minimum count is enforced; check for sparse traffic.
2. Check `candidates.generate.failure_count` and API logs. Investigate dependency errors, Elasticsearch health, and ingestion freshness.
3. Compare recent retrieval/allocation changes and affected users' source availability, freshness limits, and exclusions. Sparse follow graphs or deep pagination can exhaust candidates.
4. Check `feed.snapshot.posts_fulfilled_ratio` for impact. Ranking is downstream; changing `min_rank_score` cannot fix generator fill share.

**Recover:** Fix the dependency or roll back a confirmed regression. Verify fill share at least 0.6 with sufficient observations and normal error rates. Allow for the 1-hour lookback; missing data is not recovery.
