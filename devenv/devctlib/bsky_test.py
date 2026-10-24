import json

import pytest

from devctlib import bsky, eurosky, tunnel
from devctlib.ctx import Die
from devctlib.testing import FakeRunner, fake, make_ctx

UP_ENV = {
    "NGROK_AUTHTOKEN": "token",
    "GE_DEV_BSKY_HANDLE": "dev.bsky.social",
    "GE_BSKY_APP_PASSWORD": "pw",
    "GE_DEV_EUROSKY_HANDLE": "dev.eurosky.social",
    "GE_DEV_EUROSKY_APP_PASSWORD": "pw",
}


@pytest.mark.parametrize(
    ("up", "other_state", "message"),
    [
        (eurosky.eurosky_up, "bsky.env", "a bsky session holds the tunnel"),
        (bsky.bsky_up, "eurosky.env", "a eurosky session holds the tunnel"),
    ],
)
def test_bsky_and_eurosky_sessions_exclude_each_other(tmp_path, up, other_state, message):
    """They share one tunnel and gateway, so the second `up` refuses."""
    ctx = make_ctx(tmp_path, env=UP_ENV, runner=FakeRunner(lambda argv: "api bridgeu-api"))
    (tmp_path / "bridg-eu" / ".git").mkdir(parents=True)
    (ctx.runtime / other_state).write_text("")

    with pytest.raises(Die, match=message):
        up(ctx)
    # Nothing past the guards ran.
    assert [c for c in fake(ctx).lines if "ps" not in c] == []


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A bsky up whose tunnel, did.json and OAuth keys are stand-ins; the
    runner answers what the api and firebase are asked."""
    monkeypatch.setattr(tunnel, "start", lambda ctx: True)
    monkeypatch.setattr(tunnel, "host", lambda ctx: "abc.ngrok-free.dev")
    monkeypatch.setattr(tunnel, "stop", lambda ctx: None)
    monkeypatch.setattr(tunnel, "wait_did", lambda host, did, tries=15: True)
    failing = []

    def respond(argv):
        line = " ".join(argv)
        if "ps" in argv:
            return "api firebase"
        if "publish_feed.py" in line and any(f in line for f in failing):
            return 1
        if "-c" in argv and "app.feeds" in argv[argv.index("-c") + 1]:
            return "dev-your\tyour-feed\ndev-pop\tpopularity\n"
        if "-c" in argv and "app.main" in argv[argv.index("-c") + 1]:
            return "/.well-known/did.json /xrpc /xrpc/*\n"
        return None

    ctx = make_ctx(tmp_path, env=UP_ENV, runner=FakeRunner(respond))
    (ctx.runtime / "es_key_ro.env").write_text("GE_ELASTICSEARCH_API_KEY=k\n")
    keys = bsky.oauth_dir(ctx)
    keys.mkdir()
    for name in bsky.OAUTH_FILES:
        (keys / name).write_text(f"{name}-value")
    ctx.failing = failing  # type: ignore[attr-defined]
    return ctx


def test_bsky_up_serves_the_feed_through_the_tunnel(session, capsys):
    bsky.bsky_up(session)

    lines = fake(session).compose_lines()
    recreate_api = next(c for c in fake(session).calls if c.line.endswith("--force-recreate api"))
    assert recreate_api.env["GE_DEV_FEED_GENERATOR_DID"] == "did:web:abc.ngrok-free.dev"
    assert recreate_api.env["GE_DEV_ES_API_KEY"] == "k"
    # The DID was for that one recreate, not every later one.
    assert "GE_DEV_FEED_GENERATOR_DID" not in session.env
    assert "up -d --no-deps --force-recreate frontend firebase" in lines
    publish = next(c.argv for c in fake(session).calls if "scripts/publish_feed.py" in c.argv)
    assert publish[-6:] == [
        "--handle",
        "dev.bsky.social",
        "--app-password",
        "pw",
        "--generator-did",
        "did:web:abc.ngrok-free.dev",
    ]
    assert (
        "@api path /.well-known/did.json /xrpc /xrpc/*"
        in (session.runtime / "Caddyfile").read_text()
    )
    assert json.loads(json.dumps(session.bsky_state.read_text().splitlines())) == [
        "GE_BSKY_HANDLE=dev.bsky.social",
        "GE_BSKY_HOST=abc.ngrok-free.dev",
        "GE_BSKY_DID=did:web:abc.ngrok-free.dev",
        "GE_BSKY_OAUTH_OK=1",
    ]
    out = capsys.readouterr().out
    assert "  your-feed:   https://bsky.app/profile/dev.bsky.social/feed/dev-your" in out
    assert "  popularity:  https://bsky.app/profile/dev.bsky.social/feed/dev-pop" in out


def test_bsky_up_rolls_back_when_publishing_fails(session):
    session.failing.append("--sync")

    with pytest.raises(Die, match="publishing feeds failed"):
        bsky.bsky_up(session)

    assert not session.bsky_state.exists()
    assert fake(session).compose_lines()[-1] == "up -d --no-deps --force-recreate api frontend"


def test_bsky_up_without_the_api_says_so(tmp_path):
    with pytest.raises(Die, match="the api isn't running"):
        bsky.bsky_up(make_ctx(tmp_path, env=UP_ENV))


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("NGROK_AUTHTOKEN", "set NGROK_AUTHTOKEN"),
        ("GE_DEV_BSKY_HANDLE", "set GE_DEV_BSKY_HANDLE"),
        ("GE_BSKY_APP_PASSWORD", "set GE_BSKY_APP_PASSWORD"),
    ],
)
def test_bsky_up_names_missing_configuration(tmp_path, missing, message):
    env = {k: v for k, v in UP_ENV.items() if k != missing}
    ctx = make_ctx(tmp_path, env=env, runner=FakeRunner(lambda argv: "api"))
    with pytest.raises(Die, match=message):
        bsky.bsky_up(ctx)


def test_bsky_down_deletes_the_records_and_restores_the_stack(session, capsys):
    bsky.bsky_up(session)
    capsys.readouterr()

    bsky.bsky_down(session)

    delete = next(c.argv for c in fake(session).calls if "--delete-all" in c.argv)
    assert delete[-4:] == ["--handle", "dev.bsky.social", "--app-password", "pw"]
    assert not session.bsky_state.exists()
    assert not (session.runtime / bsky.FEEDS_TSV).exists()
    assert "back to a local-only feed" in capsys.readouterr().out


def test_bsky_status_distinguishes_a_dead_tunnel(tmp_path, capsys):
    ctx = make_ctx(tmp_path, runner=FakeRunner(lambda argv: ""))
    ctx.bsky_state.write_text("GE_BSKY_HANDLE=me\nGE_BSKY_DID=did:web:old.ngrok.dev\n")

    bsky.bsky_status(ctx)

    out = capsys.readouterr().out
    assert "but its tunnel isn't running" in out
    # A state file from before the tunnels merged recorded no host.
    assert "Public URL:  https://old.ngrok.dev" in out


def test_oauth_keys_are_generated_in_firebase_once(tmp_path):
    pair = {"kid": "k1", "privateJwk": {"d": "x"}, "publicJwks": {"keys": []}}

    def respond(argv):
        if "ps" in argv:
            return "firebase"
        if "node" in argv:
            return json.dumps(pair)
        return None

    ctx = make_ctx(tmp_path, runner=FakeRunner(respond))

    assert bsky.ensure_oauth_keys(ctx) is True
    keys = bsky.oauth_dir(ctx)
    assert (keys / "kid").read_text() == "k1"
    assert json.loads((keys / "private-key.json").read_text()) == {"d": "x"}
    assert len((keys / "state-key").read_text().strip()) == 64
    calls = len(fake(ctx).calls)
    assert bsky.ensure_oauth_keys(ctx) is True
    assert len(fake(ctx).calls) == calls  # cached


def test_oauth_keys_need_firebase(tmp_path, capsys):
    assert bsky.ensure_oauth_keys(make_ctx(tmp_path)) is False
    assert "firebase container isn't running" in capsys.readouterr().err
