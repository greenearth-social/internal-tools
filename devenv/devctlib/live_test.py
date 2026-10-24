import json

import pytest

from devctlib import http, live
from devctlib.bridgeu import BRIDGEU_LOCAL_MYSKY_API_KEY
from devctlib.ctx import Die
from devctlib.testing import FakeRunner, fake, make_ctx


def live_ctx(tmp_path, live_services="", live_env="stage", **env):
    return make_ctx(
        tmp_path, env={"GE_DEV_LIVE": live_services, "GE_DEV_LIVE_ENV": live_env, **env}
    )


def mysky_target(ctx):
    return ctx.env["GE_DEV_BRIDGEU_MYSKY_API_URL"], ctx.env["GE_DEV_BRIDGEU_MYSKY_API_KEY"]


# --- resolve_live --------------------------------------------------------------


def test_bridgeu_calls_the_local_api_with_the_local_key_by_default(tmp_path):
    ctx = live_ctx(tmp_path, "inference", GE_DEV_INFERENCE_API_KEY="k")
    live.resolve_live(ctx)
    assert mysky_target(ctx) == ("http://mysky-api:8000", BRIDGEU_LOCAL_MYSKY_API_KEY)


def test_bridgeu_local_mysky_key_is_a_well_formed_api_key():
    # gea_ + 8-char key id + 48-char secret: the api's parse_key_id rejects
    # anything else, which would surface as every BridgEU feed request 401ing.
    assert BRIDGEU_LOCAL_MYSKY_API_KEY.startswith("gea_")
    assert len(BRIDGEU_LOCAL_MYSKY_API_KEY) == 60


def test_bridgeu_ignores_a_live_url_override_when_mysky_is_local(tmp_path):
    ctx = live_ctx(tmp_path, GE_DEV_BRIDGEU_MYSKY_API_URL="https://elsewhere.example")
    live.resolve_live(ctx)
    assert mysky_target(ctx)[0] == "http://mysky-api:8000"


def test_live_mysky_calls_the_prod_api_with_the_real_key(tmp_path):
    ctx = live_ctx(tmp_path, "mysky", "prod", GE_DEV_MYSKY_API_KEY="gea_real")
    live.resolve_live(ctx)
    assert mysky_target(ctx) == ("https://api.greenearth.social", "gea_real")


def test_live_mysky_needs_a_key(tmp_path):
    with pytest.raises(Die, match="GE_DEV_MYSKY_API_KEY"):
        live.resolve_live(live_ctx(tmp_path, "mysky", "prod"))


def test_live_mysky_has_no_stage_default(tmp_path):
    with pytest.raises(Die, match="GE_DEV_BRIDGEU_MYSKY_API_URL"):
        live.resolve_live(live_ctx(tmp_path, "mysky", "stage", GE_DEV_MYSKY_API_KEY="gea_real"))


def test_live_mysky_url_override(tmp_path):
    ctx = live_ctx(
        tmp_path,
        "mysky",
        "stage",
        GE_DEV_MYSKY_API_KEY="gea_real",
        GE_DEV_BRIDGEU_MYSKY_API_URL="https://stage-api.example",
    )
    live.resolve_live(ctx)
    assert mysky_target(ctx) == ("https://stage-api.example", "gea_real")


@pytest.mark.parametrize(
    ("services", "env", "url_var", "url"),
    [
        ("inference", "stage", "GE_DEV_INFERENCE_URL", "https://inference-stage.greenearth.social"),
        ("inference", "prod", "GE_DEV_INFERENCE_URL", "https://inference.greenearth.social"),
        (
            "perspective",
            "stage",
            "GE_DEV_PERSPECTIVE_HOST",
            "https://commentanalyzer.googleapis.com",
        ),
        ("es", "prod", "GE_DEV_ES_URL", "https://host.docker.internal:9200"),
    ],
)
def test_live_services_get_their_urls(tmp_path, services, env, url_var, url):
    keys = {var: "k" for var in live.KEY_VARS.values()}
    ctx = live_ctx(tmp_path, services, env, **keys)
    live.resolve_live(ctx)
    assert ctx.env[url_var] == url


def test_an_explicit_inference_url_wins(tmp_path):
    ctx = live_ctx(
        tmp_path, "inference", GE_DEV_INFERENCE_API_KEY="k", GE_DEV_INFERENCE_URL="http://mine:1"
    )
    live.resolve_live(ctx)
    assert ctx.env["GE_DEV_INFERENCE_URL"] == "http://mine:1"


@pytest.mark.parametrize(
    ("services", "env", "message"),
    [
        ("inference", "dev", r"--live-env must be stage or prod \(got: dev\)"),
        ("nope", "stage", "unknown live service: nope"),
        ("es,inference", "stage", "--live es needs GE_DEV_ES_API_KEY"),
    ],
)
def test_resolve_live_refuses(tmp_path, services, env, message):
    with pytest.raises(Die, match=message):
        live.resolve_live(live_ctx(tmp_path, services, env))


def test_prod_is_called_out(tmp_path, capsys):
    live.resolve_live(live_ctx(tmp_path, "inference", "prod", GE_DEV_INFERENCE_API_KEY="k"))
    assert "using PROD services: inference" in capsys.readouterr().err


def test_resolve_live_reapplies_an_active_tunnel_session(tmp_path):
    """`devctl restart api` or `restart bridgeu-api` after an edit mustn't drop
    the tunnel identity."""
    ctx = live_ctx(tmp_path)
    ctx.runner = FakeRunner(lambda argv: "ngrok" if "ps" in argv else None)
    ctx.bsky_state.write_text("GE_BSKY_HOST=b.ngrok.dev\nGE_BSKY_OAUTH_OK=0\n")
    live.resolve_live(ctx)
    assert ctx.env["GE_DEV_FEED_GENERATOR_DID"] == "did:web:b.ngrok.dev"

    ctx = live_ctx(tmp_path / "e")
    ctx.runner = FakeRunner(lambda argv: "ngrok" if "ps" in argv else None)
    ctx.eurosky_state.write_text("GE_EUROSKY_HOST=e.ngrok.dev\nGE_EUROSKY_PUBLISHER_DID=did:x\n")
    live.resolve_live(ctx)
    assert ctx.env["GE_DEV_BRIDGEU_HOSTNAME"] == "e.ngrok.dev"


def test_record_live_choice(tmp_path):
    ctx = live_ctx(tmp_path, "es,inference", "prod")
    live.record_live_choice(ctx)
    assert ctx.live_state.read_text() == "GE_DEV_LIVE=es,inference\nGE_DEV_LIVE_ENV=prod\n"


# --- preflight ---------------------------------------------------------------


def answering(monkeypatch, answers):
    """http.request answering by path suffix; records what was asked."""
    asked = []

    def request(url, **kwargs):
        asked.append((url, kwargs))
        for suffix, response in answers.items():
            if url.endswith(suffix):
                return response
        return http.Response(0)

    monkeypatch.setattr(http, "request", request)
    return asked


def resolved(tmp_path, services, **env):
    keys = {var: "k" for var in live.KEY_VARS.values()}
    ctx = live_ctx(tmp_path, services, "prod", **{**keys, **env})
    live.resolve_live(ctx)
    return ctx


READ_ONLY = http.Response(
    200, json.dumps({"index": {"*": {p: False for p in live.WRITE_PRIVILEGES}}})
)


def test_preflight_probes_through_the_host(tmp_path, monkeypatch, capsys):
    asked = answering(
        monkeypatch,
        {
            "/ready": http.Response(200),
            "/_cluster/health": http.Response(200),
            "/_has_privileges": READ_ONLY,
        },
    )
    live.preflight_live(resolved(tmp_path, "inference,es"))

    assert [url for url, _ in asked] == [
        "https://inference.greenearth.social/ready",
        "https://127.0.0.1:9200/_cluster/health",
        "https://127.0.0.1:9200/_security/user/_has_privileges",
    ]
    assert "live Elasticsearch key is read-only" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("answers", "message"),
    [
        ({"/ready": http.Response(401)}, "rejected GE_DEV_INFERENCE_API_KEY"),
        ({}, "live inference unreachable at https://inference.greenearth.social"),
        ({"/ready": http.Response(503)}, "returned HTTP 503"),
    ],
)
def test_preflight_inference_failures(tmp_path, monkeypatch, answers, message):
    answering(monkeypatch, answers)
    with pytest.raises(Die, match=message):
        live.preflight_live(resolved(tmp_path, "inference"))


def test_a_closed_tunnel_names_the_command_that_opens_it(tmp_path, monkeypatch):
    answering(monkeypatch, {})
    with pytest.raises(Die, match="is the tunnel open\\?\n        devctl tunnel prod"):
        live.preflight_live(resolved(tmp_path, "es"))


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (
            http.Response(200, json.dumps({"index": {"*": {"write": True, "index": True}}})),
            r"can write to live Elasticsearch \(grants: index,write\)",
        ),
        (http.Response(200, "garbage"), "couldn't verify that GE_DEV_ES_API_KEY is read-only"),
    ],
)
def test_a_write_capable_or_unreadable_key_is_refused(tmp_path, monkeypatch, answer, message):
    answering(monkeypatch, {"/_cluster/health": http.Response(200), "/_has_privileges": answer})
    with pytest.raises(Die, match=message):
        live.preflight_live(resolved(tmp_path, "es"))


def test_the_write_check_can_be_waived(tmp_path, monkeypatch, capsys):
    asked = answering(monkeypatch, {"/_cluster/health": http.Response(200)})
    live.preflight_live(resolved(tmp_path, "es", GE_DEV_ALLOW_ES_WRITE_KEY="1"))
    assert len(asked) == 1
    assert "write check skipped" in capsys.readouterr().err


def test_preflight_mysky(tmp_path, monkeypatch):
    answering(monkeypatch, {"/candidates/generators": http.Response(403)})
    with pytest.raises(Die, match="live mysky api rejected GE_DEV_MYSKY_API_KEY"):
        live.preflight_live(resolved(tmp_path, "mysky"))


# --- tunnel --------------------------------------------------------------------


class Stop(Exception):
    pass


def tunnel(tmp_path, args, *, context="gke_test_context", gcloud_rc=0, **env):
    def respond(argv):
        if argv[0] == "gcloud":
            return gcloud_rc
        if argv[1:3] == ["config", "current-context"]:
            return f"{context}\n" if context else 1
        if "port-forward" in argv:
            raise Stop
        return None

    ctx = make_ctx(tmp_path, runner=FakeRunner(respond), env=env)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("gcloud", "kubectl"):
        (bin_dir / tool).write_text("")
        (bin_dir / tool).chmod(0o755)
    ctx.env["PATH"] = str(bin_dir)
    return ctx


def calls_by_tool(ctx):
    return {
        call.argv[0] + ("-forward" if "port-forward" in call.argv else ""): call.argv
        for call in fake(ctx).calls
    }


@pytest.mark.parametrize("env", ["stage", "prod"])
def test_tunnel_selects_and_pins_the_environment_cluster(tmp_path, capsys, env):
    ctx = tunnel(tmp_path, [env])
    with pytest.raises(Stop):
        live.cmd_tunnel(ctx, [env])

    calls = calls_by_tool(ctx)
    assert calls["gcloud"][1:] == [
        "container",
        "clusters",
        "get-credentials",
        f"greenearth-{env}-cluster",
        "--location=us-east1",
        "--project=greenearth-471522",
    ]
    assert calls["kubectl-forward"][1:] == [
        "--context",
        "gke_test_context",
        "port-forward",
        "svc/greenearth-es-internal-lb",
        "9200:9200",
        "-n",
        f"greenearth-{env}",
        "--address",
        "127.0.0.1",
    ]
    assert "via gke_test_context" in capsys.readouterr().out


def test_tunnel_honors_cluster_and_port_forward_overrides(tmp_path):
    ctx = tunnel(
        tmp_path,
        ["stage"],
        context="other-context",
        GE_DEV_K8S_CLUSTER="other-cluster",
        GE_GCP_REGION="other-region",
        GE_GCP_PROJECT_ID="other-project",
        GE_DEV_ES_NAMESPACE="other-namespace",
        GE_DEV_ES_SERVICE="svc/other-es",
        GE_DEV_ES_TUNNEL_PORT="19200",
        GE_DEV_ES_TUNNEL_BIND="0.0.0.0",
    )
    with pytest.raises(Stop):
        live.cmd_tunnel(ctx, ["stage"])

    calls = calls_by_tool(ctx)
    assert calls["gcloud"][4:] == [
        "other-cluster",
        "--location=other-region",
        "--project=other-project",
    ]
    assert calls["kubectl-forward"][1:] == [
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


def test_tunnel_reconnects_after_a_drop(tmp_path, capsys):
    forwards = []

    def respond(argv):
        if argv[1:3] == ["config", "current-context"]:
            return "ctx\n"
        if "port-forward" in argv:
            forwards.append(argv)
            if len(forwards) == 3:
                raise Stop
            return 1
        return None

    ctx = tunnel(tmp_path, ["stage"])
    ctx.runner = FakeRunner(respond)
    with pytest.raises(Stop):
        live.cmd_tunnel(ctx, ["stage"], sleep=lambda s: None)
    assert len(forwards) == 3
    assert capsys.readouterr().err.count("tunnel dropped, reconnecting") == 2


def test_tunnel_stops_if_cluster_selection_fails(tmp_path):
    ctx = tunnel(tmp_path, ["prod"], gcloud_rc=23)
    with pytest.raises(Die, match="couldn't configure kubectl for greenearth-prod-cluster"):
        live.cmd_tunnel(ctx, ["prod"])
    assert "kubectl-forward" not in calls_by_tool(ctx)


def test_tunnel_stops_if_gcloud_does_not_set_a_context(tmp_path):
    ctx = tunnel(tmp_path, ["stage"], context="")
    with pytest.raises(
        Die, match="gcloud configured greenearth-stage-cluster, but kubectl has no current context"
    ):
        live.cmd_tunnel(ctx, ["stage"])


def test_tunnel_refuses_another_environment(tmp_path):
    with pytest.raises(Die, match=r"usage: devctl tunnel \[stage\|prod\]"):
        live.cmd_tunnel(make_ctx(tmp_path), ["dev"])
