"""Local ranker selection downloads artifacts without touching running services."""

import json
import subprocess
from pathlib import Path

import pytest
import use_ranker

RUN_URI = "gs://example-models/BST training run.task-id"
MANIFEST_URI = RUN_URI + use_ranker.MANIFEST_SUFFIX
CONFIG_URI = RUN_URI + "/artifacts/bst_model_config/model_config.json"
AUTHOR_URI = RUN_URI + "/artifacts/author_idx_mapping/ranker_author_idx.parquet"
MODEL_URI = RUN_URI + "/models/ranker.pt"


@pytest.fixture
def remote(monkeypatch):
    manifest = {
        "ranker_uri": MODEL_URI,
        "ranker_clearml_model_id": "model-id",
        "clearml_task_id": "task-id",
    }
    config = {
        "model_type": "bst-ranker",
        "embedding_model": "all_MiniLM_L12_v2",
        "max_history_len": 64,
        "author_pad_idx": 0,
        "author_unk_idx": 1,
        "constructor_args": {
            "post_embedding_dim": 384,
            "num_transformer_layers": 1,
        },
    }
    objects = {
        MANIFEST_URI: json.dumps(manifest).encode(),
        CONFIG_URI: json.dumps(config).encode(),
        AUTHOR_URI: b"author map",
        MODEL_URI: b"torchscript weights",
    }
    calls = []
    failures = {}

    def fake_run(command, **kwargs):
        assert command[:2] == ["gcloud", "storage"]
        assert kwargs == {"capture_output": True, "text": True, "check": True}
        calls.append(command)
        if command[2] == "ls":
            assert command[3] == "--recursive"
            uri = command[4]
        else:
            assert command[2] == "cp"
            uri = command[3]
        if uri in failures:
            raise subprocess.CalledProcessError(1, command, stderr=failures[uri])
        if command[2] == "ls":
            output = "\n".join(sorted(key for key in objects if key.startswith(uri))) + "\n"
        else:
            if uri not in objects:
                raise subprocess.CalledProcessError(1, command, stderr="Object not found")
            Path(command[4]).write_bytes(objects[uri])
            output = ""
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(use_ranker.subprocess, "run", fake_run)
    return objects, calls, failures


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def selection(runtime):
    return json.loads((runtime / use_ranker.SELECTION_FILE).read_text())


@pytest.mark.parametrize("source", [RUN_URI, RUN_URI + "/", MANIFEST_URI])
def test_selects_specific_run_with_matching_author_map_and_derived_history(
    tmp_path, remote, source
):
    runtime = tmp_path / "named instance"
    models = write_json(
        tmp_path / "models.json",
        {
            "content_embedding_model": "all-MiniLM-L12-v2",
            "content_embedding_dims": 384,
        },
    )
    fixture = write_json(
        tmp_path / "fixture.json",
        {
            "embedding_model": "all-MiniLM-L12-v2",
            "embedding_dims": 384,
        },
    )

    assert (
        use_ranker.main(
            [
                source,
                "--runtime",
                str(runtime),
                "--models-manifest",
                str(models),
                "--fixture-manifest",
                str(fixture),
            ]
        )
        == 0
    )

    chosen = selection(runtime)
    inference = chosen["services"]["inference"]
    assert inference["environment"] == {
        "GE_INFERENCE_RANKER_MANIFEST_URI": "/ranker/ranker_serving_manifest.json",
        "GE_INFERENCE_RANKER_AUTHOR_MAP_URI": "/ranker/ranker_author_idx.parquet",
        "GE_INFERENCE_RANKER_MAX_HISTORY_LEN": "64",
    }
    volume = inference["volumes"][0]
    artifact_dir = Path(volume["source"])
    assert artifact_dir.parent == runtime / "rankers"
    assert volume == {
        "type": "bind",
        "source": str(artifact_dir),
        "target": "/ranker",
        "read_only": True,
    }
    assert (artifact_dir / "ranker.pt").read_bytes() == b"torchscript weights"
    assert (artifact_dir / "ranker_author_idx.parquet").read_bytes() == b"author map"
    manifest = json.loads((artifact_dir / "ranker_serving_manifest.json").read_text())
    assert manifest["ranker_uri"] == "/ranker/ranker.pt"
    assert manifest["ranker_clearml_model_id"] == "model-id"
    assert chosen["x-ge-ranker"]["source"] == MANIFEST_URI
    assert chosen["x-ge-ranker"]["model_config"]["max_history_len"] == 64
    assert remote[1][0][3] == MANIFEST_URI  # Spaces stay inside one subprocess argument.


def test_reports_progress_before_each_external_operation(tmp_path, remote, monkeypatch, capsys):
    download = use_ranker.gcloud
    expected_steps = {
        MANIFEST_URI: "downloading ranker manifest",
        RUN_URI + "/artifacts/": "locating training artifacts",
        CONFIG_URI: "downloading and checking training metadata",
        AUTHOR_URI: "downloading author map",
        MODEL_URI: "downloading ranker weights",
    }

    def check_progress(*args):
        uri = args[-1] if args[0] == "ls" else args[1]
        # The user should see the current step while the external command is
        # still running, including when that command eventually fails.
        assert expected_steps[uri] in capsys.readouterr().out
        return download(*args)

    monkeypatch.setattr(use_ranker, "gcloud", check_progress)
    assert use_ranker.main([RUN_URI, "--runtime", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "saving local ranker selection" in output
    assert "selected ranker model-id" in output


def test_legacy_requires_explicit_history_and_records_unknown_compatibility(
    tmp_path, remote, capsys
):
    del remote[0][CONFIG_URI]
    args = [RUN_URI, "--runtime", str(tmp_path)]
    assert use_ranker.main(args) == 1
    assert "--max-history-len" in capsys.readouterr().err
    assert not (tmp_path / use_ranker.SELECTION_FILE).exists()
    assert list((tmp_path / "rankers").iterdir()) == []

    assert use_ranker.main([*args, "--max-history-len", "50"]) == 0
    chosen = selection(tmp_path)
    assert (
        chosen["services"]["inference"]["environment"]["GE_INFERENCE_RANKER_MAX_HISTORY_LEN"]
        == "50"
    )
    assert chosen["x-ge-ranker"]["model_config"] is None
    assert "could not be checked" in capsys.readouterr().out


def test_history_override_wins_over_training_metadata(tmp_path, remote):
    assert use_ranker.main([RUN_URI, "--runtime", str(tmp_path), "--max-history-len", "32"]) == 0
    assert (
        selection(tmp_path)["services"]["inference"]["environment"][
            "GE_INFERENCE_RANKER_MAX_HISTORY_LEN"
        ]
        == "32"
    )


@pytest.mark.parametrize(
    "patch,error",
    [
        ({"model_type": "other"}, "bst-ranker"),
        ({"max_history_len": 0}, "positive integer"),
        ({"author_pad_idx": 1}, "author_pad_idx=0"),
        (
            {"constructor_args": {"post_embedding_dim": 384, "num_transformer_layers": 2}},
            "one transformer layer",
        ),
        (
            {
                "constructor_args": {
                    "post_embedding_dim": 384,
                    "num_transformer_layers": 1,
                    "use_post_liker_feature": True,
                }
            },
            "use_post_liker_feature",
        ),
        (
            {
                "constructor_args": {
                    "post_embedding_dim": 384,
                    "num_transformer_layers": 1,
                    "use_target_user_feature": True,
                }
            },
            "use_target_user_feature",
        ),
    ],
)
def test_rejects_incompatible_model_metadata_before_downloading_weights(
    tmp_path, remote, capsys, patch, error
):
    config = json.loads(remote[0][CONFIG_URI])
    config.update(patch)
    remote[0][CONFIG_URI] = json.dumps(config).encode()
    assert use_ranker.main([RUN_URI, "--runtime", str(tmp_path)]) == 1
    assert error in capsys.readouterr().err
    assert not any(call[2:4] == ["cp", MODEL_URI] for call in remote[1])


@pytest.mark.parametrize(
    "patch", [{"ranker_contract_version": 2}, {"post_liker_feature_enabled": True}]
)
def test_rejects_unsupported_serving_manifest(tmp_path, remote, capsys, patch):
    manifest = json.loads(remote[0][MANIFEST_URI])
    manifest.update(patch)
    remote[0][MANIFEST_URI] = json.dumps(manifest).encode()
    assert use_ranker.main([RUN_URI, "--runtime", str(tmp_path)]) == 1
    assert "serving contract" in capsys.readouterr().err


@pytest.mark.parametrize(
    "bundle,encoder,dimensions",
    [
        ("models", "other-encoder", 384),
        ("models", "all-MiniLM-L12-v2", 768),
        ("fixture", "other-encoder", 384),
        ("fixture", "all-MiniLM-L12-v2", 768),
    ],
)
def test_rejects_model_or_fixture_embedding_mismatch(
    tmp_path, remote, capsys, bundle, encoder, dimensions
):
    keys = (
        ("content_embedding_model", "content_embedding_dims")
        if bundle == "models"
        else ("embedding_model", "embedding_dims")
    )
    manifest = write_json(
        tmp_path / "bundle.json", dict(zip(keys, (encoder, dimensions), strict=True))
    )
    assert (
        use_ranker.main(
            [RUN_URI, "--runtime", str(tmp_path), f"--{bundle}-manifest", str(manifest)]
        )
        == 1
    )
    assert "disagree" in capsys.readouterr().err


def test_check_validates_bundles_that_arrive_after_selection_without_network(
    tmp_path, remote, capsys
):
    models = tmp_path / "models.json"
    args = ["--runtime", str(tmp_path), "--models-manifest", str(models)]
    assert use_ranker.main([RUN_URI, *args]) == 0
    calls_before = list(remote[1])
    write_json(models, {"content_embedding_model": "different", "content_embedding_dims": 384})
    assert use_ranker.main(["--check", *args]) == 1
    assert "disagree" in capsys.readouterr().err
    write_json(
        models, {"content_embedding_model": "all-MiniLM-L12-v2", "content_embedding_dims": 384}
    )
    assert use_ranker.main(["--check", *args]) == 0
    assert remote[1] == calls_before


@pytest.mark.parametrize(
    "failure_uri", [MANIFEST_URI, RUN_URI + "/artifacts/", CONFIG_URI, AUTHOR_URI, MODEL_URI]
)
def test_failed_download_preserves_selection_and_removes_partial_version(
    tmp_path, remote, capsys, failure_uri
):
    args = [RUN_URI, "--runtime", str(tmp_path)]
    assert use_ranker.main(args) == 0
    previous = (tmp_path / use_ranker.SELECTION_FILE).read_bytes()
    directories = list((tmp_path / "rankers").iterdir())
    remote[2][failure_uri] = "Permission denied"
    capsys.readouterr()
    assert use_ranker.main(args) == 1
    output = capsys.readouterr()
    assert "Permission denied" in output.err
    assert "saving local ranker selection" not in output.out
    assert "selected ranker" not in output.out
    assert (tmp_path / use_ranker.SELECTION_FILE).read_bytes() == previous
    assert list((tmp_path / "rankers").iterdir()) == directories


@pytest.mark.parametrize(
    "author_maps", [[], [AUTHOR_URI, RUN_URI + "/artifacts/author_idx_mapping/another.parquet"]]
)
def test_rejects_missing_or_ambiguous_author_map(tmp_path, remote, capsys, author_maps):
    del remote[0][AUTHOR_URI]
    for uri in author_maps:
        remote[0][uri] = b"author map"
    assert use_ranker.main([RUN_URI, "--runtime", str(tmp_path)]) == 1
    assert "Expected one author_idx_mapping" in capsys.readouterr().err


def test_reselection_and_reset_retain_artifacts_for_existing_container_mounts(tmp_path, remote):
    args = [RUN_URI, "--runtime", str(tmp_path)]
    assert use_ranker.main(args) == 0
    first = selection(tmp_path)["services"]["inference"]["volumes"][0]["source"]
    assert use_ranker.main(args) == 0
    second = selection(tmp_path)["services"]["inference"]["volumes"][0]["source"]
    assert first != second
    calls_before = list(remote[1])
    assert use_ranker.main(["--reset", "--runtime", str(tmp_path)]) == 0
    assert not (tmp_path / use_ranker.SELECTION_FILE).exists()
    assert Path(first, "ranker.pt").is_file()
    assert Path(second, "ranker.pt").is_file()
    assert use_ranker.main(["--reset", "--runtime", str(tmp_path)]) == 0
    assert remote[1] == calls_before


def test_missing_gcloud_reports_actionable_error_without_publishing(tmp_path, monkeypatch, capsys):
    def missing(*args, **kwargs):
        raise FileNotFoundError("gcloud")

    monkeypatch.setattr(use_ranker.subprocess, "run", missing)
    assert use_ranker.main([RUN_URI, "--runtime", str(tmp_path)]) == 1
    assert "gcloud is required" in capsys.readouterr().err
    assert not (tmp_path / use_ranker.SELECTION_FILE).exists()


@pytest.mark.parametrize("source", ["gs://example-models", "gs://example-models/*", "/local/path"])
def test_requires_specific_gcs_run_without_network(tmp_path, remote, source):
    assert use_ranker.main([source, "--runtime", str(tmp_path)]) == 1
    assert remote[1] == []


@pytest.mark.parametrize("length", ["0", "-1"])
def test_rejects_nonpositive_history_override_without_network(tmp_path, remote, length):
    assert use_ranker.main([RUN_URI, "--runtime", str(tmp_path), "--max-history-len", length]) == 1
    assert remote[1] == []


def test_reset_does_not_create_runtime_or_require_gcloud(tmp_path, remote):
    runtime = tmp_path / "not-created"
    assert use_ranker.main(["--reset", "--runtime", str(runtime)]) == 0
    assert not runtime.exists()
    assert remote[1] == []
