"""Issue the mysky api key BridgEU calls the local api with (bridg-eu#16).

In production, BridgEU holds an API key a mysky admin issued with the api's
scripts/apikeys.py, and sends it to the api in X-API-Key. Here the key is a
fixed, well-known value (devctl passes it in GE_DEV_BRIDGEU_MYSKY_API_KEY and
hands the same value to bridgeu-api), so no state has to travel between the
two sides: this script just makes the local api accept it.

The Firestore emulator keeps nothing across a recreate, so devctl runs this on
every `up` (and after `restart firebase`). Writing the same document again is
harmless.

Runs inside the api image over the api checkout, like seed_firestore_users.py,
so the stored document is the api's own ApiKeyDocument, hashed the api's way.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, "/app/src")

# The api repo is bind-mounted at /app in the container this runs in; not
# resolvable from internal-tools' own pyright environment.
from app.documents import ApiKeyDocument  # noqa: E402  # pyright: ignore[reportMissingImports]
from app.lib.api_keys import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    API_KEYS_COLLECTION,
    _hash_key,
    parse_key_id,
)
from app.lib.firestore import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    init_firestore_client,
)

OWNER_EMAIL = "bridgeu@devenv.local"


async def issue(full_key: str) -> str:
    key_id = parse_key_id(full_key)
    if key_id is None:
        sys.exit("FATAL: GE_DEV_BRIDGEU_MYSKY_API_KEY isn't a well-formed mysky api key")
    now = datetime.now(UTC)
    doc = ApiKeyDocument(
        key_id=key_id,
        key_hash=_hash_key(full_key),
        email=OWNER_EMAIL,
        is_active=True,
        is_admin=False,
        created_at=now,
        last_used_at=now,
        monthly_call_count=0,
        monthly_period=now.strftime("%Y-%m"),
    )
    db = init_firestore_client()
    await db.collection(API_KEYS_COLLECTION).document(key_id).set(doc.model_dump())
    return key_id


def main() -> None:
    full_key = os.environ.get("GE_DEV_BRIDGEU_MYSKY_API_KEY", "")
    if not full_key:
        sys.exit("FATAL: GE_DEV_BRIDGEU_MYSKY_API_KEY is not set")
    key_id = asyncio.run(issue(full_key))
    print(f"BridgEU's mysky api key is active (api_keys/{key_id})")


if __name__ == "__main__":
    main()
