"""The port against the bash original, command by command.

Each case runs `devctl <args>` through devctl-legacy and through devctlib, in
identical sandboxes with fake docker/kubectl/gcloud and a fake Elasticsearch,
and checks they did the same thing (see testing.assert_parity). Removed with
devctl-legacy once everything is ported.
"""

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


def es_answers(method, path):
    if path.endswith("/_count"):
        return 200, '{"count":42}'
    return 200, '{"status":"green"}'


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
