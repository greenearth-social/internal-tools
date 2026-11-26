"""Replace the local BridgEU graph tables with the rebased fixture graph.

Runs in the bridgeu-da-pipeline image (psycopg is there) against this
instance's bridgeu-postgres only. Loads graph.actor, graph.follow and
graph.likes the way the graph ingester would have written them, so the
pipeline's snapshot and score jobs read them unchanged. Record seq values are
synthetic and increase with indexed_at. One transaction: a bad row or a count
that disagrees with the manifest leaves the previous graph in place.
"""

from __future__ import annotations

import gzip
import json
import sys
import zlib
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from psycopg import Connection  # pyright: ignore[reportMissingImports]

SEED_DIR = Path("/runtime/seed")
DATABASE_URL = "postgresql://bridgeu:bridgeu@bridgeu-postgres:5432/bridgeu?sslmode=disable"
POST_COLLECTION = "app.bsky.feed.post"

# name in manifest["graph"]["files"] -> staging table, its columns, the timestamp columns
STAGES = {
    "actors": ("stage_actor", ("did", "status"), ()),
    "follows": (
        "stage_follow",
        ("follower_did", "subject_did", "rkey", "created_at", "indexed_at"),
        ("created_at", "indexed_at"),
    ),
    "likes": (
        "stage_like",
        ("liker_did", "rkey", "subject_uri", "created_at", "indexed_at"),
        ("created_at", "indexed_at"),
    ),
}

STAGE_DDL = """
CREATE TEMP TABLE stage_actor (did text NOT NULL, status text NOT NULL) ON COMMIT DROP;
CREATE TEMP TABLE stage_follow (follower_did text NOT NULL, subject_did text NOT NULL,
    rkey text NOT NULL, created_at timestamptz, indexed_at timestamptz) ON COMMIT DROP;
CREATE TEMP TABLE stage_like (liker_did text NOT NULL, rkey text NOT NULL,
    subject_uri text NOT NULL, created_at timestamptz, indexed_at timestamptz) ON COMMIT DROP;
"""

# Every DID a follow or like names becomes an actor (the ingester creates them
# the same way), then the edges join to their ids.
LOAD_SQL = f"""
INSERT INTO graph.actor (did, status) SELECT did, status FROM stage_actor;
INSERT INTO graph.actor (did)
    SELECT did FROM (
        SELECT follower_did AS did FROM stage_follow UNION SELECT subject_did FROM stage_follow
        UNION SELECT liker_did FROM stage_like
        UNION SELECT split_part(substr(subject_uri, 6), '/', 1) FROM stage_like) d
    ON CONFLICT (did) DO NOTHING;
INSERT INTO graph.collection (nsid) VALUES ('{POST_COLLECTION}');
INSERT INTO graph.follow (follower_id, subject_id, rkey, created_at, indexed_at, seq)
    SELECT fa.id, sa.id, f.rkey, f.created_at, coalesce(f.indexed_at, f.created_at, now()),
           row_number() OVER (ORDER BY f.indexed_at, f.follower_did, f.rkey)
    FROM stage_follow f
    JOIN graph.actor fa ON fa.did = f.follower_did
    JOIN graph.actor sa ON sa.did = f.subject_did;
INSERT INTO graph.likes (liker_id, rkey, subject_actor_id, subject_collection_id, subject_rkey,
                         created_at, indexed_at, seq)
    SELECT la.id, l.rkey, sa.id, c.id, split_part(substr(l.subject_uri, 6), '/', 3),
           l.created_at, coalesce(l.indexed_at, l.created_at, now()),
           (SELECT count(*) FROM stage_follow)
             + row_number() OVER (ORDER BY l.indexed_at, l.liker_did, l.rkey)
    FROM stage_like l
    JOIN graph.actor la ON la.did = l.liker_did
    JOIN graph.actor sa ON sa.did = split_part(substr(l.subject_uri, 6), '/', 1)
    JOIN graph.collection c ON c.nsid = split_part(substr(l.subject_uri, 6), '/', 2);
"""

COUNT_SQL = {
    "actors": "SELECT count(*) FROM stage_actor",
    "follows": "SELECT count(*) FROM graph.follow",
    "likes": "SELECT count(*) FROM graph.likes",
}

TRUNCATE_SQL = (
    "TRUNCATE graph.likes, graph.reposts, graph.follow, graph.verification, graph.actor,"
    " graph.collection RESTART IDENTITY CASCADE"
)


def parse_time(value: object, where: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{where}: timestamps must be ISO strings or null")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{where}: timestamps must include a timezone")
    return parsed


def read_rows(path: Path, columns: tuple[str, ...], times: tuple[str, ...]) -> Iterator[tuple]:
    """Validated rows of one graph file, in column order, ready for COPY."""
    with gzip.open(path, "rt", encoding="utf-8") as lines:
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            where = f"{path.name} line {number}"
            row = json.loads(line)
            if not isinstance(row, dict) or set(row) != set(columns):
                raise ValueError(f"{where}: expected the fields {', '.join(columns)}")
            for column in columns:
                if column not in times and (not isinstance(row[column], str) or not row[column]):
                    raise ValueError(f"{where}: {column} must be a nonempty string")
            if "subject_uri" in row and not row["subject_uri"].startswith("at://"):
                raise ValueError(f"{where}: subject_uri must be an AT-URI")
            yield tuple(parse_time(row[c], where) if c in times else row[c] for c in columns)


def graph_metadata(manifest: dict) -> dict | None:
    graph = manifest.get("graph")
    if graph is None:
        return None
    if not isinstance(graph, dict) or set(graph.get("files") or {}) != set(STAGES):
        raise ValueError("manifest graph.files must name actors, follows and likes")
    counts = graph.get("counts") or {}
    if any(type(counts.get(name)) is not int for name in STAGES):
        raise ValueError("manifest graph.counts must give actors, follows and likes")
    return graph


def load_graph(conn: Connection, seed_dir: Path) -> dict[str, int]:
    """Replace the graph tables atomically; empties them for a fixture without a graph."""
    graph = graph_metadata(json.loads((seed_dir / "manifest.json").read_text()))
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(TRUNCATE_SQL)
        if graph is None:
            return {}
        cursor.execute(STAGE_DDL)
        for name, (table, columns, times) in STAGES.items():
            path = seed_dir / graph["files"][name]
            with cursor.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as copy:
                for row in read_rows(path, columns, times):
                    copy.write_row(row)
        cursor.execute(LOAD_SQL)
        loaded = {name: cursor.execute(sql).fetchone()[0] for name, sql in COUNT_SQL.items()}
        for name, count in loaded.items():
            if count != graph["counts"][name]:
                raise ValueError(
                    f"loaded {count} {name}; the manifest declares {graph['counts'][name]}"
                )
        cursor.execute("ANALYZE graph.actor, graph.follow, graph.likes")
    return loaded


def main() -> None:
    import psycopg  # pyright: ignore[reportMissingImports]

    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=10) as conn:
            loaded = load_graph(conn, SEED_DIR)
    except (OSError, EOFError, ValueError, zlib.error, psycopg.Error) as exc:
        sys.exit(f"FATAL: graph seed failed: {exc}")
    if not loaded:
        print("No graph in this fixture; emptied the local graph tables")
    else:
        print(
            f"Loaded {loaded['actors']:,} actors, {loaded['follows']:,} follows"
            f" and {loaded['likes']:,} likes into the local graph"
        )


if __name__ == "__main__":
    main()
