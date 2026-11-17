"""Opt-in loader checks against an isolated PostgreSQL with the real graph schema.

Run with GE_DEV_RUN_GRAPH_SEED_INTEGRATION=1. The container has no network or
published ports; its database volume is removed when the tests finish.
"""

import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
import zipfile
from contextlib import closing
from pathlib import Path

import load_graph
import pytest
import rebase

pytestmark = pytest.mark.skipif(
    os.environ.get("GE_DEV_RUN_GRAPH_SEED_INTEGRATION") != "1",
    reason="set GE_DEV_RUN_GRAPH_SEED_INTEGRATION=1 to run isolated PostgreSQL tests",
)

BASE_END = dt.datetime(2026, 9, 2, 12, tzinfo=dt.UTC)
CAPTURE_END = dt.datetime(2026, 11, 17, 18, tzinfo=dt.UTC)
EFFECTIVE_END = dt.datetime(2026, 10, 1, 12, tzinfo=dt.UTC)
SEEDED_AT = EFFECTIVE_END + dt.timedelta(hours=1)
TABLES = (
    "actor",
    "collection",
    "follow",
    "likes",
    "verification",
    "reposts",
    "ingest_cursor",
    "schema_migrations",
)


def _run(command, *, sql=None, check=True):
    return subprocess.run(
        command, input=sql, text=True, capture_output=True, check=check, timeout=30
    )


def _rows(command, query):
    sql = f"SELECT COALESCE(json_agg(q), '[]'::json) FROM ({query}) q;"
    return json.loads(_run(command, sql=sql).stdout)


def _database_state(command):
    entries = []
    for table in TABLES:
        entries.append(
            f"'{table}', (SELECT COALESCE(jsonb_agg(to_jsonb(r) ORDER BY to_jsonb(r)::text), "
            f"'[]'::jsonb) FROM graph.{table} r)"
        )
    return json.loads(_run(command, sql=f"SELECT jsonb_build_object({', '.join(entries)});").stdout)


@pytest.fixture(scope="module")
def postgres():
    assert shutil.which("docker"), "Docker is required for graph seed integration tests"
    migrations = Path(__file__).resolve().parents[3] / "bridg-eu/graph/internal/store/migrations"
    files = sorted(migrations.glob("*.sql"))
    assert files, f"the bridg-eu checkout and its migrations are required: {migrations}"
    name = f"graph-seed-test-{uuid.uuid4().hex[:12]}"
    volume = f"{name}-data"
    command = [
        "docker",
        "exec",
        "-i",
        name,
        "psql",
        "-X",
        "-qAt",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "graph",
        "-d",
        "graph",
    ]
    _run(["docker", "volume", "create", volume])
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--network",
                "none",
                "--mount",
                f"type=volume,source={volume},target=/var/lib/postgresql",
                "-e",
                "POSTGRES_DB=graph",
                "-e",
                "POSTGRES_USER=graph",
                "-e",
                "POSTGRES_PASSWORD=graph",
                "postgres:18",
            ],
            check=True,
            text=True,
            capture_output=True,
            timeout=120,
        )
        deadline = time.monotonic() + 60
        last_logs = ""
        while time.monotonic() < deadline:
            logs = _run(["docker", "logs", name])
            last_logs = logs.stdout + logs.stderr
            # The image briefly accepts connections during init, then restarts.
            # Wait for the final startup so migration connections survive it.
            if "PostgreSQL init process complete" in last_logs:
                if _run(command, sql="SELECT 1;", check=False).returncode == 0:
                    break
            time.sleep(0.2)
        else:
            pytest.fail(f"PostgreSQL did not finish initialization: {last_logs}")

        _run(
            command,
            sql="""
            CREATE SCHEMA graph;
            CREATE TABLE graph.schema_migrations (
                name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now()
            );
        """,
        )
        for migration in files:
            _run(
                command,
                sql=f"BEGIN;\n{migration.read_text()}\n"
                f"INSERT INTO graph.schema_migrations (name) VALUES ('{migration.name}');\nCOMMIT;",
            )
        yield command
    finally:
        _run(["docker", "rm", "-f", name], check=False)
        _run(["docker", "volume", "rm", volume], check=False)


def _write_rows(path, rows):
    with gzip.open(path, "wt") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def _read_rows(path):
    with gzip.open(path, "rt") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _fixture_pair(tmp_path, tag, *, with_graph=True):
    original, seeded = tmp_path / f"{tag}-original", tmp_path / f"{tag}-seeded"
    original.mkdir()
    seeded.mkdir()
    did = {
        key: f"did:plc:{tag}{key}" for key in ("viewer", "other", "author", "missing", "neighbor")
    }
    base_manifest = {
        "schema_version": 1,
        "source": "prod-es",
        "window_start": (BASE_END - dt.timedelta(days=1)).isoformat(),
        "window_end": BASE_END.isoformat(),
        "counts": {"posts": 2, "likes": 2},
        "personas": [{"did": did["viewer"]}],
        "dev_users": [],
    }
    (original / "manifest.json").write_text(json.dumps(base_manifest))
    posts = [
        f"at://{did[key]}/app.bsky.feed.post/post{number}"
        for number, key in enumerate(("author", "missing"))
    ]
    sqlite_path = tmp_path / f"{tag}.db"
    with closing(sqlite3.connect(sqlite_path)) as db:
        db.execute("CREATE TABLE enriched_posts (at_uri TEXT PRIMARY KEY, did TEXT)")
        db.executemany(
            "INSERT INTO enriched_posts VALUES (?, ?)",
            zip(posts, (did["author"], did["missing"]), strict=True),
        )
        db.commit()
    chunk = original / "mega_jetstream_20260902_120000.db.zip"
    with zipfile.ZipFile(chunk, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(sqlite_path, "posts.db")
    likes = [
        {
            "at_uri": f"at://{did[key]}/app.bsky.feed.like/like{number}",
            "author_did": did[key],
            "subject_uri": posts[number],
            "created_at": "2026-09-01T10:03:04.123456Z",
            "indexed_at": "2026-09-01T10:03:05.654321Z",
        }
        for number, key in enumerate(("viewer", "other"))
    ]
    _write_rows(original / "likes.jsonl.gz", likes)
    delta = EFFECTIVE_END - BASE_END
    rebase.rebase_jsonl(
        original / "likes.jsonl.gz",
        seeded / "likes.jsonl.gz",
        delta,
        ("created_at", "indexed_at"),
    )
    rebased = {
        "delta_seconds": delta.total_seconds(),
        "seeded_at": SEEDED_AT.isoformat(),
        "effective_window_end": EFFECTIVE_END.isoformat(),
    }
    (seeded / "manifest.json").write_text(json.dumps({**base_manifest, "rebased": rebased}))
    if with_graph:
        graph = original / "graph"
        graph.mkdir()
        actors = [
            {
                "did": actor_did,
                "status": None if key == "missing" else "active",
                "status_at": None,
                "first_seen_at": None if key == "missing" else "2025-12-01T00:00:00Z",
                "is_root": key in ("viewer", "other"),
                "in_graph": key != "missing",
            }
            for key, actor_did in did.items()
        ]
        follows = [
            {
                "follower_did": did[owner],
                "subject_did": did[target],
                # COPY delimiters and psql-looking text must remain data.
                "rkey": f"follow{number}\tpart\n\\.\n\\! false\\tail\r",
                "created_at": None,
                "indexed_at": (CAPTURE_END - dt.timedelta(hours=1)).isoformat(),
                "deleted_at": None,
                "seq": 42 + number,
            }
            for number, (owner, target) in enumerate((("viewer", "neighbor"), ("other", "author")))
        ]
        _write_rows(graph / "actors.jsonl.gz", actors)
        _write_rows(graph / "follows.jsonl.gz", follows)
        fingerprints = {}
        for path in (original / "manifest.json", original / "likes.jsonl.gz", chunk):
            with path.open("rb") as stream:
                fingerprints[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
        graph_manifest = {
            "schema_version": 1,
            "source": "postgres",
            "selection": {
                "roots": "likers-personas-dev-users",
                "direction": "outgoing",
                "hops": 1,
                "active_only": True,
            },
            "consistency": "per-query read-committed",
            "capture_started_at": (CAPTURE_END - dt.timedelta(minutes=5)).isoformat(),
            "capture_finished_at": CAPTURE_END.isoformat(),
            "base_files": fingerprints,
            "likes_file": "../likes.jsonl.gz",
            "counts": {"roots": 2, "actors": 5, "follows": 2, "likes": 2, "missing_roots": 0},
        }
        (graph / "manifest.json").write_text(json.dumps(graph_manifest))
        assert rebase.validate_graph_fixture(original) == []
        rebase.rebase_graph(original, seeded, EFFECTIVE_END, SEEDED_AT)
    return original, seeded


def _assert_counts(stats, actors, follows, likes):
    assert {key: stats[key] for key in ("actors", "follows", "likes")} == {
        "actors": actors,
        "follows": follows,
        "likes": likes,
    }


def _add_unseeded_rows(command):
    # A real database can also contain ingester state and tables absent from
    # the fixture. Replacing a fixture must not leave those relationships.
    _run(
        command,
        sql="""
        INSERT INTO graph.reposts
        SELECT liker_id, 'stale-repost', subject_actor_id, subject_collection_id,
               subject_rkey, created_at, indexed_at, deleted_at, seq FROM graph.likes LIMIT 1;
        INSERT INTO graph.verification
        SELECT follower_id, subject_id, 'stale-verification', 'old.example', 'Old',
               created_at, indexed_at, deleted_at, seq FROM graph.follow LIMIT 1;
        INSERT INTO graph.ingest_cursor (stream, seq) VALUES ('stale-stream', 123);
    """,
    )


def test_load_graph_maps_ids_and_times_and_can_be_repeated(postgres, tmp_path):
    original, seeded = _fixture_pair(tmp_path, "first")
    migrations = _database_state(postgres)["schema_migrations"]
    stats = load_graph.load_graph(original, seeded, postgres)
    _assert_counts(stats, 5, 2, 2)
    assert stats["bytes"] > 0
    rows = _rows(
        postgres,
        """
        SELECT owner.did AS owner, l.rkey, author.did AS author, c.nsid,
               l.subject_rkey, l.created_at, l.indexed_at, l.deleted_at, l.seq
        FROM graph.likes l
        JOIN graph.actor owner ON owner.id=l.liker_id
        JOIN graph.actor author ON author.id=l.subject_actor_id
        JOIN graph.collection c ON c.id=l.subject_collection_id ORDER BY l.rkey
    """,
    )
    for actual, expected in zip(rows, _read_rows(seeded / "likes.jsonl.gz"), strict=True):
        assert actual["owner"] == expected["author_did"]
        assert actual["rkey"] == expected["at_uri"].rsplit("/", 1)[-1]
        assert (
            f"at://{actual['author']}/{actual['nsid']}/{actual['subject_rkey']}"
            == expected["subject_uri"]
        )
        assert actual["seq"] == 0 and actual["deleted_at"] is None
        for field in ("created_at", "indexed_at"):
            assert rebase.parse_iso(actual[field]) == rebase.parse_iso(expected[field])
    missing = _rows(postgres, "SELECT * FROM graph.actor WHERE did='did:plc:firstmissing'")[0]
    assert missing["status"] == "unknown" and missing["status_at"] is None
    assert rebase.parse_iso(missing["first_seen_at"]) == EFFECTIVE_END
    follows = _rows(
        postgres,
        """
        SELECT owner.did AS follower_did, subject.did AS subject_did, f.rkey,
               f.created_at, f.indexed_at, f.deleted_at, f.seq
        FROM graph.follow f JOIN graph.actor owner ON owner.id=f.follower_id
        JOIN graph.actor subject ON subject.id=f.subject_id ORDER BY f.rkey
    """,
    )
    for actual, expected in zip(
        follows, _read_rows(seeded / "graph/follows.jsonl.gz"), strict=True
    ):
        assert rebase.parse_iso(actual.pop("indexed_at")) == rebase.parse_iso(
            expected.pop("indexed_at")
        )
        assert actual == expected
    first_state = _database_state(postgres)
    assert first_state["schema_migrations"] == migrations
    assert len(first_state["collection"]) == 1
    _assert_counts(load_graph.load_graph(original, seeded, postgres), 5, 2, 2)
    assert _database_state(postgres) == first_state


def test_replacement_clears_old_fixture_and_ingester_state(postgres, tmp_path):
    first, first_seed = _fixture_pair(tmp_path, "old")
    second, second_seed = _fixture_pair(tmp_path, "new")
    load_graph.load_graph(first, first_seed, postgres)
    _add_unseeded_rows(postgres)
    migrations = _database_state(postgres)["schema_migrations"]
    _assert_counts(load_graph.load_graph(second, second_seed, postgres), 5, 2, 2)
    state = _database_state(postgres)
    assert all(row["did"].startswith("did:plc:new") for row in state["actor"])
    assert len(state["actor"]) == 5 and len(state["likes"]) == len(state["follow"]) == 2
    assert state["verification"] == state["reposts"] == state["ingest_cursor"] == []
    assert state["schema_migrations"] == migrations


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("indexed_at", "not-a-timestamp", "timestamp"),
        ("subject_did", "did:plc:missingendpoint", "count mismatch"),
    ],
)
def test_invalid_rebased_rows_roll_back_entire_replacement(postgres, tmp_path, field, value, error):
    first, first_seed = _fixture_pair(tmp_path, "kept")
    load_graph.load_graph(first, first_seed, postgres)
    _add_unseeded_rows(postgres)
    before = _database_state(postgres)
    broken, broken_seed = _fixture_pair(tmp_path, "broken")
    path = broken_seed / "graph/follows.jsonl.gz"
    rows = _read_rows(path)
    rows[-1][field] = value
    _write_rows(path, rows)
    manifest_path = broken_seed / "graph/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    with path.open("rb") as stream:
        manifest["rebased"]["files_sha256"][path.name] = hashlib.file_digest(
            stream, "sha256"
        ).hexdigest()
    manifest_path.write_text(json.dumps(manifest))

    # These inputs have matching hashes so PostgreSQL exercises both failure
    # points: a typed COPY rejection, and a missing join after TRUNCATE.
    with pytest.raises(load_graph.GraphSeedError, match=error):
        load_graph.load_graph(broken, broken_seed, postgres)

    assert _database_state(postgres) == before


def test_graphless_fixture_clears_data_but_preserves_migrations(postgres, tmp_path):
    original, seeded = _fixture_pair(tmp_path, "present")
    load_graph.load_graph(original, seeded, postgres)
    _add_unseeded_rows(postgres)
    migrations = _database_state(postgres)["schema_migrations"]
    legacy, legacy_seed = _fixture_pair(tmp_path, "legacy", with_graph=False)

    _assert_counts(load_graph.load_graph(legacy, legacy_seed, postgres), 0, 0, 0)

    state = _database_state(postgres)
    assert state["schema_migrations"] == migrations
    assert all(not state[table] for table in TABLES if table != "schema_migrations")
