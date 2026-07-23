"""Stand in for the Bluesky OAuth login leg in the dev environment (api#301).

The frontend's Sign In button navigates to /auth/bluesky, which Vite proxies
to the `authBluesky` Cloud Function. That function starts a real Bluesky OAuth
handshake and needs private keys a credential-free environment doesn't carry,
so the button just fails — the one part of the app a new engineer is most
likely to try first.

This service sits where the Functions emulator would and:

- answers the authBluesky call with a redirect straight to the app's own
  `#/auth/finish?token=...` route — the same place the real OAuth callback
  sends the browser, carrying a token for the seeded persona;
- reverse-proxies every other request to the real Functions emulator, so
  oauthJwks, oauthClientMetadata and friends behave normally.

The redirect is deliberately *relative* so the browser stays on whichever
origin it was already using: Firebase keeps auth state per origin, so bouncing
between localhost:3000 and 127.0.0.1:3000 would silently look logged out.

This exists only to make local sign-in possible. The token it mints is
unsigned and only an emulator will accept it (see firebase/mint_token.py).
"""

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

# Both the prod and stage entry points land here; either should log you in.
AUTH_FUNCTION_RE = re.compile(r"/authBluesky(Stage)?(\?|$)", re.IGNORECASE)


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
        persona = seeded_persona()
        if not persona:
            self.send_error(
                503,
                "No seeded persona yet — run `devctl seed`, then try signing in again.",
            )
            return

        # Vite's functionProxy rewrites the path to a fixed value, dropping the
        # original query string, so return_url never reaches us. The app
        # defaults to /feed, which is where sign-in should land anyway.
        params = urllib.parse.urlencode({"token": mint(persona), "return_url": "/feed"})
        self.send_response(302)
        self.send_header("Location", f"/#/auth/finish?{params}")
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
