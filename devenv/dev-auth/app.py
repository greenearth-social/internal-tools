"""Stand in for the Bluesky OAuth login leg in the dev environment (api#301).

The frontend's Sign In button navigates to /auth/bluesky, which Vite proxies
to the `authBluesky` Cloud Function. That function starts a real Bluesky OAuth
handshake and needs private keys a credential-free environment doesn't carry,
so the button just fails — the one part of the app a new engineer is most
likely to try first.

This service sits where the Functions emulator would and:

- answers the authBluesky call with the app's own
  `#/auth/finish?token=...` route — as JSON for the frontend's fetch-based
  sign-in flow, or as a redirect for ordinary browser navigation — carrying a
  token for the seeded persona;
- reverse-proxies every other request to the real Functions emulator, so
  oauthJwks, oauthClientMetadata and friends behave normally.

The redirect is deliberately *relative* so the browser stays on whichever
origin it was already using: Firebase keeps auth state per origin, so bouncing
between localhost:3000 and 127.0.0.1:3000 would silently look logged out.

This exists only to make local sign-in possible. The token it mints is
unsigned and only an emulator will accept it (see firebase/mint_token.py).
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/firebase")
from mint_token import mint  # noqa: E402  (path set above)

FUNCTIONS_UPSTREAM = os.environ.get("GE_DEV_FUNCTIONS_UPSTREAM", "http://firebase:15001")
PROBE_ENV = "/runtime/probe.env"
HANDLE_RESOLVER_URL = "https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle"
HANDLE_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)

# Both the prod and stage entry points land here; either should log you in.
AUTH_FUNCTION_RE = re.compile(r"/authBluesky(Stage)?(\?|$)", re.IGNORECASE)


class HandleResolutionError(Exception):
    pass


def resolve_handle(raw_handle: str) -> str:
    handle = raw_handle.strip().removeprefix("@").lower()
    if not HANDLE_RE.fullmatch(handle):
        raise HandleResolutionError("Enter a valid account handle, such as alice.bsky.social")

    query = urllib.parse.urlencode({"handle": handle})
    request = urllib.request.Request(
        f"{HANDLE_RESOLVER_URL}?{query}",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            raise HandleResolutionError(f"Could not find Bluesky account '{handle}'") from exc
        raise HandleResolutionError(f"Bluesky handle resolver returned HTTP {exc.code}") from exc
    except (TimeoutError, urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise HandleResolutionError("Could not reach the Bluesky handle resolver") from exc

    did = payload.get("did") if isinstance(payload, dict) else None
    if not isinstance(did, str) or not did.startswith("did:plc:"):
        raise HandleResolutionError(
            "The account did not resolve to a did:plc identity supported by the local API"
        )
    return did


def seeded_persona() -> str | None:
    """The DID `devctl seed` last wrote, or None before a seed has run."""
    try:
        with open(PROBE_ENV) as handle:
            for line in handle:
                if line.startswith("GE_PROBE_USER_DID="):
                    return line.split("=", 1)[1].strip() or None
    except OSError:
        return None
    return None


class Handler(BaseHTTPRequestHandler):
    def _redirect_to_auth_finish(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        raw_handle = query.get("handle", [None])[0]
        try:
            persona = resolve_handle(raw_handle) if raw_handle else seeded_persona()
        except HandleResolutionError as exc:
            payload = str(exc).encode()
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if not persona:
            self.send_error(
                503,
                "No seeded persona yet — run `devctl seed`, then try signing in again.",
            )
            return

        return_url = query.get("return_url", ["/feed"])[0]
        params = urllib.parse.urlencode({"token": mint(persona), "return_url": return_url})
        redirect_url = f"/#/auth/finish?{params}"

        if "application/json" in self.headers.get("Accept", ""):
            payload = json.dumps({"redirectUrl": redirect_url}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(302)
            self.send_header("Location", redirect_url)
            self.send_header("Content-Length", "0")
            self.end_headers()

        print(f"dev-auth: signed in as {persona}")

    def _proxy(self) -> None:
        url = FUNCTIONS_UPSTREAM.rstrip("/") + self.path
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() != "host"}
        request = urllib.request.Request(url, data=body, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
                self.send_response(response.status)
                for key, value in response.headers.items():
                    if key.lower() not in ("transfer-encoding", "content-length", "connection"):
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        except urllib.error.HTTPError as e:
            payload = e.read()
            self.send_response(e.code)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (urllib.error.URLError, OSError) as e:
            self.send_error(502, f"Functions emulator unreachable: {e}")

    def _dispatch(self) -> None:
        if AUTH_FUNCTION_RE.search(self.path):
            self._redirect_to_auth_finish()
        else:
            self._proxy()

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_DELETE = _dispatch

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - matches base signature
        print(f"dev-auth: {format % args}")


if __name__ == "__main__":
    print(f"dev-auth listening on :8000, proxying to {FUNCTIONS_UPSTREAM}")
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
