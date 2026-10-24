# pyright: reportMissingImports=false
"""The api's public paths, as Caddy `path` matcher tokens (run in the api).

Read from the running api itself: a list hand-copied into devctl goes stale
the next time the api adds a router, and a missed path doesn't 404, it
silently answers with the frontend's HTML. The routers are walked rather than
read from the OpenAPI schema so paths that opt out of the schema still route.

Two paths need judgement rather than a prefix:
  /                     the api has a root route, but the tunnel's "/" is the
                        app the browser loads — the frontend keeps it.
  /.well-known/did.json matched exactly, because the neighbouring
                        oauth-client-metadata and jwks.json are the frontend's.
Everything else routes by first path segment, so new endpoints under an
existing router work without regenerating anything.
"""

import sys

sys.path.insert(0, "src")
from app.main import app  # noqa: E402


def walk(routes):
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from walk(included.routes)
        elif getattr(route, "path", None):
            yield route.path


exact, prefixes, nested = [], [], set()
for path in walk(app.routes):
    if path == "/":
        continue
    if path.startswith("/.well-known/"):
        exact.append(path)
        continue
    segment = path.split("/")[1]
    prefixes.append(segment)
    if path.count("/") > 1:
        nested.add(segment)
tokens = sorted(set(exact))
for segment in sorted(set(prefixes)):
    tokens.append("/" + segment)
    if segment in nested:
        tokens.append("/" + segment + "/*")
print(" ".join(tokens))
