#!/usr/bin/env python3
"""Validate an optional graph capture against its unchanged base fixture.

The graph uses portable DIDs and reuses the base fixture's likes. A capture
can contain millions of neighbors and edges, so validation keeps identity and
record-key indexes in temporary SQLite storage rather than Python sets.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import sqlite3
import sys
import tempfile
import zlib
from contextlib import closing
from pathlib import Path


def base_fingerprints(fixture_dir: Path) -> dict[str, str]:
    """Hash base artifacts by relative filename, following staging symlinks."""
    chunks = sorted(fixture_dir.glob("*.db.zip"))
    if not chunks:
        raise ValueError("base fixture contains no post chunks")
    paths = [fixture_dir / "manifest.json", fixture_dir / "likes.jsonl.gz", *chunks]
    fingerprints = {}
    for path in paths:
        with path.open("rb") as stream:
            fingerprints[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return fingerprints


def _rows(path: Path):
    with gzip.open(path, "rt") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{path.name}:{number}: invalid JSON") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path.name}:{number}: expected a JSON object")
            yield row


def _timestamp(value, field: str, *, nullable: bool = False) -> dt.datetime | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field}: expected a timestamp")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field}: timestamp must include a timezone")
    return parsed


def _did(value) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("did:")
        or any(c.isspace() for c in value)
    ):
        raise ValueError(f"invalid actor DID: {value!r}")
    return value


def _uri_author(value, collection: str) -> str:
    parts = value.split("/") if isinstance(value, str) else []
    if len(parts) != 5 or parts[:2] != ["at:", ""] or parts[3] != collection or not parts[4]:
        raise ValueError(f"invalid {collection} URI: {value!r}")
    return _did(parts[2])


def _validate(fixture_dir: Path, db: sqlite3.Connection) -> list[str]:
    graph = fixture_dir / "graph"
    manifest = json.loads((graph / "manifest.json").read_text())
    if not isinstance(manifest, dict):
        raise ValueError("graph manifest must be a JSON object")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise ValueError("unsupported graph schema_version")
    if manifest.get("source") != "postgres":
        raise ValueError("graph source must be postgres")
    if manifest.get("selection") != {
        "roots": "likers-personas-dev-users",
        "direction": "outgoing",
        "hops": 1,
        "active_only": True,
    }:
        raise ValueError(
            "graph selection must be one hop of active outgoing liker/persona/dev follows"
        )
    if manifest.get("likes_file") != "../likes.jsonl.gz":
        raise ValueError("graph likes_file must reference ../likes.jsonl.gz")
    started = _timestamp(manifest.get("capture_started_at"), "capture_started_at")
    finished = _timestamp(manifest.get("capture_finished_at"), "capture_finished_at")
    assert started is not None and finished is not None
    if finished < started:
        raise ValueError("graph capture_finished_at precedes capture_started_at")
    if manifest.get("base_files") != base_fingerprints(fixture_dir):
        raise ValueError("graph base_files fingerprints do not match the base fixture")
    base_manifest = json.loads((fixture_dir / "manifest.json").read_text())
    if not isinstance(base_manifest, dict):
        raise ValueError("base manifest must be a JSON object")
    # Post authors stay in the actor table for like endpoints. Only likers
    # and explicitly named fixture users contribute outgoing follow roots.
    expected_roots = {
        _did(user["did"])
        for key in ("personas", "dev_users")
        for user in (base_manifest.get(key) or [])
    }
    counts = manifest.get("counts")
    names = ("roots", "actors", "follows", "likes", "missing_roots")
    if not isinstance(counts, dict) or any(
        type(counts.get(name)) is not int or counts[name] < 0 for name in names
    ):
        raise ValueError("graph counts must contain nonnegative integer counts")

    db.execute("CREATE TABLE actors (did TEXT PRIMARY KEY, is_root INTEGER, in_graph INTEGER)")
    db.execute(
        "CREATE TABLE follows (follower_did TEXT, rkey TEXT, PRIMARY KEY (follower_did, rkey))"
    )
    actor_fields = {"did", "status", "status_at", "first_seen_at", "is_root", "in_graph"}
    for row in _rows(graph / "actors.jsonl.gz"):
        if set(row) != actor_fields:
            raise ValueError("actor fields do not match the graph schema")
        did = _did(row["did"])
        if type(row["is_root"]) is not bool or type(row["in_graph"]) is not bool:
            raise ValueError(f"actor {did}: is_root and in_graph must be booleans")
        if not row["in_graph"]:
            if any(row[key] is not None for key in ("status", "status_at", "first_seen_at")):
                raise ValueError(f"actor {did}: missing actors must have null metadata")
        else:
            if not isinstance(row["status"], str) or not row["status"]:
                raise ValueError(f"actor {did}: status must be a nonempty string")
            _timestamp(row["status_at"], "actor status_at", nullable=True)
            _timestamp(row["first_seen_at"], "actor first_seen_at")
        try:
            db.execute(
                "INSERT INTO actors VALUES (?, ?, ?)", (did, row["is_root"], row["in_graph"])
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"duplicate actor: {did}") from exc

    actual: dict[str, int] = dict(
        zip(
            ("actors", "roots", "missing_roots"),
            db.execute(
                "SELECT COUNT(*), COALESCE(SUM(is_root), 0), "
                "COALESCE(SUM(is_root=1 AND in_graph=0), 0) FROM actors"
            ).fetchone(),
            strict=True,
        )
    )
    actual["follows"] = actual["likes"] = 0
    follow_fields = {
        "follower_did",
        "subject_did",
        "rkey",
        "created_at",
        "indexed_at",
        "deleted_at",
        "seq",
    }
    for row in _rows(graph / "follows.jsonl.gz"):
        if set(row) != follow_fields:
            raise ValueError("follow fields do not match the graph schema")
        follower, subject = _did(row["follower_did"]), _did(row["subject_did"])
        if row["deleted_at"] is not None:
            raise ValueError("graph contains a deleted follow")
        if not isinstance(row["rkey"], str) or not row["rkey"]:
            raise ValueError("follow rkey must be a nonempty string")
        if type(row["seq"]) is not int or row["seq"] < 0:
            raise ValueError("follow seq must be a nonnegative integer")
        _timestamp(row["created_at"], "follow created_at", nullable=True)
        _timestamp(row["indexed_at"], "follow indexed_at")
        owner = db.execute(
            "SELECT is_root, in_graph FROM actors WHERE did=?", (follower,)
        ).fetchone()
        target = db.execute("SELECT in_graph FROM actors WHERE did=?", (subject,)).fetchone()
        if owner != (1, 1):
            raise ValueError(f"follow owner must be a present root actor: {follower}")
        if target != (1,):
            raise ValueError(f"follow subject is not a present actor: {subject}")
        try:
            db.execute("INSERT INTO follows VALUES (?, ?)", (follower, row["rkey"]))
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"duplicate follow key: {follower}/{row['rkey']}") from exc
        actual["follows"] += 1

    for row in _rows(fixture_dir / "likes.jsonl.gz"):
        author = _did(row["author_did"])
        expected_roots.add(author)
        if _uri_author(row["at_uri"], "app.bsky.feed.like") != author:
            raise ValueError("like URI does not match author_did")
        subject = _uri_author(row["subject_uri"], "app.bsky.feed.post")
        actor = db.execute("SELECT is_root FROM actors WHERE did=?", (author,)).fetchone()
        if actor != (1,):
            raise ValueError(f"like author is not a fixture root actor: {author}")
        if db.execute("SELECT 1 FROM actors WHERE did=?", (subject,)).fetchone() is None:
            raise ValueError(f"like subject author is not a fixture actor: {subject}")
        actual["likes"] += 1
    actual_roots = {row[0] for row in db.execute("SELECT did FROM actors WHERE is_root=1")}
    if actual_roots != expected_roots:
        missing = len(expected_roots - actual_roots)
        unexpected = len(actual_roots - expected_roots)
        raise ValueError(
            f"graph root membership differs: {missing} missing, {unexpected} unexpected"
        )
    return [
        f"graph {name} count: manifest says {counts[name]}, files contain {actual[name]}"
        for name in names
        if counts[name] != actual[name]
    ]


def validate_graph_fixture(fixture_dir: Path) -> list[str]:
    """Return graph validation errors; fixtures without graph/ remain valid."""
    graph = fixture_dir / "graph"
    if not graph.exists() and not graph.is_symlink():
        return []
    try:
        with tempfile.TemporaryDirectory(prefix="graph-verify-") as tmp:
            with closing(sqlite3.connect(Path(tmp) / "graph.sqlite")) as db:
                return _validate(fixture_dir, db)
    except (OSError, EOFError, ValueError, TypeError, KeyError, sqlite3.Error, zlib.error) as exc:
        return [f"graph fixture: {exc}"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture_dir", type=Path)
    args = parser.parse_args()
    failures = validate_graph_fixture(args.fixture_dir)
    for failure in failures:
        print(f"FAIL: {failure}")
    return int(bool(failures))


if __name__ == "__main__":
    sys.exit(main())
