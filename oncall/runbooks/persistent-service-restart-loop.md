---
alert_id: persistent-service-restart-loop
severity: critical
---

## Persistent Service Crash Loop / Frequent Restarts

**Trigger:** More than 3 container terminations or restarts observed within 10 minutes on persistent services (`megastream-ingest-prod`, `jetstream-ingest-prod`, `greenearth-api-prod`, or `inference-service-prod`).

**Meaning:** A core persistent service is repeatedly crashing or being terminated, preventing steady indexing or feed serving.

**Likely cause:**
- Out of Memory (OOMKilled / Exit code 137) due to memory leak or undersized allocation
- Fatal panic on startup (invalid configuration, missing environment variable, missing secret)
- Downstream dependency outage on boot (Elasticsearch unavailable, Firestore auth error)

**Investigate:**
1. Identify the failing service and container exit reason in GCP Log Explorer:
   - Filter:
     ```text
     resource.type="cloud_run_revision"
     resource.labels.service_name=~"(megastream-ingest|jetstream-ingest|greenearth-api|inference-service)-prod"
     severity>=ERROR
     ```
   - Look for `Exit code 137` (OOM kill) or `Exit code 1` / `panic: ...` (fatal runtime error).
2. Check recent revisions:
   - Check if a deployment occurred right before restarts began.
3. Check external dependencies:
   - Elasticsearch internal load balancer IP and cluster health.
   - Firestore connectivity.

**Mitigation:**
1. If crashing with `Exit code 137` (OOM), increase the container memory limit:
   ```bash
   gcloud run services update <SERVICE_NAME> --memory=<NEW_LIMIT> --region=us-east1
   ```
2. If crashing due to a bad deploy or startup panic, rollback immediately:
   ```bash
   gcloud run services update-traffic <SERVICE_NAME> --to-revisions=<PREVIOUS_STABLE_REVISION>=100 --region=us-east1
   ```

**Recover:**
Service instance count stabilizes at target (1 instance for ingests) with 0 crash exits for at least 15 minutes.
