#!/usr/bin/env python3
"""Capture one hop of production follows around an existing prod-es fixture.

The ES fixture remains the source of posts, likes, and actor identities. Only
likers, personas, and dev accounts are roots whose outgoing follows are read;
post authors are retained as actors without expanding their follows. Graph
rows use DIDs instead of production integer IDs so a local loader can allocate
its own IDs. The companion graph directory is replaced only after extraction
and verification succeed; the base fixture is never modified here.

Stdlib only. Production access currently uses explicit SSH/sudo peer auth;
the extraction logic only depends on a source exposing query(sql) -> JSON rows.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from collections.abc import Generator
from contextlib import closing
from pathlib import Path
from typing import Protocol

from graph_fixture import base_fingerprints, validate_graph_fixture
from graph_source import GraphSourceError, SshGraphSource


class GraphSource(Protocol):
    def query(self, sql: str) -> Generator[dict]: ...


def timestamp() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def sql_text(value: str) -> str:
    """Encode data, including newlines/backslashes, without psql metacommands.

    A regular SQL quote escape alone is insufficient for scripts fed to psql:
    a newline could introduce a backslash command. Hex contains neither SQL
    delimiters nor psql syntax, regardless of the source string's contents.
    """
    return f"convert_from(decode('{value.encode().hex()}', 'hex'), 'UTF8')"


def split_uri(uri: str, collection: str) -> tuple[str, str]:
    parts = uri.split("/")
    if (
        len(parts) != 5
        or parts[:2] != ["at:", ""]
        or not parts[2].startswith("did:")
        or parts[3] != collection
        or not parts[4]
    ):
        raise ValueError(f"invalid {collection} URI: {uri!r}")
    return parts[2], parts[4]


def fixture_users(fixture_dir: Path) -> tuple[set[str], set[str], int]:
    """Return all actor DIDs, follow-root DIDs, and the saved like count.

    Post authors keep the like endpoints complete. Expanding only likers and
    manifest users keeps the follow graph focused on the fixture's viewers.
    """
    manifest = json.loads((fixture_dir / "manifest.json").read_text())
    if manifest["source"] != "prod-es":
        raise ValueError("graph extraction requires a prod-es fixture; synthetic mode is deferred")
    if manifest.get("rebased"):
        raise ValueError("extract against the original fixture, not a rebased seed directory")
    roots = {user["did"] for key in ("personas", "dev_users") for user in manifest.get(key, [])}
    fixture_dids = set(roots)
    posts: set[str] = set()
    chunks = sorted(fixture_dir.glob("*.db.zip"))
    if not chunks:
        raise ValueError("fixture contains no post chunks")
    with tempfile.TemporaryDirectory(prefix="graph-posts-") as tmp:
        extracted = Path(tmp) / "chunk.db"
        for chunk in chunks:
            source = chunk
            if zipfile.is_zipfile(chunk):
                with zipfile.ZipFile(chunk) as archive:
                    members = [name for name in archive.namelist() if name.endswith(".db")]
                    if len(members) != 1:
                        raise ValueError(f"expected one .db in {chunk.name}")
                    with archive.open(members[0]) as src, extracted.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                source = extracted
            with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as db:
                for uri, did in db.execute("SELECT at_uri, did FROM enriched_posts"):
                    author, _ = split_uri(uri, "app.bsky.feed.post")
                    if did != author or uri in posts:
                        raise ValueError(f"duplicate post or mismatched author: {uri}")
                    posts.add(uri)
                    fixture_dids.add(did)
    likes: set[str] = set()
    with gzip.open(fixture_dir / "likes.jsonl.gz", "rt") as stream:
        for line in stream:
            if not line.strip():
                continue
            like = json.loads(line)
            liker, _ = split_uri(like["at_uri"], "app.bsky.feed.like")
            if liker != like["author_did"] or like["at_uri"] in likes:
                raise ValueError(f"duplicate like or mismatched author: {like['at_uri']}")
            if like["subject_uri"] not in posts:
                raise ValueError(
                    f"like references a post outside the fixture: {like['subject_uri']}"
                )
            likes.add(like["at_uri"])
            roots.add(liker)
    if len(posts) != manifest["counts"]["posts"] or len(likes) != manifest["counts"]["likes"]:
        raise ValueError("fixture post/like counts do not match its manifest")
    fixture_dids.update(roots)
    if not roots or any(
        not isinstance(did, str) or not did.startswith("did:") for did in fixture_dids
    ):
        raise ValueError("fixture has no users or contains an invalid DID")
    return fixture_dids, roots, len(likes)


def actor_query(dids: list[str]) -> str:
    return f"""
        SELECT row_to_json(a) FROM (
            SELECT id, did, status, status_at, first_seen_at FROM graph.actor
            WHERE did IN (SELECT jsonb_array_elements_text({sql_text(json.dumps(dids))}::jsonb))
            ORDER BY id
        ) a;
    """


def follow_query(ids: list[int], after: tuple[int, str], page_size: int) -> str:
    # The PK begins with follower_id. Keyset paging keeps each result bounded
    # without OFFSET scans, including for accounts with very many follows.
    return f"""
        SELECT json_build_object(
            'follower_id', f.follower_id,
            'follow', json_build_object(
                'subject_did', s.did, 'rkey', f.rkey, 'created_at', f.created_at,
                'indexed_at', f.indexed_at, 'deleted_at', f.deleted_at, 'seq', f.seq),
            'subject', json_build_object(
                'did', s.did, 'status', s.status, 'status_at', s.status_at,
                'first_seen_at', s.first_seen_at))
        FROM graph.follow f JOIN graph.actor s ON s.id = f.subject_id
        WHERE f.follower_id = ANY(ARRAY[{",".join(str(int(i)) for i in ids)}]::bigint[])
          AND (f.follower_id, f.rkey) > ({int(after[0])}, {sql_text(after[1])})
          AND f.deleted_at IS NULL
        ORDER BY f.follower_id, f.rkey
        LIMIT {int(page_size)};
    """


def save_actor(db: sqlite3.Connection, row: dict, roots: set[str], *, in_graph: bool) -> None:
    actor = {key: row.get(key) for key in ("did", "status", "status_at", "first_seen_at")}
    actor.update(is_root=actor["did"] in roots, in_graph=in_graph)
    # Deduplicate target accounts on disk: a one-hop capture can still have
    # millions of endpoints. A later observed actor replaces a missing user.
    db.execute(
        "INSERT INTO actors (did, in_graph, is_root, data) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(did) DO UPDATE SET in_graph=excluded.in_graph, data=excluded.data "
        "WHERE actors.in_graph=0 AND excluded.in_graph=1",
        (actor["did"], in_graph, actor["is_root"], json.dumps(actor)),
    )


def extract_graph(fixture_dir: Path, source: GraphSource, batch_size: int, page_size: int) -> dict:
    if batch_size <= 0 or page_size <= 0:
        raise ValueError("batch_size and page_size must be positive")
    fingerprints = base_fingerprints(fixture_dir)
    fixture_dids, roots, like_count = fixture_users(fixture_dir)
    print(
        f"Graph follow roots: {len(roots):,} likers/personas/dev accounts; "
        f"{len(fixture_dids):,} fixture actors; reusing {like_count:,} saved likes",
        flush=True,
    )
    started = timestamp()
    # Stage on the same filesystem so the final rename cannot cross devices.
    with tempfile.TemporaryDirectory(prefix=".graph-extract-", dir=fixture_dir) as tmp:
        staging = Path(tmp)
        graph_dir = staging / "graph"
        graph_dir.mkdir()
        with closing(sqlite3.connect(staging / "actors.sqlite")) as db:
            db.execute(
                "CREATE TABLE actors (did TEXT PRIMARY KEY, in_graph INTEGER, "
                "is_root INTEGER, data TEXT)"
            )
            root_ids: dict[int, str] = {}
            ordered_dids = sorted(fixture_dids)
            for offset in range(0, len(ordered_dids), batch_size):
                batch = ordered_dids[offset : offset + batch_size]
                found = set()
                with closing(source.query(actor_query(batch))) as rows:
                    for row in rows:
                        if row["did"] not in batch or row["did"] in found:
                            raise ValueError(
                                "unexpected or duplicate fixture actor in graph response"
                            )
                        found.add(row["did"])
                        if row["did"] in roots:
                            root_ids[int(row["id"])] = row["did"]
                        save_actor(db, row, roots, in_graph=True)
                for did in set(batch) - found:
                    save_actor(db, {"did": did}, roots, in_graph=False)
                db.commit()
                checked = min(offset + batch_size, len(fixture_dids))
                if (
                    offset == 0
                    or (offset // batch_size + 1) % 25 == 0
                    or checked == len(fixture_dids)
                ):
                    print(
                        f"  looked up {checked:,}/{len(fixture_dids):,} fixture actors",
                        flush=True,
                    )

            follow_count = 0
            ordered_ids = sorted(root_ids)
            with gzip.open(graph_dir / "follows.jsonl.gz", "wt") as follows:
                for offset in range(0, len(ordered_ids), batch_size):
                    ids = ordered_ids[offset : offset + batch_size]
                    after = (0, "")
                    while True:
                        page_count = 0
                        with closing(source.query(follow_query(ids, after, page_size))) as rows:
                            for row in rows:
                                follower_id = int(row["follower_id"])
                                follow = row["follow"]
                                cursor = (follower_id, follow["rkey"])
                                if (
                                    follower_id not in ids
                                    or follower_id < after[0]
                                    or cursor == after
                                ):
                                    raise ValueError(
                                        "unexpected or unordered follow in graph response"
                                    )
                                if follow["deleted_at"] is not None:
                                    raise ValueError("graph returned a deleted follow")
                                if follow["subject_did"] != row["subject"]["did"]:
                                    raise ValueError("follow subject does not match its actor")
                                follow["follower_did"] = root_ids[follower_id]
                                follows.write(json.dumps(follow) + "\n")
                                save_actor(db, row["subject"], roots, in_graph=True)
                                after = cursor
                                page_count += 1
                                if page_count > page_size:
                                    raise ValueError("graph returned more rows than the page limit")
                        follow_count += page_count
                        db.commit()
                        print(
                            f"  {follow_count:,} follows; "
                            f"root batch {offset // batch_size + 1}/"
                            f"{(len(ordered_ids) + batch_size - 1) // batch_size}",
                            flush=True,
                        )
                        if page_count < page_size:
                            break

            actor_count, missing_count = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(is_root=1 AND in_graph=0), 0) FROM actors"
            ).fetchone()
            with gzip.open(graph_dir / "actors.jsonl.gz", "wt") as actors:
                for (data,) in db.execute("SELECT data FROM actors ORDER BY did"):
                    actors.write(data + "\n")

        manifest = {
            "schema_version": 1,
            "source": "postgres",
            "selection": {
                "roots": "likers-personas-dev-users",
                "direction": "outgoing",
                "hops": 1,
                "active_only": True,
            },
            # Each short query sees committed data at its own start. Keeping a
            # transaction open over the entire capture would hold back vacuum
            # on the live graph database. This is not a historical snapshot.
            "consistency": "per-query read-committed",
            "capture_started_at": started,
            "capture_finished_at": timestamp(),
            "base_files": fingerprints,
            "likes_file": "../likes.jsonl.gz",
            "counts": {
                "roots": len(roots),
                "actors": actor_count,
                "follows": follow_count,
                "likes": like_count,
                "missing_roots": missing_count,
            },
        }
        (graph_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if base_fingerprints(fixture_dir) != fingerprints:
            raise ValueError("base fixture changed during graph extraction; discard and retry")
        # Verification expects a full fixture directory. Link the unchanged
        # base files into the staging directory without copying the archives.
        for name in fingerprints:
            (staging / name).symlink_to((fixture_dir / name).resolve())
        failures = validate_graph_fixture(staging)
        if failures:
            raise ValueError("graph fixture verification failed: " + "; ".join(failures))

        destination = fixture_dir / "graph"
        previous = staging / "previous-graph"
        if destination.exists():
            destination.rename(previous)
        try:
            graph_dir.rename(destination)
        except BaseException:
            # Restore the previous capture on cancellation as well as I/O
            # failure, before TemporaryDirectory removes the staged files.
            if previous.exists() and not destination.exists():
                previous.rename(destination)
            raise
    print(
        f"Graph fixture saved: {actor_count:,} actors, {follow_count:,} follows, "
        f"{missing_count:,} follow roots missing from production",
        flush=True,
    )
    return manifest


def positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--fixture-dir", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument("--ssh-host", required=True, help="explicit SSH/sudo peer-auth host")
    parser.add_argument("--database", default="bridgeu")
    parser.add_argument("--batch-size", type=positive_int, default=100)
    parser.add_argument("--page-size", type=positive_int, default=10_000)
    parser.add_argument("--statement-timeout-ms", type=positive_int, default=30_000)
    args = parser.parse_args()
    try:
        source = SshGraphSource(args.ssh_host, args.database, args.statement_timeout_ms)
        extract_graph(args.fixture_dir.expanduser(), source, args.batch_size, args.page_size)
    except (OSError, ValueError, KeyError, sqlite3.Error, GraphSourceError) as exc:
        sys.exit(f"FATAL: graph extraction failed: {exc}")


if __name__ == "__main__":
    main()
