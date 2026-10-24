from devctlib import http
from devctlib.testing import serve


def test_status_and_body_including_errors():
    with serve(lambda m, p: (404, '{"error":"no"}') if p == "/missing" else (200, "ok")) as srv:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        assert http.request(f"{base}/x") == http.Response(200, "ok")
        missing = http.request(f"{base}/missing")
    assert (missing.status, missing.body, missing.code) == (404, '{"error":"no"}', "404")


def test_a_body_makes_it_a_post():
    with serve(lambda m, p: (200, m)) as srv:
        url = f"http://127.0.0.1:{srv.server_address[1]}/q"
        assert http.request(url, method="POST", body="{}").body == "POST"
    assert srv.requests == [("POST", "/q")]


def test_nothing_answering_is_000():
    with serve(lambda m, p: (200, "")) as srv:
        port = srv.server_address[1]
    response = http.status(f"http://127.0.0.1:{port}/health", timeout=2)
    assert (response.status, response.code) == (0, "000")
