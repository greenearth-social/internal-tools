import pytest

from devctlib import info
from devctlib.ctx import Die
from devctlib.testing import Exec, FakeRunner, fake, make_ctx


def test_ports_lists_this_instance(tmp_path, capsys):
    ctx = make_ctx(tmp_path, env={"GE_DEV_NAME": "ge-dev"})
    info.cmd_ports(ctx, [])
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "instance      dev (compose project ge-dev, offset 0)"
    assert "api           8300" in out
    assert "bridgeu-api   8400" in out


@pytest.mark.parametrize(
    ("name", "port"),
    [("api", "8300"), ("elasticsearch", "9201"), ("auth", "9099"), ("bridgeu-api", "8400")],
)
def test_ports_prints_one_number_by_service(tmp_path, capsys, name, port):
    ctx = make_ctx(
        tmp_path,
        env={
            f"GE_DEV_PORT_{k}": v
            for k, v in [
                ("API", "8300"),
                ("ES", "9201"),
                ("FIREBASE_AUTH", "9099"),
                ("BRIDGEU_API", "8400"),
            ]
        },
    )
    info.cmd_ports(ctx, [name])
    assert capsys.readouterr().out == f"{port}\n"


def test_ports_refuses_an_unknown_service(tmp_path):
    with pytest.raises(Die, match="no published port for 'nope'"):
        info.cmd_ports(make_ctx(tmp_path), ["nope"])


def test_ports_ssh_forwards_every_client_port(tmp_path, capsys):
    info.cmd_ports(make_ctx(tmp_path), ["--ssh"])
    flags = capsys.readouterr().out.split()
    assert flags[:2] == ["-L", "3000:127.0.0.1:3000"]
    assert "4041:127.0.0.1:4041" not in flags  # ngrok's inspection API stays local
    assert len(flags) == 2 * len(info.SSH_PORTS)


def test_ls_shows_state_and_an_unusual_repo_root(tmp_path, capsys):
    runner = FakeRunner(
        lambda argv: '[{"Name": "ge-alpha"}]' if argv[:3] == ["docker", "compose", "ls"] else None
    )
    ctx = make_ctx(tmp_path, runner=runner)
    alpha = ctx.devenv / ".runtime.alpha"
    alpha.mkdir()
    (alpha / "ports.json").write_text('{"offset": 10}')
    (alpha / "repo_root.env").write_text("GE_DEV_REPO_ROOT=/work/tree\n")

    info.cmd_ls(ctx, [])

    rows = [line.split() for line in capsys.readouterr().out.splitlines()]
    assert rows == [
        ["NAME", "STATE", "OFFSET", "API", "FRONTEND", "ROOT"],
        ["dev", "stopped", "0", "8300", "3000", "-"],
        ["alpha", "running", "10", "8310", "3010", "/work/tree"],
    ]


def test_ls_without_docker_still_lists(tmp_path, capsys):
    ctx = make_ctx(tmp_path, runner=FakeRunner(lambda argv: 1))
    info.cmd_ls(ctx, [])
    assert "dev            stopped" in capsys.readouterr().out


def test_logs_defaults_to_the_last_hundred_lines(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(Exec) as exc:
        info.cmd_logs(ctx, ["api"])
    assert exc.value.argv[-3:] == ["logs", "--tail=100", "api"]


def test_logs_follows_with_t_and_keeps_a_chosen_tail(tmp_path):
    ctx = make_ctx(tmp_path)
    with pytest.raises(Exec) as exc:
        info.cmd_logs(ctx, ["-t", "--tail=5", "api"])
    assert exc.value.argv[-4:] == ["logs", "-f", "--tail=5", "api"]


def test_es_needs_a_path(tmp_path):
    with pytest.raises(Die, match="usage: devctl es <path>"):
        info.cmd_es(make_ctx(tmp_path), [])


def test_es_names_the_fix_without_a_key(tmp_path):
    with pytest.raises(Die, match="no ES key yet — run: devctl up"):
        info.cmd_es(make_ctx(tmp_path), ["/posts/_count"])


def test_es_curls_with_the_read_key_and_passes_curl_args(tmp_path, capsys):
    ctx = make_ctx(tmp_path)
    (ctx.runtime / "es_key_ro.env").write_text("GE_ELASTICSEARCH_API_KEY=k1\n")

    info.cmd_es(ctx, ["/posts/_search", "-X", "POST"])

    assert fake(ctx).calls[-1].argv == [
        "curl",
        "-s",
        "-H",
        "Authorization: ApiKey k1",
        "http://127.0.0.1:9201/posts/_search",
        "-X",
        "POST",
    ]
    assert capsys.readouterr().out == "\n"


def test_a_sharing_instance_queries_the_owner(tmp_path):
    ctx = make_ctx(tmp_path, instance="alpha", env={"GE_DEV_SHARE_ES": "1"})
    ctx.offset = 10
    ctx.ports["ES"] = 9211
    owner = ctx.devenv / ".runtime"
    owner.mkdir()
    (owner / "es_key_ro.env").write_text("GE_ELASTICSEARCH_API_KEY=owner\n")

    info.cmd_es(ctx, ["/_count"])

    assert fake(ctx).calls[-1].argv[3:5] == [
        "Authorization: ApiKey owner",
        "http://127.0.0.1:9201/_count",
    ]
