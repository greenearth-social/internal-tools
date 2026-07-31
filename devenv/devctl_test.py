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
