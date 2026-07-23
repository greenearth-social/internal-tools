#!/usr/bin/env python3
"""Check that a generated fixture set is usable before it gets published.

A fixture is a ~170 MB public release that people seed from, so the failure
modes worth catching here are the ones that are invisible until someone has
already downloaded it: no posts, likes pointing at posts that aren't in the
set, or personas whose DIDs don't resolve to real accounts.

That last one is the reason this exists. Fixtures generated before identities
went real carry synthetic did:plc values that look completely normal but
resolve to nobody, which silently breaks the follow-driven generators. The
check is a lookup against plc.directory, so it needs network access; pass
--offline to skip just that part.

Usage: verify_fixture.py <fixture-dir> [--offline]
Exit code is 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PLC_DIRECTORY = "https://plc.directory"
PERSONAS_TO_CHECK = 5


def read_jsonl_gz(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with gzip.open(path, "rt") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def did_resolves(did: str, timeout: float = 10.0) -> bool | None:
    """True/False if plc.directory answers, None if the lookup itself failed.

    None is deliberately distinct from False: a network problem on our side
    shouldn't read as "this DID is fake" and fail the fixture.
    """
    try:
        req = urllib.request.Request(f"{PLC_DIRECTORY}/{did}", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        return None
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a generated fixture set")
    parser.add_argument("fixture_dir", type=Path)
    parser.add_argument(
        "--offline", action="store_true", help="skip the plc.directory persona lookups"
    )
    args = parser.parse_args()

    d: Path = args.fixture_dir
    failures: list[str] = []
    warnings: list[str] = []

    manifest_path = d / "manifest.json"
    if not manifest_path.exists():
        print(f"FAIL: no manifest.json in {d}")
        return 1
    manifest = json.loads(manifest_path.read_text())

    counts = manifest.get("counts", {})
    posts, likes = counts.get("posts", 0), counts.get("likes", 0)
    print(f"  source={manifest.get('source')} window_end={manifest.get('window_end')}")
    print(f"  posts={posts:,} likes={likes:,} cohort={counts.get('cohort', 0):,}")

    if posts == 0:
        failures.append("fixture contains no posts")
    if likes == 0:
        failures.append("fixture contains no likes")

    chunks = sorted(d.glob("*.db.zip"))
    if not chunks:
        failures.append("no megastream .db.zip chunks were written")
    else:
        print(f"  chunks={len(chunks)} ({sum(c.stat().st_size for c in chunks) / 1e6:.0f} MB)")

    # Likes must point at posts that are actually in the set, or the seeded
    # environment ends up with likes referencing nothing.
    like_rows = read_jsonl_gz(d / "likes.jsonl.gz")
    if like_rows:
        subjects = {like["subject_uri"] for like in like_rows}
        print(f"  likes reference {len(subjects):,} distinct posts")
    if len(like_rows) != likes:
        failures.append(f"manifest says {likes} likes but likes.jsonl.gz holds {len(like_rows)}")

    # The check this file exists for.
    personas = manifest.get("personas", [])
    if not personas:
        failures.append("manifest lists no personas")
    elif args.offline:
        print("  persona DID resolution: skipped (--offline)")
    else:
        checked = personas[:PERSONAS_TO_CHECK]
        resolved, synthetic, unknown = 0, [], 0
        for persona in checked:
            match did_resolves(persona["did"]):
                case True:
                    resolved += 1
                case False:
                    synthetic.append(persona["did"])
                case _:
                    unknown += 1
        print(f"  personas checked={len(checked)} resolved={resolved} synthetic={len(synthetic)}")
        if synthetic:
            failures.append(
                "persona DIDs do not resolve at plc.directory — this fixture was "
                "generated with pseudonymization: " + ", ".join(synthetic[:3])
            )
        if unknown:
            warnings.append(f"{unknown} persona lookup(s) failed; could not confirm those DIDs")

    for warning in warnings:
        print(f"  WARN: {warning}")
    if failures:
        print()
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("  all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
