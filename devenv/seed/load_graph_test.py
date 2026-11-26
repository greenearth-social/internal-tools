import gzip
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import load_graph as loader
import pytest

FILES = {
    "actors": "graph_actors.jsonl.gz",
    "follows": "graph_follows.jsonl.gz",
    "likes": "graph_likes.jsonl.gz",
}
ACTOR = {"did": "did:plc:a", "status": "active"}
FOLLOW = {
    "follower_did": "did:plc:a",
    "subject_did": "did:plc:b",
    "rkey": "f1",
    "created_at": None,
    "indexed_at": "2026-11-25T10:00:00Z",
}
LIKE = {
    "liker_did": "did:plc:a",
    "rkey": "l1",
    "subject_uri": "at://did:plc:b/app.bsky.feed.post/p1",
    "created_at": "2026-11-25T10:00:00+00:00",
    "indexed_at": "2026-11-25T10:00:01Z",
}


def _write(path, rows):
    with gzip.open(path, "wt") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _seed(tmp_path, *, graph=True, counts=None):
    _write(tmp_path / FILES["actors"], [ACTOR])
    _write(tmp_path / FILES["follows"], [FOLLOW])
    _write(tmp_path / FILES["likes"], [LIKE])
    manifest = {}
    if graph:
        manifest["graph"] = {
            "files": FILES,
            "counts": counts or {"actors": 1, "follows": 1, "likes": 1},
        }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path


def _database(count=1):
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.execute.return_value.fetchone.return_value = (count,)
    copy = cursor.copy.return_value.__enter__.return_value
    return conn, cursor, copy


def test_read_rows_orders_columns_and_parses_times(tmp_path):
    _write(tmp_path / "f.gz", [FOLLOW])
    _, columns, times = loader.STAGES["follows"]
    assert list(loader.read_rows(tmp_path / "f.gz", columns, times)) == [
        ("did:plc:a", "did:plc:b", "f1", None, datetime(2026, 11, 25, 10, tzinfo=UTC))
    ]


@pytest.mark.parametrize(
    ("row", "error"),
    [
        ({**LIKE, "extra": 1}, "expected the fields"),
        ({**LIKE, "rkey": ""}, "rkey must be a nonempty string"),
        ({**LIKE, "subject_uri": "https://x"}, "AT-URI"),
        ({**LIKE, "created_at": "2026-11-25T10:00:00"}, "timezone"),
    ],
)
def test_read_rows_rejects_bad_rows_with_their_line(tmp_path, row, error):
    _write(tmp_path / "l.gz", [LIKE, row])
    _, columns, times = loader.STAGES["likes"]
    with pytest.raises(ValueError, match=f"line 2: .*{error}"):
        list(loader.read_rows(tmp_path / "l.gz", columns, times))


def test_load_graph_stages_loads_and_checks_counts_in_one_transaction(tmp_path):
    conn, cursor, copy = _database()

    loaded = loader.load_graph(conn, _seed(tmp_path))

    assert loaded == {"actors": 1, "follows": 1, "likes": 1}
    conn.transaction.assert_called_once()
    statements = [c.args[0] for c in cursor.execute.call_args_list]
    assert statements[0] == loader.TRUNCATE_SQL
    assert statements[1] == loader.STAGE_DDL
    assert statements[2] == loader.LOAD_SQL
    assert [c.args[0] for c in cursor.copy.call_args_list] == [
        "COPY stage_actor (did, status) FROM STDIN",
        "COPY stage_follow (follower_did, subject_did, rkey, created_at, indexed_at) FROM STDIN",
        "COPY stage_like (liker_did, rkey, subject_uri, created_at, indexed_at) FROM STDIN",
    ]
    assert copy.write_row.call_count == 3


def test_load_graph_fails_inside_the_transaction_when_counts_disagree(tmp_path):
    conn, _, _ = _database(count=1)
    seed = _seed(tmp_path, counts={"actors": 1, "follows": 2, "likes": 1})
    with pytest.raises(ValueError, match="loaded 1 follows; the manifest declares 2"):
        loader.load_graph(conn, seed)
    assert conn.transaction.return_value.__exit__.call_args.args[0] is ValueError


def test_a_fixture_without_a_graph_empties_the_tables(tmp_path):
    conn, cursor, _ = _database()
    assert loader.load_graph(conn, _seed(tmp_path, graph=False)) == {}
    assert [c.args[0] for c in cursor.execute.call_args_list] == [loader.TRUNCATE_SQL]


def test_a_malformed_graph_block_is_an_error_before_touching_the_database(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"graph": {"files": {"actors": "a"}}}))
    conn, cursor, _ = _database()
    with pytest.raises(ValueError, match="graph.files"):
        loader.load_graph(conn, tmp_path)
    cursor.execute.assert_not_called()
