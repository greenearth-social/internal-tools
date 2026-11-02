---
alert_id: persistent-service-memory-high
severity: warning
---

## Persistent Service Memory High (>90%)

**Trigger:** Container memory utilization exceeds 90% sustained for 3 minutes on any persistent production service (`megastream-ingest-prod`, `jetstream-ingest-prod`, `greenearth-api-prod`, `inference-service-prod`).

**Meaning:** A persistent service is approaching its memory limit. If unmitigated, Linux cgroups will OOM-kill the process (`Exit code 137`).

**Investigate:**
1. Identify the affected service from the incident labels.
2. In Cloud Monitoring / Cloud Run Metrics, check the memory consumption trend:
   - Linear growth over hours suggests a heap or goroutine leak.
   - Sudden spike suggests an unusually large batch or unbuffered payload.
3. Check logs for warnings or memory exhaustion notices:
   - Filter: `resource.type="cloud_run_revision" AND resource.labels.service_name="<SERVICE_NAME>"`

**Mitigation:**
1. If the service is under heavy backlog or legitimate load surge, scale memory up:
   - `megastream-ingest-prod`: bump to `2Gi`
   - `jetstream-ingest-prod`: bump to `1Gi`
   - `greenearth-api-prod`: bump to `4Gi`
   - `inference-service-prod`: bump to `4Gi`
   ```bash
   gcloud run services update <SERVICE_NAME> --memory=<LIMIT> --region=us-east1
   ```
2. If memory growth started after a recent revision, roll back traffic to the previous revision.

**Recover:**
Memory utilization falls below 75% for at least 10 minutes without service restarts.
