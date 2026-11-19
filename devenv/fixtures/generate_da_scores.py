#!/usr/bin/env python3
"""Add synthetic Diverse Approval scores to an existing dev fixture.

Reads the original post chunks locally; no Elasticsearch, Postgres, or
production credentials are needed. Each unique post gets a reproducible
random score in [0, 1). These scores exercise the feed plumbing, not the
Diverse Approval algorithm.

Usage: python3 generate_da_scores.py [fixture-dir] [--seed 42]

Timestamps stay in the fixture's original time window. A future seeder must
shift them by the same delta as the Elasticsearch posts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import io
import json
import random
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

DEFAULT_SEED = 42
SCORES_FILE = "da_post_scores.jsonl.gz"


def read_fixture_posts(fixture_dir: Path) -> dict[str, str]:
    """Read unique post URIs and the creation times used by ES ingestion."""
    chunks = sorted(fixture_dir.glob("*.db.zip"))
    if not chunks:
        raise ValueError(f"no post chunks (*.db.zip) in {fixture_dir}")

    posts: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "chunk.db"
        for chunk in chunks:
            if zipfile.is_zipfile(chunk):
                with zipfile.ZipFile(chunk) as archive:
                    members = [name for name in archive.namelist() if name.endswith(".db")]
                    if len(members) != 1:
                        raise ValueError(f"expected one .db in {chunk.name}")
                    with archive.open(members[0]) as src, db_path.open("wb") as dest:
                        shutil.copyfileobj(src, dest)
                source = db_path
            else:
                # The existing fixture tools also accept uncompressed SQLite
                # files carrying the .db.zip suffix.
                source = chunk

            conn = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
            try:
                for uri, raw_post in conn.execute("SELECT at_uri, raw_post FROM enriched_posts"):
                    if not isinstance(uri, str) or not uri:
                        raise ValueError(f"missing post URI in {chunk.name}")
                    try:
                        # Match ingex's NormalizeTimestampToUTC: ingest reads
                        # record.createdAt and stores UTC with second precision.
                        raw = json.loads(raw_post)
                        created = dt.datetime.fromisoformat(
                            raw["message"]["commit"]["record"]["createdAt"].replace("Z", "+00:00")
                        )
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=dt.UTC)
                        timestamp = (
                            created.astimezone(dt.UTC)
                            .isoformat(timespec="seconds")
                            .replace("+00:00", "Z")
                        )
                    except (ValueError, KeyError, TypeError, AttributeError) as exc:
                        raise ValueError(
                            f"invalid creation timestamp for {uri} in {chunk.name}"
                        ) from exc
                    if uri in posts and posts[uri] != timestamp:
                        raise ValueError(f"conflicting creation timestamps for {uri}")
                    posts[uri] = timestamp
            finally:
                conn.close()

    if not posts:
        raise ValueError(f"no posts in {fixture_dir}")
    return posts


def generate(fixture_dir: Path, *, seed: int = DEFAULT_SEED) -> dict:
    """Write scores and record their synthetic origin in the fixture manifest."""
    manifest_path = fixture_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    posts = read_fixture_posts(fixture_dir)
    metadata = {
        "source": "synthetic-random",
        "seed": seed,
        "file": SCORES_FILE,
        "count": len(posts),
    }
    manifest["diverse_approval"] = metadata

    # An independent RNG leaves the post/like sampler untouched. Sorting
    # makes scores independent of SQLite row order and chunk boundaries.
    rng = random.Random(seed)
    with tempfile.TemporaryDirectory(dir=fixture_dir, prefix=".da-scores-") as tmp:
        scores_path = Path(tmp) / SCORES_FILE
        with (
            scores_path.open("wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
            io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as output,
        ):
            for uri in sorted(posts):
                row = {"post_uri": uri, "score": rng.random(), "created_at": posts[uri]}
                output.write(json.dumps(row) + "\n")
        new_manifest = Path(tmp) / "manifest.json"
        new_manifest.write_text(json.dumps(manifest, indent=2) + "\n")
        # Finish reading and validating the source before replacing outputs.
        scores_path.replace(fixture_dir / SCORES_FILE)
        new_manifest.replace(manifest_path)

    print(f"  wrote {SCORES_FILE} ({len(posts):,} synthetic scores, seed={seed})")
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument(
        "fixture_dir",
        nargs="?",
        type=Path,
        default=Path(__file__).parent / "data",
        help="existing fixture directory (default: fixtures/data next to this script)",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="random seed (default: 42)")
    args = parser.parse_args()
    try:
        generate(args.fixture_dir.expanduser(), seed=args.seed)
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
