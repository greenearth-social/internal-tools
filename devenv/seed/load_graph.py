"""Bulk-load the saved graph into this devenv instance's PostgreSQL database.

The fixture supplies portable DIDs; PostgreSQL allocates local actor and
collection IDs. COPY stages the rows before a transaction replaces the raw
graph tables. Any failed COPY, join, or constraint check rolls back the load.
The schema and migration history are never replaced.

Runs with Python's stdlib and psql, installed in the graph seed image. The
connection is fixed to the local Compose service and does not read production
connection settings or credentials.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import IO

# Checkout imports for tests; the container exposes the same helper through
# PYTHONPATH=/fixturetools, independently of its fixture data mount.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
from graph_fixture import validate_graph_fixture  # noqa: E402

FIXTURES_DIR = Path("/fixtures")
SEED_DIR = Path("/runtime/seed")
PSQL_COMMAND = [
    "psql",
    "-X",
    "-qAt",
    "--no-password",
    "--set=ON_ERROR_STOP=1",
    "--dbname=postgres://graph:graph@bridgeu-postgres:5432/graph?sslmode=disable",
]
RAW_TABLES = (
    "graph.follow, graph.likes, graph.reposts, graph.verification, "
    "graph.ingest_cursor, graph.actor, graph.collection"
)


class GraphSeedError(RuntimeError):
    """Fixture validation or the atomic database load failed."""


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def parse_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("seed timestamps must include a timezone")
    return parsed


def prepare_seed(fixture_dir: Path, seed_dir: Path) -> dict | None:
    """Check the original capture and its rebased companion before connecting."""
    failures = validate_graph_fixture(fixture_dir)
    if failures:
        raise GraphSeedError("; ".join(failures))
    original_base = json.loads((fixture_dir / "manifest.json").read_text())
    seeded_base = json.loads((seed_dir / "manifest.json").read_text())
    rebased_base = seeded_base.pop("rebased", None)
    if seeded_base != original_base or not isinstance(rebased_base, dict):
        raise GraphSeedError("rebased base fixture does not match the source; rerun devctl seed")
    effective_end = parse_timestamp(rebased_base["effective_window_end"])
    parse_timestamp(rebased_base["seeded_at"])

    original_graph = fixture_dir / "graph"
    seeded_graph = seed_dir / "graph"
    if not original_graph.exists():
        if seeded_graph.exists():
            raise GraphSeedError("stale rebased graph without a source graph; rerun devctl seed")
        return None
    source_manifest = json.loads((original_graph / "manifest.json").read_text())
    graph_manifest = json.loads((seeded_graph / "manifest.json").read_text())
    rebased = graph_manifest.pop("rebased", None)
    if graph_manifest != source_manifest or not isinstance(rebased, dict):
        raise GraphSeedError("rebased graph does not match the source capture; rerun devctl seed")
    expected_delta = effective_end - parse_timestamp(source_manifest["capture_finished_at"])
    if (
        rebased["seeded_at"] != rebased_base["seeded_at"]
        or parse_timestamp(rebased["effective_capture_finished_at"]) != effective_end
        or dt.timedelta(seconds=rebased["delta_seconds"]) != expected_delta
    ):
        raise GraphSeedError("graph and base fixture were rebased to different times")
    if rebased["likes_sha256"] != file_sha256(seed_dir / "likes.jsonl.gz"):
        raise GraphSeedError("shared rebased likes changed; rerun devctl seed")
    for name in ("actors.jsonl.gz", "follows.jsonl.gz"):
        if rebased["files_sha256"][name] != file_sha256(seeded_graph / name):
            raise GraphSeedError(f"rebased graph {name} changed; rerun devctl seed")
    return {**graph_manifest, "rebased": rebased}


def json_rows(path: Path):
    with gzip.open(path, "rt") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def copy_value(value: object) -> str:
    """Escape COPY text data so record contents never become psql commands."""
    if value is None:
        return "\\N"
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def copy_rows(stream: IO[str], table: str, rows: Iterable[Sequence[object]]) -> int:
    # Table names are internal constants. Values use COPY text escaping,
    # including backslashes, newlines, and the end-of-copy marker.
    stream.write(f"COPY {table} FROM STDIN;\n")
    count = 0
    for row in rows:
        stream.write("\t".join(copy_value(value) for value in row) + "\n")
        count += 1
    stream.write("\\.\n")
    print(f"  staged {count:,} {table.removeprefix('seed_')}", flush=True)
    return count


def actor_rows(path: Path, effective_end: str):
    for row in json_rows(path):
        # Missing production metadata is explicit in the capture. 'unknown'
        # avoids claiming these accounts are active; first_seen_at records
        # the effective local observation time, not an invented account age.
        yield (
            row["did"],
            row["status"] if row["in_graph"] else "unknown",
            row["status_at"],
            row["first_seen_at"] if row["in_graph"] else effective_end,
        )


def follow_rows(path: Path):
    for row in json_rows(path):
        yield tuple(
            row[key]
            for key in (
                "follower_did",
                "subject_did",
                "rkey",
                "created_at",
                "indexed_at",
                "deleted_at",
                "seq",
            )
        )


def split_uri(uri: str, collection: str) -> tuple[str, str]:
    parts = uri.split("/")
    if (
        len(parts) != 5
        or parts[:2] != ["at:", ""]
        or not parts[2].startswith("did:")
        or parts[3] != collection
        or not parts[4]
    ):
        raise ValueError(f"invalid {collection} URI: {uri!r}")
    return parts[2], parts[4]


def like_rows(path: Path):
    for row in json_rows(path):
        liker, rkey = split_uri(row["at_uri"], "app.bsky.feed.like")
        subject, subject_rkey = split_uri(row["subject_uri"], "app.bsky.feed.post")
        if liker != row["author_did"]:
            raise ValueError("like URI does not match author_did")
        yield liker, rkey, subject, subject_rkey, row["created_at"], row["indexed_at"]


def write_seed_sql(stream: IO[str], seed_dir: Path, manifest: dict | None) -> None:
    stream.write("BEGIN;\nSET LOCAL lock_timeout = '5s';\nSET LOCAL timezone = 'UTC';\n")
    counts = {"actors": 0, "follows": 0, "likes": 0}
    if manifest is not None:
        stream.write("""
CREATE TEMP TABLE seed_actors (
    did text NOT NULL, status text NOT NULL,
    status_at timestamptz, first_seen_at timestamptz NOT NULL
) ON COMMIT DROP;
CREATE TEMP TABLE seed_follows (
    follower_did text NOT NULL, subject_did text NOT NULL, rkey text NOT NULL,
    created_at timestamptz, indexed_at timestamptz NOT NULL, deleted_at timestamptz,
    seq bigint NOT NULL, CHECK (deleted_at IS NULL)
) ON COMMIT DROP;
CREATE TEMP TABLE seed_likes (
    liker_did text NOT NULL, rkey text NOT NULL, subject_did text NOT NULL,
    subject_rkey text NOT NULL, created_at timestamptz, indexed_at timestamptz NOT NULL
) ON COMMIT DROP;
""")
        counts["actors"] = copy_rows(
            stream,
            "seed_actors",
            actor_rows(
                seed_dir / "graph/actors.jsonl.gz",
                manifest["rebased"]["effective_capture_finished_at"],
            ),
        )
        counts["follows"] = copy_rows(
            stream, "seed_follows", follow_rows(seed_dir / "graph/follows.jsonl.gz")
        )
        counts["likes"] = copy_rows(stream, "seed_likes", like_rows(seed_dir / "likes.jsonl.gz"))
        if any(count != manifest["counts"][table] for table, count in counts.items()):
            raise GraphSeedError("rebased row counts do not match the graph manifest")

    # Explicitly include the other raw graph tables: old ingestion state or
    # test-created edges must not survive a fresh fixture load. No CASCADE,
    # so an unexpected schema dependency fails instead of deleting extra data.
    stream.write(f"TRUNCATE {RAW_TABLES} RESTART IDENTITY;\n")
    if manifest is not None:
        stream.write("""
INSERT INTO graph.actor (did, status, status_at, first_seen_at)
    SELECT did, status, status_at, first_seen_at FROM seed_actors;
ANALYZE graph.actor;
ANALYZE seed_follows;
ANALYZE seed_likes;
INSERT INTO graph.follow (follower_id, subject_id, rkey, created_at, indexed_at, deleted_at, seq)
    SELECT a.id, s.id, f.rkey, f.created_at, f.indexed_at, f.deleted_at, f.seq
    FROM seed_follows f JOIN graph.actor a ON a.did=f.follower_did
    JOIN graph.actor s ON s.did=f.subject_did;
INSERT INTO graph.collection (nsid) VALUES ('app.bsky.feed.post');
-- ES fixtures do not carry Jetstream sequence numbers. Zero is a local
-- fixture sentinel; the ingest cursor remains empty, not a production cursor.
INSERT INTO graph.likes (liker_id, rkey, subject_actor_id, subject_collection_id,
                         subject_rkey, created_at, indexed_at, deleted_at, seq)
    SELECT a.id, l.rkey, s.id, c.id, l.subject_rkey, l.created_at, l.indexed_at, NULL, 0
    FROM seed_likes l JOIN graph.actor a ON a.did=l.liker_did
    JOIN graph.actor s ON s.did=l.subject_did
    CROSS JOIN graph.collection c WHERE c.nsid='app.bsky.feed.post';
""")
    # Inner joins must not silently drop edges with a missing endpoint.
    for table, count in counts.items():
        name = "follow" if table == "follows" else "actor" if table == "actors" else table
        stream.write(f"""
DO $check$ BEGIN
    IF (SELECT count(*) FROM graph.{name}) <> {count} THEN
        RAISE EXCEPTION 'graph {table} count mismatch; an endpoint may be missing';
    END IF;
END; $check$;
""")
    stream.write("""
ANALYZE graph.follow;
ANALYZE graph.likes;
COMMIT;
SELECT json_build_object(
    'actors', (SELECT count(*) FROM graph.actor),
    'follows', (SELECT count(*) FROM graph.follow),
    'likes', (SELECT count(*) FROM graph.likes),
    'bytes', (SELECT COALESCE(sum(pg_total_relation_size(c.oid)),0)
              FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
              WHERE n.nspname='graph' AND c.relkind='r' AND c.relname<>'schema_migrations')
);
""")


def load_graph(fixture_dir: Path, seed_dir: Path, psql_command: Sequence[str]) -> dict:
    started = time.monotonic()
    manifest = prepare_seed(fixture_dir, seed_dir)
    print(
        "Loading local graph fixture..."
        if manifest
        else "No graph fixture; clearing local graph...",
        flush=True,
    )
    with tempfile.TemporaryFile(mode="w+") as output, tempfile.TemporaryFile(mode="w+") as errors:
        process = subprocess.Popen(
            psql_command,
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=errors,
            text=True,
            encoding="utf-8",
        )
        try:
            assert process.stdin is not None
            try:
                write_seed_sql(process.stdin, seed_dir, manifest)
                process.stdin.close()
            except BrokenPipeError:
                # Prefer PostgreSQL's actual constraint/COPY error to the
                # Python pipe error caused by psql exiting on that failure.
                pass
            returncode = process.wait()
            if returncode:
                errors.seek(0)
                raise GraphSeedError(f"PostgreSQL seed failed: {errors.read()[-8192:].strip()}")
            output.seek(0)
            result = json.loads(output.read())
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
    print(
        f"Graph seed complete: {result['actors']:,} actors, {result['follows']:,} follows, "
        f"{result['likes']:,} likes; {result['bytes'] / 2**20:.1f} MiB including indexes; "
        f"{time.monotonic() - started:.1f}s",
        flush=True,
    )
    return result


def main() -> None:
    try:
        load_graph(FIXTURES_DIR, SEED_DIR, PSQL_COMMAND)
    except (GraphSeedError, OSError, EOFError, ValueError, KeyError) as exc:
        sys.exit(f"FATAL: graph seed failed: {exc}")


if __name__ == "__main__":
    main()
