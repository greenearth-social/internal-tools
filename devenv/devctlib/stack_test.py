import json

import pytest

from devctlib import http, stack
from devctlib.ctx import Die
from devctlib.instance import set_instance
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
    ("bridgeu", "built"),
    [
        (False, "seed-firestore-users"),
        (True, "bridgeu-api bridgeu-seed-da-scores bridgeu-da-pipeline"),
    ],
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
    pulled = "elasticsearch perspective-stub dev-auth seed-rebase seed-megastream"
    if bridgeu:
        pulled += " bridgeu-postgres"
    assert calls[1] == f"--profile tools pull {pulled} --ignore-buildable"
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


def seed_ctx(
    tmp_path, fail: str = "", running: bool = True, *, bridgeu: bool = False, graph: bool = False
):
    def respond(argv):
        line = " ".join(argv)
        if fail and line.endswith(fail):
            return 23
        if "ps" in argv and "running" in argv:
            return "api" if running else ""
        return None

    ctx = make_ctx(tmp_path, runner=FakeRunner(respond))
    docker_on_path(ctx, tmp_path)
    if bridgeu:
        (tmp_path / "bridg-eu" / ".git").mkdir(parents=True)
    fixtures = ctx.devenv / "fixtures" / "data"
    fixtures.mkdir(parents=True)
    (fixtures / "manifest.json").write_text(json.dumps({"graph": {}} if graph else {}))
    for name in ("es_key_ro.env", "es_key_rw.env"):
        (ctx.runtime / name).write_text("GE_ELASTICSEARCH_API_KEY=k\n")
    (ctx.runtime / "probe.env").write_text("GE_PROBE_USER_DID=did:plc:p\n")
    return ctx


def service_calls(ctx):
    return [c for c in fake(ctx).compose_lines() if not c.startswith("ps ")]


@pytest.mark.parametrize("bridgeu", [False, True])
def test_seed_runs_post_loaders_before_optional_da_and_api_restart(tmp_path, capsys, bridgeu):
    ctx = seed_ctx(tmp_path, bridgeu=bridgeu)

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
        *(BRIDGEU_SYNTHETIC_SEED if bridgeu else []),
        "up -d --force-recreate api",
    ]
    assert "seed complete (persona: did:plc:p)" in capsys.readouterr().out


BRIDGEU_SYNTHETIC_SEED = [
    "run --rm bridgeu-db-init",
    # Empties the graph tables: nothing may score a previous fixture's graph.
    "run --rm bridgeu-da-pipeline python /seedscripts/load_graph.py",
    "run --rm bridgeu-seed-da-scores",
]
BRIDGEU_GRAPH_SEED = [
    "run --rm bridgeu-db-init",
    "run --rm bridgeu-da-pipeline python /seedscripts/load_graph.py",
    "run --rm bridgeu-da-pipeline python -m pipeline snapshot",
    "run --rm bridgeu-da-pipeline python -m pipeline fit",
    "run --rm bridgeu-da-pipeline python /seedscripts/da_score.py",
]


def test_seed_runs_the_da_pipeline_when_the_fixture_has_a_graph(tmp_path, capsys):
    ctx = seed_ctx(tmp_path, bridgeu=True, graph=True)

    stack.cmd_seed(ctx, [])

    calls = service_calls(ctx)
    start = calls.index("run --rm bridgeu-db-init")
    assert calls[start : start + len(BRIDGEU_GRAPH_SEED)] == BRIDGEU_GRAPH_SEED
    assert "run --rm bridgeu-seed-da-scores" not in calls
    assert calls[-1] == "up -d --force-recreate api"


@pytest.mark.parametrize(
    "failed_command",
    [
        "run --rm seed-quality",
        "run --rm seed-likes python /seedscripts/load_likes.py --aliases-only",
        "run --rm bridgeu-db-init",
        "run --rm bridgeu-seed-da-scores",
        "run --rm bridgeu-da-pipeline python -m pipeline fit",
    ],
)
def test_seed_stops_if_a_loader_or_schema_init_fails(tmp_path, capsys, failed_command):
    graph = "bridgeu-da-pipeline" in failed_command
    ctx = seed_ctx(tmp_path, fail=failed_command, bridgeu=True, graph=graph)

    with pytest.raises(CommandFailed) as exc:
        stack.cmd_seed(ctx, [])

    assert exc.value.returncode == 23
    assert service_calls(ctx)[-1] == failed_command
    assert "up -d --force-recreate api" not in service_calls(ctx)
    assert "seed complete" not in capsys.readouterr().out


@pytest.mark.parametrize("bridgeu", [False, True])
def test_seed_leaves_a_stopped_api_stopped(tmp_path, capsys, bridgeu):
    ctx = seed_ctx(tmp_path, running=False, bridgeu=bridgeu)
    stack.cmd_seed(ctx, [])
    assert "up -d --force-recreate api" not in service_calls(ctx)
    assert "The api isn't running" in capsys.readouterr().out


@pytest.mark.parametrize("bridgeu", [False, True])
def test_a_sharing_instance_cannot_seed(tmp_path, bridgeu):
    ctx = seed_ctx(tmp_path, bridgeu=bridgeu)
    ctx.instance = "alpha"
    ctx.env["GE_DEV_SHARE_ES"] = "1"
    with pytest.raises(Die, match="read-only here"):
        stack.cmd_seed(ctx, [])
    assert fake(ctx).compose_lines() == []


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


def up_ctx(tmp_path, monkeypatch, *, bridgeu=True, instance="dev", fail=""):
    def respond(argv):
        if fail and " ".join(argv).endswith(fail):
            return 23
        return None

    ctx = make_ctx(tmp_path, instance=instance, runner=FakeRunner(respond))
    set_instance(ctx, instance)
    docker_on_path(ctx, tmp_path)
    with_repos(tmp_path, *(["bridg-eu"] if bridgeu else []))
    with_models(ctx)
    (ctx.runtime / "es_key_ro.env").write_text("GE_ELASTICSEARCH_API_KEY=local-key\n")
    if instance != "dev":
        ctx.offset = 10
        ctx.ports = {name: port + ctx.offset for name, port in ctx.ports.items()}
        owner = ctx.devenv / ".runtime"
        owner.mkdir()
        (owner / "es_key_ro.env").write_text("GE_ELASTICSEARCH_API_KEY=owner-key\n")

    def request(url, **kwargs):
        if url.endswith("/_cluster/health"):
            return http.Response(200)
        if url.endswith("/posts/_count"):
            return http.Response(200, '{"count":0}')
        raise AssertionError(f"unexpected HTTP request: {url}")

    monkeypatch.setattr(http, "request", request)
    return ctx


def test_up_initializes_postgres_before_issuing_key_and_starting_apps(
    tmp_path, monkeypatch, capsys
):
    ctx = up_ctx(tmp_path, monkeypatch)

    stack.cmd_up(ctx, [])

    calls = fake(ctx).compose_lines()
    ready = next(call for call in calls if call.startswith("up -d --remove-orphans --wait "))
    assert "bridgeu-postgres" in ready.split()
    initialized = "run --rm bridgeu-db-init"
    issued_key = "run --rm issue-bridgeu-key"
    started = "up -d api frontend bridgeu-api"
    assert (
        calls.index(ready)
        < calls.index(initialized)
        < calls.index(issued_key)
        < calls.index(started)
    )
    assert "(database bridgeu, user bridgeu / bridgeu)" in capsys.readouterr().out


def test_up_reinitializes_postgres_on_each_run(tmp_path, monkeypatch):
    ctx = up_ctx(tmp_path, monkeypatch)

    stack.cmd_up(ctx, [])
    first_calls = len(fake(ctx).calls)
    stack.cmd_up(ctx, [])

    assert fake(ctx).compose_lines().count("run --rm bridgeu-db-init") == 2
    assert any(
        call.argv[-3:] == ["run", "--rm", "bridgeu-db-init"]
        for call in fake(ctx).calls[first_calls:]
    )


def test_up_without_bridgeu_stops_its_services(tmp_path, monkeypatch):
    ctx = up_ctx(tmp_path, monkeypatch, bridgeu=False)

    stack.cmd_up(ctx, [])

    calls = fake(ctx).compose_lines()
    assert "stop bridgeu-api bridgeu-postgres" in calls
    assert "run --rm bridgeu-db-init" not in calls
    assert "up -d api frontend" in calls
    assert all("bridgeu-postgres" not in call for call in calls if call.startswith("up "))


@pytest.mark.parametrize(
    "failed",
    [
        "up -d --remove-orphans --wait firebase dev-auth bridgeu-postgres "
        "elasticsearch inference perspective-stub",
        "run --rm bridgeu-db-init",
    ],
    ids=["database-startup", "schema-init"],
)
def test_up_stops_before_apps_if_database_startup_fails(tmp_path, monkeypatch, capsys, failed):
    ctx = up_ctx(tmp_path, monkeypatch, fail=failed)

    with pytest.raises(CommandFailed) as exc:
        stack.cmd_up(ctx, [])

    assert exc.value.returncode == 23
    calls = fake(ctx).compose_lines()
    assert calls[-1] == failed
    assert "run --rm issue-bridgeu-key" not in calls
    assert not any(call.startswith("up ") and "bridgeu-api" in call.split() for call in calls)
    assert "environment is up" not in capsys.readouterr().out


def test_up_es_sharer_still_starts_its_own_postgres(tmp_path, monkeypatch):
    ctx = up_ctx(tmp_path, monkeypatch, instance="alpha")

    stack.cmd_up(ctx, [])

    calls = fake(ctx).compose_lines()
    ready = next(call for call in calls if call.startswith("up -d --remove-orphans --wait "))
    assert "bridgeu-postgres" in ready.split()
    assert "elasticsearch" not in ready.split()
    assert "run --rm es-init" not in calls
    initialized = "run --rm bridgeu-db-init"
    started = "up -d --no-deps api frontend bridgeu-api"
    assert calls.index(ready) < calls.index(initialized) < calls.index(started)
    startup = next(call for call in fake(ctx).calls if "bridgeu-postgres" in call.argv)
    assert startup.env["GE_DEV_NAME"] == "ge-alpha"
    assert startup.env["GE_DEV_RUNTIME"] == "./.runtime.alpha"
    initializer = next(call for call in fake(ctx).calls if call.argv[-1] == "bridgeu-db-init")
    assert initializer.env["GE_DEV_NAME"] == "ge-alpha"
    assert initializer.env["GE_DEV_RUNTIME"] == "./.runtime.alpha"
    assert ctx.get("GE_DEV_ES_API_KEY") == "owner-key"
    assert ctx.port("BRIDGEU_POSTGRES") == 55442


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


def test_restart_on_a_sharing_instance_points_the_api_at_the_owner(tmp_path):
    """It used to look for a key of the sharer's own, find none, and stop —
    and without the owner's endpoint the api would have come back pointing at
    a cluster that isn't there."""
    ctx = make_ctx(tmp_path, instance="alpha", env={"GE_DEV_SHARE_ES": "1"})
    ctx.offset = 10
    ctx.ports["ES"] = 9211
    docker_on_path(ctx, tmp_path)
    owner = ctx.devenv / ".runtime"
    owner.mkdir()
    (owner / "es_key_ro.env").write_text("GE_ELASTICSEARCH_API_KEY=owner-key\n")

    stack.cmd_restart(ctx, ["api"])

    env = fake(ctx).calls[-1].env
    assert env["GE_DEV_ES_API_KEY"] == "owner-key"
    assert env["GE_DEV_ES_URL"] == "http://host.docker.internal:9201"


# --- da-pipeline ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], "run --rm bridgeu-da-pipeline python -m pipeline --help"),
        (["fit", "--seed", "7"], "run --rm bridgeu-da-pipeline python -m pipeline fit --seed 7"),
        # score lines rebased dates up with now (seed/da_score.py).
        (
            ["score", "--lam", "1"],
            "run --rm bridgeu-da-pipeline python /seedscripts/da_score.py --lam 1",
        ),
    ],
)
def test_da_pipeline_runs_a_job_in_the_pipeline_image(tmp_path, args, expected):
    from devctlib import bridgeu

    ctx = seed_ctx(tmp_path, bridgeu=True)
    bridgeu.cmd_da_pipeline(ctx, args)
    assert service_calls(ctx) == [expected]


def test_da_pipeline_needs_the_checkout(tmp_path):
    from devctlib import bridgeu

    with pytest.raises(Die, match="needs bridg-eu checked out"):
        bridgeu.cmd_da_pipeline(seed_ctx(tmp_path), ["fit"])
