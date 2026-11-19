import gzip
import json
import sys
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call

import load_da_scores as loader
import pytest

URI = "at://did:plc:author/app.bsky.feed.post/a"
CREATED_AT = "2026-07-22T10:00:00.123456Z"


def _row(**changes):
    return {"post_uri": URI, "score": 0.3141592653589793, "created_at": CREATED_AT, **changes}


def _seed(tmp_path, rows):
    directory = tmp_path / "seed"
    directory.mkdir()
    (directory / "manifest.json").write_text(
        json.dumps({"diverse_approval": {"file": loader.SCORES_FILE, "count": len(rows)}})
    )
    with gzip.open(directory / loader.SCORES_FILE, "wt") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
    return directory


@pytest.fixture
def database():
    conn = MagicMock()
    transaction = conn.transaction.return_value
    cursor = conn.cursor.return_value.__enter__.return_value
    copy = cursor.copy.return_value.__enter__.return_value
    return conn, transaction, cursor, copy


def _assert_transaction_failed(transaction, error):
    transaction.__exit__.assert_called_once()
    error_type, caught, traceback = transaction.__exit__.call_args.args
    assert error_type is type(error)
    assert caught is error
    assert traceback is not None


@pytest.mark.parametrize("score", [0, 0.3141592653589793, 1])
def test_parse_row_preserves_values_and_accepts_score_bounds(score):
    assert loader.parse_row(json.dumps(_row(score=score)), 1) == (
        URI,
        score,
        datetime(2026, 7, 22, 10, 0, 0, 123456, tzinfo=UTC),
    )


def test_parse_row_preserves_aware_timestamp_precision_and_offset():
    timestamp = "2026-07-22T12:30:00.654321+02:30"
    _, _, created = loader.parse_row(json.dumps(_row(created_at=timestamp)), 1)
    assert created == datetime(2026, 7, 22, 10, 0, 0, 654321, tzinfo=UTC)
    assert created.tzinfo == timezone(timedelta(hours=2, minutes=30))


@pytest.mark.parametrize(
    "row",
    [
        [],
        {"post_uri": URI, "score": 0.5},
        _row(extra="unexpected"),
        _row(post_uri=None),
        _row(post_uri=""),
        _row(post_uri=123),
        _row(score=None),
        _row(score=True),
        _row(score="0.5"),
        _row(score=-0.001),
        _row(score=1.001),
        _row(score=float("inf")),
        _row(score=float("-inf")),
        _row(score=float("nan")),
        _row(created_at=None),
        _row(created_at="not a timestamp"),
        _row(created_at="2026-07-22T10:00:00"),
    ],
)
def test_parse_row_rejects_invalid_shape_types_bounds_and_timestamps(row):
    with pytest.raises(ValueError, match=r"da_post_scores\.jsonl\.gz line 9:"):
        loader.parse_row(json.dumps(row), 9)


def test_parse_row_reports_line_number_for_invalid_json():
    with pytest.raises(ValueError, match=r"da_post_scores\.jsonl\.gz line 7:"):
        loader.parse_row("{broken", 7)


def test_load_streams_native_values_through_copy_in_one_transaction(tmp_path, database):
    rows = [_row(), _row(post_uri=URI + "b", score=0.75)]
    directory = _seed(tmp_path, rows)
    with gzip.open(directory / loader.SCORES_FILE, "wt") as stream:
        stream.write("\n" + json.dumps(rows[0]) + "\n  \n" + json.dumps(rows[1]) + "\n")
    conn, transaction, cursor, copy = database

    assert loader.load_scores(conn, directory) == 2

    conn.transaction.assert_called_once_with()
    transaction.__enter__.assert_called_once_with()
    transaction.__exit__.assert_called_once_with(None, None, None)
    cursor.execute.assert_called_once_with("TRUNCATE da_post_scores")
    cursor.copy.assert_called_once_with(
        "COPY da_post_scores (post_uri, score, created_at) FROM STDIN"
    )
    created = datetime(2026, 7, 22, 10, 0, 0, 123456, tzinfo=UTC)
    assert copy.write_row.call_args_list == [
        call((URI, 0.3141592653589793, created)),
        call((URI + "b", 0.75, created)),
    ]


def test_empty_declared_fixture_replaces_previous_rows(tmp_path, database):
    directory = _seed(tmp_path, [])
    conn, transaction, cursor, copy = database

    assert loader.load_scores(conn, directory) == 0

    cursor.execute.assert_called_once_with("TRUNCATE da_post_scores")
    copy.write_row.assert_not_called()
    transaction.__exit__.assert_called_once_with(None, None, None)


@pytest.mark.parametrize("missing", ["manifest.json", loader.SCORES_FILE])
def test_missing_required_file_does_not_start_a_transaction(tmp_path, database, missing):
    directory = _seed(tmp_path, [_row()])
    (directory / missing).unlink()
    conn, _, cursor, _ = database

    with pytest.raises(FileNotFoundError):
        loader.load_scores(conn, directory)

    conn.transaction.assert_not_called()
    conn.execute.assert_not_called()
    cursor.execute.assert_not_called()


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [],
        {"file": "other.jsonl.gz", "count": 1},
        {"file": loader.SCORES_FILE},
        {"file": loader.SCORES_FILE, "count": -1},
        {"file": loader.SCORES_FILE, "count": True},
        {"file": loader.SCORES_FILE, "count": 1.5},
        {"file": loader.SCORES_FILE, "count": "1"},
    ],
)
def test_invalid_manifest_metadata_does_not_truncate(tmp_path, database, metadata):
    directory = _seed(tmp_path, [_row()])
    (directory / "manifest.json").write_text(json.dumps({"diverse_approval": metadata}))
    conn, _, cursor, _ = database

    with pytest.raises(ValueError, match="manifest diverse_approval"):
        loader.load_scores(conn, directory)

    conn.transaction.assert_not_called()
    conn.execute.assert_not_called()
    cursor.execute.assert_not_called()


def test_score_file_without_metadata_does_not_truncate(tmp_path, database):
    directory = _seed(tmp_path, [_row()])
    (directory / "manifest.json").write_text("{}")
    conn, _, cursor, _ = database

    with pytest.raises(ValueError, match="manifest diverse_approval"):
        loader.load_scores(conn, directory)

    conn.transaction.assert_not_called()
    conn.execute.assert_not_called()
    cursor.execute.assert_not_called()


@pytest.mark.parametrize("manifest", [[], "legacy", None, 42])
def test_non_object_manifest_without_score_file_does_not_clear_scores(tmp_path, database, manifest):
    directory = _seed(tmp_path, [])
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / loader.SCORES_FILE).unlink()
    conn, _, cursor, _ = database

    with pytest.raises(ValueError, match="manifest"):
        loader.load_scores(conn, directory)

    conn.transaction.assert_not_called()
    conn.execute.assert_not_called()
    cursor.execute.assert_not_called()


def test_legacy_fixture_clears_stale_scores_in_a_transaction(tmp_path, database):
    directory = _seed(tmp_path, [])
    (directory / "manifest.json").write_text("{}")
    (directory / loader.SCORES_FILE).unlink()
    conn, transaction, _, _ = database

    assert loader.load_scores(conn, directory) == 0

    conn.execute.assert_called_once_with("TRUNCATE da_post_scores")
    conn.cursor.assert_not_called()
    transaction.__exit__.assert_called_once_with(None, None, None)


def test_late_invalid_row_leaves_transaction_with_error(tmp_path, database):
    directory = _seed(tmp_path, [_row(), _row(created_at="invalid")])
    conn, transaction, _, copy = database

    with pytest.raises(ValueError, match="line 2:") as error:
        loader.load_scores(conn, directory)

    copy.write_row.assert_called_once()
    _assert_transaction_failed(transaction, error.value)


def test_count_mismatch_is_checked_before_transaction_commits(tmp_path, database):
    directory = _seed(tmp_path, [_row()])
    (directory / "manifest.json").write_text(
        json.dumps({"diverse_approval": {"file": loader.SCORES_FILE, "count": 2}})
    )
    conn, transaction, _, copy = database

    with pytest.raises(ValueError, match="has 1 rows; manifest declares 2") as error:
        loader.load_scores(conn, directory)

    copy.write_row.assert_called_once()
    _assert_transaction_failed(transaction, error.value)


@pytest.mark.parametrize("operation", ["truncate", "copy"])
def test_database_write_error_leaves_transaction_with_error(tmp_path, database, operation):
    directory = _seed(tmp_path, [_row()])
    conn, transaction, cursor, copy = database
    failure = RuntimeError("database write failed")
    target = cursor.execute if operation == "truncate" else copy.write_row
    target.side_effect = failure

    with pytest.raises(RuntimeError) as error:
        loader.load_scores(conn, directory)

    assert error.value is failure
    _assert_transaction_failed(transaction, failure)


class DatabaseError(Exception):
    pass


@pytest.fixture
def psycopg(monkeypatch):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    module = SimpleNamespace(connect=Mock(return_value=conn), Error=DatabaseError)
    monkeypatch.setitem(sys.modules, "psycopg", module)
    return module, conn


def test_main_uses_fixed_local_database_regardless_of_environment(
    monkeypatch, tmp_path, psycopg, capsys
):
    module, conn = psycopg
    for name in ("BRIDGEU_DATABASE_URL", "GRAPH_DATABASE_URL", "DATABASE_URL"):
        monkeypatch.setenv(name, "postgresql://production.example/production")
    monkeypatch.setenv("PGHOST", "production.example")
    monkeypatch.setenv("PGDATABASE", "production")
    monkeypatch.setattr(loader, "SEED_DIR", tmp_path)
    load = Mock(return_value=7)
    monkeypatch.setattr(loader, "load_scores", load)

    loader.main()

    module.connect.assert_called_once_with(
        "postgresql://graph:graph@bridgeu-postgres:5432/graph?sslmode=disable", connect_timeout=10
    )
    load.assert_called_once_with(conn, tmp_path)
    conn.__exit__.assert_called_once_with(None, None, None)
    assert "Loaded 7 DA scores into local Postgres" in capsys.readouterr().out


@pytest.mark.parametrize("failure", [ValueError("invalid fixture"), DatabaseError("COPY failed")])
def test_main_reports_fixture_and_database_failures(monkeypatch, psycopg, failure):
    _, conn = psycopg
    monkeypatch.setattr(loader, "load_scores", Mock(side_effect=failure))

    with pytest.raises(SystemExit, match="FATAL: DA score seed failed:") as error:
        loader.main()

    assert str(failure) in str(error.value)
    _assert_transaction_failed(conn, failure)
