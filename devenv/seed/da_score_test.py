import json
import sys

import da_score


def test_shift_comes_from_the_rebased_manifest(tmp_path):
    assert da_score.rebase_shift_seconds(tmp_path) == 0
    (tmp_path / "manifest.json").write_text(json.dumps({"window_end": "x"}))
    assert da_score.rebase_shift_seconds(tmp_path) == 0
    (tmp_path / "manifest.json").write_text(json.dumps({"rebased": {"delta_seconds": 86400}}))
    assert da_score.rebase_shift_seconds(tmp_path) == 86400


def test_score_widens_the_age_window_by_the_shift_and_passes_flags_last():
    argv = da_score.score_argv(36 * 3600, ["--lam", "0.5", "--max-age-hours", "24"])
    assert argv[:4] == [sys.executable, "-m", "pipeline", "score"]
    assert argv[4:8] == ["--window-hours", "168.0", "--max-age-hours", "204.0"]
    # The caller's own flags come after, so argparse lets them win.
    assert argv[8:] == ["--lam", "0.5", "--max-age-hours", "24"]


def test_a_failed_score_run_skips_the_shift(monkeypatch, tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"rebased": {"delta_seconds": 60}}))
    monkeypatch.setattr(da_score, "SEED_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["da_score.py"])
    monkeypatch.setattr(da_score.subprocess, "run", lambda argv: type("R", (), {"returncode": 3})())
    shifted = []
    monkeypatch.setattr(da_score, "shift_created_at", shifted.append)
    assert da_score.main() == 3
    assert shifted == []
