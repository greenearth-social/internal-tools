import datetime as dt
import gzip
import json
import sqlite3
import sys
import zipfile

import generate_sample as gs
import pytest
import verify_fixture as vf


def _fixture(tmp_path, topics):
    posts = []
    for i, topic in enumerate(topics):
        inference = (
            {} if topic == "missing" else {"text": {"message.commit.record.text": {"topic": topic}}}
        )
        uri = f"at://did:plc:a/app.bsky.feed.post/{i}"
        created_at = dt.datetime(2026, 9, 12, tzinfo=dt.UTC)
        posts.append(
            {
                "at_uri": uri,
                "did": "did:plc:a",
                "created_at": created_at,
                "raw_post": gs.build_raw_post(uri, "did:plc:a", "fixture post", created_at, None),
                "inferences": json.dumps(inference),
            }
        )
    gs.write_fixture_set(
        tmp_path,
        "prod-es",
        posts,
        [{"subject_uri": posts[0]["at_uri"]}],
        [],
        [{"did": "did:plc:a", "likes": 1}],
        dt.datetime(2026, 9, 12, tzinfo=dt.UTC),
        dt.datetime(2026, 9, 14, tzinfo=dt.UTC),
        1,
    )
    return sorted(tmp_path.glob("*.db.zip"))


@pytest.mark.parametrize("raw_sqlite", [False, True])
def test_coverage_includes_zero_and_allows_missing_history(tmp_path, raw_sqlite):
    chunks = _fixture(tmp_path, [{"News & Social Concern": 0.0}, "missing", {"Other": 1.0}])
    if raw_sqlite:
        for chunk in chunks:
            with zipfile.ZipFile(chunk) as archive:
                data = archive.read(archive.namelist()[0])
            chunk.write_bytes(data)
    assert vf.topic_score_counts(chunks) == (3, 2, 0)


@pytest.mark.parametrize("value", [True, None, "0.5", -0.1, 1.1])
def test_invalid_score_values_fail_verification(tmp_path, monkeypatch, value):
    _fixture(tmp_path, [{"News & Social Concern": value}])
    monkeypatch.setattr(sys, "argv", ["verify_fixture.py", str(tmp_path), "--offline"])
    assert vf.main() == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_scores_in_external_chunks_are_rejected(tmp_path, value):
    # External chunks need not have our writer's json_valid constraint.
    chunk = tmp_path / "external.db.zip"
    with sqlite3.connect(chunk) as conn:
        conn.execute("CREATE TABLE enriched_posts (inferences TEXT)")
        conn.execute(
            "INSERT INTO enriched_posts VALUES (?)",
            (json.dumps({"text": {"message.commit.record.text": {"topic": {"Other": value}}}}),),
        )
    assert vf.topic_score_counts([chunk]) == (1, 0, 1)


@pytest.mark.parametrize("topics", [None, [], "invalid"])
def test_invalid_topic_objects_are_counted(tmp_path, topics):
    assert vf.topic_score_counts(_fixture(tmp_path, [topics])) == (1, 0, 1)


def test_only_post_body_scores_count(tmp_path):
    chunks = _fixture(tmp_path, ["missing"])
    with zipfile.ZipFile(chunks[0]) as archive:
        data = archive.read(archive.namelist()[0])
    chunks[0].write_bytes(data)
    with sqlite3.connect(chunks[0]) as conn:
        conn.execute(
            "UPDATE enriched_posts SET inferences = ?",
            (json.dumps({"text": {"external.title": {"topic": {"News & Social Concern": 0.9}}}}),),
        )
    assert vf.topic_score_counts(chunks) == (1, 0, 0)


def test_fresh_capture_requires_scores_but_legacy_fixture_can_be_checked(tmp_path, monkeypatch):
    _fixture(tmp_path, ["missing", {}])
    argv = ["verify_fixture.py", str(tmp_path), "--offline"]
    monkeypatch.setattr(sys, "argv", argv)
    assert vf.main() == 0
    monkeypatch.setattr(sys, "argv", [*argv, "--require-topic-scores"])
    assert vf.main() == 1


def test_partial_coverage_passes_and_reports_percentage(tmp_path, monkeypatch, capsys):
    _fixture(tmp_path, [{"News & Social Concern": 0}, "missing"])
    monkeypatch.setattr(
        sys, "argv", ["verify_fixture.py", str(tmp_path), "--offline", "--require-topic-scores"]
    )
    assert vf.main() == 0
    assert "topic scores: 1/2 posts (50.0%)" in capsys.readouterr().out


def _da_fixture(tmp_path):
    _fixture(tmp_path, [{"Other": 1.0}, "missing"])
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    rows = vf.read_jsonl_gz(tmp_path / vf.SCORES_FILE)
    return manifest, rows


def _write_da_rows(tmp_path, rows):
    with gzip.open(tmp_path / vf.SCORES_FILE, "wt") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_da_scores_match_every_fixture_post_and_verify_offline(tmp_path, monkeypatch):
    manifest, rows = _da_fixture(tmp_path)
    assert len(rows) == 2
    assert vf.validate_da_scores(tmp_path, manifest) == []

    def no_network(*args, **kwargs):
        pytest.fail("Offline fixture verification must not access the network")

    monkeypatch.setattr(vf.urllib.request, "urlopen", no_network)
    monkeypatch.setattr(sys, "argv", ["verify_fixture.py", str(tmp_path), "--offline"])
    assert vf.main() == 0


def test_legacy_fixture_without_da_artifact_or_metadata_remains_valid(tmp_path, monkeypatch):
    manifest, _ = _da_fixture(tmp_path)
    del manifest["diverse_approval"]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / vf.SCORES_FILE).unlink()
    assert vf.validate_da_scores(tmp_path, manifest) == []
    monkeypatch.setattr(sys, "argv", ["verify_fixture.py", str(tmp_path), "--offline"])
    assert vf.main() == 0


@pytest.mark.parametrize("missing", ["file", "metadata"])
def test_da_artifact_and_manifest_metadata_are_both_required(tmp_path, monkeypatch, missing):
    manifest, _ = _da_fixture(tmp_path)
    if missing == "file":
        (tmp_path / vf.SCORES_FILE).unlink()
    else:
        del manifest["diverse_approval"]
        (tmp_path / "manifest.json").write_text(json.dumps(manifest))

    assert vf.validate_da_scores(tmp_path, manifest)
    monkeypatch.setattr(sys, "argv", ["verify_fixture.py", str(tmp_path), "--offline"])
    assert vf.main() == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [("source", "production"), ("file", "other.gz"), ("seed", True), ("count", -1)],
)
def test_da_manifest_metadata_is_validated(tmp_path, field, value):
    manifest, _ = _da_fixture(tmp_path)
    manifest["diverse_approval"][field] = value
    assert vf.validate_da_scores(tmp_path, manifest) == [
        "invalid Diverse Approval manifest metadata"
    ]


def test_da_manifest_count_matches_file_rows(tmp_path):
    manifest, _ = _da_fixture(tmp_path)
    manifest["diverse_approval"]["count"] += 1
    assert "manifest says 3 scores but file holds 2" in vf.validate_da_scores(tmp_path, manifest)[0]


def test_da_scores_must_cover_all_original_posts_even_when_count_matches(tmp_path):
    manifest, rows = _da_fixture(tmp_path)
    _write_da_rows(tmp_path, rows[:1])
    manifest["diverse_approval"]["count"] = 1
    assert vf.validate_da_scores(tmp_path, manifest) == [
        "Diverse Approval scores are missing 1 fixture posts"
    ]


def test_da_scores_reject_unknown_posts(tmp_path):
    manifest, rows = _da_fixture(tmp_path)
    rows[0]["post_uri"] = "at://did:plc:unknown/app.bsky.feed.post/unknown"
    _write_da_rows(tmp_path, rows)
    assert vf.validate_da_scores(tmp_path, manifest) == ["unknown Diverse Approval post on line 1"]


def test_da_scores_reject_duplicate_posts_even_when_count_matches(tmp_path):
    manifest, rows = _da_fixture(tmp_path)
    _write_da_rows(tmp_path, [rows[0], rows[0]])
    assert vf.validate_da_scores(tmp_path, manifest) == [
        "duplicate Diverse Approval post on line 2"
    ]


def test_da_scores_preserve_original_post_timestamp(tmp_path):
    manifest, rows = _da_fixture(tmp_path)
    rows[0]["created_at"] = "2026-09-13T00:00:00Z"
    _write_da_rows(tmp_path, rows)
    assert vf.validate_da_scores(tmp_path, manifest) == [
        "Diverse Approval post timestamp mismatch on line 1"
    ]


@pytest.mark.parametrize("score", [0, 0.0, 1, 1.0])
def test_da_score_boundaries_are_valid(tmp_path, score):
    manifest, rows = _da_fixture(tmp_path)
    rows[0]["score"] = score
    _write_da_rows(tmp_path, rows)
    assert vf.validate_da_scores(tmp_path, manifest) == []


@pytest.mark.parametrize(
    "score", [True, None, "0.5", -0.1, 1.1, float("nan"), float("inf"), float("-inf")]
)
def test_da_scores_reject_invalid_values(tmp_path, monkeypatch, score):
    manifest, rows = _da_fixture(tmp_path)
    rows[0]["score"] = score
    _write_da_rows(tmp_path, rows)
    assert vf.validate_da_scores(tmp_path, manifest) == [
        "invalid Diverse Approval score on line 1: expected [0, 1]"
    ]
    monkeypatch.setattr(sys, "argv", ["verify_fixture.py", str(tmp_path), "--offline"])
    assert vf.main() == 1


@pytest.mark.parametrize("change", ["missing", "extra", "not-object"])
def test_da_rows_require_exact_fields(tmp_path, change):
    manifest, rows = _da_fixture(tmp_path)
    if change == "missing":
        del rows[0]["score"]
    elif change == "extra":
        rows[0]["actor_id"] = 1
    _write_da_rows(tmp_path, [[]] if change == "not-object" else rows)
    assert vf.validate_da_scores(tmp_path, manifest) == [
        "invalid Diverse Approval score fields on line 1"
    ]


@pytest.mark.parametrize("corruption", ["json", "gzip", "truncated"])
def test_da_corruption_fails_verification_concisely(tmp_path, monkeypatch, capsys, corruption):
    manifest, _ = _da_fixture(tmp_path)
    path = tmp_path / vf.SCORES_FILE
    if corruption == "json":
        with gzip.open(path, "wt") as fh:
            fh.write("{invalid json}\n")
    elif corruption == "gzip":
        path.write_bytes(b"not a gzip file")
    else:
        path.write_bytes(path.read_bytes()[:-8])
    assert vf.validate_da_scores(tmp_path, manifest)[0].startswith(
        "could not inspect Diverse Approval scores:"
    )
    monkeypatch.setattr(sys, "argv", ["verify_fixture.py", str(tmp_path), "--offline"])
    assert vf.main() == 1
    assert "FAIL: could not inspect Diverse Approval scores:" in capsys.readouterr().out
