---
alert_id: megastream-ingest-stalled
---

# Megastream Ingest Stalled — Filename Format Mismatch

## Likely cause
Upstream Megastream occasionally changes the `.db.zip` filename format. When
that happens, our filename regex fails to match, every new file is skipped,
and the post pool drains until feeds return empty.

## Steps
1. Check `megastream_ingest` logs for `"Skipping file with invalid filename format"` — if new filenames are being logged at each poll, this is the same failure mode.
2. List the source bucket (S3 / Digital Ocean Spaces) and inspect the newest object keys. Compare against the regex:
   - Regex: `^mega_jetstream_(\d{8})_(\d{6})(?:_[a-z0-9]+)?\.db\.zip$`
   - Defined in `ingex/ingest/internal/common/filename_parser.go`
3. Update the regex in `filename_parser.go` (and any hardcoded format strings in `TimestampToMegastreamFilename`) to support the new format. Add a test case to `filename_parser_test.go` covering a real new filename.
4. Run tests: `go test ./ingest/internal/common/...`
5. Rebuild and redeploy `megastream_ingest` to GKE.
6. Verify recovery:
   - Logs show files being processed (no more "Skipping file with invalid filename format" warnings/errors).
   - `count` on the current `posts-*` index in Elasticsearch increases within one poll cycle.
   - `/health` on the API and a sample feed request return a non-empty skeleton.

## Note
While API 5xx alerts won't catch this failure and `feed-low-fulfillment` will eventually fire once the post pool drains, relying on fulfillment means waiting hours for the pool to deplete. Tracking ingest lag directly ensures we catch the stall within a single poll cycle (~minutes) before users ever see stale or empty feeds.
