"""Regression tests for devctl's generated and declarative ngrok configuration."""

import json
import os
import re
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
    for name in ("check_docker", "resolve_live", "export_api_es_env"):
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


def test_bridgeu_local_mysky_key_is_a_well_formed_api_key():
    # gea_ + 8-char key id + 48-char secret: the api's parse_key_id rejects
    # anything else, which would surface as every BridgEU feed request 401ing.
    key = shell_constant("BRIDGEU_LOCAL_MYSKY_API_KEY")
    assert key.startswith("gea_")
    assert len(key) == 60


def test_bridgeu_key_issuer_is_a_mysky_side_tool():
    """The key lives in the api's Firestore, so the job writing it is mysky's."""
    mysky = yaml.safe_load(COMPOSE_FILE.read_text())["services"]
    issuer = mysky["issue-bridgeu-key"]
    assert issuer["profiles"] == ["tools"]
    assert "GE_DEV_BRIDGEU_MYSKY_API_KEY=${GE_DEV_BRIDGEU_MYSKY_API_KEY:-}" in issuer["environment"]

    bridgeu_api = yaml.safe_load(BRIDGEU_COMPOSE_FILE.read_text())["services"]["bridgeu-api"]
    assert "BRIDGEU_MYSKY_API_KEY=${GE_DEV_BRIDGEU_MYSKY_API_KEY:-}" in bridgeu_api["environment"]


BSKY_SESSION_VARS = (
    "GE_DEV_FEED_GENERATOR_DID",
    "GE_DEV_FRONTEND_ALLOWED_HOSTS",
    "GE_DEV_APP_ORIGIN",
    "GE_DEV_FUNCTIONS_TARGET",
    "BLUESKY_OAUTH_CLIENT_KID",
)


def run_bsky_session_env(
    tmp_path: Path, *, state: bool, ngrok_running: bool, oauth_ok: str = "1"
) -> dict[str, str]:
    state_file = tmp_path / "bsky.env"
    if state:
        state_file.write_text(
            "GE_BSKY_HANDLE=dev.bsky.social\n"
            "GE_BSKY_HOST=abc.ngrok-free.dev\n"
            "GE_BSKY_DID=did:web:abc.ngrok-free.dev\n"
            f"GE_BSKY_OAUTH_OK={oauth_ok}\n"
        )
    oauth_dir = tmp_path / "oauth"
    oauth_dir.mkdir()
    for name, value in [
        ("kid", "kid-1"),
        ("private-key.json", "{}"),
        ("public-jwks.json", "{}"),
        ("state-key", "k"),
    ]:
        (oauth_dir / name).write_text(value)
    script = "\n".join(
        [
            "set -euo pipefail",
            f'BSKY_STATE="{state_file}"',
            f'BSKY_OAUTH_DIR="{oauth_dir}"',
            f"service_running() {{ {'true' if ngrok_running else 'false'}; }}",
            shell_function("env_file_value"),
            shell_function("bsky_export_oauth_env"),
            shell_function("bsky_export_session_env"),
            "bsky_export_session_env",
            *[f'echo "{v}=${{{v}-unset}}"' for v in BSKY_SESSION_VARS],
        ]
    )
    result = subprocess.run(
        ["bash", "-s"],
        input=script,
        text=True,
        capture_output=True,
        env={"PATH": os.environ["PATH"]},
    )
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


def test_bsky_session_env_keeps_the_api_and_frontend_on_the_tunnel(tmp_path):
    env = run_bsky_session_env(tmp_path, state=True, ngrok_running=True)

    assert env == {
        "GE_DEV_FEED_GENERATOR_DID": "did:web:abc.ngrok-free.dev",
        "GE_DEV_FRONTEND_ALLOWED_HOSTS": "localhost,abc.ngrok-free.dev",
        "GE_DEV_APP_ORIGIN": "https://abc.ngrok-free.dev",
        "GE_DEV_FUNCTIONS_TARGET": "firebase:15001",
        "BLUESKY_OAUTH_CLIENT_KID": "kid-1",
    }


def test_bsky_session_env_without_oauth_leaves_sign_in_on_the_shim(tmp_path):
    env = run_bsky_session_env(tmp_path, state=True, ngrok_running=True, oauth_ok="0")

    assert env["GE_DEV_FEED_GENERATOR_DID"] == "did:web:abc.ngrok-free.dev"
    assert env["GE_DEV_APP_ORIGIN"] == "unset"
    assert env["GE_DEV_FUNCTIONS_TARGET"] == "unset"


@pytest.mark.parametrize(("state", "ngrok_running"), [(False, True), (True, False)])
def test_bsky_session_env_leaves_the_stack_local_otherwise(tmp_path, state, ngrok_running):
    env = run_bsky_session_env(tmp_path, state=state, ngrok_running=ngrok_running)

    assert set(env.values()) == {"unset"}


def run_eurosky_session_env(
    tmp_path: Path, *, state: bool, ngrok_running: bool
) -> subprocess.CompletedProcess:
    state_file = tmp_path / "eurosky.env"
    if state:
        state_file.write_text(
            "GE_EUROSKY_HANDLE=dev.eurosky.social\n"
            "GE_EUROSKY_PUBLISHER_DID=did:plc:dev\n"
            "GE_EUROSKY_HOST=abc.ngrok-free.dev\n"
        )
    script = "\n".join(
        [
            "set -euo pipefail",
            f'EUROSKY_STATE="{state_file}"',
            f"service_running() {{ {'true' if ngrok_running else 'false'}; }}",
            shell_function("env_file_value"),
            shell_function("eurosky_export_env"),
            shell_function("eurosky_export_session_env"),
            "eurosky_export_session_env",
            'echo "${GE_DEV_BRIDGEU_HOSTNAME-unset} ${GE_DEV_BRIDGEU_PUBLISHER_DID-unset}'
            ' [${GE_DEV_BRIDGEU_SESSION_SECRET-unset}]"',
        ]
    )
    return subprocess.run(
        ["bash", "-s"],
        input=script,
        text=True,
        capture_output=True,
        env={"PATH": os.environ["PATH"]},
    )


def test_eurosky_session_env_points_bridgeu_api_at_the_tunnel(tmp_path):
    result = run_eurosky_session_env(tmp_path, state=True, ngrok_running=True)

    assert result.returncode == 0, result.stderr
    # The dev session goes off: bridg-eu refuses it on a public hostname.
    assert result.stdout.strip() == "abc.ngrok-free.dev did:plc:dev []"


@pytest.mark.parametrize(("state", "ngrok_running"), [(False, True), (True, False)])
def test_eurosky_session_env_leaves_bridgeu_api_local_otherwise(tmp_path, state, ngrok_running):
    """No session, or one whose tunnel is gone (after `devctl down`): stay local."""
    result = run_eurosky_session_env(tmp_path, state=state, ngrok_running=ngrok_running)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "unset unset [unset]"


def test_eurosky_gateway_sends_everything_to_bridgeu_api(tmp_path):
    config = tmp_path / "Caddyfile"
    subprocess.run(
        ["bash", "-s", "--", str(config)],
        check=True,
        input="\n".join(
            [
                "set -euo pipefail",
                "umask 077",
                shell_function("eurosky_write_gateway_config"),
                'eurosky_write_gateway_config "$1"',
            ]
        ),
        text=True,
        env={"PATH": os.environ["PATH"]},
    )

    lines = [line.strip() for line in config.read_text().splitlines()]
    assert [line for line in lines if line.startswith("reverse_proxy")] == [
        "reverse_proxy bridgeu-api:8000"
    ]
    assert stat.S_IMODE(config.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    ("up", "other_state", "message"),
    [
        ("eurosky_up", "bsky.env", "a bsky session holds the tunnel"),
        ("bsky_up", "eurosky.env", "a eurosky session holds the tunnel"),
    ],
)
def test_bsky_and_eurosky_sessions_exclude_each_other(tmp_path, up, other_state, message):
    """They share one tunnel and gateway, so the second `up` refuses."""
    (tmp_path / other_state).write_text("")
    script = "\n".join(
        [
            "set -euo pipefail",
            'die() { echo "devctl: $*" >&2; exit 1; }',
            "name_flag() { :; }",
            "have_bridgeu() { true; }",
            "service_running() { true; }",
            # Anything past the guard would start real work.
            "bsky_ensure_oauth_keys() { echo 'past the guard'; exit 3; }",
            "eurosky_resolve_did() { echo 'past the guard'; exit 3; }",
            f'BSKY_STATE="{tmp_path / "bsky.env"}"',
            f'EUROSKY_STATE="{tmp_path / "eurosky.env"}"',
            shell_function(up),
            up,
        ]
    )
    result = subprocess.run(
        ["bash", "-s"],
        input=script,
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ["PATH"],
            "NGROK_AUTHTOKEN": "token",
            "GE_DEV_BSKY_HANDLE": "dev.bsky.social",
            "GE_BSKY_APP_PASSWORD": "pw",
            "GE_DEV_EUROSKY_HANDLE": "dev.eurosky.social",
            "GE_DEV_EUROSKY_APP_PASSWORD": "pw",
        },
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stderr


def run_feed(
    tmp_path: Path, *args: str, bridgeu_secret: str = "ge-dev-session", eurosky: bool = False
):
    """Run cmd_feed with docker stubbed out, recording what it would execute."""
    calls_path = tmp_path / "feed-calls"
    eurosky_state = tmp_path / "eurosky.env"
    if eurosky:
        eurosky_state.write_text("GE_EUROSKY_HANDLE=dev.eurosky.social\n")
    script = "\n".join(
        [
            "set -euo pipefail",
            f'EUROSKY_STATE="{eurosky_state}"',
            'die() { echo "devctl: $*" >&2; exit 1; }',
            "service_running() { return 0; }",
            "have_bridgeu() { return 0; }",
            "warn_stale_data() { :; }",
            "live_has() { return 1; }",
            "name_flag() { :; }",
            "BRIDGEU_REPO=bridg-eu; BRIDGEU_CLONE_URL=x; GE_DEV_REPO_ROOT=/r",
            """compose() {
  case "$*" in
  *"printenv BRIDGEU_PUBLISHER_DID") echo did:plc:bridgeupub ;;
  *"printenv BRIDGEU_DEV_SESSION_SECRET") [[ -z "$TEST_SECRET" ]] || echo "$TEST_SECRET" ;;
  esac
}""",
            'compose_exec() { printf "%s\\n" "$@" >"$TEST_CALLS"; }',
            f'BRIDGEU_DEFAULT_FEED="{shell_constant("BRIDGEU_DEFAULT_FEED")}"',
            shell_function("bridgeu_feed_args"),
            shell_function("cmd_feed"),
            'cmd_feed "$@"',
        ]
    )
    result = subprocess.run(
        ["bash", "-s", "--", *args],
        input=script,
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ["PATH"],
            "TEST_CALLS": str(calls_path),
            "TEST_SECRET": bridgeu_secret,
        },
    )
    calls = calls_path.read_text().splitlines() if calls_path.exists() else []
    return result, calls


VIEWER = ["pipenv", "run", "python", "scripts/feed_view.py"]
BRIDGEU_VIEWER = ["-e", "GE_DEV_SESSION_SECRET=ge-dev-session", "api", *VIEWER]


def test_feed_defaults_to_mysky_feeds_on_the_api(tmp_path):
    result, calls = run_feed(tmp_path, "--limit", "5")
    assert result.returncode == 0, result.stderr
    assert calls == ["api", *VIEWER, "--limit", "5"]


def test_bridgeu_feed_defaults_to_its_passthrough_feed(tmp_path):
    result, calls = run_feed(tmp_path, "--bridgeu")
    assert result.returncode == 0, result.stderr
    assert calls == [
        *BRIDGEU_VIEWER,
        "at://did:plc:bridgeupub/app.bsky.feed.generator/passthrough",
        "--api-url",
        "http://bridgeu-api:8000",
        "--no-pipeline",
    ]


def test_bridgeu_feed_names_a_feed_among_viewer_options(tmp_path):
    # --user's value is not the feed; the bare word after it is.
    result, calls = run_feed(
        tmp_path, "--user", "did:plc:me", "other", "--bridgeu", "--limit", "3", "--json"
    )
    assert result.returncode == 0, result.stderr
    assert calls == [
        *BRIDGEU_VIEWER,
        "at://did:plc:bridgeupub/app.bsky.feed.generator/other",
        "--api-url",
        "http://bridgeu-api:8000",
        "--no-pipeline",
        "--user",
        "did:plc:me",
        "--limit",
        "3",
        "--json",
    ]


def test_bridgeu_feed_passes_a_full_uri_through(tmp_path):
    uri = "at://did:plc:else/app.bsky.feed.generator/x"
    result, calls = run_feed(tmp_path, "--bridgeu", uri)
    assert result.returncode == 0, result.stderr
    assert calls[len(BRIDGEU_VIEWER)] == uri


def test_bridgeu_feed_needs_the_dev_session(tmp_path):
    result, calls = run_feed(tmp_path, "--bridgeu", bridgeu_secret="")
    assert result.returncode == 1
    assert "dev session turned off" in result.stderr
    assert calls == []


def test_bridgeu_feed_points_a_eurosky_session_at_a_real_client(tmp_path):
    result, calls = run_feed(tmp_path, "--bridgeu", bridgeu_secret="", eurosky=True)
    assert result.returncode == 1
    assert "serving a eurosky session" in result.stderr
    assert "devctl eurosky down" in result.stderr
    assert calls == []
