---
alert_id: api-5xx-error-rate-high
severity: critical
---

## API 5xx Error Rate High

This alert triggers when the Cloud Run 5xx response rate or the feed render failure rate exceeds 1% sustained over a 5-minute window in production.

**Trigger:** `namespace=prod`, `traffic=real`: HTTP 5xx responses or failed/degraded renders exceed 1% of total requests over a 5-minute window.

**Likely cause:**
- Cloud Run instance saturation or asyncio event-loop lag during traffic surge
- Downstream service failure (Elasticsearch cluster unavailable, inference-service down/timing out, Perspective API rate limit)
- Connection pool exhaustion (Elasticsearch client or shared httpx client pinned at pool cap)
- Broken deployment or unhandled exception in a newly deployed Cloud Run revision

**Steps to Investigate:**
1. Check Cloud Run logs in GCP Log Explorer:
   - Filter: `resource.type="cloud_run_revision" AND resource.labels.service_name="greenearth-api-prod" AND severity>=ERROR`
   - Look for recurring exception stack traces or panics.
2. Inspect the API Bottleneck Dashboard (Monitoring → Dashboards → `greenearth-api-prod`):
   - **Row 1 (User Experience)**: Check "Failure, degradation and 5xx rate (% of renders)" to determine the exact failure class.
   - **Row 2 & 3 (Dependencies)**: Check "Dependency failures per minute by cause" and "Concurrent outbound requests in flight vs pool cap" (detects if ES client or HTTP client connection pool is saturated).
   - **Row 3 (Instance Health)**: Check "Event-loop scheduling lag per instance" (>100 ms indicates loop starvation) and Cloud Run CPU/memory utilization.
3. Check downstream health endpoints:
   - Inference service: `curl https://inference.greenearth.social/health`
   - API health check: `curl https://api.greenearth.social/health`
   - Elasticsearch cluster status in GCP Console
4. If errors started immediately after a new deployment, roll back to the previous stable Cloud Run revision:
   ```bash
   gcloud run services update-traffic greenearth-api-prod --to-revisions=<PREVIOUS_REVISION>=100 --region=us-east1
   ```

**Recover:**
The 5xx and failure rate drops below 1% for at least 10 minutes under continuous traffic. Ensure connection pools have cleared and downstream latencies have returned to baseline.
