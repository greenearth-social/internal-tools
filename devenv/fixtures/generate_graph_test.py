import gzip
import json
import re
import sqlite3
import zipfile
from pathlib import Path

import generate_graph as gg
import pytest
from graph_fixture import base_fingerprints, validate_graph_fixture
from graph_source import GraphSourceError

AUTHOR = "did:plc:author"
LIKER = "did:plc:liker"
PERSONA = "did:plc:persona"
DEV = "did:plc:dev"
NEIGHBOR = "did:plc:neighbor"
STAMP = "2026-09-12T10:00:00Z"
POST = f"at://{AUTHOR}/app.bsky.feed.post/post1"


def write_gzip(path, rows):
    with gzip.open(path, "wt") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


@pytest.fixture
def fixture(tmp_path):
    manifest = {
        "source": "prod-es",
        "window_end": STAMP,
        "counts": {"posts": 1, "likes": 1},
        "personas": [{"did": PERSONA}],
        "dev_users": [{"did": DEV, "likes": 0}],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with sqlite3.connect(tmp_path / "posts.db") as db:
        db.execute("CREATE TABLE enriched_posts (at_uri TEXT, did TEXT)")
        db.execute("INSERT INTO enriched_posts VALUES (?, ?)", (POST, AUTHOR))
    with zipfile.ZipFile(tmp_path / "mega_jetstream_20260912_100000.db.zip", "w") as archive:
        archive.write(tmp_path / "posts.db", "posts.db")
    (tmp_path / "posts.db").unlink()
    write_gzip(
        tmp_path / "likes.jsonl.gz",
        [
            {
                "at_uri": f"at://{LIKER}/app.bsky.feed.like/like1",
                "subject_uri": POST,
                "author_did": LIKER,
                "created_at": STAMP,
                "indexed_at": STAMP,
            }
        ],
    )
    return tmp_path


def actor(did, actor_id):
    return {
        "id": actor_id,
        "did": did,
        "status": "active",
        "status_at": None,
        "first_seen_at": STAMP,
    }


def edge(follower_id, subject, rkey):
    return {
        "follower_id": follower_id,
        "follow": {
            "subject_did": subject["did"],
            "rkey": rkey,
            "created_at": None,
            "indexed_at": STAMP,
            "deleted_at": None,
            "seq": 123,
        },
        "subject": {k: v for k, v in subject.items() if k != "id"},
    }


class Source:
    """Scripted pages record the actual SQL so limits/roots can be checked."""

    def __init__(self, pages):
        self.pages = iter(pages)
        self.queries = []
        self.closed = 0

    def query(self, sql):
        self.queries.append(sql)
        try:
            for row in next(self.pages):
                if isinstance(row, Exception):
                    raise row
                yield row
        finally:
            self.closed += 1


def read_gzip(path):
    with gzip.open(path, "rt") as stream:
        return [json.loads(line) for line in stream]


def test_keeps_post_authors_as_actors_but_only_expands_likers_and_manifest_users(fixture):
    expected = ({AUTHOR, LIKER, PERSONA, DEV}, {LIKER, PERSONA, DEV}, 1)
    assert gg.fixture_users(fixture) == expected
    # Raw SQLite with the archive suffix is another supported ingest format.
    chunk = next(fixture.glob("*.db.zip"))
    with zipfile.ZipFile(chunk) as archive:
        data = archive.read("posts.db")
    chunk.write_bytes(data)
    assert gg.fixture_users(fixture) == expected


def test_extract_pages_all_selected_follows_without_expanding_authors_or_neighbors(fixture, capsys):
    before = base_fingerprints(fixture)
    source = Source(
        [
            [actor(AUTHOR, 10), actor(LIKER, 20), actor(DEV, 30)],
            [edge(20, actor(NEIGHBOR, 999), "a"), edge(20, actor(AUTHOR, 10), "b")],
            [edge(30, actor(NEIGHBOR, 999), "c")],
        ]
    )
    result = gg.extract_graph(fixture, source, batch_size=100, page_size=2)
    assert result["counts"] == {
        "roots": 3,
        "actors": 5,
        "follows": 3,
        "likes": 1,
        "missing_roots": 1,
    }
    assert result["base_files"] == before == base_fingerprints(fixture)
    assert result["likes_file"] == "../likes.jsonl.gz"
    assert result["selection"]["roots"] == "likers-personas-dev-users"
    assert result["capture_started_at"] <= result["capture_finished_at"]
    assert source.closed == 3
    for sql in source.queries[1:]:
        assert "ARRAY[20,30]" in sql
        assert "999" not in sql
        assert "f.deleted_at IS NULL" in sql
        assert "LIMIT 2" in sql
    assert "(20," in source.queries[2]
    assert gg.sql_text("b") in source.queries[2]
    actors = {a["did"]: a for a in read_gzip(fixture / "graph/actors.jsonl.gz")}
    assert actors[NEIGHBOR]["is_root"] is False
    assert actors[AUTHOR]["is_root"] is False
    assert actors[AUTHOR]["in_graph"] is True
    assert actors[DEV]["is_root"] is True
    assert actors[PERSONA] == {
        "did": PERSONA,
        "status": None,
        "status_at": None,
        "first_seen_at": None,
        "is_root": True,
        "in_graph": False,
    }
    follows = read_gzip(fixture / "graph/follows.jsonl.gz")
    assert [f["follower_did"] for f in follows] == [LIKER, LIKER, DEV]
    assert all(f["created_at"] is None and f["seq"] == 123 for f in follows)
    assert all("id" not in a for a in actors.values())
    assert not validate_graph_fixture(fixture)
    assert list(fixture.glob(".graph-extract-*")) == []
    output = capsys.readouterr().out
    assert "Graph follow roots: 3" in output
    assert "looked up 4/4 fixture actors" in output


def test_author_who_is_also_a_manifest_user_is_expanded_once(fixture):
    path = fixture / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["personas"] += [{"did": AUTHOR}, {"did": LIKER}, {"did": DEV}]
    manifest["dev_users"].append({"did": AUTHOR})
    path.write_text(json.dumps(manifest))
    assert gg.fixture_users(fixture) == (
        {AUTHOR, LIKER, PERSONA, DEV},
        {AUTHOR, LIKER, PERSONA, DEV},
        1,
    )
    source = Source([[actor(AUTHOR, 10)], [edge(10, actor(NEIGHBOR, 999), "a")]])
    result = gg.extract_graph(fixture, source, batch_size=10, page_size=10)
    assert result["counts"]["roots"] == 4
    assert result["counts"]["follows"] == 1
    assert "ARRAY[10]" in source.queries[1]


def test_missing_post_author_is_retained_without_counting_as_a_missing_root(fixture):
    source = Source([[actor(LIKER, 20), actor(DEV, 30), actor(PERSONA, 40)], []])
    result = gg.extract_graph(fixture, source, batch_size=10, page_size=10)
    assert result["counts"]["missing_roots"] == 0
    actors = {a["did"]: a for a in read_gzip(fixture / "graph/actors.jsonl.gz")}
    assert actors[AUTHOR] == {
        "did": AUTHOR,
        "status": None,
        "status_at": None,
        "first_seen_at": None,
        "is_root": False,
        "in_graph": False,
    }
    assert not validate_graph_fixture(fixture)


def test_actor_batches_deduplicate_roots_and_keep_existing_zero_follow_actors(fixture):
    source = Source([[actor(AUTHOR, 10)], [actor(LIKER, 20)], [], []])
    result = gg.extract_graph(fixture, source, batch_size=2, page_size=2)
    assert result["counts"]["follows"] == 0
    assert result["counts"]["actors"] == 4
    assert result["counts"]["missing_roots"] == 2
    requested = []
    for sql in source.queries[:2]:
        encoded = re.search(r"decode\('([a-f0-9]+)'", sql)
        assert encoded is not None
        requested += json.loads(bytes.fromhex(encoded[1]).decode())
    assert requested == sorted({AUTHOR, LIKER, DEV, PERSONA})


def test_all_missing_roots_are_reported_without_querying_follows(fixture):
    source = Source([[]])
    result = gg.extract_graph(fixture, source, batch_size=10, page_size=10)
    assert result["counts"]["missing_roots"] == 3
    assert result["counts"]["follows"] == 0
    assert len(source.queries) == 1


@pytest.mark.parametrize("failure", [GraphSourceError("timeout"), ValueError("invalid JSON")])
def test_failed_partial_capture_keeps_previous_graph_and_base_untouched(fixture, failure):
    previous = fixture / "graph"
    previous.mkdir()
    (previous / "sentinel").write_text("previous complete capture")
    before = base_fingerprints(fixture)
    source = Source([[actor(LIKER, 20)], [edge(20, actor(NEIGHBOR, 999), "a"), failure]])
    with pytest.raises(type(failure), match=str(failure)):
        gg.extract_graph(fixture, source, batch_size=10, page_size=10)
    assert (previous / "sentinel").read_text() == "previous complete capture"
    assert base_fingerprints(fixture) == before
    assert list(fixture.glob(".graph-extract-*")) == []
    assert source.closed == 2


def test_success_replaces_previous_graph(fixture):
    (fixture / "graph").mkdir()
    (fixture / "graph/old-file").touch()
    gg.extract_graph(fixture, Source([[]]), batch_size=10, page_size=10)
    assert not (fixture / "graph/old-file").exists()
    assert (fixture / "graph/manifest.json").exists()


@pytest.mark.parametrize("failure", [OSError("rename failed"), KeyboardInterrupt()])
def test_failed_install_restores_previous_capture(fixture, monkeypatch, failure):
    (fixture / "graph").mkdir()
    (fixture / "graph/sentinel").write_text("previous complete capture")
    original_rename = Path.rename

    def fail_install(path, target):
        if path.name == "graph" and path.parent.name.startswith(".graph-extract-"):
            raise failure
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_install)
    with pytest.raises(type(failure)):
        gg.extract_graph(fixture, Source([[]]), batch_size=10, page_size=10)
    assert (fixture / "graph/sentinel").read_text() == "previous complete capture"
    assert list(fixture.glob(".graph-extract-*")) == []


def test_modified_base_is_not_paired_with_capture(fixture):
    class MutatingSource(Source):
        def query(self, sql):
            (fixture / "manifest.json").write_text("{}")
            yield from super().query(sql)

    with pytest.raises(ValueError, match="base fixture changed"):
        gg.extract_graph(fixture, MutatingSource([[]]), batch_size=10, page_size=10)
    assert not (fixture / "graph").exists()


@pytest.mark.parametrize("key,value", [("source", "megastream-files"), ("rebased", {"x": 1})])
def test_rejects_synthetic_or_rebased_fixture_before_remote_access(fixture, key, value):
    manifest = json.loads((fixture / "manifest.json").read_text())
    manifest[key] = value
    (fixture / "manifest.json").write_text(json.dumps(manifest))
    source = Source([])
    with pytest.raises(ValueError):
        gg.extract_graph(fixture, source, batch_size=10, page_size=10)
    assert not source.queries


def test_rejects_dangling_likes_before_remote_access(fixture):
    likes = read_gzip(fixture / "likes.jsonl.gz")
    likes[0]["subject_uri"] = POST + "missing"
    write_gzip(fixture / "likes.jsonl.gz", likes)
    source = Source([])
    with pytest.raises(ValueError, match="outside the fixture"):
        gg.extract_graph(fixture, source, batch_size=10, page_size=10)
    assert not source.queries


@pytest.mark.parametrize("change", ["neighbor_root", "author_root", "deleted", "duplicate"])
def test_unexpected_follow_rows_abort_capture(fixture, change):
    row = edge(20, actor(NEIGHBOR, 999), "a")
    page = [row]
    if change == "neighbor_root":
        row["follower_id"] = 999
    elif change == "author_root":
        row["follower_id"] = 10
    elif change == "deleted":
        row["follow"]["deleted_at"] = STAMP
    else:
        page.append(row)
    source = Source([[actor(AUTHOR, 10), actor(LIKER, 20)], page])
    with pytest.raises(ValueError):
        gg.extract_graph(fixture, source, batch_size=10, page_size=10)
    assert not (fixture / "graph").exists()
    assert source.closed == 2


def test_sql_data_cannot_introduce_sql_or_psql_commands():
    hostile = "did:plc:a'); COMMIT;\n\\! touch /tmp/executed\n\\copy x to program 'bad'"
    encoded = gg.sql_text(hostile)
    assert hostile not in gg.actor_query([hostile])
    assert "\\" not in encoded and "\n" not in encoded
    assert encoded == f"convert_from(decode('{hostile.encode().hex()}', 'hex'), 'UTF8')"
