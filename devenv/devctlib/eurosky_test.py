import pytest

from devctlib import eurosky, tunnel
from devctlib.ctx import Die
from devctlib.testing import FakeRunner, fake, make_ctx

ENV = {
    "NGROK_AUTHTOKEN": "token",
    "GE_DEV_EUROSKY_HANDLE": "dev.eurosky.social",
    "GE_DEV_EUROSKY_APP_PASSWORD": "secret-pw",
}


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(tunnel, "start", lambda ctx: True)
    monkeypatch.setattr(tunnel, "host", lambda ctx: "abc.ngrok-free.dev")
    monkeypatch.setattr(tunnel, "stop", lambda ctx: None)
    monkeypatch.setattr(tunnel, "wait_did", lambda host, did, tries=15: True)

    def respond(argv):
        if "ps" in argv:
            return "bridgeu-api ngrok"
        if "-c" in argv:
            return "did:plc:publisher\r\n"
        if "--configured" in argv:
            return "beu-pass\tpassthrough\r\n"
        return None

    ctx = make_ctx(tmp_path, env=ENV, runner=FakeRunner(respond))
    (tmp_path / "bridg-eu" / ".git").mkdir(parents=True)
    return ctx


def test_eurosky_up_publishes_from_bridgeu_api(session, capsys):
    eurosky.eurosky_up(session)

    recreate = next(
        c for c in fake(session).calls if c.line.endswith("--force-recreate bridgeu-api")
    )
    assert recreate.env["GE_DEV_BRIDGEU_HOSTNAME"] == "abc.ngrok-free.dev"
    assert recreate.env["GE_DEV_BRIDGEU_PUBLISHER_DID"] == "did:plc:publisher"
    assert recreate.env["GE_DEV_BRIDGEU_SESSION_SECRET"] == ""
    publish = next(c.argv for c in fake(session).calls if "--handle" in c.argv)
    # The password is in the environment, not argv.
    assert "-e" in publish and "BRIDGEU_PUBLISHER_APP_PASSWORD=secret-pw" in publish
    assert "secret-pw" not in publish[publish.index("bridgeu-api") :]
    assert "reverse_proxy bridgeu-api:8000" in (session.runtime / "Caddyfile").read_text()
    assert session.eurosky_state.read_text() == (
        "GE_EUROSKY_HANDLE=dev.eurosky.social\n"
        "GE_EUROSKY_PUBLISHER_DID=did:plc:publisher\n"
        "GE_EUROSKY_HOST=abc.ngrok-free.dev\n"
    )
    assert (
        "  passthrough:  https://bsky.app/profile/dev.eurosky.social/feed/beu-pass"
        in capsys.readouterr().out
    )


def test_eurosky_down_deletes_exactly_what_up_published(session):
    eurosky.eurosky_up(session)

    eurosky.eurosky_down(session)

    delete = next(c.argv for c in fake(session).calls if "--delete" in c.argv)
    assert delete[-2:] == ["--delete", "beu-pass"]
    assert "BRIDGEU_PUBLISHER_DID=did:plc:publisher" in delete
    assert not session.eurosky_state.exists()
    assert fake(session).compose_lines()[-1] == "up -d --no-deps --force-recreate bridgeu-api"


def test_eurosky_down_keeps_the_session_if_deletion_fails(session):
    eurosky.eurosky_up(session)
    respond = fake(session).respond
    assert respond is not None
    fake(session).respond = lambda argv: 1 if "--delete" in argv else respond(argv)

    with pytest.raises(Die, match="the session is left in place"):
        eurosky.eurosky_down(session)
    assert session.eurosky_state.exists()


def test_eurosky_up_needs_a_resolvable_handle(session):
    respond = fake(session).respond
    assert respond is not None
    fake(session).respond = lambda argv: "not-a-did" if "-c" in argv else respond(argv)
    with pytest.raises(Die, match="couldn't resolve dev.eurosky.social to a DID"):
        eurosky.eurosky_up(session)


def test_eurosky_needs_the_checkout(tmp_path):
    with pytest.raises(Die, match="needs bridg-eu checked out"):
        eurosky.eurosky_up(make_ctx(tmp_path, env=ENV, runner=FakeRunner(lambda a: "bridgeu-api")))
