#!/usr/bin/env python3
"""Add a BridgEU social-graph fixture to an existing dev fixture (issue #70).

BridgEU's Diverse Approval pipeline (bridg-eu/algo/pipeline) fits communities
on the follow graph and scores posts by which communities liked them. This
writes the graph that pipeline reads, over the fixture's own posts, so the
scores it computes at seed time point at posts that are in Elasticsearch:

- graph_actors.jsonl.gz   did, status
- graph_follows.jsonl.gz  follower_did, subject_did, rkey, created_at, indexed_at
- graph_likes.jsonl.gz    liker_did, rkey, subject_uri, created_at, indexed_at

plus a `graph` block in manifest.json. seed/load_graph.py loads them into the
local BridgEU graph tables.

--source prod-graph (the default)
    Reads prod BridgEU's graph database (read-only is enough) through psql,
    which must be on PATH. Takes every live like on the fixture's posts in the
    fixture window, from many more likers than the Elasticsearch cohort, so a
    post gathers likes from several communities; the likers are capped at
    --max-likers (the most active first; the cohort and dev users always stay).
    The follows are the ones among those likers and the posts' authors.

--source synthetic
    No credentials: plants --communities communities over the fixture's post
    authors, the cohort and --synthetic-likers invented likers, draws follows
    mostly within a community, and likes that favour posts by the liker's own
    community. The fit at seed time should roughly recover the planted
    communities, so the pipeline is exercised for real, on made-up structure.

Usage:
    python3 generate_graph.py [fixture-dir] --graph-url postgres://...
    python3 generate_graph.py [fixture-dir] --source synthetic [--seed 42]

Timestamps stay in the fixture's original window; the seed rebase shifts them
with everything else. Stdlib only (plus psql for prod-graph).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from generate_da_scores import read_fixture_posts

SOURCES = ("prod-graph", "synthetic")
GRAPH_URL_ENV = "GE_GRAPH_DATABASE_URL"
ACTORS_FILE = "graph_actors.jsonl.gz"
FOLLOWS_FILE = "graph_follows.jsonl.gz"
LIKES_FILE = "graph_likes.jsonl.gz"
POST_COLLECTION = "app.bsky.feed.post"
TID_ALPHABET = "234567abcdefghijklmnopqrstuvwxyz"

DEFAULT_SEED = 42
DEFAULT_MAX_LIKERS = 20_000
DEFAULT_COMMUNITIES = 8
DEFAULT_SYNTHETIC_LIKERS = 3_000


def parse_iso(ts: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def iso_z(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def split_uri(uri: str) -> tuple[str, str, str]:
    """at://did/collection/rkey -> (did, collection, rkey)."""
    did, collection, rkey = uri.removeprefix("at://").split("/", 2)
    return did, collection, rkey


def read_fixture_likes(fixture_dir: Path) -> list[dict]:
    path = fixture_dir / "likes.jsonl.gz"
    if not path.exists():
        return []
    with gzip.open(path, "rt") as f:
        return [json.loads(line) for line in f if line.strip()]


def fixed_likers(manifest: dict, likes: list[dict]) -> set[str]:
    """The likers a capped sample must keep: the cohort, personas and dev users."""
    keep = {like["author_did"] for like in likes}
    keep |= {p["did"] for p in manifest.get("personas") or []}
    keep |= {u["did"] for u in manifest.get("dev_users") or [] if u.get("did")}
    return keep


# --- prod graph ---------------------------------------------------------------


def prod_graph_sql(window_start: str, window_end: str, max_likers: int, out: Path) -> str:
    """The psql script: load the fixture's posts and fixed likers, then export the graph.

    Everything parameterized is formatted here (timestamps we produced, an int),
    so the script needs no psql variables, which \\copy doesn't interpolate.
    """
    start, end = parse_iso(window_start), parse_iso(window_end)
    return f"""\\set ON_ERROR_STOP on
-- Writes only this session's temp tables (TEMP is granted to PUBLIC by default), so a
-- read-only role is enough. Not BEGIN READ ONLY: that refuses CREATE TEMP TABLE too.
BEGIN;
CREATE TEMP TABLE fx_post (author_did text, rkey text, uri text);
\\copy fx_post FROM '{out}/posts.csv' WITH (FORMAT csv)
CREATE TEMP TABLE fx_keep (did text);
\\copy fx_keep FROM '{out}/keep.csv' WITH (FORMAT csv)

CREATE TEMP TABLE fx_post_id AS
    SELECT s.id AS author_id, p.rkey, p.uri
    FROM fx_post p JOIN graph.actor s ON s.did = p.author_did;
ANALYZE fx_post_id;

-- The window's likes through the BRIN index on indexed_at, joined to the fixture's posts.
CREATE TEMP TABLE fx_like AS
    SELECT l.liker_id, l.rkey, p.uri AS subject_uri, l.created_at, l.indexed_at
    FROM graph.likes l
    JOIN graph.collection c ON c.id = l.subject_collection_id AND c.nsid = '{POST_COLLECTION}'
    JOIN fx_post_id p ON p.author_id = l.subject_actor_id AND p.rkey = l.subject_rkey
    WHERE l.deleted_at IS NULL
      AND l.indexed_at >= '{iso_z(start)}' AND l.indexed_at < '{iso_z(end)}';

CREATE TEMP TABLE fx_node AS
    SELECT a.id, a.did, a.status FROM graph.actor a WHERE a.id IN (
        (SELECT liker_id FROM fx_like GROUP BY liker_id ORDER BY count(*) DESC, liker_id
         LIMIT {int(max_likers)})
        UNION SELECT a2.id FROM graph.actor a2 JOIN fx_keep k ON k.did = a2.did
        UNION SELECT author_id FROM fx_post_id);
CREATE UNIQUE INDEX ON fx_node (id);
ANALYZE fx_node;

-- psql's \\copy takes its query on one line, so the exports are views.
CREATE TEMP VIEW out_actor AS SELECT did, status FROM fx_node ORDER BY did;
CREATE TEMP VIEW out_like AS
    SELECT n.did, l.rkey, l.subject_uri, l.created_at, l.indexed_at
    FROM fx_like l JOIN fx_node n ON n.id = l.liker_id
    ORDER BY l.indexed_at, n.did, l.rkey;
-- One index lookup per node on the (follower_id, rkey) primary key, never a scan of graph.follow.
CREATE TEMP VIEW out_follow AS
    SELECT n.did AS follower_did, s.did AS subject_did, f.rkey, f.created_at, f.indexed_at
    FROM fx_node n
    CROSS JOIN LATERAL (
        SELECT * FROM graph.follow f WHERE f.follower_id = n.id AND f.deleted_at IS NULL) f
    JOIN fx_node s ON s.id = f.subject_id
    ORDER BY n.did, f.rkey;
\\copy (SELECT * FROM out_actor) TO '{out}/actors.csv' WITH (FORMAT csv)
\\copy (SELECT * FROM out_like) TO '{out}/likes.csv' WITH (FORMAT csv)
\\copy (SELECT * FROM out_follow) TO '{out}/follows.csv' WITH (FORMAT csv)
COMMIT;
"""


def pg_time(value: str) -> str | None:
    """A psql CSV timestamptz ('2026-11-20 10:00:00.123+00') as ISO Z, or None."""
    if not value:
        return None
    if value.endswith(("+00", "-00")):
        value += ":00"
    return iso_z(dt.datetime.fromisoformat(value))


def read_csv(path: Path) -> Iterable[list[str]]:
    with path.open(newline="") as f:
        yield from csv.reader(f)


def prod_graph(
    manifest: dict,
    posts: dict[str, str],
    likes: list[dict],
    graph_url: str,
    max_likers: int,
) -> tuple[list[dict], list[dict], list[dict]]:
    if not shutil.which("psql"):
        raise ValueError("--source prod-graph needs psql on PATH")
    with tempfile.TemporaryDirectory(prefix="ge-graph-") as tmp:
        out = Path(tmp)
        with (out / "posts.csv").open("w", newline="") as f:
            writer = csv.writer(f)
            for uri in posts:
                did, _, rkey = split_uri(uri)
                writer.writerow([did, rkey, uri])
        with (out / "keep.csv").open("w", newline="") as f:
            csv.writer(f).writerows([did] for did in sorted(fixed_likers(manifest, likes)))
        script = out / "export.sql"
        script.write_text(
            prod_graph_sql(manifest["window_start"], manifest["window_end"], max_likers, out)
        )
        print("  querying the prod graph database (read-only)...")
        result = subprocess.run(
            ["psql", "-X", "-q", "--dbname", graph_url, "--file", str(script)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(f"psql failed:\n{result.stderr.strip()}")
        actors = [{"did": did, "status": status} for did, status in read_csv(out / "actors.csv")]
        graph_likes = [
            {
                "liker_did": did,
                "rkey": rkey,
                "subject_uri": uri,
                "created_at": pg_time(created),
                "indexed_at": pg_time(indexed),
            }
            for did, rkey, uri, created, indexed in read_csv(out / "likes.csv")
        ]
        follows = [
            {
                "follower_did": follower,
                "subject_did": subject,
                "rkey": rkey,
                "created_at": pg_time(created),
                "indexed_at": pg_time(indexed),
            }
            for follower, subject, rkey, created, indexed in read_csv(out / "follows.csv")
        ]
    return actors, follows, graph_likes


# --- synthetic graph ------------------------------------------------------------


def synth_did(rng: random.Random, i: int) -> str:
    # 24 base32 characters, like a did:plc, with a prefix that marks it as made up.
    prefix = f"gegraph{i:05d}"
    return f"did:plc:{prefix}{''.join(rng.choices(TID_ALPHABET, k=24 - len(prefix)))}"


def synth_tid(rng: random.Random) -> str:
    return "".join(rng.choices(TID_ALPHABET, k=13))


def synthetic_graph(
    manifest: dict,
    posts: dict[str, str],
    likes: list[dict],
    *,
    seed: int,
    communities: int,
    synthetic_likers: int,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Planted communities: follows and likes mostly stay inside a community."""
    rng = random.Random(seed)
    window_start = parse_iso(manifest["window_start"])
    window_end = parse_iso(manifest["window_end"])

    authors = sorted({split_uri(uri)[0] for uri in posts})
    real_likers = sorted(fixed_likers(manifest, likes) - set(authors))
    invented = [synth_did(rng, i) for i in range(synthetic_likers)]
    likers = real_likers + invented
    nodes = authors + likers
    community = {did: rng.randrange(communities) for did in nodes}
    members: dict[int, list[str]] = defaultdict(list)
    for did in nodes:
        members[community[did]].append(did)
    posts_by_community: dict[int, list[str]] = defaultdict(list)
    for uri in sorted(posts):
        posts_by_community[community[split_uri(uri)[0]]].append(uri)
    all_posts = sorted(posts)

    def stamp(earliest: dt.datetime) -> str:
        t = earliest + dt.timedelta(minutes=rng.expovariate(1 / 90.0))
        return iso_z(min(max(t, window_start), window_end - dt.timedelta(seconds=1)))

    follows = []
    for did in nodes:
        # Likers follow more accounts than the authors do; 90% stay in the community.
        degree = rng.randint(10, 25) if did in likers else rng.randint(2, 8)
        targets = set()
        for _ in range(degree):
            pool = members[community[did]] if rng.random() < 0.9 else nodes
            target = rng.choice(pool)
            if target != did:
                targets.add(target)
        for target in sorted(targets):
            created = stamp(window_start)
            follows.append(
                {
                    "follower_did": did,
                    "subject_did": target,
                    "rkey": synth_tid(rng),
                    "created_at": created,
                    "indexed_at": created,
                }
            )

    graph_likes = [
        {
            "liker_did": like["author_did"],
            "rkey": split_uri(like["at_uri"])[2],
            "subject_uri": like["subject_uri"],
            "created_at": like.get("created_at"),
            "indexed_at": like.get("indexed_at") or like.get("created_at"),
        }
        for like in likes
        if like["subject_uri"] in posts
    ]
    for did in invented:
        own = posts_by_community[community[did]] or all_posts
        liked: set[str] = set()
        for _ in range(rng.randint(5, 40)):
            liked.add(rng.choice(own if rng.random() < 0.8 else all_posts))
        for uri in sorted(liked):
            at = stamp(parse_iso(posts[uri]))
            graph_likes.append(
                {
                    "liker_did": did,
                    "rkey": synth_tid(rng),
                    "subject_uri": uri,
                    "created_at": at,
                    "indexed_at": at,
                }
            )

    actors = [{"did": did, "status": "active"} for did in sorted(nodes)]
    return actors, follows, graph_likes


# --- output -------------------------------------------------------------------


def write_jsonl_gz(path: Path, rows: list[dict]) -> None:
    # mtime=0 keeps a regenerated file byte-identical when its rows are.
    with (
        path.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed,
        io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as out,
    ):
        for row in rows:
            out.write(json.dumps(row) + "\n")


def generate(
    fixture_dir: Path,
    *,
    source: str,
    graph_url: str | None = None,
    seed: int = DEFAULT_SEED,
    max_likers: int = DEFAULT_MAX_LIKERS,
    communities: int = DEFAULT_COMMUNITIES,
    synthetic_likers: int = DEFAULT_SYNTHETIC_LIKERS,
) -> dict:
    """Write the graph files and record them in the fixture manifest."""
    if source not in SOURCES:
        raise ValueError(f"unknown graph source {source!r}")
    if source == "prod-graph" and not graph_url:
        raise ValueError(
            f"--source prod-graph needs --graph-url or {GRAPH_URL_ENV}"
            " (no access? use --source synthetic)"
        )
    manifest_path = fixture_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    posts = read_fixture_posts(fixture_dir)
    likes = read_fixture_likes(fixture_dir)

    if source == "prod-graph":
        assert graph_url
        actors, follows, graph_likes = prod_graph(manifest, posts, likes, graph_url, max_likers)
        params: dict = {"max_likers": max_likers}
    else:
        actors, follows, graph_likes = synthetic_graph(
            manifest,
            posts,
            likes,
            seed=seed,
            communities=communities,
            synthetic_likers=synthetic_likers,
        )
        params = {"seed": seed, "communities": communities, "synthetic_likers": synthetic_likers}

    check_graph(actors, follows, graph_likes, posts)
    metadata = {
        "source": source,
        **params,
        "files": {"actors": ACTORS_FILE, "follows": FOLLOWS_FILE, "likes": LIKES_FILE},
        "counts": {"actors": len(actors), "follows": len(follows), "likes": len(graph_likes)},
    }
    manifest["graph"] = metadata
    with tempfile.TemporaryDirectory(dir=fixture_dir, prefix=".graph-") as tmp:
        staged = Path(tmp)
        write_jsonl_gz(staged / ACTORS_FILE, actors)
        write_jsonl_gz(staged / FOLLOWS_FILE, follows)
        write_jsonl_gz(staged / LIKES_FILE, graph_likes)
        (staged / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        for name in (ACTORS_FILE, FOLLOWS_FILE, LIKES_FILE, "manifest.json"):
            (staged / name).replace(fixture_dir / name)

    counts = metadata["counts"]
    print(
        f"  wrote the {source} graph: {counts['actors']:,} actors,"
        f" {counts['follows']:,} follows, {counts['likes']:,} likes"
    )
    return metadata


def check_graph(
    actors: list[dict], follows: list[dict], likes: list[dict], posts: dict[str, str]
) -> None:
    """The invariants load_graph and the pipeline rely on."""
    dids = {a["did"] for a in actors}
    if len(dids) != len(actors):
        raise ValueError("duplicate actor DIDs in the graph")
    for f in follows:
        if f["follower_did"] not in dids or f["subject_did"] not in dids:
            raise ValueError(
                f"follow {f['follower_did']}/{f['rkey']} has an endpoint not in actors"
            )
    if len({(f["follower_did"], f["rkey"]) for f in follows}) != len(follows):
        raise ValueError("duplicate follow (follower_did, rkey)")
    for like in likes:
        if like["liker_did"] not in dids:
            raise ValueError(f"like {like['liker_did']}/{like['rkey']} has a liker not in actors")
        if like["subject_uri"] not in posts:
            raise ValueError(f"like subject {like['subject_uri']} is not a fixture post")
    if len({(lk["liker_did"], lk["rkey"]) for lk in likes}) != len(likes):
        raise ValueError("duplicate like (liker_did, rkey)")
    if not follows:
        raise ValueError("the graph has no follows; nothing to fit communities on")


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument(
        "fixture_dir",
        nargs="?",
        type=Path,
        default=Path(__file__).parent / "data",
        help="existing fixture directory (default: fixtures/data next to this script)",
    )
    add_arguments(parser, flags=("--source", "--graph-source"), with_seed=True)
    args = parser.parse_args()
    try:
        generate(args.fixture_dir.expanduser(), **(graph_kwargs(args) or {}))
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


def add_arguments(
    parser: argparse.ArgumentParser,
    *,
    flags: tuple[str, ...] = ("--graph-source",),
    default_source: str | None = "prod-graph",
    with_seed: bool = False,
) -> None:
    """The graph options, shared with generate_sample.py (which has its own --source and --seed)."""
    parser.add_argument(
        *flags,
        dest="graph_source",
        choices=SOURCES,
        default=default_source,
        help="where the follow/like graph comes from (default: prod-graph)",
    )
    parser.add_argument(
        "--graph-url",
        default=os.environ.get(GRAPH_URL_ENV),
        help=f"prod BridgEU graph database, read-only (default: ${GRAPH_URL_ENV})",
    )
    parser.add_argument(
        "--max-likers",
        type=int,
        default=DEFAULT_MAX_LIKERS,
        help="prod-graph: keep at most this many of the most active likers",
    )
    parser.add_argument(
        "--communities",
        type=int,
        default=DEFAULT_COMMUNITIES,
        help="synthetic: planted communities",
    )
    parser.add_argument(
        "--synthetic-likers",
        type=int,
        default=DEFAULT_SYNTHETIC_LIKERS,
        help="synthetic: invented likers besides the cohort",
    )
    if with_seed:
        parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="synthetic: random seed")


def graph_kwargs(args: argparse.Namespace) -> dict | None:
    """generate()'s arguments from add_arguments' options; None when they weren't added."""
    if getattr(args, "graph_source", None) is None:
        return None
    return {
        "source": args.graph_source,
        "graph_url": args.graph_url,
        "seed": args.seed,
        "max_likers": args.max_likers,
        "communities": args.communities,
        "synthetic_likers": args.synthetic_likers,
    }


if __name__ == "__main__":
    sys.exit(main())
