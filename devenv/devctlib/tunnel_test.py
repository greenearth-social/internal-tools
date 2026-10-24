import json
import stat
import subprocess
import sys

import pytest
import yaml

from devctlib import bsky, http, incontainer, tunnel
from devctlib.testing import FakeRunner, fake, make_ctx


def test_ngrok_config_is_readable_and_contains_no_authtoken(tmp_path):
    ctx = make_ctx(tmp_path, env={"NGROK_AUTHTOKEN": "must-not-be-written"})
    config = tmp_path / "ngrok.yml"

    tunnel.write_ngrok_config(ctx, config)

    assert config.read_text() == (
        'version: "2"\n'
        "web_addr: 0.0.0.0:4040\n"
        "tunnels:\n"
        "  public:\n"
        "    proto: http\n"
        "    addr: gateway:80\n"
    )
    assert "authtoken" not in config.read_text().lower()
    assert stat.S_IMODE(config.stat().st_mode) == 0o644


def test_ngrok_config_defines_exactly_one_tunnel(tmp_path):
    """Two tunnels share one hostname on the free tier, and ngrok pools them."""
    ctx = make_ctx(tmp_path, env={"GE_DEV_NGROK_DOMAIN_FRONTEND": "app.ngrok.dev"})
    config = tmp_path / "ngrok.yml"

    tunnel.write_ngrok_config(ctx, config)

    assert list(yaml.safe_load(config.read_text())["tunnels"]) == ["public"]
    assert "app.ngrok.dev" not in config.read_text()


@pytest.mark.parametrize(
    ("environment", "expected_domain"),
    [
        ({"GE_DEV_NGROK_DOMAIN": "shared.ngrok.dev"}, "shared.ngrok.dev"),
        # The pre-gateway name for the same reserved domain still works.
        ({"GE_DEV_NGROK_DOMAIN_API": "api.ngrok.dev"}, "api.ngrok.dev"),
        (
            {"GE_DEV_NGROK_DOMAIN": "shared.ngrok.dev", "GE_DEV_NGROK_DOMAIN_API": "api.ngrok.dev"},
            "shared.ngrok.dev",
        ),
    ],
)
def test_ngrok_config_preserves_domain_selection(tmp_path, environment, expected_domain):
    config = tmp_path / "ngrok.yml"
    tunnel.write_ngrok_config(make_ctx(tmp_path, env=environment), config)
    assert yaml.safe_load(config.read_text())["tunnels"]["public"]["domain"] == expected_domain


def test_gateway_routes_the_api_paths_and_falls_through_to_the_frontend(tmp_path):
    """The routing table is the fix: one hostname, deterministic by path."""
    config = tmp_path / "Caddyfile"

    bsky.write_gateway_config(config, "/.well-known/did.json /health /xrpc /xrpc/*")

    text = config.read_text()
    lines = [line.strip() for line in text.splitlines()]
    assert "@api path /.well-known/did.json /health /xrpc /xrpc/*" in lines
    assert "reverse_proxy @api api:8000" in lines
    # Unmatched paths — the app, its assets, the OAuth metadata and callback —
    # fall through to the frontend.
    assert lines.index("reverse_proxy @api api:8000") < lines.index("reverse_proxy frontend:3000")
    assert "trusted_proxies static private_ranges" in lines
    assert stat.S_IMODE(config.stat().st_mode) == 0o644


def test_eurosky_gateway_sends_everything_to_bridgeu_api(tmp_path):
    config = tmp_path / "Caddyfile"
    tunnel.write_caddyfile(config, "reverse_proxy bridgeu-api:8000")
    lines = [line.strip() for line in config.read_text().splitlines()]
    assert [line for line in lines if line.startswith("reverse_proxy")] == [
        "reverse_proxy bridgeu-api:8000"
    ]


def derive_api_paths(tmp_path, paths, included=None):
    """Run incontainer/api_gateway_paths.py over a fake api.

    The program imports the api's FastAPI app from `src`; standing up a module
    of that shape is what lets the path derivation — which decides what the
    public hostname exposes — be tested without the api container. `included`
    becomes a nested router, the shape FastAPI gives an include_router call and
    the reason the walk has to recurse at all.
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
        [sys.executable, "-c", incontainer.source("api_gateway_paths")],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    ).stdout.strip()


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
    assert derive_api_paths(tmp_path, ["/", "/health"]).split() == ["/health"]


def test_gateway_fallback_covers_the_feed_generator():
    fallback = bsky.GATEWAY_FALLBACK_PATHS.split()
    assert "/.well-known/did.json" in fallback
    assert "/xrpc/*" in fallback
    assert "/" not in fallback


def tunnel_record(public_url, addr):
    return {"public_url": public_url, "config": {"addr": addr}}


def ngrok_reports(monkeypatch, tunnels):
    monkeypatch.setattr(
        http, "request", lambda url, **kw: http.Response(200, json.dumps({"tunnels": tunnels}))
    )


def test_tunnel_host_reports_the_single_public_hostname(tmp_path, monkeypatch):
    ngrok_reports(
        monkeypatch,
        [
            tunnel_record("http://dev-domain.ngrok-free.dev", "http://gateway:80"),
            tunnel_record("https://dev-domain.ngrok-free.dev", "http://gateway:80"),
        ],
    )
    assert tunnel.host(make_ctx(tmp_path), sleep=lambda s: None) == "dev-domain.ngrok-free.dev"


def test_tunnel_host_refuses_a_pooled_hostname(tmp_path, monkeypatch, capsys):
    """Two endpoints on one URL are load-balanced, not routed — issue #25."""
    ngrok_reports(
        monkeypatch,
        [
            tunnel_record("https://dev-domain.ngrok-free.dev", "http://api:8000"),
            tunnel_record("https://dev-domain.ngrok-free.dev", "http://frontend:3000"),
        ],
    )
    assert tunnel.host(make_ctx(tmp_path), sleep=lambda s: None) is None
    assert "pooled" in capsys.readouterr().err


def test_tunnel_host_gives_up_when_no_tunnel_appears(tmp_path, monkeypatch, capsys):
    ngrok_reports(monkeypatch, [])
    assert tunnel.host(make_ctx(tmp_path), sleep=lambda s: None) is None
    assert "timed out" in capsys.readouterr().err


def test_start_shows_why_a_container_exited(tmp_path, capsys):
    def respond(argv):
        if "ps" in argv:
            return "gateway\n"
        if "logs" in argv:
            return "ERR_NGROK_334 domain in use\n"
        return None

    ctx = make_ctx(tmp_path, runner=FakeRunner(respond))
    assert tunnel.start(ctx, sleep=lambda s: None) is False
    err = capsys.readouterr().err
    assert "the ngrok container exited on startup" in err
    assert "        ERR_NGROK_334" in err
    assert (ctx.runtime / "ngrok.yml").exists()
    assert fake(ctx).compose_lines()[0] == "--profile bsky up -d --force-recreate gateway ngrok"
