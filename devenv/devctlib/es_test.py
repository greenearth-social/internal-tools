import json
import time

import pytest

from devctlib import es
from devctlib.testing import FakeRunner, make_ctx, serve


def ctx_with_es(tmp_path, port, **kwargs):
    ctx = make_ctx(tmp_path, ports={"ES": port}, **kwargs)
    (ctx.runtime / "es_key_ro.env").write_text("GE_ELASTICSEARCH_API_KEY=k\n")
    return ctx


def newest(hours_ago):
    ms = int((time.time() - hours_ago * 3600 - 30) * 1000)
    return lambda m, p: (200, json.dumps({"aggregations": {"newest": {"value": ms}}}))


@pytest.mark.parametrize(("hours", "age"), [(0, 0), (5, 5), (30, 30), (-2, 0)])
def test_data_age_hours(tmp_path, hours, age):
    with serve(newest(hours)) as srv:
        ctx = ctx_with_es(tmp_path, srv.server_address[1])
        assert es.data_age_hours(ctx) == age
    assert srv.requests == [("POST", "/posts/_search")]


@pytest.mark.parametrize(
    "answer", [(200, '{"aggregations":{"newest":{"value":null}}}'), (404, "{}"), (200, "junk")]
)
def test_data_age_is_unknown_without_posts(tmp_path, answer):
    with serve(lambda m, p: answer) as srv:
        assert es.data_age_hours(ctx_with_es(tmp_path, srv.server_address[1])) is None


def test_data_age_is_unknown_without_a_key(tmp_path, capsys):
    assert es.data_age_hours(make_ctx(tmp_path)) is None
    assert capsys.readouterr().err == ""


def test_stale_data_warns_with_the_fix(tmp_path, capsys):
    with serve(newest(30)) as srv:
        es.warn_stale_data(ctx_with_es(tmp_path, srv.server_address[1]))
    err = capsys.readouterr().err
    assert "the newest indexed post is 30h old" in err
    assert "Re-seed: devctl seed" in err


def test_fresh_data_is_quiet(tmp_path, capsys):
    with serve(newest(3)) as srv:
        es.warn_stale_data(ctx_with_es(tmp_path, srv.server_address[1]))
    assert capsys.readouterr().err == ""


def test_count(tmp_path):
    with serve(lambda m, p: (200, '{"count":7,"_shards":{}}')) as srv:
        assert es.es_count(ctx_with_es(tmp_path, srv.server_address[1]), "likes") == "7"
    assert srv.requests == [("GET", "/likes/_count")]


def test_count_of_a_missing_alias_is_unknown(tmp_path):
    with serve(lambda m, p: (404, '{"error":{}}')) as srv:
        assert es.es_count(ctx_with_es(tmp_path, srv.server_address[1]), "posts") == ""


def test_count_without_a_key_says_so_unless_quiet(tmp_path, capsys):
    ctx = make_ctx(tmp_path)
    assert es.es_count(ctx, "posts") == ""
    assert "no ES key yet" in capsys.readouterr().err
    assert es.es_count(ctx, "posts", quiet=True) == ""
    assert capsys.readouterr().err == ""


def memory_ctx(tmp_path, total_gb, running, sharers=(), instance="dev", share=False):
    def respond(argv):
        if argv[:3] == ["docker", "info", "--format"]:
            return str(total_gb * 1024**3)
        if argv[:3] == ["docker", "compose", "ls"]:
            return json.dumps([{"Name": f"ge-{n}"} for n in running])
        return None

    env = {"GE_DEV_SHARE_ES": "1"} if share else {}
    ctx = make_ctx(tmp_path, runner=FakeRunner(respond), instance=instance, env=env)
    for name in {*running, *sharers}:
        runtime = ctx.devenv / (".runtime" if name == "dev" else f".runtime.{name}")
        runtime.mkdir(exist_ok=True)
        if name in sharers:
            (runtime / "share.env").write_text("GE_DEV_SHARE_ES=1\n")
    return ctx


@pytest.mark.parametrize(
    ("total_gb", "running", "sharers", "instance", "share", "warned"),
    [
        (16, ["dev", "beta"], [], "dev", False, False),  # 2*3+2 = 8 fits
        (7, ["dev", "beta"], [], "dev", False, True),
        (7, ["dev"], [], "beta", False, True),  # beta is about to start its own
        (7, ["dev", "alpha"], ["alpha"], "dev", False, False),  # a sharer has no JVM
        (7, ["dev"], [], "alpha", True, False),
        (0, ["dev", "beta"], [], "dev", False, False),  # unknown memory: no claim
    ],
)
def test_memory_budget(tmp_path, capsys, total_gb, running, sharers, instance, share, warned):
    ctx = memory_ctx(tmp_path, total_gb, running, sharers, instance, share)
    es.check_memory_budget(ctx)
    assert ("instances run their own Elasticsearch" in capsys.readouterr().err) == warned
