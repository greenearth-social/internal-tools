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
