"""Regression tests for devctl's generated and declarative ngrok configuration."""

import json
import os
import re
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
    Top-level functions end at the first unindented closing brace — ignoring
    heredoc bodies, which are data (a generated config can contain one).
    """
    lines = DEVCTL.read_text().splitlines()
    start = lines.index(f"{name}() {{")
    end = None
    terminator = None
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        heredoc = re.search(r"<<-?\s*'?([A-Za-z_][A-Za-z0-9_]*)'?\s*$", line)
        if heredoc:
            terminator = heredoc.group(1)
        elif line == "}":
            end = index
            break
    assert end is not None, f"no closing brace for {name}()"
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


def shell_constant(name: str) -> str:
    """Expand one top-level assignment from devctl, continuation lines included."""
    lines = DEVCTL.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}="))
    end = start
    while lines[end].endswith("\\"):
        end += 1
    assignment = "\n".join(lines[start : end + 1])
    return subprocess.run(
        ["bash", "-c", f'{assignment}\nprintf "%s" "${name}"'],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def embedded_python(function_name: str) -> str:
    """The single-quoted python program a devctl function passes to python -c."""
    body = shell_function(function_name)
    program = re.search(r"python -c '\n(.*?)\n'", body, re.DOTALL)
    assert program is not None, f"no embedded python program in {function_name}()"
    return program.group(1)


def write_gateway_config(path: Path, api_paths: str) -> None:
    script = "\n".join(
        [
            "set -euo pipefail",
            "umask 077",
            shell_function("bsky_write_gateway_config"),
            'bsky_write_gateway_config "$1" "$2"',
        ]
    )
    subprocess.run(
        ["bash", "-s", "--", str(path), api_paths],
        check=True,
        input=script,
        text=True,
        env={"PATH": os.environ["PATH"]},
    )


def derive_api_paths(tmp_path: Path, paths: list[str], included: list[str] | None = None) -> str:
    """Run the route-walking half of bsky_api_gateway_paths over a fake api.

    The production snippet imports the api's FastAPI app from ``src``; standing
    up a module of that shape is what lets the path derivation — which decides
    what the public hostname exposes — be tested without the api container.
    ``included`` becomes a nested router, the shape FastAPI gives an
    ``include_router`` call and the reason the walk has to recurse at all.
    """
    package = tmp_path / "src" / "app"
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    (package / "main.py").write_text(
        f"""\
import json


class Route:
    def __init__(self, path):
        self.path = path


class Router:
    def __init__(self, routes):
        self.routes = routes


class IncludedRouter:
    def __init__(self, routes):
        self.original_router = Router(routes)


class App:
    routes = [Route(p) for p in json.loads({json.dumps(json.dumps(paths))})]
    included = json.loads({json.dumps(json.dumps(included or []))})
    if included:
        routes = routes + [IncludedRouter([Route(p) for p in included])]


app = App()
"""
    )
    return subprocess.run(
        ["python3", "-c", embedded_python("bsky_api_gateway_paths")],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    ).stdout.strip()


def run_tunnel_host(tmp_path: Path, tunnels: object) -> subprocess.CompletedProcess:
    """Run bsky_tunnel_host against a canned ngrok agent API response.

    Faking curl (and sleep, so the retry loop doesn't really wait) exercises the
    production polling and pooling checks without an ngrok account or a tunnel.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "tunnels.json").write_text(json.dumps(tunnels))
    write_executable(
        bin_dir / "curl",
        """#!/usr/bin/env bash
cat "$TEST_TUNNELS_JSON"
""",
    )
    write_executable(bin_dir / "sleep", "#!/usr/bin/env bash\nexit 0\n")

    script = "\n".join(
        [
            "set -euo pipefail",
            shell_function("bsky_tunnel_host"),
            "bsky_tunnel_host",
        ]
    )
    return subprocess.run(
        ["bash", "-s"],
        input=script,
        text=True,
        capture_output=True,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "TEST_TUNNELS_JSON": str(tmp_path / "tunnels.json"),
            "NGROK_PORT": "4041",
        },
    )


def tunnel_record(public_url: str, addr: str) -> dict:
    return {"public_url": public_url, "config": {"addr": addr}}


def test_ngrok_config_is_readable_and_contains_no_authtoken(tmp_path):
    config = tmp_path / "ngrok.yml"

    write_ngrok_config(config)

    assert (
        config.read_text()
        == """\
version: "2"
web_addr: 0.0.0.0:4040
tunnels:
  public:
    proto: http
    addr: gateway:80
"""
    )
    assert "must-not-be-written" not in config.read_text()
    assert "authtoken" not in config.read_text().lower()
    assert stat.S_IMODE(config.stat().st_mode) == 0o644


def test_ngrok_config_defines_exactly_one_tunnel(tmp_path):
    """Two tunnels share one hostname on the free tier, and ngrok pools them."""
    config = tmp_path / "ngrok.yml"

    write_ngrok_config(config, GE_DEV_NGROK_DOMAIN_FRONTEND="app.ngrok.dev")

    parsed = yaml.safe_load(config.read_text())
    assert list(parsed["tunnels"]) == ["public"]
    assert "app.ngrok.dev" not in config.read_text()


@pytest.mark.parametrize(
    ("environment", "expected_domain"),
    [
        ({"GE_DEV_NGROK_DOMAIN": "shared.ngrok.dev"}, "shared.ngrok.dev"),
        # The pre-gateway name for the same reserved domain still works.
        ({"GE_DEV_NGROK_DOMAIN_API": "api.ngrok.dev"}, "api.ngrok.dev"),
        (
            {
                "GE_DEV_NGROK_DOMAIN": "shared.ngrok.dev",
                "GE_DEV_NGROK_DOMAIN_API": "api.ngrok.dev",
            },
            "shared.ngrok.dev",
        ),
    ],
)
def test_ngrok_config_preserves_domain_selection(tmp_path, environment, expected_domain):
    config = tmp_path / "ngrok.yml"

    write_ngrok_config(config, **environment)

    parsed = yaml.safe_load(config.read_text())
    assert parsed["tunnels"]["public"]["domain"] == expected_domain


def test_gateway_routes_the_api_paths_and_falls_through_to_the_frontend(tmp_path):
    """The routing table is the fix: one hostname, deterministic by path."""
    config = tmp_path / "Caddyfile"

    write_gateway_config(config, "/.well-known/did.json /health /xrpc /xrpc/*")

    lines = [line.strip() for line in config.read_text().splitlines()]
    assert "@api path /.well-known/did.json /health /xrpc /xrpc/*" in lines
    assert "reverse_proxy @api api:8000" in lines
    # Unmatched paths — the app, its assets, the OAuth metadata and callback —
    # fall through to the frontend.
    assert lines.index("reverse_proxy @api api:8000") < lines.index("reverse_proxy frontend:3000")
    assert stat.S_IMODE(config.stat().st_mode) == 0o644


def test_gateway_paths_come_from_the_api_including_nested_routers(tmp_path):
    derived = derive_api_paths(
        tmp_path,
        ["/", "/.well-known/did.json", "/health", "/openapi.json"],
        included=["/xrpc/app.bsky.feed.getFeedSkeleton", "/rank/models", "/diversify"],
    )

    assert derived.split() == [
        # Exact: the neighbouring oauth-client-metadata is the frontend's.
        "/.well-known/did.json",
        # A router with sub-paths gets the prefix and the wildcard; a bare
        # endpoint gets just itself.
        "/diversify",
        "/health",
        "/openapi.json",
        "/rank",
        "/rank/*",
        "/xrpc",
        "/xrpc/*",
    ]


def test_gateway_leaves_the_root_path_to_the_frontend(tmp_path):
    """The api has a root route, but "/" on the tunnel is the app to load."""
    derived = derive_api_paths(tmp_path, ["/", "/health"])

    assert derived.split() == ["/health"]


def test_gateway_fallback_covers_the_feed_generator():
    fallback = shell_constant("BSKY_GATEWAY_FALLBACK_PATHS").split()

    assert "/.well-known/did.json" in fallback
    assert "/xrpc/*" in fallback
    assert "/" not in fallback


def test_tunnel_host_reports_the_single_public_hostname(tmp_path):
    result = run_tunnel_host(
        tmp_path,
        {
            "tunnels": [
                tunnel_record("http://dev-domain.ngrok-free.dev", "http://gateway:80"),
                tunnel_record("https://dev-domain.ngrok-free.dev", "http://gateway:80"),
            ]
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "dev-domain.ngrok-free.dev"


def test_tunnel_host_refuses_a_pooled_hostname(tmp_path):
    """Two endpoints on one URL are load-balanced, not routed — issue #25."""
    result = run_tunnel_host(
        tmp_path,
        {
            "tunnels": [
                tunnel_record("https://dev-domain.ngrok-free.dev", "http://api:8000"),
                tunnel_record("https://dev-domain.ngrok-free.dev", "http://frontend:3000"),
            ]
        },
    )

    assert result.returncode == 2
    assert result.stdout.strip() == ""
    assert "pooled" in result.stderr


def test_tunnel_host_gives_up_when_no_tunnel_appears(tmp_path):
    result = run_tunnel_host(tmp_path, {"tunnels": []})

    assert result.returncode == 1
    assert result.stdout.strip() == ""
    assert "timed out" in result.stderr


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
