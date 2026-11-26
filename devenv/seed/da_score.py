"""Run BridgEU's DA `score` job on rebased fixture data, then line its dates up.

The score job dates each post by the TID in its record key, as production
does. The seed rebase can't move those (the URIs must match Elasticsearch), so
every fixture post looks as old as the original capture window: past the
168 h age filter, and served by nothing. This runs the job with the age
window widened by the rebase shift, then moves da_post_scores.created_at
forward by the same shift, so the table looks as it would have had the posts
been written when the rebased data says they were.

Arguments after the script name go to the score job unchanged. Runs in the
bridgeu-da-pipeline image, against this instance's database only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SEED_DIR = Path("/runtime/seed")
DATABASE_URL = "postgresql://bridgeu:bridgeu@bridgeu-postgres:5432/bridgeu?sslmode=disable"
MAX_AGE_HOURS = 168.0
# The score job's writer lock (bridg-eu algo/pipeline/score.py LOCK_ID), so a
# concurrent score run can't interleave with the shift.
LOCK_ID = 0x62726467


def rebase_shift_seconds(seed_dir: Path) -> int:
    """The seed rebase's shift, 0 when nothing was rebased."""
    manifest = seed_dir / "manifest.json"
    if not manifest.exists():
        return 0
    rebased = json.loads(manifest.read_text()).get("rebased") or {}
    return int(rebased.get("delta_seconds") or 0)


def score_argv(shift_seconds: int, extra: list[str]) -> list[str]:
    window = ["--window-hours", str(MAX_AGE_HOURS)]
    max_age = ["--max-age-hours", str(MAX_AGE_HOURS + shift_seconds / 3600)]
    return [sys.executable, "-m", "pipeline", "score", *window, *max_age, *extra]


def shift_created_at(shift_seconds: int) -> int:
    import psycopg  # pyright: ignore[reportMissingImports]

    with psycopg.connect(DATABASE_URL, connect_timeout=10) as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        cur = conn.execute(
            "UPDATE da_post_scores SET created_at = created_at + make_interval(secs => %s)",
            (shift_seconds,),
        )
        return cur.rowcount


def main() -> int:
    shift = rebase_shift_seconds(SEED_DIR)
    result = subprocess.run(score_argv(shift, sys.argv[1:]))
    if result.returncode != 0:
        return result.returncode
    if shift:
        rows = shift_created_at(shift)
        days = shift / 86400
        print(f"Moved {rows:,} da_post_scores dates forward by the rebase shift ({days:.2f}d)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
