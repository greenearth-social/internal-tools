"""Replace local BridgEU DA scores with the rebased fixture.

Runs with the BridgEU API's psycopg dependency, but uses a writable connection
directly: the serving API's pool is read-only. Only da_post_scores is touched.
"""

from __future__ import annotations

import gzip
import json
import math
import sys
import zlib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Installed by bridg-eu/api/Pipfile in the seed container.
    from psycopg import Connection  # pyright: ignore[reportMissingImports]

SEED_DIR = Path("/runtime/seed")
SCORES_FILE = "da_post_scores.jsonl.gz"
# Deliberately independent of .env, live mode, and the API's configuration.
# The Compose service can only resolve this instance's Postgres container.
DATABASE_URL = "postgresql://graph:graph@bridgeu-postgres:5432/graph?sslmode=disable"


def parse_row(line: str, line_number: int) -> tuple[str, float, datetime]:
    """Validate a fixture row before passing native values to COPY."""
    try:
        row = json.loads(line)
        if not isinstance(row, dict) or set(row) != {"post_uri", "score", "created_at"}:
            raise ValueError("expected post_uri, score, and created_at")
        uri, score, created_at = row["post_uri"], row["score"], row["created_at"]
        if not isinstance(uri, str) or not uri:
            raise ValueError("post_uri must be a nonempty string")
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("score must be a finite number between 0 and 1")
        if not isinstance(created_at, str):
            raise ValueError("created_at must be an ISO timestamp with a timezone")
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        return uri, score, created
    except ValueError as exc:
        raise ValueError(f"{SCORES_FILE} line {line_number}: {exc}") from exc


def load_scores(conn: Connection, seed_dir: Path) -> int:
    """Replace scores atomically; a failed COPY or count check rolls back."""
    manifest = json.loads((seed_dir / "manifest.json").read_text())
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")
    scores_path = seed_dir / SCORES_FILE
    if "diverse_approval" not in manifest and not scores_path.exists():
        # Older releases have no DA fixture. Keeping the previous seed's
        # scores would leave candidates pointing at posts no longer in ES.
        with conn.transaction():
            conn.execute("TRUNCATE da_post_scores")
        print("No DA scores in this fixture; cleared previous da_post_scores rows")
        return 0

    metadata = manifest.get("diverse_approval")
    if not isinstance(metadata, dict) or metadata.get("file") != SCORES_FILE:
        raise ValueError("manifest diverse_approval must name da_post_scores.jsonl.gz")
    expected = metadata.get("count")
    if type(expected) is not int or expected < 0:
        raise ValueError("manifest diverse_approval.count must be a nonnegative integer")

    count = 0
    # Opening first catches a missing file without taking the table lock.
    # TRUNCATE and COPY share a transaction, including validation at EOF.
    # Use the same search_path as the canonical schema initializer and API.
    with gzip.open(scores_path, "rt", encoding="utf-8") as rows:
        with conn.transaction(), conn.cursor() as cursor:
            cursor.execute("TRUNCATE da_post_scores")
            with cursor.copy(
                "COPY da_post_scores (post_uri, score, created_at) FROM STDIN"
            ) as copy:
                for line_number, line in enumerate(rows, start=1):
                    if line.strip():
                        copy.write_row(parse_row(line, line_number))
                        count += 1
            if count != expected:
                raise ValueError(f"DA fixture has {count} rows; manifest declares {expected}")
    return count


def main() -> None:
    import psycopg  # pyright: ignore[reportMissingImports]

    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=10) as conn:
            count = load_scores(conn, SEED_DIR)
    except (OSError, EOFError, ValueError, zlib.error, psycopg.Error) as exc:
        sys.exit(f"FATAL: DA score seed failed: {exc}")
    print(f"Loaded {count:,} DA scores into local Postgres")


if __name__ == "__main__":
    main()
