---
alert_id: feed-low-post-count
severity: warning
---

## Feed Returned Fewer Than 5 Posts

**Likely cause:** Candidate generator returned too few results, ranking cutoffs filtered most posts, or the user has a very limited follow graph / history.

**Steps:**
1. In Log Explorer, filter for `textPayload:"Feed returned fewer than 5 posts"` — note the `request_id`, `user_did`, `feed_name`, and `post_count` in the log line
2. Look up the Firestore snapshot at `users/{user_did}/feed_snapshots/{request_id}` — this has the full `generator_diagnostics`, `ranker_model`, and `applied_social_radius`
3. In `generator_diagnostics`, find which generator has `returned_count = 0` — that is the bottleneck
4. If the failing generator is ES-related, check Elasticsearch cluster health and the `posts-*` index
5. If it is inference-related, check `https://inference.greenearth.social/health`
6. If it is isolated to one user (unusual follow graph, cold start), no action needed — the feed will recover as ingestion continues
7. If it affects many users, check the `feed.render.degraded_count` metric on the monitoring dashboard for a systemic signal

**Note:** The `best-of-friends` feed fires at WARNING (demoted severity) because a small follow graph can legitimately produce fewer than 5 posts. Still follow steps 1–3 to check Firestore — if `generator_diagnostics` shows a generator failure rather than a thin follow graph, treat it the same as any other feed.
