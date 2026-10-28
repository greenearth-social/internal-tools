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

import argparse
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

ES_URL = os.environ["GE_ELASTICSEARCH_URL"].rstrip("/")
ES_KEY = os.environ["GE_ELASTICSEARCH_API_KEY"]
SEED_DIR = Path("/runtime/seed")
BATCH_SIZE = 1000
# Concurrent bulk requests for the like-count pass. Each partial update
# rewrites the whole post, re-inserting both of its HNSW vectors, so the pass
# is bound by Elasticsearch CPU; a serial client leaves all but one core idle.
# 4 measured 2.5x faster than serial; 8 pushed requests past the 60s timeout.
LIKE_COUNT_WORKERS = 4


# How long one request may take before it's retried. A bulk update can run
# well past a few seconds when Elasticsearch is short of heap (the dev cluster
# has 1GB, and spends most of its time in GC under the like-count pass on a
# long-running instance) — slow, but still making progress.
REQUEST_TIMEOUT = 120
# Attempts per request; the waits between them double from 2s, about a
# minute in all.
ATTEMPTS = 6
# Responses that mean "busy, try again", not "wrong".
RETRY_STATUSES = {429, 502, 503, 504}


def request(
    method: str,
    path: str,
    body: bytes | None = None,
    ndjson: bool = False,
    *,
    exists_ok: bool = False,
) -> dict:
    """One Elasticsearch request, retried while the cluster is busy.

    Retrying is safe because every write this script makes can be repeated:
    likes are indexed by _id, like counts are absolute, and alias updates
    converge. The one exception is creating an index, whose retry would find
    the index a timed-out first attempt already made — exists_ok accepts that.
    """
    content_type = "application/x-ndjson" if ndjson else "application/json"
    req = urllib.request.Request(
        ES_URL + path,
        method=method,
        data=body,
        headers={"Authorization": f"ApiKey {ES_KEY}", "Content-Type": content_type},
    )
    for attempt in range(ATTEMPTS):
        last = attempt == ATTEMPTS - 1
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read()[:1000]
            if exists_ok and e.code == 400 and b"resource_already_exists_exception" in detail:
                return {}
            if e.code not in RETRY_STATUSES or last:
                sys.exit(f"FATAL: {method} {path} -> {e.code}: {detail}")
            reason = f"HTTP {e.code}"
        except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
            if last:
                sys.exit(f"FATAL: {method} {path} failed after {ATTEMPTS} attempts: {e}")
            reason = str(e) or type(e).__name__
        delay = 2 ** (attempt + 1)
        print(f"  {method} {path}: {reason}; retrying in {delay}s", flush=True)
        time.sleep(delay)
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
    batches: list[list[str]] = []
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
                batches.append(lines)
                lines = []
    batches.append(lines)
    # Each update sets an absolute count, so batches commute and can land in
    # any order. map() re-raises the first failure; flush already exits on one.
    with ThreadPoolExecutor(LIKE_COUNT_WORKERS) as pool:
        missing = sum(pool.map(lambda batch: flush(batch, allow_missing=True), batches))
    print(f"Applied like counts to {total - missing}/{total} posts", flush=True)
    if missing:
        print(f"  ({missing} posts not found — likely skipped by ingest; harmless in dev)")


def update_posts_recent(*, ensure_quality: bool = False) -> None:
    """Point the posts_recent and posts_recent_quality aliases at the seeded
    posts indexes. In prod the update-recent-alias cronjob keeps them on the two
    (resp. three) most recent period indexes; dev data is small enough to alias
    everything.

    posts_recent must exclude posts-quality-*: those indexes match the posts-*
    pattern too, and sweeping them in would surface every quality post twice in
    the api's candidate generators (greenearth-social/ingex#442).

    After the quality backfill, ensure_quality also creates an empty quality
    index when no posts qualify, so the api can query the alias without a 404.
    Its weekly name selects the same posts-quality-* template as the backfill.

    Resolve concrete names before updating aliases. Elasticsearch treats a
    nonexistent negative wildcard such as -posts-quality-* as a missing index,
    so using wildcard exclusion makes a normal pre-backfill seed fail."""
    rows = request("GET", "/_cat/indices/posts-*?format=json&h=index")
    indexes = sorted(row["index"] for row in rows)
    quality_indexes = [index for index in indexes if index.startswith("posts-quality-")]
    post_indexes = [index for index in indexes if not index.startswith("posts-quality-")]
    if not post_indexes:
        sys.exit("FATAL: no posts-* indexes found after megastream seed")

    if ensure_quality and not quality_indexes:
        year, week, _ = datetime.now(UTC).isocalendar()
        index = f"posts-quality-{year}-w{week:02d}"
        request("PUT", f"/{index}", b"{}", exists_ok=True)
        quality_indexes = [index]

    actions = [{"add": {"indices": post_indexes, "alias": "posts_recent"}}]
    if quality_indexes:
        actions.append({"add": {"indices": quality_indexes, "alias": "posts_recent_quality"}})

    request("POST", "/_aliases", json.dumps({"actions": actions}).encode())
    print(f"aliases updated ({len(actions)} action(s))")


def refresh_and_report(*aliases: str) -> None:
    for alias in aliases:
        request("POST", f"/{alias}/_refresh")
        count = request("GET", f"/{alias}/_count")["count"]
        print(f"{alias}: {count} docs")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--aliases-only",
        action="store_true",
        help="Publish and refresh recent-post aliases after quality backfill",
    )
    args = parser.parse_args(argv)
    if args.aliases_only:
        update_posts_recent(ensure_quality=True)
        refresh_and_report("posts_recent", "posts_recent_quality")
        return

    load_likes()
    apply_like_counts()
    update_posts_recent()
    # Make the updated like counts visible before the quality backfill scans.
    refresh_and_report("posts", "likes")


if __name__ == "__main__":
    main()
