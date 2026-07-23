"""Wipe seeded data indexes so each seed is a fresh, idempotent load.

Data in this environment is disposable by design; without this step a re-seed
would accumulate stale documents (previous synthetic likes especially, whose
at_uris differ run to run) alongside the fresh load.

Deletes the data indexes and recreates the write-alias bootstrap indexes that
don't get recreated by the ingest binary itself (megastream_ingest re-creates
the posts/replies/tombstones period indexes via EnsureIndex on its next run).

Runs with the read-write key; stdlib only.
"""

import json
import os
import sys
import urllib.error
import urllib.request

ES_URL = os.environ["GE_ELASTICSEARCH_URL"].rstrip("/")
ES_KEY = os.environ["GE_ELASTICSEARCH_API_KEY"]

DELETE_PATTERNS = [
    "posts-*",
    "post-tombstones-*",
    "replies-*",
    "reply-tombstones-*",
    "likes-*",
    "like-tombstones-*",
    "inferences-*",
    "hashtags_v1",
]

BOOTSTRAP = [
    ("likes", "likes-000001"),
    ("like_tombstones", "like-tombstones-000001"),
    ("inferences", "inferences-000001"),
    ("hashtags", "hashtags_v1"),
]


def request(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        ES_URL + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"ApiKey {ES_KEY}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main() -> None:
    # ES refuses wildcard deletes (action.destructive_requires_name); resolve
    # each pattern to concrete index names first.
    deleted = []
    for pattern in DELETE_PATTERNS:
        status, payload = request("GET", f"/{pattern}?expand_wildcards=open,closed")
        if status == 404:
            continue
        if status >= 300:
            sys.exit(f"FATAL: GET /{pattern} -> {status}: {json.dumps(payload)[:500]}")
        for index in payload:
            status, del_payload = request("DELETE", f"/{index}")
            if status not in (200, 404):
                sys.exit(f"FATAL: DELETE /{index} -> {status}: {json.dumps(del_payload)[:500]}")
            deleted.append(index)
    print(f"Deleted data indexes: {', '.join(sorted(deleted)) or '(none)'}")

    for alias, index in BOOTSTRAP:
        status, payload = request(
            "PUT", f"/{index}", {"aliases": {alias: {"is_write_index": True}}}
        )
        if status >= 300:
            sys.exit(f"FATAL: PUT /{index} -> {status}: {json.dumps(payload)[:500]}")
    print("Recreated bootstrap indexes: " + ", ".join(i for _, i in BOOTSTRAP))


if __name__ == "__main__":
    main()
