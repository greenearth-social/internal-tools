---
alert_id: es-storage-high
severity: warning
---

## ES Storage > 80%

**Likely cause:** Post volume spike or stale index lifecycle policy not clearing old indices.

**Steps:**
1. Check current usage in GCP Console → Elasticsearch → Storage
2. Identify the largest indices: `GET /_cat/indices?v&s=store.size:desc`
3. If old indices exist, delete them: `DELETE /posts-YYYY-MM`
4. If all indices are recent, scale up ES storage in GKE node pool
5. Verify usage drops below 70% before closing
