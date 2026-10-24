import json

import pytest

from devctlib import doctor
from devctlib.ctx import Die
from devctlib.testing import FakeRunner, make_ctx


def running(*services):
    def respond(argv):
        if "ps" in argv and "running" in argv:
            return "\n".join(services) + "\n"
        return None

    return FakeRunner(respond)


def with_bundles(ctx):
    models = ctx.devenv / "models" / "data"
    fixtures = ctx.devenv / "fixtures" / "data"
    models.mkdir(parents=True)
    fixtures.mkdir(parents=True)
    (models / "manifest.json").write_text(json.dumps({"tag": "t"}))
    (fixtures / "manifest.json").write_text(json.dumps({"counts": {"posts": 1, "likes": 2}}))
    for repo in ("api", "ingex", "frontend", "inference-service", "bridg-eu"):
        (ctx.repo_root / repo / ".git").mkdir(parents=True)


def test_a_running_api_without_es_keys_still_gets_a_full_report(tmp_path, capsys):
    """The bash version stopped silently here: its posts count ran es_curl,
    which died for want of a key, and set -e took the whole run with it."""
    ctx = make_ctx(tmp_path, runner=running("api", "firebase"))
    with_bundles(ctx)

    doctor.cmd_doctor(ctx, [])

    out = capsys.readouterr().out
    assert "no minted Elasticsearch keys yet" in out
    assert "no posts indexed" in out
    assert "api isn't answering" in out
    assert "warning(s), nothing fatal" in out


@pytest.mark.parametrize(("live", "skipped"), [("es", True), ("perspective", False)])
def test_only_live_es_skips_the_local_data_checks(tmp_path, capsys, live, skipped):
    """`*es*` used to match perspective too."""
    ctx = make_ctx(tmp_path, runner=running("api"), env={"GE_DEV_LIVE": live})

    assert doctor.check_stack(ctx, doctor.Report()) is not skipped
    assert ("local data checks skipped" in capsys.readouterr().out) == skipped


def test_summary():
    report = doctor.Report()
    report.ok("fine")
    report.warn("hmm", "do this")
    with pytest.raises(Die, match=r"^1 problem\(s\) and 1 warning\(s\) found$"):
        report.fail("bad")
        report.summary()
