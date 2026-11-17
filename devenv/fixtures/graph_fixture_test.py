import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

import graph_fixture as gf
import pytest

STAMP = "2026-11-16T12:00:00Z"


def write_rows(path, rows):
    with gzip.open(path, "wt") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def read_rows(path):
    with gzip.open(path, "rt") as stream:
        return [json.loads(line) for line in stream]


def graph_fixture(tmp_path):
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "source": "prod-es",
                "personas": [{"did": "did:plc:persona"}],
                "dev_users": [{"did": "did:plc:missing"}],
            }
        )
    )
    (tmp_path / "posts.db.zip").write_bytes(b"base post chunk; verifier hashes without opening it")
    write_rows(
        tmp_path / "likes.jsonl.gz",
        [
            {
                "at_uri": "at://did:plc:a/app.bsky.feed.like/l1",
                "author_did": "did:plc:a",
                "subject_uri": "at://did:plc:b/app.bsky.feed.post/p1",
            }
        ],
    )
    graph = tmp_path / "graph"
    graph.mkdir()
    actors = [
        {
            "did": f"did:plc:{name}",
            "status": "active",
            "status_at": None,
            "first_seen_at": STAMP,
            "is_root": name in ("a", "persona"),
            "in_graph": True,
        }
        for name in ("a", "b", "persona", "neighbor")
    ]
    actors.append(
        {
            "did": "did:plc:missing",
            "status": None,
            "status_at": None,
            "first_seen_at": None,
            "is_root": True,
            "in_graph": False,
        }
    )
    write_rows(graph / "actors.jsonl.gz", actors)
    write_rows(
        graph / "follows.jsonl.gz",
        [
            {
                "follower_did": "did:plc:a",
                "subject_did": "did:plc:neighbor",
                "rkey": "f1",
                "created_at": None,
                "indexed_at": STAMP,
                "deleted_at": None,
                "seq": 42,
            }
        ],
    )
    manifest = {
        "schema_version": 1,
        "source": "postgres",
        "selection": {
            "roots": "likers-personas-dev-users",
            "direction": "outgoing",
            "hops": 1,
            "active_only": True,
        },
        "capture_started_at": STAMP,
        "capture_finished_at": STAMP,
        "base_files": gf.base_fingerprints(tmp_path),
        "likes_file": "../likes.jsonl.gz",
        "counts": {"roots": 3, "actors": 5, "follows": 1, "likes": 1, "missing_roots": 1},
    }
    (graph / "manifest.json").write_text(json.dumps(manifest))
    return graph


def test_valid_graph_preserves_missing_roots_and_portable_identities(tmp_path):
    graph_fixture(tmp_path)
    assert gf.validate_graph_fixture(tmp_path) == []


def test_legacy_fixture_needs_no_graph_files(tmp_path):
    assert gf.validate_graph_fixture(tmp_path) == []


def test_base_fingerprints_follow_staging_symlinks(tmp_path):
    graph = graph_fixture(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    for name in gf.base_fingerprints(tmp_path):
        (staging / name).symlink_to(tmp_path / name)
    (staging / "graph").symlink_to(graph)
    assert gf.base_fingerprints(staging) == gf.base_fingerprints(tmp_path)
    assert gf.validate_graph_fixture(staging) == []


@pytest.mark.parametrize("name", ["manifest.json", "likes.jsonl.gz", "posts.db.zip"])
def test_base_file_changes_invalidate_graph(tmp_path, name):
    graph_fixture(tmp_path)
    with (tmp_path / name).open("ab") as stream:
        stream.write(b"changed")
    assert "fingerprints" in gf.validate_graph_fixture(tmp_path)[0]


def test_extra_base_chunk_invalidates_graph(tmp_path):
    graph_fixture(tmp_path)
    (tmp_path / "another.db.zip").write_bytes(b"new")
    assert "fingerprints" in gf.validate_graph_fixture(tmp_path)[0]


@pytest.mark.parametrize("name", ["manifest.json", "actors.jsonl.gz", "follows.jsonl.gz"])
def test_partial_graph_directory_is_rejected(tmp_path, name):
    graph = graph_fixture(tmp_path)
    (graph / name).unlink()
    assert gf.validate_graph_fixture(tmp_path)


@pytest.mark.parametrize("name", ["actors.jsonl.gz", "follows.jsonl.gz"])
def test_truncated_gzip_is_rejected(tmp_path, name):
    graph = graph_fixture(tmp_path)
    path = graph / name
    path.write_bytes(path.read_bytes()[:-5])
    assert gf.validate_graph_fixture(tmp_path)


@pytest.mark.parametrize("payload", ["{", "[]\n", '"row"\n'])
def test_malformed_json_rows_are_rejected(tmp_path, payload):
    graph = graph_fixture(tmp_path)
    with gzip.open(graph / "actors.jsonl.gz", "wt") as stream:
        stream.write(payload)
    assert gf.validate_graph_fixture(tmp_path)


@pytest.mark.parametrize("name", ["actors", "follows"])
def test_duplicate_actor_or_follow_primary_key_is_rejected(tmp_path, name):
    graph = graph_fixture(tmp_path)
    path = graph / f"{name}.jsonl.gz"
    rows = read_rows(path)
    write_rows(path, [*rows, rows[0]])
    assert "duplicate" in gf.validate_graph_fixture(tmp_path)[0]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("subject_did", "did:plc:absent", "subject is not a present actor"),
        ("follower_did", "did:plc:neighbor", "owner must be a present root"),
        ("follower_did", "did:plc:b", "owner must be a present root"),
        ("follower_did", "did:plc:missing", "owner must be a present root"),
        ("deleted_at", STAMP, "deleted follow"),
        ("indexed_at", None, "expected a timestamp"),
        ("seq", True, "seq must be"),
        ("follower_id", 12, "fields do not match"),
    ],
)
def test_invalid_follow_and_second_hop_are_rejected(tmp_path, field, value, message):
    graph = graph_fixture(tmp_path)
    path = graph / "follows.jsonl.gz"
    rows = read_rows(path)
    rows[0][field] = value
    write_rows(path, rows)
    assert message in gf.validate_graph_fixture(tmp_path)[0]


def test_missing_actor_metadata_cannot_claim_a_production_status(tmp_path):
    graph = graph_fixture(tmp_path)
    path = graph / "actors.jsonl.gz"
    rows = read_rows(path)
    rows[-1]["status"] = "active"
    write_rows(path, rows)
    assert "missing actors must have null metadata" in gf.validate_graph_fixture(tmp_path)[0]


def test_like_subject_author_must_be_in_actors(tmp_path):
    graph = graph_fixture(tmp_path)
    path = graph / "actors.jsonl.gz"
    write_rows(path, [row for row in read_rows(path) if row["did"] != "did:plc:b"])
    assert "like subject author is not a fixture actor" in gf.validate_graph_fixture(tmp_path)[0]


def test_nonroot_post_author_is_retained_even_when_missing_from_production(tmp_path):
    graph = graph_fixture(tmp_path)
    path = graph / "actors.jsonl.gz"
    actors = read_rows(path)
    author = next(actor for actor in actors if actor["did"] == "did:plc:b")
    assert not author["is_root"]
    author.update(in_graph=False, status=None, status_at=None, first_seen_at=None)
    write_rows(path, actors)
    # Only the missing dev user contributes to missing_roots; the post author
    # remains available as the saved like's endpoint without expanding roots.
    assert json.loads((graph / "manifest.json").read_text())["counts"]["missing_roots"] == 1
    assert gf.validate_graph_fixture(tmp_path) == []


@pytest.mark.parametrize("source", ["likes", "personas", "dev_users"])
def test_post_author_can_own_follows_when_also_a_selected_user(tmp_path, source):
    graph = graph_fixture(tmp_path)
    manifest_path = graph / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if source == "likes":
        likes_path = tmp_path / "likes.jsonl.gz"
        likes = read_rows(likes_path)
        likes.append(
            {
                "at_uri": "at://did:plc:b/app.bsky.feed.like/l2",
                "author_did": "did:plc:b",
                "subject_uri": "at://did:plc:a/app.bsky.feed.post/p2",
            }
        )
        write_rows(likes_path, likes)
        manifest["counts"]["likes"] += 1
    else:
        base_path = tmp_path / "manifest.json"
        base = json.loads(base_path.read_text())
        base[source].append({"did": "did:plc:b"})
        base_path.write_text(json.dumps(base))
    actors_path = graph / "actors.jsonl.gz"
    actors = read_rows(actors_path)
    next(actor for actor in actors if actor["did"] == "did:plc:b")["is_root"] = True
    write_rows(actors_path, actors)
    follows_path = graph / "follows.jsonl.gz"
    follows = read_rows(follows_path)
    follows[0]["follower_did"] = "did:plc:b"
    write_rows(follows_path, follows)
    manifest["counts"]["roots"] += 1
    manifest["base_files"] = gf.base_fingerprints(tmp_path)
    manifest_path.write_text(json.dumps(manifest))
    assert gf.validate_graph_fixture(tmp_path) == []


@pytest.mark.parametrize(
    ("did", "is_root", "message"),
    [
        ("did:plc:b", True, "0 missing, 1 unexpected"),
        ("did:plc:persona", False, "1 missing, 0 unexpected"),
        ("did:plc:missing", False, "1 missing, 0 unexpected"),
    ],
)
def test_root_membership_must_exactly_match_selected_users(tmp_path, did, is_root, message):
    graph = graph_fixture(tmp_path)
    path = graph / "actors.jsonl.gz"
    actors = read_rows(path)
    next(actor for actor in actors if actor["did"] == did)["is_root"] = is_root
    write_rows(path, actors)
    assert message in gf.validate_graph_fixture(tmp_path)[0]


@pytest.mark.parametrize("name", ["roots", "actors", "follows", "likes", "missing_roots"])
def test_counts_are_checked_against_files(tmp_path, name):
    graph = graph_fixture(tmp_path)
    path = graph / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["counts"][name] += 1
    path.write_text(json.dumps(manifest))
    assert f"graph {name} count" in gf.validate_graph_fixture(tmp_path)[0]


def test_cli_fails_without_network_for_invalid_graph(tmp_path, monkeypatch, capsys):
    (tmp_path / "graph").mkdir()
    monkeypatch.setattr(sys, "argv", ["graph_fixture.py", str(tmp_path)])
    assert gf.main() == 1
    assert "FAIL: graph fixture" in capsys.readouterr().out


def test_publish_refuses_invalid_graph_before_calling_github(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "manifest.json").write_text("{}")
    (fixture / "graph").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh-called"
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\ntouch "{log}"\n')
    gh.chmod(0o755)
    result = subprocess.run(
        ["bash", str(Path(__file__).with_name("publish_fixture.sh")), str(fixture)],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "graph fixture verification failed" in result.stdout + result.stderr
    assert not log.exists()
