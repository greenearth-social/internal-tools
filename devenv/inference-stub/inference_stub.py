"""Milestone-1 stand-in for inference-service (issue api#268).

The seed pipeline and the ranked feeds both need *something* answering the
inference endpoints, so this stub implements the three the api calls:

  POST /models/post-tower/predict — required by megastream_ingest to compute
      ge_post_embedding at ingest time. Returns the first 128 dims of the
      input MiniLM vector, L2-normalized.
  POST /models/user-tower/predict — required by the `two_tower` generator.
      Returns the L2-normalized mean of the user's history embeddings, which
      lands in the same space as the post tower above, so kNN over it returns
      posts resembling what the user liked rather than noise.
  POST /models/ranker/predict — required by the `heavy_ranker` ranker (and so
      by `your-feed`). Scores each candidate by cosine similarity to that same
      mean history embedding.

All three are deterministic and reproducible. They are *not* trained models:
ordering is "similar to your history" rather than predicted engagement, which
is enough to exercise the full retrieve → rank → diversify path end to end
without the real models. Replaced by the real inference-service + published
trained models in milestone 2 (api#269) — same env vars, same endpoints,
drop-in swap.

Stdlib only; runs on a stock python image.
"""

import json
import math
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

API_KEY = os.environ.get("GE_INFERENCE_API_KEY", "")
OUTPUT_DIM = 128
MODEL_UUID = "ge-dev-post-tower-stub"


def l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm < 1e-12:
        # ES cosine-similarity fields reject zero vectors, and a zero user
        # embedding would make every candidate score identically.
        return [1.0] + [0.0] * (len(vector) - 1)
    return [x / norm for x in vector]


def pseudo_embed(vector: list[float]) -> list[float]:
    return l2_normalize((vector + [0.0] * OUTPUT_DIM)[:OUTPUT_DIM])


def mean_embedding(vectors: list[list[float]]) -> list[float]:
    """L2-normalized mean of *vectors*, in the post tower's output space.

    An empty history still has to produce a usable unit vector — a new user
    hitting a two_tower feed is normal, and returning nothing would fail the
    kNN query rather than just giving unpersonalized results.
    """
    if not vectors:
        return [1.0] + [0.0] * (OUTPUT_DIM - 1)
    projected = [pseudo_embed(v) for v in vectors]
    dim = len(projected[0])
    summed = [sum(vec[i] for vec in projected) for i in range(dim)]
    return l2_normalize([x / len(projected) for x in summed])


def cosine(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n))


class Handler(BaseHTTPRequestHandler):
    # Keep-alive plus the larger backlog on Server below: the api can issue
    # many inference calls in quick succession, and HTTP/1.0 would open a
    # fresh connection for each. Safe because every response below sends an
    # accurate Content-Length.
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok", "stub": True})
        elif self.path == "/ready":
            # Shape matters: the api scans `models` for the entry whose "type"
            # is post-tower and reads its "model_uuid" — that UUID is stamped
            # on indexed posts and is what the two_tower generator filters its
            # kNN query by. Keying the entries on "name" instead made the api
            # raise "ready response model entry 0 missing string type", which
            # surfaced as the two_tower generator failing. Mirrors
            # inference-service's /ready payload.
            self._send(
                200,
                {
                    "ready": True,
                    "registry_error": None,
                    "embed_dim": OUTPUT_DIM,
                    "author_idx_maps_error": None,
                    "author_idx_maps": {},
                    "models": [
                        {
                            "type": model_type,
                            "model_uuid": MODEL_UUID if model_type == "post-tower" else None,
                            "ready": True,
                            "device": "cpu",
                            "load_error": None,
                        }
                        for model_type in ("post-tower", "user-tower", "ranker")
                    ],
                },
            )
        else:
            self._send(404, {"detail": "Not Found"})

    def do_POST(self) -> None:
        handlers = {
            "/models/post-tower/predict": self._post_tower,
            "/models/user-tower/predict": self._user_tower,
            "/models/ranker/predict": self._ranker,
        }
        handler = handlers.get(self.path)
        if handler is None:
            self._send(404, {"detail": f"Unknown model endpoint {self.path}"})
            return
        if API_KEY and self.headers.get("X-API-Key") != API_KEY:
            self._send(401, {"detail": "Invalid API key"})
            return
        try:
            body = self._read_body()
            status, response = handler(body)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
            self._send(422, {"detail": f"Bad request: {e}"})
            return
        self._send(status, response)

    def _post_tower(self, body: dict) -> tuple[int, dict]:
        outputs = [pseudo_embed(vec) for vec in body["post_embeddings"]]
        return 200, {"outputs": outputs, "model_type": "post-tower", "model_uuid": MODEL_UUID}

    def _user_tower(self, body: dict) -> tuple[int, dict]:
        # The api sends one user's history and expects a list of embeddings
        # back (a batch of one).
        user_embedding = mean_embedding(body["history_embeddings"])
        return 200, {"outputs": [user_embedding], "model_type": "user-tower"}

    def _ranker(self, body: dict) -> tuple[int, dict]:
        user_embedding = mean_embedding(body["history_embeddings"])
        candidates = body["candidate_post_embeddings"]
        # Cosine over unit vectors is in [-1, 1], which is the range the api's
        # rank-score normalization already expects.
        scores = [cosine(user_embedding, pseudo_embed(vec)) for vec in candidates]
        return 200, {"outputs": scores, "model_type": "ranker"}

    # Parameter is named `format` to match BaseHTTPRequestHandler's signature.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        print(f"inference-stub: {format % args}")


class Server(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True


if __name__ == "__main__":
    server = Server(("0.0.0.0", 8000), Handler)
    print("inference-stub listening on :8000")
    server.serve_forever()
