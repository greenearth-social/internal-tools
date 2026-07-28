import io
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "firebase"))
import app


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@pytest.fixture
def auth_server(monkeypatch) -> Iterator[str]:
    monkeypatch.setattr(app, "seeded_persona", lambda: "did:plc:test")
    monkeypatch.setattr(app, "resolve_handle", lambda _: "did:plc:resolved")
    monkeypatch.setattr(app, "mint", lambda did: f"token-for-{did}")
    server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join()
    server.server_close()


def test_json_request_returns_redirect_url_and_preserves_return_route(auth_server):
    request = urllib.request.Request(
        auth_server
        + "/greenearth-471522/us-central1/authBluesky"
        + "?return_url=%2Fcontrols&handle=alice.bsky.social",
        headers={"Accept": "application/json"},
    )

    with urllib.request.urlopen(request) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "application/json"
        payload = json.load(response)

    redirect_url = payload["redirectUrl"]
    fragment = urllib.parse.urlsplit(redirect_url).fragment
    route, query = fragment.split("?", 1)
    assert route == "/auth/finish"
    assert urllib.parse.parse_qs(query) == {
        "token": ["token-for-did:plc:resolved"],
        "return_url": ["/controls"],
    }


def test_browser_navigation_keeps_redirect_response(auth_server):
    request = urllib.request.Request(
        auth_server + "/greenearth-471522/us-central1/authBluesky",
        headers={"Accept": "text/html"},
    )
    opener = urllib.request.build_opener(NoRedirect())

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        opener.open(request)
    assert exc_info.value.code == 302
    assert exc_info.value.headers["Location"].startswith("/#/auth/finish?")
    assert exc_info.value.read() == b""


def test_missing_return_route_defaults_to_feed(auth_server):
    request = urllib.request.Request(
        auth_server + "/greenearth-471522/us-central1/authBluesky",
        headers={"Accept": "application/json, text/plain"},
    )

    with urllib.request.urlopen(request) as response:
        payload = json.load(response)

    query = urllib.parse.urlsplit(payload["redirectUrl"]).fragment.split("?", 1)[1]
    assert urllib.parse.parse_qs(query)["return_url"] == ["/feed"]


def test_handle_resolution_error_is_returned_to_frontend(auth_server, monkeypatch):
    def fail(_):
        raise app.HandleResolutionError("Could not find Bluesky account 'missing.example'")

    monkeypatch.setattr(app, "resolve_handle", fail)
    request = urllib.request.Request(
        auth_server
        + "/greenearth-471522/us-central1/authBluesky"
        + "?handle=missing.example",
        headers={"Accept": "application/json"},
    )

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request)
    assert exc_info.value.code == 400
    assert exc_info.value.read().decode() == "Could not find Bluesky account 'missing.example'"


def test_handleless_request_uses_seeded_persona(auth_server):
    request = urllib.request.Request(
        auth_server + "/greenearth-471522/us-central1/authBluesky",
        headers={"Accept": "application/json"},
    )

    with urllib.request.urlopen(request) as response:
        payload = json.load(response)

    query = urllib.parse.urlsplit(payload["redirectUrl"]).fragment.split("?", 1)[1]
    assert urllib.parse.parse_qs(query)["token"] == ["token-for-did:plc:test"]


@pytest.mark.parametrize(
    "handle",
    ["", "alice", "https://alice.bsky.social", "alice..bsky.social"],
)
def test_resolve_handle_rejects_invalid_handle(handle):
    with pytest.raises(app.HandleResolutionError, match="Enter a valid account handle"):
        app.resolve_handle(handle)


def test_resolve_handle_uses_public_appview_and_normalizes(monkeypatch):
    requests = []

    def urlopen(request, timeout):
        requests.append((request, timeout))
        return io.BytesIO(b'{"did":"did:plc:alice"}')

    monkeypatch.setattr(app.urllib.request, "urlopen", urlopen)

    assert app.resolve_handle(" @Alice.Bsky.Social ") == "did:plc:alice"
    request, timeout = requests[0]
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query) == {
        "handle": ["alice.bsky.social"]
    }
    assert timeout == 10


def test_resolve_handle_rejects_identity_unsupported_by_local_api(monkeypatch):
    monkeypatch.setattr(
        app.urllib.request,
        "urlopen",
        lambda request, timeout: io.BytesIO(b'{"did":"did:web:alice.example"}'),
    )

    with pytest.raises(app.HandleResolutionError, match="did:plc"):
        app.resolve_handle("alice.example")
