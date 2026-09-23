---
alert_id: megastream-ingest-stalled
---

# Megastream Ingest Stalled — Filename Format Mismatch

## Likely cause
Upstream Megastream occasionally changes the `.db.zip` filename format. When
that happens our filenay new file is skipped,and the post pool drains until feeds return empty.

## Steps
1. Check `megastream_ine with invalid filenameformat"` — if new filenames are being logged at each poll, this is the same
failure mode.
2. List the source bucket (S3 / Digital Ocean Spaces) and inspect the newest
object keys. Compare ag
   - Regex: `^mega_jetstream_(\d{8})_(\d{6})(?:_[a-z0-9]+)?\.db\.zip$`
   - Defined in `ingex/me_parser.go`
3. Update the regex in `filename_parser.go` (and any hardcoded format strings
 in `TimestampToMegastrew format. Add a case to`filename_parser_test.go` covering a real new filename.
4. `go test ./internal/
5. Rebuild and redeploy `megastream_ingest` to GKE.
6. Verify recovery:
   - Logs show files being processed (no more "invalid filename format"
spam).
   - `count` on the current `posts-*` index in Elasticsearch increases within
 one poll cycle.
   - `/health` on the API and a sample feed request return a non-empty
skeleton.

## Note
This failure is silent from the API's perspective — feeds go empty graduallyas recent posts age outgest lag (time since last successful Megastream file) so we catch this within one poll cycle instead of one pool lifetime.
