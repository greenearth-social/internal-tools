"""Stage 4: prod Firestore activity data, tiered by cost.

Tier A reads users/{doc_id} for every DID (cheap: one batched get_all() pass).
Tier B reads feed_activity/interactions/seen_posts subcollections for a
sample only — interactions isn't indexed for a user_did-filtered
collection-group query today, and per-user subcollection reads at 129k scale
would be far more expensive than a sample already answers the "do they
behave like a real client" question for.

Requires GE_FIRESTORE_PROJECT / GE_FIRESTORE_DATABASE and application-default
credentials, same as api/scripts/apikeys.py and api/scripts/feed_debug.py.
"""

import random
import sys

from dids import chunk
from google.cloud import firestore
from scoring import FirestoreTierA, ScoredRow

PLC_PREFIX = "did:plc:"


def user_doc_id(did: str) -> str:
    return did[len(PLC_PREFIX) :] if did.startswith(PLC_PREFIX) else did


def parse_user_doc(did: str, doc_dict: dict | None) -> FirestoreTierA:
    if doc_dict is None:
        return FirestoreTierA(
            did=did, found=False, created_at=None, last_seen_at=None, social_radius=None
        )
    return FirestoreTierA(
        did=did,
        found=True,
        created_at=doc_dict.get("created_at"),
        last_seen_at=doc_dict.get("last_seen_at"),
        social_radius=doc_dict.get("social_radius"),
    )


def select_tier_b_sample(
    scored_rows: list[ScoredRow],
    top_n: int = 500,
    control_n: int = 500,
    seed: int = 0,
) -> list[str]:
    ranked = sorted(scored_rows, key=lambda r: r.score, reverse=True)
    top = [r.did for r in ranked[:top_n]]
    remainder = [r.did for r in ranked[top_n:]]
    rng = random.Random(seed)
    control = rng.sample(remainder, min(control_n, len(remainder)))
    return top + control


def run_tier_a(all_dids: list[str], project: str, database: str) -> dict[str, FirestoreTierA]:
    db = firestore.Client(project=project, database=database)
    results: dict[str, FirestoreTierA] = {}
    for batch in chunk(all_dids, 500):
        refs = [db.collection("users").document(user_doc_id(did)) for did in batch]
        found_by_ref = {}
        for snapshot in db.get_all(refs):
            if snapshot.exists:
                found_by_ref[snapshot.id] = snapshot.to_dict()
        for did in batch:
            results[did] = parse_user_doc(did, found_by_ref.get(user_doc_id(did)))
        print(f"tier A: {len(results):,}/{len(all_dids):,} DIDs", file=sys.stderr)
    return results


def run_tier_b(sample_dids: list[str], project: str, database: str) -> dict[str, dict]:
    db = firestore.Client(project=project, database=database)
    results: dict[str, dict] = {}
    for did in sample_dids:
        doc_id = user_doc_id(did)
        user_ref = db.collection("users").document(doc_id)
        results[did] = {
            "feed_activity": [d.to_dict() for d in user_ref.collection("feed_activity").stream()],
            "interactions": [
                d.to_dict()
                for d in db.collection("interactions").where("user_did", "==", did).stream()
            ],
            "seen_posts": [d.to_dict() for d in user_ref.collection("seen_posts").stream()],
        }
    print(f"tier B: pulled behavioral data for {len(results):,} sampled DIDs", file=sys.stderr)
    return results
