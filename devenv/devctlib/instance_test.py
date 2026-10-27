import json

import pytest

from devctlib import instance
from devctlib.ctx import Ctx, Die
from devctlib.testing import FakeRunner, fake


def fresh_ctx(tmp_path, **env) -> Ctx:
    devenv = tmp_path / "internal-tools" / "devenv"
    devenv.mkdir(parents=True)
    return Ctx(devenv=devenv, env=dict(env), runner=FakeRunner())


@pytest.mark.parametrize(
    ("name", "runtime", "config"),
    [
        ("dev", "./.runtime", "firebase.devenv.json"),
        ("alpha", "./.runtime.alpha", "firebase.devenv.alpha.json"),
    ],
)
def test_set_instance_exports_what_compose_reads(tmp_path, name, runtime, config):
    ctx = fresh_ctx(tmp_path)
    instance.set_instance(ctx, name)

    assert ctx.env["GE_DEV_NAME"] == f"ge-{name}"
    assert ctx.env["GE_DEV_RUNTIME"] == runtime
    assert ctx.env["GE_DEV_FIREBASE_CONFIG"] == config
    assert ctx.runtime == ctx.devenv / runtime.removeprefix("./")


@pytest.mark.parametrize("name", ["Alpha", "-x", "a b", "a.b", ""])
def test_set_instance_refuses_bad_names(tmp_path, name):
    with pytest.raises(Die, match="invalid instance name"):
        instance.set_instance(fresh_ctx(tmp_path), name)


def test_load_state_defaults(tmp_path):
    ctx = fresh_ctx(tmp_path)
    instance.set_instance(ctx, "dev")
    instance.load_state(ctx)

    assert ctx.repo_root == tmp_path
    assert ctx.env["GE_DEV_REPO_ROOT"] == str(tmp_path)
    assert (ctx.live, ctx.live_env) == ("", "stage")
    assert ctx.repo_root_configured == ""
    assert (ctx.live_configured, ctx.live_env_configured) == ("", "stage")


def test_load_state_applies_the_record_over_the_configuration(tmp_path):
    """What `up` recorded wins for every command; what was configured is kept
    aside for `up`, which decides afresh from it."""
    ctx = fresh_ctx(
        tmp_path, GE_DEV_REPO_ROOT="/configured", GE_DEV_LIVE="es", GE_DEV_LIVE_ENV="prod"
    )
    instance.set_instance(ctx, "alpha")
    ctx.runtime.mkdir()
    (ctx.runtime / "repo_root.env").write_text("GE_DEV_REPO_ROOT=/recorded\n")
    (ctx.runtime / "live.env").write_text("GE_DEV_LIVE=inference\nGE_DEV_LIVE_ENV=stage\n")
    (ctx.runtime / "share.env").write_text("GE_DEV_SHARE_ES=1\n")

    instance.load_state(ctx)

    assert str(ctx.repo_root) == "/recorded"
    assert (ctx.live, ctx.live_env) == ("inference", "stage")
    assert ctx.env["GE_DEV_SHARE_ES"] == "1"
    assert ctx.repo_root_configured == "/configured"
    assert (ctx.live_configured, ctx.live_env_configured) == ("es", "prod")


def test_state_is_read_from_the_selected_instance(tmp_path):
    ctx = fresh_ctx(tmp_path)
    (ctx.devenv / ".runtime").mkdir()
    (ctx.devenv / ".runtime" / "share.env").write_text("GE_DEV_SHARE_ES=dev-only\n")
    instance.set_instance(ctx, "alpha")
    instance.load_state(ctx)

    assert "GE_DEV_SHARE_ES" not in ctx.env


def test_resolve_ports_for_the_default_instance(tmp_path):
    ctx = fresh_ctx(tmp_path, GE_DEV_PORT_API="9300")
    instance.set_instance(ctx, "dev")

    instance.resolve_ports(ctx)

    assert ctx.offset == 0
    assert ctx.port("API") == 9300
    assert ctx.env["GE_DEV_PORT_API"] == "9300"
    assert ctx.env["GE_DEV_PORT_OFFSET"] == "0"


def test_resolve_ports_refuses_an_unknown_instance(tmp_path):
    ctx = fresh_ctx(tmp_path)
    instance.set_instance(ctx, "ghost")

    with pytest.raises(SystemExit, match="no such instance: 'ghost'"):
        instance.resolve_ports(ctx)
    assert not ctx.runtime.exists()


def test_resolve_ports_allocates_once(tmp_path, monkeypatch):
    monkeypatch.setattr(instance.ports, "port_free", lambda port: True)
    ctx = fresh_ctx(tmp_path)
    instance.set_instance(ctx, "alpha")

    instance.resolve_ports(ctx, "allocate")
    # A second call must not add the offset to already-offset ports.
    instance.resolve_ports(ctx, "allocate")

    assert ctx.offset == 10
    assert ctx.port("API") == 8310
    assert json.loads((ctx.runtime / "ports.json").read_text())["offset"] == 10


def test_ensure_runtime_creates_what_compose_needs(tmp_path):
    ctx = fresh_ctx(tmp_path)
    instance.set_instance(ctx, "dev")

    instance.ensure_runtime(ctx)

    for name in ("es_key_ro.env", "es_key_rw.env", "probe.env"):
        assert (ctx.runtime / name).is_file()
    assert (ctx.devenv / "models" / "data" / "models.env").is_file()
    assert fake(ctx).lines == [
        "docker volume inspect ge-dev-gomod",
        "docker volume inspect ge-dev-gocache",
    ]


def test_check_repos_names_what_is_missing(tmp_path, capsys):
    ctx = fresh_ctx(tmp_path, GE_DEV_REPO_ROOT=str(tmp_path))
    for repo in ("api", "ingex"):
        (tmp_path / repo / ".git").mkdir(parents=True)

    with pytest.raises(Die, match="missing required repos"):
        instance.check_repos(ctx)
    err = capsys.readouterr().err
    assert f"required repo missing: {tmp_path}/frontend" in err
    assert f"required repo missing: {tmp_path}/inference-service" in err
