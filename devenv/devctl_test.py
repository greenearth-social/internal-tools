"""Regression tests for devctl's generated and declarative ngrok configuration."""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from devctlib import stack
from devctlib.compose import compose_argv
from devctlib.ctx import Ctx
from devctlib.runner import Runner
from devctlib.testing import devctl_argv

DEVENV_DIR = Path(__file__).parent
# The bash original, until every command is ported to devctlib; tests of its
# functions read them from here.
DEVCTL = DEVENV_DIR / "devctl-legacy"
COMPOSE_FILE = DEVENV_DIR / "docker-compose.yml"
BRIDGEU_COMPOSE_FILE = DEVENV_DIR / "docker-compose.bridgeu.yml"
FIREBASE_UI_PROXY_TEST = DEVENV_DIR / "firebase" / "ui-proxy_test.mjs"


def run_devctl(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the real devctl with a docker that only records that it was called."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    marker = tmp_path / "docker-called"
    docker = bin_dir / "docker"
    docker.write_text(f'#!/bin/sh\necho "$@" >>"{marker}"\nexit 1\n')
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    result = subprocess.run(
        devctl_argv(DEVENV_DIR, *args),
        text=True,
        capture_output=True,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path)},
    )
    return result, marker


@pytest.mark.parametrize(
    "command", ["seed", "nuke", "down", "up", "bootstrap", "setup", "test", "use-ranker"]
)
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_flag_prints_usage_without_touching_docker(tmp_path, command, flag):
    # Issue #42: `seed --help` used to ignore the flag and wipe the seeded data.
    result, marker = run_devctl(tmp_path, "--name", "helptest", command, flag)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Usage: devctl" in result.stdout
    assert not marker.exists(), marker.read_text()


@pytest.mark.parametrize("command", ["seed", "nuke", "down", "status"])
def test_argumentless_commands_refuse_stray_arguments(tmp_path, command):
    result, marker = run_devctl(tmp_path, command, "oops")

    assert result.returncode == 1
    assert f"{command} takes no arguments" in result.stderr
    assert not marker.exists(), marker.read_text()


@pytest.fixture
def ranker_cli(tmp_path):
    """Run the full CLI in a fresh checkout with fake GCS and no Docker daemon."""
    devenv = tmp_path / "internal-tools" / "devenv"
    (devenv / "models").mkdir(parents=True)
    shutil.copy2(DEVCTL, devenv / DEVCTL.name)
    shutil.copytree(
        DEVENV_DIR / "devctlib", devenv / "devctlib", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copy2(DEVENV_DIR / "models" / "use_ranker.py", devenv / "models")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_calls = tmp_path / "docker-calls"
    write_executable(bin_dir / "docker", '#!/bin/sh\necho "$@" >>"$DOCKER_CALLS"\nexit 1\n')
    run_uri = "gs://rankers/Engagement Prediction/train_bst-ranker.task-1"
    manifest_uri = run_uri + "/artifacts/ranker_serving_manifest/ranker_serving_manifest.json"
    objects = {
        manifest_uri: json.dumps(
            {
                "ranker_uri": run_uri + "/models/ranker.pt",
                "ranker_clearml_model_id": "ranker-1",
                "clearml_task_id": "task-1",
            }
        ),
        run_uri + "/artifacts/bst_model_config/model_config.json": json.dumps(
            {
                "model_type": "bst-ranker",
                "embedding_model": "all_MiniLM_L12_v2",
                "max_history_len": 64,
                "constructor_args": {"post_embedding_dim": 384, "num_transformer_layers": 1},
            }
        ),
        run_uri + "/artifacts/author_idx_mapping/ranker_author_idx.parquet": "author map",
        run_uri + "/models/ranker.pt": "weights",
    }
    objects_path = tmp_path / "gcs.json"
    objects_path.write_text(json.dumps(objects))
    write_executable(
        bin_dir / "gcloud",
        """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
objects = json.loads(Path(os.environ["GCS_OBJECTS"]).read_text())
args = sys.argv[1:]
if args[:2] == ["storage", "ls"]:
    print("\\n".join(uri for uri in objects if uri.startswith(args[-1])))
elif args[:2] == ["storage", "cp"]:
    Path(args[-1]).write_text(objects[args[-2]])
else:
    sys.exit("Unexpected gcloud invocation: " + repr(args))
""",
    )
    environment = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "DOCKER_CALLS": str(docker_calls),
        "GCS_OBJECTS": str(objects_path),
    }

    def run(*args):
        return subprocess.run(
            devctl_argv(devenv, *args),
            env=environment,
            text=True,
            capture_output=True,
        )

    return run, devenv, run_uri, manifest_uri, docker_calls


@pytest.mark.parametrize("source_kind", ["run", "manifest"])
def test_use_ranker_prepares_fresh_named_instance_without_docker(ranker_cli, source_kind):
    run, devenv, run_uri, manifest_uri, docker_calls = ranker_cli
    source = run_uri if source_kind == "run" else manifest_uri

    result = run("use-ranker", source, "--name", "candidate")

    assert result.returncode == 0, result.stdout + result.stderr
    runtime = devenv / ".runtime.candidate"
    selection = json.loads((runtime / "ranker.compose.json").read_text())
    inference = selection["services"]["inference"]
    assert inference["environment"]["GE_INFERENCE_RANKER_MAX_HISTORY_LEN"] == "64"
    artifacts = Path(inference["volumes"][0]["source"])
    assert artifacts.is_relative_to(runtime)
    assert (artifacts / "ranker.pt").read_text() == "weights"
    assert not (runtime / "ports.json").exists()
    assert not (devenv / ".runtime").exists()
    assert not docker_calls.exists()
    assert "devctl restart --name candidate inference" in result.stdout


def test_use_ranker_reset_preserves_artifacts_and_other_instances(ranker_cli):
    run, devenv, run_uri, _, docker_calls = ranker_cli
    for name in ("dev", "candidate"):
        result = run("use-ranker", run_uri, "--name", name)
        assert result.returncode == 0, result.stdout + result.stderr
    runtime = devenv / ".runtime.candidate"
    selection = json.loads((runtime / "ranker.compose.json").read_text())
    artifacts = Path(selection["services"]["inference"]["volumes"][0]["source"])

    result = run("use-ranker", "--reset", "--name", "candidate")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (runtime / "ranker.compose.json").exists()
    assert (artifacts / "ranker.pt").exists()
    assert (devenv / ".runtime" / "ranker.compose.json").exists()
    assert not docker_calls.exists()


@pytest.mark.parametrize("flag", ["--restart", "--latest", "--max-history-len", "--unknown"])
def test_use_ranker_rejects_unsupported_flags_without_docker(ranker_cli, flag):
    run, devenv, run_uri, _, docker_calls = ranker_cli

    result = run("use-ranker", run_uri, flag, "--name", "candidate")

    assert result.returncode != 0
    assert "unrecognized arguments" in result.stderr
    assert not (devenv / ".runtime.candidate").exists()
    assert not docker_calls.exists()


def test_compose_applies_ranker_override_only_to_selected_instance(ranker_cli):
    run, devenv, run_uri, _, _ = ranker_cli
    result = run("use-ranker", run_uri, "--name", "candidate")
    assert result.returncode == 0, result.stdout + result.stderr

    for name in ("candidate", "other"):
        arguments = compose_argv(Ctx(devenv=devenv, env={}, instance=name), "config")
        assert arguments[:6] == [
            "docker",
            "compose",
            "-f",
            str(devenv / "docker-compose.yml"),
            "-f",
            str(devenv / "docker-compose.bridgeu.yml"),
        ]
        override = str(devenv / ".runtime.candidate" / "ranker.compose.json")
        assert (override in arguments) == (name == "candidate")


class ToolsOnlyRunner(Runner):
    """Runs devenv's own Python helpers; anything else is a service operation
    the compatibility check should have stopped before."""

    def run(self, argv, **kwargs):
        if argv[0] != sys.executable:
            raise AssertionError(f"unexpected service operation: {argv}")
        return super().run(argv, **kwargs)


@pytest.mark.parametrize("entrypoint", ["check_model_fixture_compat", "cmd_restart inference"])
def test_ranker_compatibility_checked_when_bundle_arrives_after_selection(
    ranker_cli, monkeypatch, capfd, entrypoint
):
    run, devenv, run_uri, _, docker_calls = ranker_cli
    result = run("use-ranker", run_uri)
    assert result.returncode == 0, result.stdout + result.stderr
    bundle = devenv / "models" / "data"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "content_embedding_model": "different-encoder",
                "content_embedding_dims": 384,
            }
        )
    )
    for name in ("check_docker", "resolve_live", "export_api_env"):
        monkeypatch.setattr(stack, name, lambda ctx: None)
    ctx = Ctx(devenv=devenv, env=dict(os.environ), runner=ToolsOnlyRunner())

    with pytest.raises(SystemExit) as exc:
        if entrypoint == "check_model_fixture_compat":
            stack.check_model_fixture_compat(ctx)
        else:
            stack.cmd_restart(ctx, ["inference"])

    assert exc.value.code == 1
    assert "disagree" in capfd.readouterr().err
    assert not docker_calls.exists()


def test_quality_backfill_uses_local_es_and_the_ingest_checkout():
    services = yaml.safe_load(COMPOSE_FILE.read_text())["services"]
    quality = services["seed-quality"]
    ingest = services["seed-megastream"]

    assert quality["command"] == ["go", "run", "./cmd/backfill_quality_index"]
    assert "GE_ELASTICSEARCH_URL=http://elasticsearch:9200" in quality["environment"]
    assert "GE_INDEX_PERIOD=week" in quality["environment"]
    assert quality["env_file"] == ingest["env_file"]
    assert "${GE_DEV_REPO_ROOT:-../..}/ingex/ingest:/src" in quality["volumes"]
    assert "inference" not in quality.get("depends_on", {})


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_firebase_ui_proxy_rewrites_named_instance_ports():
    result = subprocess.run(
        ["node", "--test", str(FIREBASE_UI_PROXY_TEST)],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def write_executable(path: Path, contents: str) -> None:
    path.write_text(contents)
    path.chmod(0o755)


def test_compose_forwards_authtoken_without_running_ngrok_as_root():
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    ngrok = compose["services"]["ngrok"]

    assert ngrok["environment"] == ["NGROK_AUTHTOKEN"]
    assert "user" not in ngrok
    assert ngrok["volumes"] == ["${GE_DEV_RUNTIME:-./.runtime}/ngrok.yml:/etc/ngrok.yml:ro"]


def test_compose_tunnels_into_the_gateway_and_starts_neither_by_default():
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    gateway = compose["services"]["gateway"]

    assert gateway["profiles"] == ["bsky"]
    assert compose["services"]["ngrok"]["profiles"] == ["bsky"]
    assert gateway["volumes"] == ["${GE_DEV_RUNTIME:-./.runtime}/Caddyfile:/etc/caddy/Caddyfile:ro"]
    # The gateway is only reachable from the compose network: the tunnel is the
    # one way in, so nothing is exposed on the host when no session is up.
    assert "ports" not in gateway


def test_bridgeu_services_reach_mysky_only_through_the_api():
    """BridgEU talks to mysky over the api's HTTP surface and nothing else.

    In production the two run on different clouds; here the network layout is
    what keeps a BridgEU service from quietly depending on mysky's
    Elasticsearch or Firestore. Mysky services stay on the default network
    (they declare none), BridgEU services sit only on `bridgeu`, and the api is
    the single service on both.
    """
    mysky = yaml.safe_load(COMPOSE_FILE.read_text())["services"]
    bridgeu = yaml.safe_load(BRIDGEU_COMPOSE_FILE.read_text())["services"]

    assert all("networks" not in svc for svc in mysky.values())

    api = bridgeu.pop("api")
    assert set(api) == {"networks"}, "the BridgEU file only adds the api to its network"
    assert set(api["networks"]) == {"default", "bridgeu"}
    assert api["networks"]["bridgeu"]["aliases"] == ["mysky-api"]

    # The tunnel gateway joins too, to route a `devctl eurosky` session to
    # bridgeu-api. It's a profile-gated reverse proxy that never runs by default.
    gateway = bridgeu.pop("gateway")
    assert gateway == {"networks": {"default": {}, "bridgeu": {}}}
    assert mysky["gateway"]["profiles"] == ["bsky"]

    assert bridgeu, "expected at least one BridgEU service"
    for name, svc in bridgeu.items():
        assert name.startswith("bridgeu-"), name
        assert name not in mysky, f"{name} would merge into a mysky service"
        assert svc["networks"] == ["bridgeu"], name
        assert "depends_on" not in svc or set(svc["depends_on"]) <= set(bridgeu), name
        env = " ".join(svc.get("environment", []))
        for mysky_store in ("elasticsearch", "firebase:"):
            assert mysky_store not in env, f"{name} addresses {mysky_store} directly"


def test_bridgeu_key_issuer_is_a_mysky_side_tool():
    """The key lives in the api's Firestore, so the job writing it is mysky's."""
    mysky = yaml.safe_load(COMPOSE_FILE.read_text())["services"]
    issuer = mysky["issue-bridgeu-key"]
    assert issuer["profiles"] == ["tools"]
    assert "GE_DEV_BRIDGEU_MYSKY_API_KEY=${GE_DEV_BRIDGEU_MYSKY_API_KEY:-}" in issuer["environment"]

    bridgeu_api = yaml.safe_load(BRIDGEU_COMPOSE_FILE.read_text())["services"]["bridgeu-api"]
    assert "BRIDGEU_MYSKY_API_KEY=${GE_DEV_BRIDGEU_MYSKY_API_KEY:-}" in bridgeu_api["environment"]
