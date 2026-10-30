---
alert_id: es-disk-usage-exceeding-capacity
severity: warning
---

## ES Disk Usage Exceeding Capacity

**Trigger:** Filesystem usage exceeds 85% for 30 minutes for a `cluster`/`name` series. The policy evaluates every 5 minutes and does not filter to production; use the incident labels to identify the affected cluster.

**Likely cause:** Data growth, uneven shard allocation, or index lifecycle management (ILM) failing to remove expired indices.

**Steps:**
1. On the affected Elasticsearch cluster, check node disk usage with `GET /_cat/allocation?v` and cluster health with `GET /_cluster/health`.
2. Identify the largest indices with `GET /_cat/indices?v&s=store.size:desc`. Check `GET /_ilm/status` and `GET /posts-*,likes-*/_ilm/explain` for stalled retention. Confirm retention requirements and backups before removing data.
3. If usage is uneven, inspect shard allocation. If capacity is insufficient, increase the affected node set's `volumeClaimTemplates` storage in `ingex/index/deploy/k8s/environments/<env>/kustomization.yaml`, then apply from `ingex/index/` with `./deploy.sh <env> --ctypes resource`.
4. Verify disk usage is below the threshold with headroom, cluster health is healthy, and ingestion writes succeed. Check for flood-stage write blocks if the disk filled.
