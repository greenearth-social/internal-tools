"""Re-applying an active bsky or eurosky session's environment."""

import pytest

from devctlib import bsky, eurosky
from devctlib.testing import FakeRunner, make_ctx

BSKY_SESSION_VARS = (
    "GE_DEV_FEED_GENERATOR_DID",
    "GE_DEV_FRONTEND_ALLOWED_HOSTS",
    "GE_DEV_APP_ORIGIN",
    "GE_DEV_FUNCTIONS_TARGET",
    "BLUESKY_OAUTH_CLIENT_KID",
)


def session_ctx(tmp_path, *, ngrok_running):
    return make_ctx(tmp_path, runner=FakeRunner(lambda argv: "ngrok" if ngrok_running else ""))


def run_bsky_session_env(tmp_path, *, state, ngrok_running, oauth_ok="1"):
    ctx = session_ctx(tmp_path, ngrok_running=ngrok_running)
    if state:
        ctx.bsky_state.write_text(
            "GE_BSKY_HANDLE=dev.bsky.social\n"
            "GE_BSKY_HOST=abc.ngrok-free.dev\n"
            "GE_BSKY_DID=did:web:abc.ngrok-free.dev\n"
            f"GE_BSKY_OAUTH_OK={oauth_ok}\n"
        )
    keys = bsky.oauth_dir(ctx)
    keys.mkdir()
    for name, value in [
        ("kid", "kid-1"),
        ("private-key.json", "{}"),
        ("public-jwks.json", "{}"),
        ("state-key", "k"),
    ]:
        (keys / name).write_text(value)
    bsky.export_session_env(ctx)
    return {v: ctx.env.get(v, "unset") for v in BSKY_SESSION_VARS}


def test_bsky_session_env_keeps_the_api_and_frontend_on_the_tunnel(tmp_path):
    assert run_bsky_session_env(tmp_path, state=True, ngrok_running=True) == {
        "GE_DEV_FEED_GENERATOR_DID": "did:web:abc.ngrok-free.dev",
        "GE_DEV_FRONTEND_ALLOWED_HOSTS": "localhost,abc.ngrok-free.dev",
        "GE_DEV_APP_ORIGIN": "https://abc.ngrok-free.dev",
        "GE_DEV_FUNCTIONS_TARGET": "firebase:15001",
        "BLUESKY_OAUTH_CLIENT_KID": "kid-1",
    }


def test_bsky_session_env_without_oauth_leaves_sign_in_on_the_shim(tmp_path):
    env = run_bsky_session_env(tmp_path, state=True, ngrok_running=True, oauth_ok="0")
    assert env["GE_DEV_FEED_GENERATOR_DID"] == "did:web:abc.ngrok-free.dev"
    assert env["GE_DEV_APP_ORIGIN"] == "unset"
    assert env["GE_DEV_FUNCTIONS_TARGET"] == "unset"


@pytest.mark.parametrize(("state", "ngrok_running"), [(False, True), (True, False)])
def test_bsky_session_env_leaves_the_stack_local_otherwise(tmp_path, state, ngrok_running):
    env = run_bsky_session_env(tmp_path, state=state, ngrok_running=ngrok_running)
    assert set(env.values()) == {"unset"}


def run_eurosky_session_env(tmp_path, *, state, ngrok_running):
    ctx = session_ctx(tmp_path, ngrok_running=ngrok_running)
    if state:
        ctx.eurosky_state.write_text(
            "GE_EUROSKY_HANDLE=dev.eurosky.social\n"
            "GE_EUROSKY_PUBLISHER_DID=did:plc:dev\n"
            "GE_EUROSKY_HOST=abc.ngrok-free.dev\n"
        )
    eurosky.export_session_env(ctx)
    return tuple(
        ctx.env.get(v, "unset")
        for v in (
            "GE_DEV_BRIDGEU_HOSTNAME",
            "GE_DEV_BRIDGEU_PUBLISHER_DID",
            "GE_DEV_BRIDGEU_SESSION_SECRET",
        )
    )


def test_eurosky_session_env_points_bridgeu_api_at_the_tunnel(tmp_path):
    # The dev session goes off: bridg-eu refuses it on a public hostname.
    assert run_eurosky_session_env(tmp_path, state=True, ngrok_running=True) == (
        "abc.ngrok-free.dev",
        "did:plc:dev",
        "",
    )


@pytest.mark.parametrize(("state", "ngrok_running"), [(False, True), (True, False)])
def test_eurosky_session_env_leaves_bridgeu_api_local_otherwise(tmp_path, state, ngrok_running):
    """No session, or one whose tunnel is gone (after `devctl down`): stay local."""
    assert run_eurosky_session_env(tmp_path, state=state, ngrok_running=ngrok_running) == (
        "unset",
        "unset",
        "unset",
    )
