#!/usr/bin/env python3
"""Generate the dev-environment sample fixture set (issue api#268).

Produces, in fixtures/data/:
- mega_jetstream_YYYYMMDD_HHMMSS.db.zip  — posts in megastream SQLite format
  (chunked), ingested by the real megastream_ingest binary at seed time
- likes.jsonl.gz        — like documents (LikeDoc schema)
- like_counts.jsonl.gz  — per-post like_count to apply after ingest
- manifest.json         — window, counts, dev personas, generation metadata

Two source modes:

--source prod-es (default)
    Samples a contiguous window from a real Elasticsearch cluster (read-only
    key is enough). Requires GE_ELASTICSEARCH_URL / GE_ELASTICSEARCH_API_KEY;
    for prod, port-forward first (see api/.claude/skills/verify).

    What makes the sample good, encoded here rather than as tribal knowledge:
    - a contiguous time window, because candidate generators lean on
      recency/popularity;
    - cohort-densified likes: pick active likers in the window and take ALL
      their likes on sampled posts, so per-user history is dense enough to
      exercise user featurization (uniform sampling would give everyone a
      1–2 like history and personalization would look broken);
    - danglers hydrated: posts liked by the cohort but outside the window are
      pulled into the sample, so likes never point at missing posts;
    - named dev personas: the densest likers, recorded in the manifest so
      feed requests have stable, interesting identities;
    - only posts with MiniLM embeddings (post_similarity / two_tower need
      them), keeping the popularity head and the long tail alike.

    Liker identities are pseudonymized — see "Pseudonymization" below.

--source megastream-files
    Builds the fixture from existing megastream .db.zip files (default: the
    checked-in samples in ingex/ingest/test_data/megastream) and synthesizes
    a like graph over them with Zipf-flavored popularity. No credentials or
    network needed — the path external contributors (and CI-less smoke tests)
    use. Deterministic under --seed. Liker DIDs here are invented, so
    pseudonymization doesn't apply.

Pseudonymization (prod-es mode)
-------------------------------
Post content and post *author* DIDs are kept real — they're public, and the
fixture is only useful if the content is genuine. Liker identities are not:
cohort densification deliberately captures each cohort member's complete like
history, so a real-DID fixture would be a per-person interest profile, and
this one ships as a public download that can't honor later deletions the way
prod's tombstones do.

So every liker DID is replaced by a synthetic did:plc, consistently across
the like documents, the like at_uris (which embed the liker's DID), and the
manifest personas. The like graph — who liked what, how densely, in what
order — is preserved exactly, which is all the dev environment needs.

The mapping is deliberately irreversible: it's a SHA-256 of the DID under a
random 32-byte salt generated per run and never written anywhere. A plain
hash would be no protection at all, because did:plc identifiers are public
and enumerable from the firehose — anyone could hash the whole DID space and
look ours up. Discarding the salt is what makes that attack impossible.

The cost of that choice: you cannot map a fixture DID back to a real user,
ever, including for legitimate debugging ("whose likes are these?"). If you
need real identities — say, to investigate a specific author's like behavior
— generate a private fixture with --no-pseudonymize and DO NOT publish it.
`publish_fixture.sh` refuses to upload one, and `manifest.json` records
`pseudonymized: false` so the file itself says what it is. A durable fix for
that use case (a held mapping table, or querying prod directly) is not built;
it would need designing, with its own handling of the deletion problem.

Stdlib only, so it runs on any python3 ≥ 3.11 or a stock python container.
"""

import argparse
import base64
import datetime as dt
import gzip
import hashlib
import json
import os
import random
import secrets
import sqlite3
import ssl
import struct
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
import zlib
from collections import Counter
from pathlib import Path

EMBED_MODEL = "all-MiniLM-L12-v2"
EMBED_FIELD = "embeddings.all_MiniLM_L12_v2"
EMBED_DIMS = 384
CHUNK_ROWS = 20_000
TID_ALPHABET = "234567abcdefghijklmnopqrstuvwxyz"


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------


def parse_iso(ts: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def iso_z(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def encode_embedding(vector: list[float]) -> str:
    """float32 LE -> zlib -> RFC1924 base85; matches ingex's embeddings codec."""
    packed = struct.pack(f"<{len(vector)}f", *vector)
    return base64.b85encode(zlib.compress(packed)).decode()


def make_pseudonymizer(enabled: bool):
    """Return a stable did -> did mapping function for liker identities.

    Irreversibility is the point, so the salt is random per run and is never
    returned, logged, or persisted: did:plc identifiers are public and
    enumerable, so an unsalted hash could be reversed by hashing the whole DID
    space. Consequence to be aware of before relying on it: nobody, including
    us, can undo this mapping later. See the module docstring.

    When *enabled* is False the identity function is returned, for private
    fixtures that must keep real DIDs (never publishable).
    """
    if not enabled:
        return lambda did: did

    salt = secrets.token_bytes(32)
    cache: dict[str, str] = {}

    def pseudonymize(did: str) -> str:
        if did not in cache:
            digest = hashlib.sha256(salt + did.encode()).digest()
            # did:plc identifiers are 24 chars of lowercase base32 ([a-z2-7]).
            suffix = base64.b32encode(digest).decode().lower().replace("=", "")[:24]
            cache[did] = f"did:plc:{suffix}"
        return cache[did]

    return pseudonymize


def pseudonymize_like(like: dict, pseudonymize) -> dict:
    """Apply *pseudonymize* to a like's author identity.

    The at_uri embeds the liker's DID (at://<did>/app.bsky.feed.like/<rkey>),
    so it has to be rewritten too or the real identity leaks straight back out
    through the document id. subject_uri points at the liked post and keeps
    its real author, which is public and what makes the content genuine.
    """
    fake_did = pseudonymize(like["author_did"])
    rkey = like["at_uri"].rsplit("/", 1)[-1]
    return {
        **like,
        "author_did": fake_did,
        "at_uri": f"at://{fake_did}/app.bsky.feed.like/{rkey}",
    }


def generator_commit() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(Path(__file__).parent), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def build_raw_post(
    at_uri: str, did: str, text: str, created_at: dt.datetime, quote_uri: str | None
) -> str:
    time_us = int(created_at.timestamp() * 1_000_000)
    rkey = at_uri.rsplit("/", 1)[-1]
    raw: dict = {
        "at_uri": at_uri,
        "did": did,
        "time_us": time_us,
        "message": {
            "did": did,
            "kind": "commit",
            "time_us": time_us,
            "commit": {
                "operation": "create",
                "collection": "app.bsky.feed.post",
                "rkey": rkey,
                "record": {
                    "$type": "app.bsky.feed.post",
                    "text": text,
                    "createdAt": iso_z(created_at),
                },
            },
        },
    }
    if quote_uri:
        raw["hydrated_metadata"] = {"quote_post": {"uri": quote_uri}}
    return json.dumps(raw)


def write_megastream_chunks(out_dir: Path, posts: list[dict]) -> list[str]:
    """Write posts (sorted by created_at) as megastream SQLite .db.zip chunks.

    Each post dict needs: at_uri, did, created_at (datetime), raw_post (str),
    inferences (str). Chunk filenames carry the chunk's last-post timestamp,
    matching the spool convention megastream_ingest orders files by.
    """
    posts = sorted(posts, key=lambda p: p["created_at"])
    filenames: list[str] = []
    last_ts: dt.datetime | None = None
    for start in range(0, len(posts), CHUNK_ROWS):
        chunk = posts[start : start + CHUNK_ROWS]
        file_ts = chunk[-1]["created_at"]
        if last_ts is not None and file_ts <= last_ts:
            file_ts = last_ts + dt.timedelta(seconds=1)
        last_ts = file_ts
        name = f"mega_jetstream_{file_ts:%Y%m%d_%H%M%S}.db.zip"

        db_path = out_dir / (name.removesuffix(".zip"))
        db_path.unlink(missing_ok=True)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE enriched_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at_uri TEXT CHECK(LENGTH(at_uri) <= 300),
                    did TEXT CHECK(LENGTH(did) <= 100),
                    time_us INTEGER,
                    raw_post TEXT CHECK(json_valid(raw_post)),
                    inferences TEXT CHECK(json_valid(inferences)),
                    enriched_metadata TEXT CHECK(json_valid(enriched_metadata)),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.executemany(
                "INSERT INTO enriched_posts"
                " (at_uri, did, time_us, raw_post, inferences, enriched_metadata, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        p["at_uri"],
                        p["did"],
                        int(p["created_at"].timestamp() * 1_000_000),
                        p["raw_post"],
                        p["inferences"],
                        "{}",
                        p["created_at"].strftime("%Y-%m-%d %H:%M:%S"),
                    )
                    for p in chunk
                ],
            )
            conn.commit()
        finally:
            conn.close()

        zip_path = out_dir / name
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_path, db_path.name)
        db_path.unlink()
        filenames.append(name)
        print(f"  wrote {name} ({len(chunk)} posts)")
    return filenames


def write_jsonl_gz(path: Path, docs: list[dict]) -> None:
    with gzip.open(path, "wt") as f:
        for doc in docs:
            f.write(json.dumps(doc) + "\n")


def write_fixture_set(
    out_dir: Path,
    source: str,
    posts: list[dict],
    likes: list[dict],
    like_counts: list[dict],
    personas: list[dict],
    window_start: dt.datetime,
    window_end: dt.datetime,
    cohort_size: int,
    pseudonymized: bool,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.db.zip"):
        stale.unlink()

    files = write_megastream_chunks(out_dir, posts)
    write_jsonl_gz(out_dir / "likes.jsonl.gz", likes)
    write_jsonl_gz(out_dir / "like_counts.jsonl.gz", like_counts)

    manifest = {
        "schema_version": 1,
        "source": source,
        "generated_at": iso_z(dt.datetime.now(dt.UTC)),
        "generator_commit": generator_commit(),
        "window_start": iso_z(window_start),
        "window_end": iso_z(window_end),
        "embedding_model": EMBED_MODEL,
        "embedding_dims": EMBED_DIMS,
        # False means the fixture carries real liker DIDs and must stay local;
        # publish_fixture.sh refuses to upload it. See the module docstring.
        "pseudonymized": pseudonymized,
        "counts": {
            "posts": len(posts),
            "likes": len(likes),
            "cohort": cohort_size,
            "files": len(files),
        },
        "personas": personas,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Fixture set written to {out_dir}")
    print(f"  posts={len(posts)} likes={len(likes)} cohort={cohort_size}")
    if not pseudonymized:
        print("  liker DIDs: REAL — local use only, not publishable")
    for persona in personas:
        print(f"  persona {persona['did']} ({persona['likes']} likes)")


# --------------------------------------------------------------------------
# prod-es mode
# --------------------------------------------------------------------------


class EsClient:
    def __init__(self) -> None:
        try:
            self.url = os.environ["GE_ELASTICSEARCH_URL"].rstrip("/")
            self.key = os.environ["GE_ELASTICSEARCH_API_KEY"]
        except KeyError as e:
            sys.exit(f"FATAL: {e.args[0]} must be set for --source prod-es")
        verify = os.environ.get("GE_ELASTICSEARCH_VERIFY_SSL", "false").lower()
        self.ctx = None
        if self.url.startswith("https") and verify not in ("1", "true", "yes"):
            self.ctx = ssl._create_unverified_context()  # noqa: SLF001 - dev tooling, port-forwarded ES

    def search(self, index: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.url}/{index}/_search",
            method="POST",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"ApiKey {self.key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120, context=self.ctx) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            sys.exit(f"FATAL: ES search on {index} -> {e.code}: {e.read()[:2000]}")

    def scan(self, index: str, query: dict, source: list[str], page_size: int = 1000):
        """search_after scan sorted by (created_at, at_uri)."""
        search_after = None
        while True:
            body: dict = {
                "size": page_size,
                "query": query,
                "sort": [{"created_at": "asc"}, {"at_uri": "asc"}],
                "_source": source,
                "track_total_hits": False,
            }
            if search_after:
                body["search_after"] = search_after
            hits = self.search(index, body)["hits"]["hits"]
            if not hits:
                return
            yield from hits
            search_after = hits[-1]["sort"]


def run_prod_es(args: argparse.Namespace, out_dir: Path) -> None:
    es = EsClient()
    window_end = (
        parse_iso(args.window_end)
        if args.window_end
        else dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
        - dt.timedelta(hours=2)
    )
    window_start = window_end - dt.timedelta(hours=args.window_hours)
    window_range = {"range": {"created_at": {"gte": iso_z(window_start), "lt": iso_z(window_end)}}}
    print(f"Sampling window {iso_z(window_start)} .. {iso_z(window_end)}")

    # 1. Posts in window (with embeddings), up to --max-posts. A prod-scale
    # window holds millions of posts, so a single sorted scan is both slow and
    # wrong (capping it would keep only the first sliver of the window).
    # Instead, sample every hour bucket independently: half the bucket budget
    # goes to the top posts by like_count (the popularity head), half to an
    # arbitrary slice (the long tail), so coverage spans the whole window.
    source_fields = [
        "at_uri",
        "author_did",
        "content",
        "created_at",
        "like_count",
        "quote_post",
        EMBED_FIELD,
    ]
    hours = max(1, int((window_end - window_start).total_seconds() // 3600))
    per_bucket = max(10, args.max_posts // hours)
    posts_by_uri: dict[str, dict] = {}
    for i in range(hours):
        bucket_start = window_start + dt.timedelta(hours=i)
        bucket_end = min(bucket_start + dt.timedelta(hours=1), window_end)
        bucket_range = {
            "range": {"created_at": {"gte": iso_z(bucket_start), "lt": iso_z(bucket_end)}}
        }
        query = {"bool": {"filter": [bucket_range, {"exists": {"field": EMBED_FIELD}}]}}
        for sort in (
            [{"like_count": "desc"}, {"created_at": "asc"}],
            [{"created_at": "asc"}],
        ):
            resp = es.search(
                "posts",
                {
                    "size": per_bucket // 2,
                    "query": query,
                    "sort": sort,
                    "_source": source_fields,
                    "track_total_hits": False,
                },
            )
            for hit in resp["hits"]["hits"]:
                posts_by_uri[hit["_source"]["at_uri"]] = hit["_source"]
        if (i + 1) % 8 == 0:
            print(f"  ...{i + 1}/{hours} hour buckets, {len(posts_by_uri)} posts")
    print(f"  {len(posts_by_uri)} posts sampled across {hours} hour buckets")

    # 2. Cohort: active likers in the window.
    agg = es.search(
        "likes",
        {
            "size": 0,
            "query": {"bool": {"filter": [window_range]}},
            "aggs": {
                "likers": {
                    "terms": {
                        "field": "author_did",
                        "size": args.max_cohort,
                        "min_doc_count": args.min_cohort_likes,
                    }
                }
            },
        },
    )
    cohort = [b["key"] for b in agg["aggregations"]["likers"]["buckets"]]
    print(f"  cohort: {len(cohort)} likers with >= {args.min_cohort_likes} likes")
    if not cohort:
        sys.exit("FATAL: empty cohort; widen the window or lower --min-cohort-likes")

    # 3. All cohort likes in the window.
    raw_likes: list[dict] = []
    for i in range(0, len(cohort), 64):
        chunk = cohort[i : i + 64]
        query = {"bool": {"filter": [window_range, {"terms": {"author_did": chunk}}]}}
        like_fields = ["at_uri", "subject_uri", "author_did", "created_at", "indexed_at"]
        for hit in es.scan("likes", query, like_fields):
            raw_likes.append(hit["_source"])
    print(f"  {len(raw_likes)} cohort likes fetched")

    # 4. Hydrate dangling like targets (liked posts outside the sample). Cap
    # the hydration set, keeping the subjects the cohort liked most — those
    # contribute the most history density per added post.
    missing_counts = Counter(
        like["subject_uri"] for like in raw_likes if like["subject_uri"] not in posts_by_uri
    )
    hydrate_cap = args.max_posts // 2
    missing = [uri for uri, _ in missing_counts.most_common(hydrate_cap)]
    if len(missing_counts) > hydrate_cap:
        print(f"  capping dangler hydration at {hydrate_cap} of {len(missing_counts)} subjects")
    hydrated = 0
    for i in range(0, len(missing), 500):
        chunk = missing[i : i + 500]
        query = {
            "bool": {
                "filter": [
                    {"terms": {"at_uri": chunk}},
                    {"exists": {"field": EMBED_FIELD}},
                ]
            }
        }
        for hit in es.scan("posts", query, source_fields):
            doc = hit["_source"]
            posts_by_uri[doc["at_uri"]] = doc
            hydrated += 1
    kept = [like for like in raw_likes if like["subject_uri"] in posts_by_uri]
    print(
        f"  hydrated {hydrated} liked posts from outside the window; "
        f"dropped {len(raw_likes) - len(kept)} dangling likes"
    )

    # 5. Pseudonymize liker identities (see module docstring). Post authors
    # stay real; only the people whose like histories we densified are masked.
    pseudonymize = make_pseudonymizer(not args.no_pseudonymize)
    if args.no_pseudonymize:
        print("  WARNING: real liker DIDs retained (--no-pseudonymize) — do not publish")
    likes = [pseudonymize_like(like, pseudonymize) for like in kept]

    # 6. Personas: densest likers among kept likes.
    by_liker = Counter(like["author_did"] for like in likes)
    personas = [{"did": did, "likes": count} for did, count in by_liker.most_common(args.personas)]

    # 6. Convert to fixture rows.
    posts = []
    for doc in posts_by_uri.values():
        created = parse_iso(doc["created_at"])
        embedding = doc["embeddings"]["all_MiniLM_L12_v2"]
        posts.append(
            {
                "at_uri": doc["at_uri"],
                "did": doc["author_did"],
                "created_at": created,
                "raw_post": build_raw_post(
                    doc["at_uri"],
                    doc["author_did"],
                    doc.get("content") or "",
                    created,
                    doc.get("quote_post") or None,
                ),
                "inferences": json.dumps(
                    {"text_embeddings": {EMBED_MODEL: encode_embedding(embedding)}}
                ),
            }
        )
    like_counts = [
        {
            "at_uri": doc["at_uri"],
            "author_did": doc["author_did"],
            "like_count": int(doc.get("like_count") or 0),
        }
        for doc in posts_by_uri.values()
        if int(doc.get("like_count") or 0) > 0
    ]

    write_fixture_set(
        out_dir,
        "prod-es",
        posts,
        likes,
        like_counts,
        personas,
        window_start,
        window_end,
        len(cohort),
        pseudonymized=not args.no_pseudonymize,
    )


# --------------------------------------------------------------------------
# megastream-files mode
# --------------------------------------------------------------------------


def read_megastream_posts(input_dir: Path) -> list[dict]:
    """Read post rows out of existing megastream .db.zip files, keeping only
    creates that carry a MiniLM embedding (what ingest will actually index)."""
    rows: list[dict] = []
    seen_uris: set[str] = set()
    zips = sorted(input_dir.glob("*.db.zip"))
    if not zips:
        sys.exit(f"FATAL: no *.db.zip files in {input_dir}")
    for src in zips:
        tmp_db = src.with_suffix(".tmp-extract")
        if zipfile.is_zipfile(src):
            with zipfile.ZipFile(src) as zf:
                member = next(m for m in zf.namelist() if m.endswith(".db"))
                tmp_db.write_bytes(zf.read(member))
        else:
            tmp_db.write_bytes(src.read_bytes())
        conn = sqlite3.connect(tmp_db)
        try:
            for at_uri, did, raw_post, inferences in conn.execute(
                "SELECT at_uri, did, raw_post, inferences FROM enriched_posts"
            ):
                if not (at_uri and raw_post and inferences):
                    continue
                try:
                    raw = json.loads(raw_post)
                    inf = json.loads(inferences)
                except json.JSONDecodeError:
                    continue
                message = raw.get("message") or {}
                commit = message.get("commit") or {}
                record = commit.get("record") or {}
                created_raw = record.get("createdAt")
                if commit.get("operation") != "create" or not created_raw:
                    continue
                if EMBED_MODEL not in (inf.get("text_embeddings") or {}):
                    continue
                if at_uri in seen_uris:
                    continue
                seen_uris.add(at_uri)
                rows.append(
                    {
                        "at_uri": at_uri,
                        "did": did,
                        "created_at": parse_iso(created_raw),
                        "raw_post": raw_post,
                        "inferences": inferences,
                    }
                )
        finally:
            conn.close()
            tmp_db.unlink()
        print(f"  read {src.name}")
    return rows


def synth_did(rng: random.Random, i: int) -> str:
    # did:plc identifiers are 24 chars of base32; use a recognizable dev prefix.
    suffix = "".join(rng.choices(TID_ALPHABET, k=24 - len(f"gedev{i:03d}")))
    return f"did:plc:gedev{i:03d}{suffix}"


def synth_tid(rng: random.Random) -> str:
    return "".join(rng.choices(TID_ALPHABET, k=13))


def run_megastream_files(args: argparse.Namespace, out_dir: Path) -> None:
    input_dir = Path(args.input).expanduser()
    print(f"Reading megastream files from {input_dir}")
    posts = read_megastream_posts(input_dir)
    if not posts:
        sys.exit("FATAL: no usable posts (creates with MiniLM embeddings) found")
    print(f"  {len(posts)} usable posts")

    rng = random.Random(args.seed)

    # The input files may span months (the checked-in test archives do), which
    # would leave almost nothing inside the api's recency windows after
    # rebasing. Compress timestamps into a dense window ending at the newest
    # original post, preserving order.
    posts.sort(key=lambda p: p["created_at"])
    span = dt.timedelta(hours=args.window_hours)
    end = max(p["created_at"] for p in posts)
    for i, post in enumerate(posts):
        frac = (i + rng.uniform(0.1, 0.9)) / len(posts)
        new_created = end - span + frac * span
        post["created_at"] = new_created
        raw = json.loads(post["raw_post"])
        time_us = int(new_created.timestamp() * 1_000_000)
        raw["time_us"] = time_us
        message = raw.get("message") or {}
        message["time_us"] = time_us
        record = ((message.get("commit") or {}).get("record")) or {}
        record["createdAt"] = iso_z(new_created)
        post["raw_post"] = json.dumps(raw)

    window_start = end - span
    window_end = end + dt.timedelta(hours=1)

    # Zipf-flavored popularity over a shuffled post order, so a stable head of
    # popular posts exists (popularity generator) along with a long tail.
    shuffled = posts[:]
    rng.shuffle(shuffled)
    weights = [1.0 / (rank + 10) for rank in range(len(shuffled))]

    def sample_without_replacement(k: int) -> list[dict]:
        # Efraimidis-Spirakis weighted reservoir keys: top-k by u^(1/w).
        keyed = [
            (rng.random() ** (1.0 / w), post) for w, post in zip(weights, shuffled, strict=True)
        ]
        keyed.sort(key=lambda pair: pair[0], reverse=True)
        return [post for _, post in keyed[:k]]

    cohort = [synth_did(rng, i) for i in range(args.cohort)]
    likes: list[dict] = []
    per_post = Counter()
    per_user = Counter()
    for did in cohort:
        target_count = min(len(posts), rng.randint(args.min_likes, args.max_likes))
        for post in sample_without_replacement(target_count):
            liked_at = min(
                post["created_at"] + dt.timedelta(minutes=rng.expovariate(1 / 90.0)),
                window_end - dt.timedelta(seconds=1),
            )
            likes.append(
                {
                    "at_uri": f"at://{did}/app.bsky.feed.like/{synth_tid(rng)}",
                    "subject_uri": post["at_uri"],
                    "author_did": did,
                    "created_at": iso_z(liked_at),
                    "indexed_at": iso_z(liked_at),
                }
            )
            per_post[post["at_uri"]] += 1
            per_user[did] += 1

    post_authors = {p["at_uri"]: p["did"] for p in posts}
    like_counts = [
        {"at_uri": uri, "author_did": post_authors[uri], "like_count": count}
        for uri, count in per_post.items()
    ]
    personas = [{"did": did, "likes": count} for did, count in per_user.most_common(args.personas)]

    write_fixture_set(
        out_dir,
        "megastream-files",
        posts,
        likes,
        like_counts,
        personas,
        window_start,
        window_end,
        len(cohort),
        # Liker DIDs in this mode are invented, so there's nothing to mask.
        pseudonymized=True,
    )


# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--source", choices=["prod-es", "megastream-files"], default="prod-es")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).parent / "data"),
        help="output directory (default: fixtures/data next to this script)",
    )
    # prod-es options
    parser.add_argument("--window-end", help="ISO timestamp; default: 2h ago, on the hour")
    # Keep this comfortably larger than the api's largest recency window (24h)
    # so now-anchored queries see a full window even as a seeded environment
    # ages between re-seeds.
    parser.add_argument("--window-hours", type=int, default=48)
    parser.add_argument("--max-posts", type=int, default=100_000)
    parser.add_argument("--min-cohort-likes", type=int, default=10)
    parser.add_argument("--max-cohort", type=int, default=300)
    # megastream-files options
    parser.add_argument(
        "--input",
        default=str(Path(__file__).resolve().parents[3] / "ingex/ingest/test_data/megastream"),
        help="directory of .db.zip files (default: ingex checked-in test data)",
    )
    parser.add_argument("--cohort", type=int, default=50)
    parser.add_argument("--min-likes", type=int, default=30)
    parser.add_argument("--max-likes", type=int, default=80)
    parser.add_argument("--seed", type=int, default=42)
    # common
    parser.add_argument("--personas", type=int, default=5)
    parser.add_argument(
        "--no-pseudonymize",
        action="store_true",
        help="keep real liker DIDs (prod-es only). For local investigation that "
        "needs real identities; the result is NOT publishable and publish_fixture.sh "
        "will refuse it. See the module docstring.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out).expanduser()
    if args.source == "prod-es":
        run_prod_es(args, out_dir)
    else:
        run_megastream_files(args, out_dir)


if __name__ == "__main__":
    main()
