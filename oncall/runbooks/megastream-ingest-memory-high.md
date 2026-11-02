---
alert_id: megastream-ingest-memory-high
severity: warning
---

## Megastream Ingest Memory High (>90%)

**Trigger:** `run.googleapis.com/container/memory/utilizations` exceeds 0.90 (90%) for 3 minutes on Cloud Run service `megastream-ingest-prod`.

**Meaning:** The `megastream-ingest` process is approaching its 1Gi container memory ceiling. If memory continues to grow, Cloud Run / Linux cgroups will OOM-kill the container (`Exit code 137`), causing data ingestion to lag or stall.

**Likely cause:**
- Memory leak in batch processing, decompression, or Elasticsearch bulk indexing
- Very large `.db.zip` dump file from upstream Megastream or sudden spike in post volume
- High backlog catch-up (e.g. following downtime or rewind) accumulating messages in memory

**Investigate:**
1. Check Cloud Run metrics in GCP Console (Cloud Run → `megastream-ingest-prod` → Metrics):
   - Review "Memory utilization" and "Container instance count".
2. Inspect logs in Log Explorer:
   - Filter: `resource.type="cloud_run_revision" AND resource.labels.service_name="megastream-ingest-prod"`
   - Check if OOM messages appear: `"Memory limit of 1024 MiB exceeded with ... MiB used"`
   - Check if any panics or unhandled error loops are logged.
3. Check S3 bucket key sizes:
   - Check recent files in `s3://graze-mega-02/mega/` to see if individual `.db.zip` archives are abnormally large.
4. Check recent deployments:
   - Compare `GE_GIT_SHA` on the active revision against `main` commits to see if new changes to `ingex/ingest/cmd/megastream_ingest` were recently deployed.

**Mitigation:**
1. **Temporary headroom increase:** If the container is nearing 1Gi under high backlog load, temporarily bump memory to 2Gi:
   ```bash
   gcloud run services update megastream-ingest-prod --memory=2Gi --region=us-east1
   ```
2. **Restart instance:** If memory steadily climbs due to a leak, trigger a fresh container instance by updating revision traffic:
   ```bash
   gcloud run services update-traffic megastream-ingest-prod --to-latest --region=us-east1
   ```
3. **Roll back revision:** If triggered immediately after a new deployment, revert to the previous revision:
   ```bash
   gcloud run services update-traffic megastream-ingest-prod --to-revisions=<PREV_REVISION>=100 --region=us-east1
   ```

**Recover:**
Memory utilization drops and stays below 75% for at least 15 minutes, with no container terminations or OOM logs.
