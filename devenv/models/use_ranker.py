#!/usr/bin/env python3
"""Download and select a ranker for a local devenv instance, without starting it."""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

MANIFEST_SUFFIX = "/artifacts/ranker_serving_manifest/ranker_serving_manifest.json"
SELECTION_FILE = "ranker.compose.json"


def gcloud(*args: str) -> str:
    try:
        result = subprocess.run(
            ["gcloud", "storage", *args], capture_output=True, text=True, check=True
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "gcloud is required; install it and authenticate to read the model bucket"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"gcloud storage {args[0]} failed: {exc.stderr.strip()}") from exc
    return result.stdout


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def required_string(document: dict, key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing {key}")
    return value


def validate_gcs_uri(uri: str) -> None:
    parsed = urlsplit(uri)
    if parsed.scheme != "gs" or not parsed.netloc or not parsed.path.strip("/"):
        raise ValueError(
            "Provide a specific gs://bucket/run URI or ranker serving manifest, not a bucket root"
        )
    if any(character in uri for character in "*?[]#"):
        raise ValueError("GCS URIs must identify one run or object, without wildcards")


def source_manifest(source: str) -> tuple[str, str]:
    source = source.rstrip("/")
    validate_gcs_uri(source)
    if source.endswith("/ranker_serving_manifest.json"):
        if not source.endswith(MANIFEST_SUFFIX):
            raise ValueError(f"Expected a training manifest ending in {MANIFEST_SUFFIX}")
        return source[: -len(MANIFEST_SUFFIX)], source
    return source, source + MANIFEST_SUFFIX


def validate_config(config: dict) -> int:
    if config.get("model_type") != "bst-ranker":
        raise ValueError("model_config.json must describe a bst-ranker")
    history_len = positive_int(config.get("max_history_len"), "model_config max_history_len")
    required_string(config, "embedding_model")
    constructor = config.get("constructor_args")
    if not isinstance(constructor, dict):
        raise ValueError("model_config constructor_args must be an object")
    positive_int(constructor.get("post_embedding_dim"), "model_config post_embedding_dim")
    if constructor.get("num_transformer_layers") != 1:
        raise ValueError("The inference service requires exactly one transformer layer")
    for feature in ("use_post_liker_feature", "use_target_user_feature"):
        if constructor.get(feature, False) is not False:
            raise ValueError(f"The inference service does not support {feature}")
    for key, expected in (("author_pad_idx", 0), ("author_unk_idx", 1)):
        if key in config and config[key] != expected:
            raise ValueError(f"The inference service requires {key}={expected}")
    return history_len


def check_compatibility(
    config: dict | None, models_manifest: Path | None, fixture_manifest: Path | None
) -> None:
    if config is None:
        return
    validate_config(config)
    encoder = config["embedding_model"].replace("_", "-")
    dimensions = config["constructor_args"]["post_embedding_dim"]
    for path, encoder_key, dimensions_key in (
        (models_manifest, "content_embedding_model", "content_embedding_dims"),
        (fixture_manifest, "embedding_model", "embedding_dims"),
    ):
        # Selection can precede setup. devctl checks again when bundles arrive.
        if path is None or not path.exists():
            continue
        bundled = read_json(path)
        bundled_encoder = required_string(bundled, encoder_key)
        if (
            bundled_encoder.replace("_", "-") != encoder
            or bundled.get(dimensions_key) != dimensions
        ):
            raise ValueError(
                f"Ranker content embeddings ({config['embedding_model']}, {dimensions}) "
                f"disagree with {path} ({bundled_encoder}, {bundled.get(dimensions_key)})"
            )


def select_ranker(args: argparse.Namespace) -> None:
    run_uri, manifest_uri = source_manifest(args.source)
    print(f"devctl: downloading ranker manifest from {manifest_uri}", flush=True)
    runtime = args.runtime.resolve()
    versions = runtime / "rankers"
    versions.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="ranker-", dir=versions))
    selection_temp = None
    published = False
    try:
        manifest_path = stage / "ranker_serving_manifest.json"
        gcloud("cp", manifest_uri, str(manifest_path))
        manifest = read_json(manifest_path)
        model_uri = required_string(manifest, "ranker_uri")
        validate_gcs_uri(model_uri)
        model_id = required_string(manifest, "ranker_clearml_model_id")
        task_id = required_string(manifest, "clearml_task_id")
        if (
            manifest.get("ranker_contract_version") not in (None, 1)
            or manifest.get("post_liker_feature_enabled", False) is not False
        ):
            raise ValueError(
                "This ranker serving contract is not supported by the inference service"
            )

        # List the run's artifacts once: missing optional metadata is distinct
        # from a failed authenticated read, which must leave the old selection.
        print("devctl: locating training artifacts...", flush=True)
        objects = set(gcloud("ls", "--recursive", run_uri + "/artifacts/").splitlines())
        author_maps = sorted(
            uri
            for uri in objects
            if uri.startswith(run_uri + "/artifacts/author_idx_mapping/")
            and uri.endswith(".parquet")
        )
        if len(author_maps) != 1:
            raise ValueError(
                f"Expected one author_idx_mapping parquet for this run, found {len(author_maps)}"
            )

        config = None
        history_len = args.max_history_len
        config_uri = run_uri + "/artifacts/bst_model_config/model_config.json"
        if config_uri in objects:
            print("devctl: downloading and checking training metadata...", flush=True)
            config_path = stage / "model_config.json"
            gcloud("cp", config_uri, str(config_path))
            config = read_json(config_path)
            training_history_len = validate_config(config)
            if history_len is None:
                history_len = training_history_len
        elif history_len is None:
            raise ValueError(
                "This run has no bst_model_config/model_config.json; supply --max-history-len"
            )
        check_compatibility(config, args.models_manifest, args.fixture_manifest)

        print("devctl: downloading author map...", flush=True)
        gcloud("cp", author_maps[0], str(stage / "ranker_author_idx.parquet"))
        print("devctl: downloading ranker weights...", flush=True)
        gcloud("cp", model_uri, str(stage / "ranker.pt"))
        print("devctl: saving local ranker selection...", flush=True)
        manifest["ranker_uri"] = "/ranker/ranker.pt"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        selection = {
            "services": {
                "inference": {
                    "environment": {
                        "GE_INFERENCE_RANKER_MANIFEST_URI": "/ranker/ranker_serving_manifest.json",
                        "GE_INFERENCE_RANKER_AUTHOR_MAP_URI": "/ranker/ranker_author_idx.parquet",
                        "GE_INFERENCE_RANKER_MAX_HISTORY_LEN": str(history_len),
                    },
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(stage),
                            "target": "/ranker",
                            "read_only": True,
                        }
                    ],
                }
            },
            "x-ge-ranker": {
                "source": manifest_uri,
                "ranker_clearml_model_id": model_id,
                "clearml_task_id": task_id,
                "model_config": config,
            },
        }
        # Publish only a complete selection. Keep prior versions because a
        # running container may still have one of those directories mounted.
        with tempfile.NamedTemporaryFile(
            mode="w", dir=runtime, prefix=".ranker-", delete=False
        ) as handle:
            selection_temp = Path(handle.name)
            json.dump(selection, handle, indent=2)
            handle.write("\n")
        selection_temp.replace(runtime / SELECTION_FILE)
        published = True
        print(f"devctl: selected ranker {model_id} (task {task_id}, history {history_len})")
        if config is None:
            print(
                "devctl: legacy run has no model config; "
                "encoder and model feature compatibility could not be checked"
            )
    finally:
        if selection_temp is not None:
            selection_temp.unlink(missing_ok=True)
        if not published:
            shutil.rmtree(stage)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source", nargs="?", help="Specific GCS run URI or ranker_serving_manifest.json URI"
    )
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--models-manifest", type=Path)
    parser.add_argument("--fixture-manifest", type=Path)
    parser.add_argument(
        "--max-history-len",
        type=int,
        help="Override history length; required for legacy runs without model config",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--reset", action="store_true", help="Clear the local selection")
    actions.add_argument("--check", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if (args.reset or args.check) and (args.source or args.max_history_len is not None):
        parser.error("--reset and --check cannot be combined with a source or --max-history-len")
    if not (args.reset or args.check) and args.source is None:
        parser.error("a GCS source or --reset is required")
    try:
        if args.max_history_len is not None:
            positive_int(args.max_history_len, "--max-history-len")
        selection_path = args.runtime / SELECTION_FILE
        if args.reset:
            selection_path.unlink(missing_ok=True)
            print("devctl: cleared the local ranker selection")
        elif args.check:
            if selection_path.exists():
                selection = read_json(selection_path)
                check_compatibility(
                    selection["x-ge-ranker"]["model_config"],
                    args.models_manifest,
                    args.fixture_manifest,
                )
        else:
            select_ranker(args)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"devctl: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
