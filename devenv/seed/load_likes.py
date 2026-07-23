"""Bulk-load the rebased likes fixture into dev Elasticsearch (issue api#268).

In prod, likes arrive via jetstream_ingest from the live firehose; a static
fixture can't be replayed through a websocket, so the seed pipeline writes
like documents directly. Document identity matches prod exactly
(_id = at_uri, routing = author_did — see BulkIndex in
ingex/ingest/internal/common/elasticsearch.go).

Also applies per-post like counts from like_counts.jsonl.gz: megastream_ingest
writes every post with like_count=0 (in prod, jetstream increments the field
as likes stream in), and the api's popularity generator ranks on
posts.like_count — so without this pass the popularity feed would be flat.
Runs AFTER seed-megastream so the post docs exist.

Runs on a stock python image; stdlib only.
"""

import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ES_URL = os.environ["GE_ELASTICSEARCH_URL"].rstrip("/")
ES_KEY = os.environ["GE_ELASTICSEARCH_API_KEY"]
SEED_DIR = Path("/runtime/seed")
BATCH_SIZE = 1000


def request(method: str, path: str, body: bytes | None = None, ndjson: bool = False) -> dict:
    content_type = "application/x-ndjson" if ndjson else "application/json"
    req = urllib.request.Request(
        ES_URL + path,
        method=method,
        data=body,
        headers={"Authorization": f"ApiKey {ES_KEY}", "Content-Type": content_type},
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 4:
                time.sleep(2**attempt)
                continue
            sys.exit(f"FATAL: {method} {path} -> {e.code}: {e.read()[:1000]}")
    raise AssertionError("unreachable")


def check_bulk_response(resp: dict, allow_missing: bool = False) -> int:
    """Return the number of items that failed. Exits on unexpected errors."""
    if not resp.get("errors"):
        return 0
    failures = 0
    for item in resp.get("items", []):
        result = item.get("index") or item.get("update") or {}
        error = result.get("error")
        if not error:
            continue
        failures += 1
        if allow_missing and error.get("type") == "document_missing_exception":
            continue
        sys.exit(f"FATAL: bulk item failed: {json.dumps(result)[:1000]}")
    return failures


def flush(lines: list[str], allow_missing: bool = False) -> int:
    if not lines:
        return 0
    resp = request("POST", "/_bulk", ("\n".join(lines) + "\n").encode(), ndjson=True)
    return check_bulk_response(resp, allow_missing=allow_missing)


def load_likes() -> int:
    likes_path = SEED_DIR / "likes.jsonl.gz"
    if not likes_path.exists():
        print("No likes.jsonl.gz in seed dir; skipping like load")
        return 0
    total = 0
    lines: list[str] = []
    with gzip.open(likes_path, "rt") as fin:
        for line in fin:
            if not line.strip():
                continue
            doc = json.loads(line)
            meta = {
                "index": {
                    "_index": "likes",
                    "_id": doc["at_uri"],
                    "routing": doc["author_did"],
                }
            }
            lines.append(json.dumps(meta))
            lines.append(json.dumps(doc))
            total += 1
            if total % BATCH_SIZE == 0:
                flush(lines)
                lines = []
    flush(lines)
    print(f"Indexed {total} likes")
    return total


def apply_like_counts() -> None:
    counts_path = SEED_DIR / "like_counts.jsonl.gz"
    if not counts_path.exists():
        print("No like_counts.jsonl.gz in seed dir; posts keep like_count=0")
        return
    total = 0
    missing = 0
    lines: list[str] = []
    with gzip.open(counts_path, "rt") as fin:
        for line in fin:
            if not line.strip():
                continue
            doc = json.loads(line)
            meta = {
                "update": {
                    "_index": "posts",
                    "_id": doc["at_uri"],
                    "routing": doc["author_did"],
                }
            }
            lines.append(json.dumps(meta))
            lines.append(json.dumps({"doc": {"like_count": int(doc["like_count"])}}))
            total += 1
            if total % BATCH_SIZE == 0:
                missing += flush(lines, allow_missing=True)
                lines = []
    missing += flush(lines, allow_missing=True)
    print(f"Applied like counts to {total - missing}/{total} posts", flush=True)
    if missing:
        print(f"  ({missing} posts not found — likely skipped by ingest; harmless in dev)")


def update_posts_recent() -> None:
    """Point the posts_recent alias (the api's kNN/candidate index) at the
    seeded posts indexes. In prod a cronjob keeps it on the two most recent
    period indexes; dev data is small enough to alias everything."""
    request(
        "POST",
        "/_aliases",
        json.dumps({"actions": [{"add": {"index": "posts-*", "alias": "posts_recent"}}]}).encode(),
    )
    print("posts_recent alias updated")


def main() -> None:
    load_likes()
    apply_like_counts()
    update_posts_recent()
    for alias in ("posts", "likes"):
        request("POST", f"/{alias}/_refresh")
        count = request("GET", f"/{alias}/_count")["count"]
        print(f"{alias}: {count} docs")


if __name__ == "__main__":
    main()
