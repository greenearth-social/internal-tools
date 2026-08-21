"""Stage 2: cross-check the DID set against a labeler's account-level labels.

Ports enumerate_labeler.py's wholesale-enumeration approach from ingex#466:
pull the labeler's entire queryLabels feed once (O(labeler size)) and
intersect locally, rather than looking up each DID individually (ingex#466
notes per-DID lookups get rate-limited past ~45k lookups against a similar
labeler).
"""

import collections
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from scoring import LabelInfo

PLC_DIRECTORY = "https://plc.directory/"
PUBLIC_APPVIEW = "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor="
DEFAULT_LABELER = "skywatch.blue"


def _fetch_json(url: str, timeout: int = 30, attempts: int = 4) -> dict:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(2 * (attempt + 1))
    raise last  # type: ignore[misc]


def resolve_labeler_endpoint(actor: str) -> tuple[str, str]:
    did = actor
    if not actor.startswith("did:"):
        did = _fetch_json(PUBLIC_APPVIEW + urllib.parse.quote(actor)).get("did")
        if not did:
            raise ValueError(f"could not resolve handle {actor}")
    doc = _fetch_json(PLC_DIRECTORY + did)
    for service in doc.get("service") or []:
        if service.get("id", "").endswith("atproto_labeler"):
            return did, service["serviceEndpoint"].rstrip("/")
    raise ValueError(f"{actor} declares no #atproto_labeler service")


def apply_label_page(page_labels: list[dict], by_val: dict[str, set[str]]) -> tuple[int, int]:
    seen = retracted = 0
    for label in page_labels:
        seen += 1
        uri, val = label.get("uri") or "", label.get("val")
        if not val or not uri.startswith("did:"):
            continue
        by_val.setdefault(val, set())
        if label.get("neg"):
            by_val[val].discard(uri)
            retracted += 1
        else:
            by_val[val].add(uri)
    return seen, retracted


def enumerate_labeler(
    labeler: str = DEFAULT_LABELER,
    values: set[str] | None = None,
    max_pages: int = 100000,
) -> dict[str, set[str]]:
    did, endpoint = resolve_labeler_endpoint(labeler)
    print(f"labeler: {labeler} ({did}) at {endpoint}", file=sys.stderr)

    by_val: dict[str, set[str]] = collections.defaultdict(set)
    cursor = None
    pages = seen_total = 0
    while pages < max_pages:
        url = f"{endpoint}/xrpc/com.atproto.label.queryLabels?uriPatterns=*&limit=250"
        if cursor:
            url += f"&cursor={urllib.parse.quote(str(cursor))}"
        data = _fetch_json(url)
        page_labels = data.get("labels") or []
        if not page_labels:
            break
        seen, _ = apply_label_page(page_labels, by_val)
        seen_total += seen
        if values is not None:
            for val in list(by_val):
                if val not in values:
                    del by_val[val]
        next_cursor = data.get("cursor")
        pages += 1
        if pages % 200 == 0:
            print(f"labeler: {seen_total:,} labels seen, cursor {next_cursor}", file=sys.stderr)
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return dict(by_val)


def intersect_dids(did_set: set[str], by_val: dict[str, set[str]]) -> dict[str, LabelInfo]:
    hits: dict[str, list[str]] = collections.defaultdict(list)
    for val, val_dids in by_val.items():
        for did in val_dids & did_set:
            hits[did].append(val)
    return {did: LabelInfo(did=did, labels=sorted(vals)) for did, vals in hits.items()}
