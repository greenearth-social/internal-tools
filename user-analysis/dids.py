"""Load and dedupe the DID list exported from Posthog for the 08-18 spike."""

import csv
from pathlib import Path


def load_dids(csv_paths: list[Path]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for path in csv_paths:
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                did = (row.get("distinct_id") or "").strip()
                if did and did not in seen:
                    seen.add(did)
                    ordered.append(did)
    return ordered


def chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
