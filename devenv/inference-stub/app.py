"""Milestone-1 stand-in for inference-service (issue api#268).

megastream_ingest requires a post-tower endpoint to compute ge_post_embedding
at ingest time, so the seed pipeline needs *something* answering
POST /models/post-tower/predict. This stub returns a deterministic
pseudo-embedding: the first 128 dims of the input MiniLM vector, L2-normalized.
That keeps ES happy (cosine-similarity fields reject zero vectors), preserves
some semantic locality, and is fully reproducible.

Replaced by the real inference-service + published trained models in
milestone 2 (api#269) — same env vars, same endpoint, drop-in swap.

Stdlib only; runs on a stock python image.
"""

import json
import math
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_KEY = os.environ.get("GE_INFERENCE_API_KEY", "")
OUTPUT_DIM = 128
MODEL_UUID = "ge-dev-post-tower-stub"


def pseudo_embed(vector: list[float]) -> list[float]:
    head = (vector + [0.0] * OUTPUT_DIM)[:OUTPUT_DIM]
    norm = math.sqrt(sum(x * x for x in head))
    if norm < 1e-12:
        head = [1.0] + [0.0] * (OUTPUT_DIM - 1)
        norm = 1.0
    return [x / norm for x in head]


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok", "stub": True})
        else:
            self._send(404, {"detail": "Not Found"})

    def do_POST(self) -> None:
        if self.path != "/models/post-tower/predict":
            self._send(404, {"detail": f"Unknown model endpoint {self.path}"})
            return
        if API_KEY and self.headers.get("X-API-Key") != API_KEY:
            self._send(401, {"detail": "Invalid API key"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            embeddings = body["post_embeddings"]
            outputs = [pseudo_embed(vec) for vec in embeddings]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
            self._send(422, {"detail": f"Bad request: {e}"})
            return
        self._send(
            200,
            {"outputs": outputs, "model_type": "post-tower", "model_uuid": MODEL_UUID},
        )

    def log_message(self, fmt: str, *args) -> None:
        print(f"inference-stub: {fmt % args}")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    print("inference-stub listening on :8000")
    server.serve_forever()
