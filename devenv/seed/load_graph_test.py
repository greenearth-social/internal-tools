import datetime as dt
import gzip
import io
import json
import os

import load_graph as lg
import pytest


def write_rows(path, rows):
    with gzip.open(path, "wt") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


@pytest.fixture
def seed_pair(tmp_path, monkeypatch):
    # The shared validator has its own fixture-integrity tests. These tests
    # focus on pairing the original fixture with the derived runtime files.
    monkeypatch.setattr(lg, "validate_graph_fixture", lambda path: [])
    source, seed = tmp_path / "source", tmp_path / "seed"
    (source / "graph").mkdir(parents=True)
    (seed / "graph").mkdir(parents=True)
    base = {"source": "prod-es", "window_end": "2026-09-14T18:00:00Z"}
    anchor = "2026-11-18T11:00:00+00:00"
    seeded_at = "2026-11-18T12:00:00+00:00"
    (source / "manifest.json").write_text(json.dumps(base))
    (seed / "manifest.json").write_text(
        json.dumps(
            {
                **base,
                "rebased": {
                    "delta_seconds": 5590800,
                    "seeded_at": seeded_at,
                    "effective_window_end": anchor,
                },
            }
        )
    )
    write_rows(seed / "likes.jsonl.gz", [{"created_at": anchor}])
    graph = {
        "capture_finished_at": "2026-11-17T18:45:40.549343Z",
        "counts": {"actors": 2, "follows": 1, "likes": 1},
    }
    delta = lg.parse_timestamp(anchor) - lg.parse_timestamp(graph["capture_finished_at"])
    (source / "graph/manifest.json").write_text(json.dumps(graph))
    graph_hashes = {}
    for name in ("actors.jsonl.gz", "follows.jsonl.gz"):
        path = seed / "graph" / name
        write_rows(path, [])
        graph_hashes[name] = lg.file_sha256(path)
    (seed / "graph/manifest.json").write_text(
        json.dumps(
            {
                **graph,
                "rebased": {
                    "delta_seconds": delta.total_seconds(),
                    "seeded_at": seeded_at,
                    "effective_capture_finished_at": anchor,
                    "likes_sha256": lg.file_sha256(seed / "likes.jsonl.gz"),
                    "files_sha256": graph_hashes,
                },
            }
        )
    )
    return source, seed


def test_prepare_seed_accepts_distinct_base_and_graph_timestamp_offsets(seed_pair):
    manifest = lg.prepare_seed(*seed_pair)
    assert manifest is not None
    assert manifest["rebased"]["delta_seconds"] == pytest.approx(58459.450657)


def test_original_validation_failure_stops_before_database_connection(seed_pair, monkeypatch):
    source, seed = seed_pair
    monkeypatch.setattr(lg, "validate_graph_fixture", lambda path: ["capture fingerprints differ"])

    def forbidden(*args, **kwargs):
        pytest.fail("must not connect before validating")

    monkeypatch.setattr(lg.subprocess, "Popen", forbidden)
    with pytest.raises(lg.GraphSeedError, match="fingerprints"):
        lg.load_graph(source, seed, ["psql"])


@pytest.mark.parametrize("name", ["manifest.json", "graph/manifest.json"])
def test_stale_rebased_manifest_is_rejected(seed_pair, name):
    source, seed = seed_pair
    path = seed / name
    value = json.loads(path.read_text())
    value["different_fixture"] = True
    path.write_text(json.dumps(value))
    with pytest.raises(lg.GraphSeedError, match="does not match"):
        lg.prepare_seed(source, seed)


@pytest.mark.parametrize("field", ["seeded_at", "delta_seconds", "effective_capture_finished_at"])
def test_separate_rebasing_runs_cannot_be_mixed(seed_pair, field):
    source, seed = seed_pair
    path = seed / "graph/manifest.json"
    manifest = json.loads(path.read_text())
    if field == "delta_seconds":
        manifest["rebased"][field] += 60
    else:
        parsed = lg.parse_timestamp(manifest["rebased"][field]) + dt.timedelta(minutes=1)
        manifest["rebased"][field] = parsed.isoformat()
    path.write_text(json.dumps(manifest))
    with pytest.raises(lg.GraphSeedError, match="different times"):
        lg.prepare_seed(source, seed)


def test_changed_shared_likes_are_rejected(seed_pair):
    source, seed = seed_pair
    write_rows(seed / "likes.jsonl.gz", [{"created_at": "2026-10-01T00:00:00Z"}])
    with pytest.raises(lg.GraphSeedError, match="likes changed"):
        lg.prepare_seed(source, seed)


@pytest.mark.parametrize("name", ["actors.jsonl.gz", "follows.jsonl.gz"])
def test_stale_graph_payloads_with_matching_manifests_are_rejected(seed_pair, name):
    source, seed = seed_pair
    write_rows(seed / "graph" / name, [{"different_capture": True}])
    with pytest.raises(lg.GraphSeedError, match="rebased graph .* changed"):
        lg.prepare_seed(source, seed)


def test_graphless_source_requires_graphless_runtime(seed_pair):
    source, seed = seed_pair
    (source / "graph/manifest.json").unlink()
    (source / "graph").rmdir()
    with pytest.raises(lg.GraphSeedError, match="stale rebased graph"):
        lg.prepare_seed(source, seed)
    for path in (seed / "graph").iterdir():
        path.unlink()
    (seed / "graph").rmdir()
    assert lg.prepare_seed(source, seed) is None


def test_copy_data_escapes_null_marker_tabs_newlines_and_psql_commands():
    stream = io.StringIO()
    lg.copy_rows(stream, "seed_actors", [(None, "\\N", "a\tb\nc\rd", "\\.\n\\! touch bad")])
    assert stream.getvalue() == (
        "COPY seed_actors FROM STDIN;\n\\N\t\\\\N\ta\\tb\\nc\\rd\t\\\\.\\n\\\\! touch bad\n\\.\n"
    )


def test_missing_actor_uses_unknown_status_and_effective_observation_time(tmp_path):
    path = tmp_path / "actors.jsonl.gz"
    write_rows(
        path,
        [
            {
                "did": "did:plc:missing",
                "status": None,
                "status_at": None,
                "first_seen_at": None,
                "in_graph": False,
            }
        ],
    )
    anchor = "2026-11-18T11:00:00Z"
    assert list(lg.actor_rows(path, anchor)) == [("did:plc:missing", "unknown", None, anchor)]


def test_like_conversion_preserves_the_shared_file_timestamps(tmp_path):
    path = tmp_path / "likes.jsonl.gz"
    write_rows(
        path,
        [
            {
                "at_uri": "at://did:plc:liker/app.bsky.feed.like/abc",
                "subject_uri": "at://did:plc:author/app.bsky.feed.post/def",
                "author_did": "did:plc:liker",
                "created_at": "2026-11-18T09:20:00.123456Z",
                "indexed_at": "2026-11-18T09:20:01.654321Z",
            }
        ],
    )
    assert list(lg.like_rows(path)) == [
        (
            "did:plc:liker",
            "abc",
            "did:plc:author",
            "def",
            "2026-11-18T09:20:00.123456Z",
            "2026-11-18T09:20:01.654321Z",
        )
    ]


def test_production_environment_cannot_change_default_local_connection(monkeypatch):
    monkeypatch.setenv("GRAPH_DATABASE_URL", "postgres://production.example/bridgeu")
    monkeypatch.setenv("PGHOST", "production.example")
    assert (
        "--dbname=postgres://graph:graph@bridgeu-postgres:5432/graph?sslmode=disable"
        in lg.PSQL_COMMAND
    )
    assert os.environ["GRAPH_DATABASE_URL"] not in " ".join(lg.PSQL_COMMAND)
