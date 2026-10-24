import pytest

from devctlib import suites
from devctlib.ctx import Die
from devctlib.testing import FakeRunner, fake, make_ctx

ALL_UP = ("api", "inference", "frontend", "bridgeu-api")


def suite_ctx(tmp_path, *, bridgeu=True, running=ALL_UP, failing=()):
    def respond(argv):
        if "ps" in argv:
            return "\n".join(running)
        if any(s in argv for s in failing):
            return 1
        return None

    ctx = make_ctx(tmp_path, runner=FakeRunner(respond))
    if bridgeu:
        (tmp_path / "bridg-eu" / ".git").mkdir(parents=True)
    return ctx


def ran(ctx) -> list[str]:
    """The suites that ran, in order."""
    out = []
    for call in fake(ctx).calls:
        if call.argv[:2] == ["docker", "run"]:
            out.append("ingex")
        elif "PIPENV_VERBOSITY=-1" in call.argv:
            service = call.argv[call.argv.index("PIPENV_VERBOSITY=-1") + 1]
            out.append("bridgeu" if service == "bridgeu-api" else service)
    return out


def test_all_suites_include_bridgeu_when_checked_out(tmp_path):
    ctx = suite_ctx(tmp_path)
    suites.cmd_test(ctx, [])
    assert ran(ctx) == ["api", "inference", "ingex", "frontend", "bridgeu"]


def test_all_suites_skip_bridgeu_without_the_checkout(tmp_path, capsys):
    ctx = suite_ctx(tmp_path, bridgeu=False)
    suites.cmd_test(ctx, [])
    assert ran(ctx) == ["api", "inference", "ingex", "frontend"]
    assert "skipping bridgeu" in capsys.readouterr().err


def test_bridgeu_suite_names_the_missing_checkout(tmp_path):
    ctx = suite_ctx(tmp_path, bridgeu=False)
    with pytest.raises(Die, match="needs bridg-eu checked out"):
        suites.cmd_test(ctx, ["bridgeu"])
    assert ran(ctx) == []


def test_an_unknown_suite_is_refused(tmp_path):
    with pytest.raises(Die, match="unknown test suite: nope"):
        suites.cmd_test(suite_ctx(tmp_path), ["nope"])


def test_stopped_services_are_named_before_anything_runs(tmp_path):
    ctx = suite_ctx(tmp_path, running=("api",))
    with pytest.raises(Die, match="these need the stack running: inference frontend"):
        suites.cmd_test(ctx, ["api", "ingex", "inference", "frontend"])
    assert ran(ctx) == []


def test_ingex_needs_nothing_running(tmp_path, capsys):
    ctx = suite_ctx(tmp_path, running=())
    suites.cmd_test(ctx, ["ingex"])
    assert ran(ctx) == ["ingex"]
    run = fake(ctx).calls[-1].argv
    assert f"{tmp_path}/ingex/ingest:/src" in run
    assert run[-4:] == ["go", "test", "-race", "./..."]
    assert "all passed (ingex)" in capsys.readouterr().out


def test_failures_are_collected_and_reported(tmp_path):
    ctx = suite_ctx(tmp_path, failing=("inference",))
    with pytest.raises(Die, match="test failures: inference"):
        suites.cmd_test(ctx, ["api", "inference", "frontend"])
    assert ran(ctx) == ["api", "inference", "frontend"]
