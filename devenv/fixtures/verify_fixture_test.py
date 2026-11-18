import datetime as dt
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
        posts.append(
            {
                "at_uri": f"at://a/app.bsky.feed.post/{i}",
                "did": "did:plc:a",
                "created_at": dt.datetime(2026, 9, 12, tzinfo=dt.UTC),
                "raw_post": "{}",
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
