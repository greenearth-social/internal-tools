"""Regression tests for devctl's generated and declarative ngrok configuration."""

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

DEVENV_DIR = Path(__file__).parent
DEVCTL = DEVENV_DIR / "devctl"
COMPOSE_FILE = DEVENV_DIR / "docker-compose.yml"


def shell_function(name: str) -> str:
    """Return one top-level shell function verbatim from devctl.

    devctl is an executable orchestrator rather than a sourceable shell library.
    Extracting the function lets the test execute the production implementation
    without invoking Docker or adding a test-only command to the user-facing CLI.
    Top-level functions end at the first unindented closing brace.
    """
    lines = DEVCTL.read_text().splitlines()
    start = lines.index(f"{name}() {{")
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def write_executable(path: Path, contents: str) -> None:
    path.write_text(contents)
    path.chmod(0o755)


def run_tunnel(
    tmp_path: Path, environment_name: str, **environment: str
) -> subprocess.CompletedProcess:
    """Run cmd_tunnel against fake gcloud/kubectl executables.

    The fake port-forward records its arguments and terminates the parent shell,
    allowing the production reconnect loop to be tested without leaving a
    background process behind.
    """
    bin_dir = tmp_path / "bin"
    log_dir = tmp_path / "calls"
    bin_dir.mkdir()
    log_dir.mkdir()

    write_executable(
        bin_dir / "gcloud",
        """#!/usr/bin/env bash
printf '%s\n' "$@" >"$TEST_LOG_DIR/gcloud"
printf 'kubeconfig entry generated\n'
exit "${TEST_GCLOUD_EXIT:-0}"
""",
    )
    write_executable(
        bin_dir / "kubectl",
        """#!/usr/bin/env bash
set -eu
if [[ "${1:-}" == config && "${2:-}" == current-context ]]; then
  printf '%s\n' "$TEST_KUBE_CONTEXT"
  exit 0
fi
printf '%s\n' "$@" >"$TEST_LOG_DIR/port-forward"
kill -TERM "$PPID"
""",
    )

    script = "\n".join(
        [
            "set -u",
            'die() { echo "devctl: $*" >&2; exit 1; }',
            shell_function("configure_tunnel_context"),
            shell_function("cmd_tunnel"),
            'cmd_tunnel "$1"',
        ]
    )
    process_environment = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TEST_LOG_DIR": str(log_dir),
        "TEST_KUBE_CONTEXT": "gke_test_context",
        **environment,
    }
    return subprocess.run(
        ["bash", "-s", "--", environment_name],
        input=script,
        text=True,
        capture_output=True,
        env=process_environment,
    )


def write_ngrok_config(path: Path, **environment: str) -> None:
    script = "\n".join(
        [
            "set -euo pipefail",
            # Prove the function deliberately relaxes a secret-safe umask after
            # removing the token, rather than inheriting a coincidental 0644.
            "umask 077",
            shell_function("bsky_write_ngrok_config"),
            'bsky_write_ngrok_config "$1"',
        ]
    )
    env = {
        "PATH": os.environ["PATH"],
        "NGROK_AUTHTOKEN": "must-not-be-written",
        **environment,
    }
    subprocess.run(
        ["bash", "-s", "--", str(path)],
        check=True,
        input=script,
        text=True,
        env=env,
    )


def test_ngrok_config_is_readable_and_contains_no_authtoken(tmp_path):
    config = tmp_path / "ngrok.yml"

    write_ngrok_config(config)

    assert (
        config.read_text()
        == """\
version: "2"
web_addr: 0.0.0.0:4040
tunnels:
  api:
    proto: http
    addr: api:8000
  frontend:
    proto: http
    addr: frontend:3000
"""
    )
    assert "must-not-be-written" not in config.read_text()
    assert "authtoken" not in config.read_text().lower()
    assert stat.S_IMODE(config.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    ("environment", "expected_api_domain"),
    [
        ({"GE_DEV_NGROK_DOMAIN": "shared.ngrok.dev"}, "shared.ngrok.dev"),
        (
            {
                "GE_DEV_NGROK_DOMAIN": "shared.ngrok.dev",
                "GE_DEV_NGROK_DOMAIN_API": "api.ngrok.dev",
            },
            "api.ngrok.dev",
        ),
    ],
)
def test_ngrok_config_preserves_api_domain_selection(tmp_path, environment, expected_api_domain):
    config = tmp_path / "ngrok.yml"

    write_ngrok_config(config, **environment)

    parsed = yaml.safe_load(config.read_text())
    assert parsed["tunnels"]["api"]["domain"] == expected_api_domain


def test_ngrok_config_includes_the_optional_frontend_domain(tmp_path):
    config = tmp_path / "ngrok.yml"

    write_ngrok_config(config, GE_DEV_NGROK_DOMAIN_FRONTEND="app.ngrok.dev")

    parsed = yaml.safe_load(config.read_text())
    assert parsed["tunnels"]["frontend"]["domain"] == "app.ngrok.dev"


def test_compose_forwards_authtoken_without_running_ngrok_as_root():
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    ngrok = compose["services"]["ngrok"]

    assert ngrok["environment"] == ["NGROK_AUTHTOKEN"]
    assert "user" not in ngrok
    assert ngrok["volumes"] == ["${GE_DEV_RUNTIME:-./.runtime}/ngrok.yml:/etc/ngrok.yml:ro"]


@pytest.mark.parametrize("environment_name", ["stage", "prod"])
def test_tunnel_selects_and_pins_the_environment_cluster(tmp_path, environment_name):
    result = run_tunnel(tmp_path, environment_name)

    # The fake port-forward terminates the shell after recording the first call.
    assert result.returncode != 0
    assert (tmp_path / "calls" / "gcloud").read_text().splitlines() == [
        "container",
        "clusters",
        "get-credentials",
        f"greenearth-{environment_name}-cluster",
        "--location=us-east1",
        "--project=greenearth-471522",
    ]
    assert (tmp_path / "calls" / "port-forward").read_text().splitlines() == [
        "--context",
        "gke_test_context",
        "port-forward",
        "svc/greenearth-es-internal-lb",
        "9200:9200",
        "-n",
        f"greenearth-{environment_name}",
        "--address",
        "127.0.0.1",
    ]
    assert "via gke_test_context" in result.stdout


def test_tunnel_honors_cluster_and_port_forward_overrides(tmp_path):
    result = run_tunnel(
        tmp_path,
        "stage",
        GE_DEV_K8S_CLUSTER="other-cluster",
        GE_GCP_REGION="other-region",
        GE_GCP_PROJECT_ID="other-project",
        GE_DEV_ES_NAMESPACE="other-namespace",
        GE_DEV_ES_SERVICE="svc/other-es",
        GE_DEV_ES_TUNNEL_PORT="19200",
        GE_DEV_ES_TUNNEL_BIND="0.0.0.0",
        TEST_KUBE_CONTEXT="other-context",
    )

    assert result.returncode != 0
    assert (tmp_path / "calls" / "gcloud").read_text().splitlines() == [
        "container",
        "clusters",
        "get-credentials",
        "other-cluster",
        "--location=other-region",
        "--project=other-project",
    ]
    assert (tmp_path / "calls" / "port-forward").read_text().splitlines() == [
        "--context",
        "other-context",
        "port-forward",
        "svc/other-es",
        "19200:9200",
        "-n",
        "other-namespace",
        "--address",
        "0.0.0.0",
    ]


def test_tunnel_stops_if_cluster_selection_fails(tmp_path):
    result = run_tunnel(tmp_path, "prod", TEST_GCLOUD_EXIT="23")

    assert result.returncode == 1
    assert "couldn't configure kubectl for greenearth-prod-cluster" in result.stderr
    assert not (tmp_path / "calls" / "port-forward").exists()


def test_tunnel_stops_if_gcloud_does_not_set_a_context(tmp_path):
    result = run_tunnel(tmp_path, "stage", TEST_KUBE_CONTEXT="")

    assert result.returncode == 1
    assert "gcloud configured greenearth-stage-cluster, but kubectl has no current context" in (
        result.stderr
    )
    assert not (tmp_path / "calls" / "port-forward").exists()
