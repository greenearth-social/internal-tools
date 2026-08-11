---
alert_id: feeds-degraded
severity: critical
---

## Feeds Degraded

**Likely cause:** Inference service down, Elasticsearch unavailable, or ingestion lag.

**Steps:**
1. Check inference service: `curl https://inference.greenearth.social/health`
2. Check API health: `curl https://api.greenearth.social/health`
3. Check ES cluster status in GCP Console
4. Check ingestion freshness: look for recent docs in `posts-*` index
5. Check Cloud Run logs for api and inference-service for errors
6. If inference service is down, redeploy: `GE_ENVIRONMENT=prod ./scripts/deploy.sh` in `inference-service/`
