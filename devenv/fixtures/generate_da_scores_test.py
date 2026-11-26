import gzip
import json
import random
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import generate_da_scores as da
import pytest

URI_A = "at://did:plc:author/app.bsky.feed.post/a"
URI_B = "at://did:plc:author/app.bsky.feed.post/b"
URI_C = "at://did:plc:author/app.bsky.feed.post/c"
CREATED_AT = "2026-07-22T10:00:00.123456Z"
INGESTED_CREATED_AT = "2026-07-22T10:00:00Z"


def _raw_post(created_at: str | None = CREATED_AT):
    return json.dumps({"message": {"commit": {"record": {"createdAt": created_at}}}})


def _row(uri=URI_A, created_at=CREATED_AT):
    return uri, _raw_post(created_at)


def _fixture(directory, chunks, *, compressed=True):
    directory.mkdir()
    for index, rows in enumerate(chunks):
        name = f"mega_jetstream_20260722_10000{index}.db.zip"
        db_path = directory / ("chunk.sqlite" if compressed else name)
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "CREATE TABLE enriched_posts ("
                "id INTEGER PRIMARY KEY, at_uri TEXT, raw_post TEXT, created_at TEXT)"
            )
            connection.executemany(
                "INSERT INTO enriched_posts (at_uri, raw_post, created_at) VALUES (?, ?, ?)",
                [(uri, raw, "2001-01-01 00:00:00") for uri, raw in rows],
            )
        if compressed:
            with zipfile.ZipFile(directory / name, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(db_path, "posts.db")
            db_path.unlink()
    manifest = {
        "schema_version": 1,
        "source": "test-fixture",
        "window_start": "2026-07-22T00:00:00Z",
        "window_end": "2026-07-23T00:00:00Z",
        "counts": {"posts": sum(map(len, chunks)), "likes": 2, "files": len(chunks)},
        "personas": [{"did": "did:plc:viewer", "likes": 2}],
        "custom": {"keep": ["metadata", 123]},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "likes.jsonl.gz").write_bytes(gzip.compress(b"fixture likes\n", mtime=0))
    return directory


def _read_scores(directory):
    with gzip.open(directory / da.SCORES_FILE, "rt") as scores:
        return [json.loads(line) for line in scores]


def _snapshot(directory):
    return {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def _existing_outputs(directory):
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["diverse_approval"] = {"source": "previous", "seed": 13, "count": 1}
    manifest_path.write_text(json.dumps(manifest))
    (directory / da.SCORES_FILE).write_bytes(gzip.compress(b"previous scores\n", mtime=0))


@pytest.mark.parametrize("compressed", [True, False], ids=["zip", "raw-sqlite"])
def test_reads_raw_timestamps_with_ingestions_utc_second_precision(tmp_path, compressed):
    directory = _fixture(
        tmp_path / "fixture",
        [[_row(URI_A), _row(URI_B, "2026-07-22T12:30:00.654321+02:30")]],
        compressed=compressed,
    )

    # The builder deliberately gives SQLite's created_at a different rounded
    # timestamp. ES ingests the raw record, normalized to UTC whole seconds.
    assert da.read_fixture_posts(directory) == {
        URI_A: INGESTED_CREATED_AT,
        URI_B: "2026-07-22T10:00:00Z",
    }


def test_generates_one_score_per_post_and_preserves_other_fixture_data(tmp_path):
    directory = _fixture(tmp_path / "fixture", [[_row(URI_C), _row(URI_A), _row(URI_B)]])
    before = _snapshot(directory)
    original_manifest = json.loads(before[Path("manifest.json")])

    metadata = da.generate(directory)

    assert metadata == {
        "source": "synthetic-random",
        "seed": 42,
        "file": "da_post_scores.jsonl.gz",
        "count": 3,
    }
    assert json.loads((directory / "manifest.json").read_text()) == {
        **original_manifest,
        "diverse_approval": metadata,
    }
    rows = _read_scores(directory)
    assert [row["post_uri"] for row in rows] == [URI_A, URI_B, URI_C]
    assert all(set(row) == {"post_uri", "score", "created_at"} for row in rows)
    assert all(isinstance(row["score"], float) and 0 <= row["score"] < 1 for row in rows)
    assert all(row["created_at"] == INGESTED_CREATED_AT for row in rows)
    after = _snapshot(directory)
    assert set(after) == set(before) | {Path(da.SCORES_FILE)}
    for path, content in before.items():
        if path != Path("manifest.json"):
            assert after[path] == content


def test_scores_and_gzip_are_reproducible_across_row_and_chunk_order(tmp_path):
    left = _fixture(tmp_path / "left", [[_row(URI_B), _row(URI_A)], [_row(URI_C)]])
    right = _fixture(tmp_path / "right", [[_row(URI_C)], [_row(URI_A), _row(URI_B)]])
    rng_state = random.getstate()

    da.generate(left)
    original = (left / da.SCORES_FILE).read_bytes()
    da.generate(left)
    assert (left / da.SCORES_FILE).read_bytes() == original
    da.generate(right)
    assert (right / da.SCORES_FILE).read_bytes() == original
    assert random.getstate() == rng_state

    metadata = da.generate(right, seed=99)
    assert metadata["seed"] == 99
    assert (right / da.SCORES_FILE).read_bytes() != original
    assert [row["post_uri"] for row in _read_scores(right)] == [URI_A, URI_B, URI_C]
    assert all(row["created_at"] == INGESTED_CREATED_AT for row in _read_scores(right))


def test_duplicate_post_with_same_ingested_timestamp_is_scored_once(tmp_path):
    directory = _fixture(
        tmp_path / "fixture",
        [[_row(URI_A)], [_row(URI_B), _row(URI_A, "2026-07-22T10:00:00.987654+00:00")]],
    )

    assert da.generate(directory)["count"] == 2
    assert [row["post_uri"] for row in _read_scores(directory)] == [URI_A, URI_B]


def test_conflicting_duplicate_preserves_existing_outputs(tmp_path):
    directory = _fixture(
        tmp_path / "fixture",
        [[_row(URI_A)], [_row(URI_A, "2026-07-22T10:00:01.123456Z")]],
    )
    _existing_outputs(directory)
    before = _snapshot(directory)

    with pytest.raises(ValueError, match="conflicting"):
        da.generate(directory)

    assert _snapshot(directory) == before


@pytest.mark.parametrize(
    "bad_row",
    [
        (None, _raw_post()),
        ("", _raw_post()),
        (b"not-a-text-uri", _raw_post()),
        (URI_A, None),
        (URI_A, "not JSON"),
        (URI_A, "{}"),
        (URI_A, _raw_post(None)),
        (URI_A, _raw_post("not a timestamp")),
        (URI_A, _raw_post("2026-13-22T10:00:00Z")),
    ],
    ids=[
        "missing-uri",
        "empty-uri",
        "nonstring-uri",
        "missing-record",
        "malformed-json",
        "missing-timestamp",
        "null-timestamp",
        "malformed-timestamp",
        "invalid-date",
    ],
)
def test_invalid_source_preserves_existing_outputs(tmp_path, bad_row):
    directory = _fixture(tmp_path / "fixture", [[_row(URI_B)], [bad_row]])
    _existing_outputs(directory)
    before = _snapshot(directory)

    with pytest.raises(ValueError):
        da.generate(directory)

    assert _snapshot(directory) == before


@pytest.mark.parametrize("chunks", [[], [[]]], ids=["no-chunks", "empty-chunk"])
def test_missing_posts_preserve_existing_outputs(tmp_path, chunks):
    directory = _fixture(tmp_path / "fixture", chunks)
    _existing_outputs(directory)
    before = _snapshot(directory)

    with pytest.raises(ValueError):
        da.generate(directory)

    assert _snapshot(directory) == before


def test_cli_generates_scores_from_local_files_with_custom_seed(tmp_path):
    directory = _fixture(tmp_path / "fixture", [[_row(URI_A)]], compressed=False)

    result = subprocess.run(
        [sys.executable, str(Path(da.__file__)), str(directory), "--seed", "17"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads((directory / "manifest.json").read_text())["diverse_approval"]["seed"] == 17
    assert _read_scores(directory)[0]["post_uri"] == URI_A


def test_cli_reports_invalid_input_without_overwriting_outputs(tmp_path):
    directory = _fixture(tmp_path / "fixture", [[]])
    _existing_outputs(directory)
    before = _snapshot(directory)

    result = subprocess.run(
        [sys.executable, str(Path(da.__file__)), str(directory)],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 1
    assert "FAIL:" in result.stderr
    assert "Traceback" not in result.stderr
    assert _snapshot(directory) == before


def test_cli_defaults_target_the_fixture_next_to_the_script(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["generate_da_scores.py"])
    monkeypatch.setattr(da, "generate", lambda directory, *, seed: calls.append((directory, seed)))

    assert da.main() == 0
    assert calls == [(Path(da.__file__).parent / "data", 42)]
