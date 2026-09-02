"""Write Firestore UserDocuments for the fixture personas (ingex#followed_users_backfill).

devctl seed's rebase step (seed/rebase.py) already selects personas[0] as the
single "probe" persona and points /runtime/probe.env at it — that's what the
api container reads to serve a signed-in feed via `devctl feed`/`devctl
login`. But a Firestore users/ document only ever gets written for that one
persona, and only lazily, the first time the api serves a feed for them. No
other fixture persona ever gets a UserDocument, so ingex's
followed_users_backfill job (which enumerates the users/ collection via
ListUserDIDs) never sees more than one tracked user in devenv — its happy
path was never exercised.

This script writes a UserDocument for every persona in the rebased
manifest (not just personas[0]), using the api's own upsert_user helper so
the shape matches exactly what the real app writes (real Pydantic defaults
for social_radius, freshness, etc. — no hand-rolled placeholder fields).

Deliberately additive: it runs after seed-rebase (so /runtime/seed/manifest.json
exists) and never touches /runtime/probe.env — devctl feed/login keep pointing
at personas[0] exactly as before.

Runs inside the api container (`docker compose run --rm seed-firestore-users`
via devctl seed), not the bare seed-rebase one: that's where a Firestore
client and the app's own document models already live (api/Pipfile pins
google-cloud-firestore), so this reuses upsert_user/UserDocument instead of
hand-rolling a second copy of the schema in a stdlib-only script.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app/src")

# app.lib.firestore lives in the api repo, bind-mounted to /app inside the
# container this script runs in (see the seed-firestore-users compose
# service) — not resolvable from internal-tools' own pyright environment.
from app.lib.firestore import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    init_firestore_client,
    upsert_user,
    user_doc_id,
)

MANIFEST_PATH = Path("/runtime/seed/manifest.json")


async def seed_personas(personas: list[dict]) -> None:
    db = init_firestore_client()
    for persona in personas:
        did = persona["did"]
        await upsert_user(db, did, username=None)
        print(f"  {did} -> users/{user_doc_id(did)}")


def main() -> None:
    if not MANIFEST_PATH.exists():
        sys.exit(f"FATAL: {MANIFEST_PATH} missing — run seed-rebase first")
    manifest = json.loads(MANIFEST_PATH.read_text())
    personas = manifest.get("personas") or []
    if not personas:
        print("No personas in manifest; skipping Firestore user seed")
        return

    print(f"Writing Firestore UserDocuments for {len(personas)} persona(s):")
    asyncio.run(seed_personas(personas))
    print("Firestore persona seed complete")


if __name__ == "__main__":
    main()
