"""Local stand-in for Google's Perspective API (issue api#268).

`your-feed` runs the `perspective` ranker, which calls Google's Perspective
API and hard-fails without a key. That made the flagship feed impossible to
run locally: `devctl feed your-feed` returned a 500. This stub answers the
one endpoint that ranker uses, so the feed runs end to end with no external
key and no network.

The api already supports pointing at it — `GE_PERSPECTIVE_HOST` overrides the
API host — so nothing in the api had to change.

Scores are deterministic, derived from a hash of the comment text, and are
*not* meaningful content analysis: a post's "toxicity" here is a stable
pseudo-random number, not a judgement. That is enough to exercise ranking,
weighting, and slate cutoffs. Set GE_PERSPECTIVE_API_KEY to a real key (and
unset GE_PERSPECTIVE_HOST) to score against the real API instead.

Stdlib only; runs on a stock python image.
"""

import hashlib
import json
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

ANALYZE_PATH = "/v1alpha1/comments:analyze"


def attribute_score(text: str, attribute: str) -> float:
    """Deterministic pseudo-score in [0, 1] for (text, attribute).

    Keyed on both so one post gets a different value per attribute, and the
    same post scores identically across runs — a feed's order shouldn't
    change just because it was regenerated.
    """
    digest = hashlib.sha256(f"{attribute}\x00{text}".encode()).digest()
    (value,) = struct.unpack("<I", digest[:4])
    return value / 0xFFFFFFFF


class Handler(BaseHTTPRequestHandler):
    # The api scores every candidate in a feed concurrently — a burst of ~100
    # requests — against a 1s per-request timeout. Two stdlib defaults make
    # that burst time out even though each response takes milliseconds:
    # HTTP/1.0 gives no keep-alive, so every score opens its own connection,
    # and the listen backlog below is only 5, so the rest queue past the
    # timeout. HTTP/1.1 is safe here because every response sends an accurate
    # Content-Length.
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/health":
            self._send(200, {"status": "ok", "stub": True})
        else:
            self._send(404, {"detail": "Not Found"})

    def do_POST(self) -> None:
        # The real API takes the key as a query parameter, so the path carries
        # a query string; compare only the path portion.
        if urlparse(self.path).path != ANALYZE_PATH:
            self._send(404, {"detail": f"Unknown endpoint {self.path}"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            text = body["comment"]["text"]
            requested = body["requestedAttributes"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
            self._send(400, {"error": {"message": f"Bad request: {e}"}})
            return

        self._send(
            200,
            {
                "attributeScores": {
                    name: {
                        "summaryScore": {
                            "value": attribute_score(text, name),
                            "type": "PROBABILITY",
                        }
                    }
                    for name in requested
                },
                "languages": ["en"],
                "detectedLanguages": ["en"],
            },
        )

    # Parameter is named `format` to match BaseHTTPRequestHandler's signature.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        print(f"perspective-stub: {format % args}")


class Server(ThreadingHTTPServer):
    # See Handler.protocol_version: a feed scores its whole candidate set at
    # once, so the default backlog of 5 drops the rest of the burst.
    request_queue_size = 128
    daemon_threads = True


if __name__ == "__main__":
    server = Server(("0.0.0.0", 8000), Handler)
    print("perspective-stub listening on :8000")
    server.serve_forever()
