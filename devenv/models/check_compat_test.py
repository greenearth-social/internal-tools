"""Tests for the model/fixture compatibility gate (api#269)."""

import json

import pytest
from check_compat import incompatibilities, main


def models(model="all-MiniLM-L12-v2", dims=384):
    return {"content_embedding_model": model, "content_embedding_dims": dims}


def fixture(model="all-MiniLM-L12-v2", dims=384):
    return {"embedding_model": model, "embedding_dims": dims}


def test_matching_pair_is_compatible():
    assert incompatibilities(models(), fixture()) == []


def test_different_encoder_is_reported():
    problems = incompatibilities(models(), fixture(model="all-mpnet-base-v2"))
    assert len(problems) == 1
    # Both sides named, so the message says what to fetch rather than just
    # that something is wrong.
    assert "all-MiniLM-L12-v2" in problems[0]
    assert "all-mpnet-base-v2" in problems[0]


def test_different_dimensions_are_reported():
    problems = incompatibilities(models(), fixture(dims=768))
    assert len(problems) == 1
    assert "384" in problems[0] and "768" in problems[0]


def test_both_disagreements_are_reported_together():
    # One run should show everything that's wrong: fixing the encoder only to
    # be told the dimensions are also wrong is a bad loop to be in.
    problems = incompatibilities(models(), fixture(model="other", dims=768))
    assert len(problems) == 2


def test_same_dims_different_encoder_still_fails():
    # The case the gate exists for. Same width means nothing errors downstream
    # — the scores are just meaningless.
    assert incompatibilities(models(), fixture(model="other", dims=384))


@pytest.fixture
def manifests(tmp_path):
    def write(models_doc, fixture_doc):
        m = tmp_path / "models.json"
        f = tmp_path / "fixture.json"
        m.write_text(json.dumps(models_doc))
        f.write_text(json.dumps(fixture_doc))
        return ["check_compat.py", str(m), str(f)]

    return write


def test_main_exits_zero_when_compatible(manifests):
    assert main(manifests(models(), fixture())) == 0


def test_main_exits_nonzero_on_mismatch(manifests, capsys):
    assert main(manifests(models(), fixture(dims=768))) == 1
    # The remedy has to be in the output: this is the only place a developer
    # sees it, and it's not guessable.
    assert "fetch-models" in capsys.readouterr().err


def test_main_reports_mismatch_on_stderr_not_stdout(manifests, capsys):
    main(manifests(models(), fixture(dims=768)))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "disagree" in captured.err
