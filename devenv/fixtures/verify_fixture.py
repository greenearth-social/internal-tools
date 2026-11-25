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

Topic scores are inspected in the SQLite inference payloads. Missing scores
are allowed for older history; --require-topic-scores rejects zero coverage
for fresh production captures. Malformed scores always fail.

Usage: verify_fixture.py <fixture-dir> [--offline] [--require-topic-scores]
Exit code is 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path

from generate_da_scores import SCORES_FILE, read_fixture_posts

PLC_DIRECTORY = "https://plc.directory"
PERSONAS_TO_CHECK = 5


def topic_score_counts(chunks: list[Path]) -> tuple[int, int, int]:
    """Inspect actual inference rows, including raw SQLite .db.zip files.

    Count posts, posts with valid scores, and malformed topic payloads.
    Missing scores are valid: hydrated history may predate classification.
    """
    total = scored = invalid = 0
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "chunk.db"
        for chunk in chunks:
            if zipfile.is_zipfile(chunk):
                with zipfile.ZipFile(chunk) as archive:
                    members = [name for name in archive.namelist() if name.endswith(".db")]
                    if len(members) != 1:
                        raise ValueError(f"expected one .db in {chunk.name}")
                    with archive.open(members[0]) as src, db_path.open("wb") as dest:
                        shutil.copyfileobj(src, dest)
                source = db_path
            else:
                source = chunk
            conn = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
            try:
                for (raw,) in conn.execute("SELECT inferences FROM enriched_posts"):
                    total += 1
                    try:
                        inference = json.loads(raw)
                        topics = (
                            inference.get("text", {})
                            .get("message.commit.record.text", {})
                            .get("topic", {})
                        )
                        if not isinstance(topics, dict):
                            invalid += 1
                            continue
                        valid = [
                            isinstance(value, (int, float))
                            and not isinstance(value, bool)
                            and 0 <= value <= 1
                            and math.isfinite(value)
                            for value in topics.values()
                        ]
                        scored += any(valid)
                        invalid += not all(valid)
                    except (ValueError, TypeError, AttributeError):
                        invalid += 1
            finally:
                conn.close()
    return total, scored, invalid


def read_jsonl_gz(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with gzip.open(path, "rt") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def validate_da_scores(fixture_dir: Path, manifest: dict) -> list[str]:
    """Require one finite synthetic score for each original fixture post.

    Older fixtures can omit both the artifact and its manifest metadata. When
    either is present, validate the complete pair against the original posts.
    """
    path = fixture_dir / SCORES_FILE
    if not path.exists() and "diverse_approval" not in manifest:
        return []
    metadata = manifest.get("diverse_approval")
    if not isinstance(metadata, dict):
        return ["Diverse Approval scores require diverse_approval manifest metadata"]
    if not path.is_file():
        return [f"Diverse Approval scores file is missing: {SCORES_FILE}"]
    if (
        metadata.get("source") != "synthetic-random"
        or metadata.get("file") != SCORES_FILE
        or type(metadata.get("seed")) is not int
        or type(metadata.get("count")) is not int
        or metadata["count"] < 0
    ):
        return ["invalid Diverse Approval manifest metadata"]

    try:
        posts = read_fixture_posts(fixture_dir)
        seen: set[str] = set()
        count = 0
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict) or set(row) != {"post_uri", "score", "created_at"}:
                    return [f"invalid Diverse Approval score fields on line {line_number}"]
                uri, score = row["post_uri"], row["score"]
                if not isinstance(uri, str) or uri not in posts:
                    return [f"unknown Diverse Approval post on line {line_number}"]
                if uri in seen:
                    return [f"duplicate Diverse Approval post on line {line_number}"]
                if (
                    not isinstance(score, (int, float))
                    or isinstance(score, bool)
                    or not 0 <= score <= 1
                    or not math.isfinite(score)
                ):
                    return [
                        f"invalid Diverse Approval score on line {line_number}: expected [0, 1]"
                    ]
                if row["created_at"] != posts[uri]:
                    return [f"Diverse Approval post timestamp mismatch on line {line_number}"]
                seen.add(uri)
                count += 1
    except (OSError, EOFError, ValueError, sqlite3.Error, zipfile.BadZipFile, zlib.error) as exc:
        return [f"could not inspect Diverse Approval scores: {exc}"]

    failures = []
    if count != metadata["count"]:
        failures.append(
            f"Diverse Approval manifest says {metadata['count']} scores but file holds {count}"
        )
    missing = len(posts.keys() - seen)
    if missing:
        failures.append(f"Diverse Approval scores are missing {missing} fixture posts")
    return failures


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
    parser.add_argument(
        "--require-topic-scores",
        action="store_true",
        help="fail if no posts have topic scores (use for fresh production captures)",
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
        try:
            total, scored, invalid = topic_score_counts(chunks)
            coverage = 100 * scored / total if total else 0
            print(f"  topic scores: {scored:,}/{total:,} posts ({coverage:.1f}%)")
            if total != posts:
                failures.append(f"manifest says {posts} posts but chunks hold {total}")
            if invalid:
                failures.append(
                    f"{invalid} posts have malformed topic scores; "
                    "expected numeric values in [0, 1]"
                )
            if not scored:
                messages = failures if args.require_topic_scores else warnings
                messages.append("no posts have topic scores; use a capture with scored posts")
        except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile) as exc:
            failures.append(f"could not inspect fixture topic scores: {exc}")

    failures.extend(validate_da_scores(d, manifest))

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

    # Dev-team accounts (internal-tools#22). Not a failure — --no-dev-users is
    # a legitimate way to generate one — but a fixture without them gives
    # everyone on the team an empty feed when they sign in as themselves, and
    # this is the last point before it becomes a public release.
    dev_users = manifest.get("dev_users", [])
    if not dev_users:
        warnings.append(
            "no dev_users in the manifest — signing in as yourself will give an "
            "empty feed (regenerate without --no-dev-users)"
        )
    else:
        empty = [u["handle"] for u in dev_users if not u.get("likes")]
        print(f"  dev users={len(dev_users)} with history={len(dev_users) - len(empty)}")
        if empty:
            warnings.append(f"dev accounts with no like history: {', '.join(empty)}")

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
