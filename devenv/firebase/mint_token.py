"""Mint a Firebase custom token for the seeded dev persona (api#301).

Signing in normally means completing Bluesky OAuth, which needs private keys
we deliberately don't ship in a credential-free environment. The Auth
emulator, though, accepts *unsigned* custom tokens — it parses the JWT and
ignores the signature entirely. So a dev login needs no secrets at all: mint
an unsigned token carrying the persona's DID as the uid and hand it to the
app's own `#/auth/finish?token=...` route, the same entry point the real
OAuth callback redirects to.

The uid has to be the full `did:plc:...`: the api's verify_firebase_auth
rejects anything else, then strips the prefix to get the Firestore document
key (matching `userDocId()` in the frontend's firestore.rules).

This only ever works against an emulator. Real Firebase verifies the
signature and would reject these outright.
"""

import base64
import json
import sys
import time

AUDIENCE = (
    "https://identitytoolkit.googleapis.com/google.identity.identitytoolkit.v1.IdentityToolkit"
)
# Any issuer works — the emulator doesn't check it — but a recognizable one
# makes it obvious in logs where the token came from.
ISSUER = "devenv@greenearth.invalid"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def mint(uid: str) -> str:
    now = int(time.time())
    header = {"alg": "none", "typ": "JWT"}
    payload = {
        "iss": ISSUER,
        "sub": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 3600,
        "uid": uid,
    }
    return ".".join(
        [
            b64url(json.dumps(header).encode()),
            b64url(json.dumps(payload).encode()),
            "",  # unsigned; the emulator ignores this segment
        ]
    )


def token_for(did: str) -> str:
    """Validate *did* and mint a token for it, exiting on a bad value.

    The api rejects any uid that isn't a did:plc, so catching it here gives a
    clear error instead of an opaque 401 later.
    """
    if not did.startswith("did:plc:"):
        sys.exit(f"FATAL: uid must be a did:plc DID, got {did!r}")
    return mint(did)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: mint_token.py <did:plc:...>")
    print(token_for(sys.argv[1]))
