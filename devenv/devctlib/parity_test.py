"""The port against the bash original, command by command.

Each case runs `devctl <args>` through devctl-legacy and through devctlib, in
identical sandboxes with fake docker/kubectl/gcloud and a fake Elasticsearch,
and checks they did the same thing (see testing.assert_parity). Removed with
devctl-legacy once everything is ported.
"""

import json
import time

import pytest

from devctlib.testing import Rule, parity


@pytest.mark.parametrize("args", [(), ("help",), ("-h",), ("--help",), ("--name", "alpha", "help")])
def test_help(tmp_path, args):
    parity(tmp_path, *args, phrases=["Usage: devctl", "use-ranker"])


def test_invalid_instance_name(tmp_path):
    parity(tmp_path, "--name", "Bad", "help", phrases=["invalid instance name: 'Bad'"])


def test_name_needs_a_value(tmp_path):
    parity(tmp_path, "help", "--name", phrases=["--name needs an instance name"])


def named_instance(name: str, offset: int, **state: str):
    """Setup: an instance that `up` has already created."""

    def setup(sandbox):
        runtime = sandbox.devenv / f".runtime.{name}"
        runtime.mkdir()
        (runtime / "ports.json").write_text(f'{{"offset": {offset}}}')
        for filename, text in state.items():
            (runtime / filename.replace("_env", ".env")).write_text(text)

    return setup


def es_keys(sandbox):
    runtime = sandbox.devenv / ".runtime"
    runtime.mkdir(exist_ok=True)
    (runtime / "es_key_ro.env").write_text("GE_ELASTICSEARCH_API_KEY=ro-key\n")
    (runtime / "es_key_rw.env").write_text("GE_ELASTICSEARCH_API_KEY=rw-key\n")


def es_answers(method, path, *, age_hours=2):
    if path.endswith("/_count"):
        return 200, '{"count":42}'
    if path == "/posts/_search":
        newest_ms = int((time.time() - age_hours * 3600 - 60) * 1000)
        return 200, json.dumps({"aggregations": {"newest": {"value": newest_ms}}})
    return 200, '{"status":"green"}'


def stale_es(method, path):
    return es_answers(method, path, age_hours=30)


def bundles(agree: bool = True):
    """Setup: model and fixture manifests, agreeing on the encoder or not."""

    def setup(sandbox):
        models = sandbox.devenv / "models" / "data"
        fixtures = sandbox.devenv / "fixtures" / "data"
        models.mkdir(parents=True)
        fixtures.mkdir(parents=True)
        (models / "manifest.json").write_text(
            json.dumps(
                {
                    "tag": "models-test",
                    "content_embedding_model": "m",
                    "content_embedding_dims": 384,
                }
            )
        )
        (fixtures / "manifest.json").write_text(
            json.dumps(
                {
                    "embedding_model": "m" if agree else "other",
                    "embedding_dims": 384,
                    "counts": {"posts": 1234, "likes": 56789},
                }
            )
        )

    return setup


def live_state(live: str, live_env: str = "stage"):
    """Setup: a default instance that `up --live <live>` recorded."""

    def setup(sandbox):
        runtime = sandbox.devenv / ".runtime"
        runtime.mkdir(exist_ok=True)
        (runtime / "live.env").write_text(f"GE_DEV_LIVE={live}\nGE_DEV_LIVE_ENV={live_env}\n")

    return setup


def steps(*setups):
    def setup(sandbox):
        for step in setups:
            step(sandbox)

    return setup


@pytest.mark.parametrize(
    "args",
    [
        ("ports",),
        ("ports", "--ssh"),
        ("ports", "api"),
        ("ports", "elasticsearch"),
        ("ports", "ui"),
        ("ports", "offset"),
        ("ports", "nope"),
        ("--name", "ghost", "ports"),
        ("--name", "alpha", "ports"),
        ("ports", "--name", "alpha", "api"),
    ],
)
def test_ports(tmp_path, args):
    parity(tmp_path, *args, setup=named_instance("alpha", 10))


# Not `instances`: the bash version resolved ports for it (unlike `ls`), so
# it failed on an unknown --name; the port treats the alias as `ls`.
@pytest.mark.parametrize("args", [("ls",), ("ls", "extra")])
def test_ls(tmp_path, args):
    def setup(sandbox):
        named_instance("alpha", 10, repo_root_env="GE_DEV_REPO_ROOT=/elsewhere\n")(sandbox)
        named_instance("beta", 20)(sandbox)

    parity(
        tmp_path,
        *args,
        setup=setup,
        rules=[Rule(r"compose ls", '[{"Name": "ge-beta"}]')],
    )


def test_ls_with_docker_down(tmp_path):
    parity(tmp_path, "ls", rules=[Rule(r"^docker info", rc=1)])


@pytest.mark.parametrize(
    ("args", "setup"),
    [
        (("es", "/posts/_count"), es_keys),
        (("es", "/posts/_search", "-X", "POST", "-d", "{}"), es_keys),
        (("es", "/posts/_count"), None),
        (("es",), es_keys),
        (("es", "--help"), es_keys),
    ],
)
def test_es(tmp_path, args, setup):
    parity(tmp_path, *args, setup=setup, http=es_answers)


def test_es_from_a_sharing_instance_reads_the_owner(tmp_path):
    def setup(sandbox):
        es_keys(sandbox)
        named_instance("alpha", 10, share_env="GE_DEV_SHARE_ES=1\n")(sandbox)

    legacy, _ = parity(
        tmp_path, "--name", "alpha", "es", "/posts/_count", setup=setup, http=es_answers
    )
    assert legacy.requests == [("GET", "/posts/_count")]


@pytest.mark.parametrize(
    "args", [("logs",), ("logs", "api"), ("logs", "-t", "api"), ("logs", "--tail=5", "-f")]
)
def test_logs(tmp_path, args):
    parity(tmp_path, *args)


RUNNING = Rule(r"ps --status running", "api\nelasticsearch\nfirebase\n")
OOM = Rule(r"ps -a --format", "elasticsearch Exited (137) 2 minutes ago\napi Up 3 minutes\n")


@pytest.mark.parametrize(
    ("args", "setup", "rules", "http"),
    [
        (("status",), None, [], None),
        (("ps",), es_keys, [], es_answers),
        (("status",), es_keys, [], stale_es),
        (("status",), es_keys, [OOM], es_answers),
        (("status",), es_keys, [Rule(r"compose .* ps$", rc=3)], es_answers),
        (("status", "extra"), None, [], None),
        (
            ("--name", "alpha", "status"),
            steps(es_keys, named_instance("alpha", 10, share_env="GE_DEV_SHARE_ES=1\n")),
            [],
            es_answers,
        ),
        (
            ("--name", "alpha", "status"),
            named_instance("alpha", 10, share_env="GE_DEV_SHARE_ES=1\n"),
            [],
            es_answers,
        ),
        (
            ("--name", "alpha", "status"),
            named_instance("alpha", 10, repo_root_env="GE_DEV_REPO_ROOT=/elsewhere\n"),
            [],
            None,
        ),
    ],
)
def test_status(tmp_path, args, setup, rules, http):
    parity(tmp_path, *args, setup=setup, rules=rules, http=http)


MEMORY = Rule(r"^docker info --format \{\{\.MemTotal\}\}$", "4294967296\n")
TWO_CLUSTERS = Rule(r"compose ls", '[{"Name": "ge-dev"}, {"Name": "ge-beta"}]')


@pytest.mark.parametrize(
    ("args", "setup", "rules", "http", "phrases"),
    [
        (("doctor",), None, [], None, ["no model artifacts", "no fixture set"]),
        (("doctor",), bundles(), [], None, ["no containers running"]),
        (("doctor",), bundles(agree=False), [], None, ["disagree on the content encoder"]),
        (("doctor",), bundles(), [Rule(r"^docker info$", rc=1)], None, ["daemon isn't running"]),
        (
            ("doctor",),
            steps(bundles(), es_keys),
            [RUNNING],
            es_answers,
            ["elasticsearch is green", "42 posts indexed", "newest post 2h old"],
        ),
        (("doctor",), steps(bundles(), es_keys), [RUNNING], stale_es, ["newest post 30h old"]),
        (("doctor",), steps(bundles(), es_keys), [RUNNING, OOM], es_answers, ["out of memory"]),
        (
            ("doctor",),
            steps(bundles(), es_keys, named_instance("beta", 20)),
            [RUNNING, MEMORY, TWO_CLUSTERS],
            es_answers,
            ["2 instances run their own Elasticsearch"],
        ),
        (
            ("--name", "alpha", "doctor"),
            steps(bundles(), es_keys, named_instance("alpha", 10, share_env="GE_DEV_SHARE_ES=1\n")),
            [RUNNING],
            es_answers,
            ["shared Elasticsearch .* is answering"],
        ),
        (
            ("doctor",),
            steps(bundles(), live_state("es")),
            [RUNNING],
            None,
            ["reads LIVE Elasticsearch"],
        ),
    ],
)
# Not here, because the bash version is wrong about them (see doctor_test):
# a running api with no ES key to read with, on this instance or the owner.
def test_doctor(tmp_path, args, setup, rules, http, phrases):
    parity(tmp_path, *args, setup=setup, rules=rules, http=http, phrases=phrases)


@pytest.mark.parametrize(
    ("args", "rules"),
    [
        (("exec", "api", "pipenv", "run", "pytest", "--name", "x"), [RUNNING]),
        (("exec", "--name", "alpha", "api", "ls"), [RUNNING]),
        (("--name", "alpha", "exec", "api", "ls"), [RUNNING]),
        (("exec", "api", "ls"), []),
        (("exec", "api"), [RUNNING]),
        (("exec",), []),
        (("exec", "--name"), []),
    ],
)
def test_exec(tmp_path, args, rules):
    # With no state recorded for dev, the bash version's exec --name bug
    # (dev's recorded state applied to alpha) can't show here; cli_test has it.
    parity(tmp_path, *args, rules=rules, setup=named_instance("alpha", 10))


ALL_RUNNING = Rule(r"ps --status running", "api\ninference\nfrontend\nbridgeu-api\n")


@pytest.mark.parametrize(
    ("args", "rules", "repos"),
    [
        (("test",), [ALL_RUNNING], ("api", "ingex", "frontend", "inference-service")),
        (("test",), [ALL_RUNNING], ("api", "ingex", "frontend", "inference-service", "bridg-eu")),
        (("test", "ingex"), [], ("api", "ingex", "frontend", "inference-service")),
        (("test", "api", "frontend"), [RUNNING], ("api", "ingex", "frontend", "inference-service")),
        (("test", "bridgeu"), [ALL_RUNNING], ("api", "ingex", "frontend", "inference-service")),
        (("test", "nope"), [], ("api",)),
        (("test", "api"), [ALL_RUNNING, Rule(r"exec .* pytest", rc=2)], ("api",)),
    ],
)
def test_test(tmp_path, args, rules, repos):
    parity(tmp_path, *args, rules=rules, repos=repos)


def probe_persona(sandbox):
    runtime = sandbox.devenv / ".runtime"
    runtime.mkdir(exist_ok=True)
    (runtime / "probe.env").write_text("GE_PROBE_USER_DID=did:plc:persona\n")


@pytest.mark.parametrize(
    ("args", "setup", "phrases"),
    [
        (("login",), probe_persona, [r"auth/finish\?token=", "did:plc:persona"]),
        (("login", "did:plc:other"), None, ["did:plc:other"]),
        (("login",), None, ["no seeded persona found"]),
    ],
)
def test_login(tmp_path, args, setup, phrases):
    parity(tmp_path, *args, setup=setup, phrases=phrases)


def git_repos(sandbox):
    """Real checkouts: api behind its remote, frontend dirty, ingex on a branch."""
    import subprocess

    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    def git(cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)

    for repo in ("api", "ingex", "frontend", "inference-service"):
        remote = sandbox.root / "remotes" / repo
        remote.mkdir(parents=True)
        git(remote, "init", "-q", "-b", "main")
        git(remote, "commit", "-q", "--allow-empty", "-m", "one")
        import shutil

        shutil.rmtree(sandbox.root / repo)
        git(sandbox.root, "clone", "-q", str(remote), repo)
    git(sandbox.root / "remotes" / "api", "commit", "-q", "--allow-empty", "-m", "two")
    (sandbox.root / "frontend" / "dirty").write_text("x")
    git(sandbox.root / "ingex", "checkout", "-q", "-b", "feature")


@pytest.mark.parametrize(
    ("args", "setup"),
    [
        (("pull",), None),
        (("pull",), git_repos),
        (("pull", "--repo-root"), None),
        (("pull", "--bogus"), None),
        (("pull", "--repo-root", "/no/such/dir"), None),
    ],
)
def test_pull(tmp_path, args, setup):
    legacy, port = parity(tmp_path, *args, setup=setup, env={"GIT_CONFIG_GLOBAL": "/dev/null"})
    assert port.stdout == legacy.stdout


# Network is pointed at a proxy nobody listens on, so downloads fail fast and
# identically rather than reaching GitHub.
NO_NETWORK = {"https_proxy": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9"}


@pytest.mark.parametrize(
    ("args", "phrases"),
    [
        (("fetch-fixture",), ["no fixture release found"]),
        (("fetch-models",), ["downloading models-1bf1ea3c-177899dd", "download failed"]),
        (("fetch-models", "x"), ["takes no arguments"]),
    ],
)
def test_fetch_without_network(tmp_path, args, phrases):
    parity(tmp_path, *args, env=NO_NETWORK, phrases=phrases)


@pytest.mark.parametrize(
    ("args", "setup", "rules", "repos", "phrases"),
    [
        (
            ("setup",),
            bundles(),
            [],
            ("api", "ingex", "frontend", "inference-service"),
            ["setup complete"],
        ),
        (
            ("setup",),
            bundles(),
            [],
            ("api", "ingex", "frontend", "inference-service", "bridg-eu"),
            ["setup complete"],
        ),
        (("setup",), None, [], ("api", "ingex"), ["missing required repos"]),
        (
            ("setup",),
            bundles(),
            [Rule(r"--install-only", rc=1)],
            ("api", "ingex", "frontend", "inference-service"),
            ["failed to install"],
        ),
        (
            ("setup",),
            bundles(),
            [Rule(r" build ", rc=17)],
            ("api", "ingex", "frontend", "inference-service"),
            [],
        ),
        (("setup",), bundles(), [Rule(r"^docker info$", rc=1)], ("api",), ["daemon isn't running"]),
    ],
)
def test_setup(tmp_path, args, setup, rules, repos, phrases):
    parity(tmp_path, *args, setup=setup, rules=rules, repos=repos, phrases=phrases, env=NO_NETWORK)


@pytest.mark.parametrize("args", [("use-ranker", "--reset"), ("use-ranker", "--bogus")])
def test_use_ranker(tmp_path, args):
    parity(tmp_path, *args, env=NO_NETWORK)


# Only the ways tunnel stops: its success path port-forwards forever.
@pytest.mark.parametrize(
    ("args", "rules", "phrases"),
    [
        (("tunnel", "dev"), [], ["usage: devctl tunnel"]),
        (("tunnel", "prod"), [Rule(r"^gcloud ", rc=1)], ["couldn't configure kubectl"]),
        (("tunnel",), [Rule(r"current-context", "")], ["kubectl has no current context"]),
        (("tunnel",), [Rule(r"current-context", rc=1)], ["kubectl has no current context"]),
    ],
)
def test_tunnel(tmp_path, args, rules, phrases):
    parity(tmp_path, *args, rules=rules, phrases=phrases)
