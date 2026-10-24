import json

import pytest

from devctlib import stack
from devctlib.ctx import Die
from devctlib.runner import CommandFailed
from devctlib.testing import FakeRunner, fake, make_ctx


def with_models(ctx):
    data = ctx.devenv / "models" / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "manifest.json").write_text(json.dumps({"tag": "t"}))


def docker_on_path(ctx, tmp_path):
    """check_docker looks for the binary itself before asking the runner."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text("")
    (bin_dir / "docker").chmod(0o755)
    ctx.env["PATH"] = str(bin_dir)


def with_repos(tmp_path, *repos):
    for repo in ("api", "ingex", "frontend", "inference-service", *repos):
        (tmp_path / repo / ".git").mkdir(parents=True)


@pytest.mark.parametrize(
    ("bridgeu", "built"), [(False, "seed-firestore-users"), (True, "bridgeu-api")]
)
def test_setup_builds_pulls_and_installs(tmp_path, capsys, bridgeu, built):
    ctx = make_ctx(tmp_path)
    docker_on_path(ctx, tmp_path)
    with_repos(tmp_path, *(["bridg-eu"] if bridgeu else []))
    with_models(ctx)

    stack.cmd_setup(ctx, [])

    calls = fake(ctx).compose_lines()
    assert calls[0].startswith("--profile tools build es-init api inference")
    assert calls[0].endswith(built)
    assert calls[1] == (
        "--profile tools pull elasticsearch perspective-stub dev-auth seed-rebase"
        " seed-megastream --ignore-buildable"
    )
    assert calls[2] == "run --rm --no-deps inference bash /inference/start.sh --install-only"
    captured = capsys.readouterr()
    assert "setup complete" in captured.out
    assert "No fixture set found" in captured.err


def test_setup_stops_if_the_inference_install_fails(tmp_path):
    ctx = make_ctx(
        tmp_path, runner=FakeRunner(lambda argv: 1 if "--install-only" in argv else None)
    )
    docker_on_path(ctx, tmp_path)
    with_repos(tmp_path)
    with_models(ctx)

    with pytest.raises(Die, match="failed to install inference-service dependencies"):
        stack.cmd_setup(ctx, [])


# --- seed ----------------------------------------------------------------------


def seed_ctx(tmp_path, fail: str = "", running: bool = True):
    def respond(argv):
        line = " ".join(argv)
        if fail and line.endswith(fail):
            return 23
        if "ps" in argv and "running" in argv:
            return "api" if running else ""
        return None

    ctx = make_ctx(tmp_path, runner=FakeRunner(respond))
    docker_on_path(ctx, tmp_path)
    fixtures = ctx.devenv / "fixtures" / "data"
    fixtures.mkdir(parents=True)
    (fixtures / "manifest.json").write_text("{}")
    for name in ("es_key_ro.env", "es_key_rw.env"):
        (ctx.runtime / name).write_text("GE_ELASTICSEARCH_API_KEY=k\n")
    (ctx.runtime / "probe.env").write_text("GE_PROBE_USER_DID=did:plc:p\n")
    return ctx


def service_calls(ctx):
    return [c for c in fake(ctx).compose_lines() if not c.startswith("ps ")]


def test_seed_backfills_quality_after_likes_before_publishing_aliases_and_restarting_api(
    tmp_path, capsys
):
    ctx = seed_ctx(tmp_path)

    stack.cmd_seed(ctx, [])

    assert service_calls(ctx) == [
        "up -d --wait inference",
        "run --rm seed-wipe",
        "run --rm seed-rebase",
        "run --rm seed-firestore-users",
        "run --rm seed-megastream",
        "run --rm seed-likes",
        "run --rm seed-quality",
        "run --rm seed-likes python /seedscripts/load_likes.py --aliases-only",
        "up -d --force-recreate api",
    ]
    assert "seed complete (persona: did:plc:p)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "failed_command",
    [
        "run --rm seed-quality",
        "run --rm seed-likes python /seedscripts/load_likes.py --aliases-only",
    ],
)
def test_seed_stops_if_quality_backfill_or_alias_publication_fails(
    tmp_path, capsys, failed_command
):
    ctx = seed_ctx(tmp_path, fail=failed_command)

    with pytest.raises(CommandFailed) as exc:
        stack.cmd_seed(ctx, [])

    assert exc.value.returncode == 23
    assert service_calls(ctx)[-1] == failed_command
    assert "up -d --force-recreate api" not in service_calls(ctx)
    assert "seed complete" not in capsys.readouterr().out


def test_seed_leaves_a_stopped_api_stopped(tmp_path, capsys):
    ctx = seed_ctx(tmp_path, running=False)
    stack.cmd_seed(ctx, [])
    assert "up -d --force-recreate api" not in service_calls(ctx)
    assert "The api isn't running" in capsys.readouterr().out


def test_a_sharing_instance_cannot_seed(tmp_path):
    ctx = seed_ctx(tmp_path)
    ctx.instance = "alpha"
    ctx.env["GE_DEV_SHARE_ES"] = "1"
    with pytest.raises(Die, match="read-only here"):
        stack.cmd_seed(ctx, [])


# --- BridgEU's key ------------------------------------------------------------


@pytest.mark.parametrize(
    ("bridgeu", "live", "issued"),
    [(True, "", True), (True, "mysky", False), (False, "", False)],
)
def test_bridgeu_key_is_issued_only_for_a_local_mysky(tmp_path, bridgeu, live, issued):
    ctx = make_ctx(tmp_path, env={"GE_DEV_LIVE": live})
    if bridgeu:
        (tmp_path / "bridg-eu" / ".git").mkdir(parents=True)
    stack.issue_bridgeu_key(ctx)
    assert fake(ctx).compose_lines() == (["run --rm issue-bridgeu-key"] if issued else [])


# --- restart, down, nuke -------------------------------------------------------


def test_restart_recreates_named_services_and_reissues_the_key_with_firebase(tmp_path, capsys):
    ctx = make_ctx(tmp_path)
    docker_on_path(ctx, tmp_path)
    (tmp_path / "bridg-eu" / ".git").mkdir(parents=True)
    (ctx.runtime / "es_key_ro.env").write_text("GE_ELASTICSEARCH_API_KEY=k\n")

    stack.cmd_restart(ctx, ["api", "firebase"])

    assert fake(ctx).compose_lines() == [
        "up -d --no-deps --force-recreate api firebase",
        "run --rm issue-bridgeu-key",
    ]
    assert fake(ctx).calls[-2].env["GE_DEV_ES_API_KEY"] == "k"
    assert "restarted: api firebase" in capsys.readouterr().out


def sharers(ctx, *names):
    for name in names:
        runtime = ctx.devenv / f".runtime.{name}"
        runtime.mkdir()
        (runtime / "share.env").write_text("GE_DEV_SHARE_ES=1\n")


def test_nuke_refuses_while_instances_share_this_ones_data(tmp_path):
    ctx = make_ctx(tmp_path)
    sharers(ctx, "alpha", "beta")
    with pytest.raises(Die, match="these instances share this one's Elasticsearch: alpha beta"):
        stack.cmd_nuke(ctx, [])
    assert fake(ctx).calls == []
    assert ctx.runtime.exists()


def test_nuke_can_be_forced_and_removes_only_this_instance(tmp_path):
    ctx = make_ctx(tmp_path, env={"GE_DEV_FORCE_NUKE": "1", "GE_DEV_FIREBASE_CONFIG": "f.json"})
    sharers(ctx, "alpha")
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "f.json").write_text("{}")

    stack.cmd_nuke(ctx, [])

    assert fake(ctx).compose_lines() == ["down -v --remove-orphans"]
    assert not ctx.runtime.exists()
    assert (ctx.devenv / ".runtime.alpha").exists()
    assert not (tmp_path / "frontend" / "f.json").exists()


def test_down_notes_sharers_and_published_feeds(tmp_path, capsys):
    ctx = make_ctx(tmp_path, env={"GE_DEV_FIREBASE_CONFIG": "f.json"})
    sharers(ctx, "alpha")
    ctx.bsky_state.write_text("GE_BSKY_HANDLE=me.bsky.social\n")

    stack.cmd_down(ctx, [])

    assert fake(ctx).compose_lines() == ["down"]
    err = capsys.readouterr().err
    assert "these instances read this one's Elasticsearch: alpha" in err
    assert "still published on me.bsky.social" in err


# --- up ------------------------------------------------------------------------


def test_up_flags(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.live_configured, ctx.live_env_configured = "perspective", "stage"
    ctx.env["GE_DEV_LIVE"] = "es"  # what the last `up` recorded

    opts = stack.parse_up(ctx, ["--with-ingest", "--dedicated-es", "--watch"])

    assert (opts.jetstream, opts.megastream, opts.dedicated_es) == (True, True, True)
    assert ctx.live == "perspective"
    assert ctx.env["GE_DEV_API_RELOAD"] == "1"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--bogus"], "unknown argument to up: --bogus"),
        (["--repo-root"], "--repo-root needs a directory"),
        (["--live"], "--live needs a value"),
    ],
)
def test_up_refuses_bad_flags(tmp_path, args, message):
    with pytest.raises(Die, match=message):
        stack.parse_up(make_ctx(tmp_path), args)


def test_up_repo_root_precedence(tmp_path, monkeypatch):
    for name in ("flag", "configured", "recorded"):
        (tmp_path / name).mkdir()
    ctx = make_ctx(tmp_path, env={"GE_DEV_REPO_ROOT": str(tmp_path / "recorded")})

    stack.decide_repo_root(ctx, "")
    assert ctx.repo_root == tmp_path / "recorded"

    ctx.repo_root_configured = str(tmp_path / "configured")
    stack.decide_repo_root(ctx, "")
    assert ctx.repo_root == tmp_path / "configured"

    monkeypatch.chdir(tmp_path)
    stack.decide_repo_root(ctx, "flag")
    assert ctx.repo_root == tmp_path / "flag"

    with pytest.raises(Die, match="repo root doesn't exist: nope"):
        stack.decide_repo_root(ctx, "nope")
