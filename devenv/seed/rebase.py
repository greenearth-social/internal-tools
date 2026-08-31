"""Rebase fixture timestamps so seeded data looks recent (issue api#268).

Replaying a stale capture window verbatim would break everything anchored to
"now" (recency filters, popularity decay, candidate windows). This script
copies the fixtures from /fixtures into /runtime/seed with every timestamp
shifted forward by one uniform delta chosen so the fixture window ends one
hour before now:

    delta = (now_utc - 1h) - manifest.window_end

A single uniform shift preserves all relative structure — inter-post spacing,
like-after-post ordering, decay curves. Freshness deliberately wins over
time-of-day alignment: any whole-day scheme leaves the window end up to 24h
stale, which can starve the api's now-anchored recency windows (e.g. the
popularity generator's 24h filter) down to nothing, and feeds that return
data matter more than diurnal realism.

What gets shifted:
- megastream .db.zip files: the `time_us`/`created_at` columns, plus the
  timestamps inside `raw_post` JSON that megastream_ingest actually reads
  (`message.time_us`, `message.commit.record.createdAt`), and the filename
  timestamp (the spooler orders/filters files by it).
- topic_post_fixture.json: one synthetic top-level post appended to the
  rebased window so local seeds always exercise Graze topic ingestion.
- likes.jsonl.gz: `created_at` / `indexed_at` per like.
- like_counts.jsonl.gz: copied unchanged (no timestamps).
- manifest.json: copied with a `rebased` block recording the shift.

Also writes /runtime/probe.env pointing GE_PROBE_USER_DID at the first dev
persona, so the api serves feeds as a user with dense seeded history.

The output dir is wiped first: each seed is a fresh, disposable load, and this
also clears megastream_ingest's cursor state file so re-seeds re-process.

Runs on a stock python image; stdlib only.
"""

import datetime as dt
import gzip
import json
import re
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path

FIXTURES_DIR = Path("/fixtures")
OUT_DIR = Path("/runtime/seed")
PROBE_ENV = Path("/runtime/probe.env")
TOPIC_POST_FIXTURE = Path(__file__).with_name("topic_post_fixture.json")

FILENAME_RE = re.compile(r"^(?P<prefix>.*_)(?P<date>\d{8})_(?P<time>\d{6})\.db\.zip$")


def parse_filename(name: str) -> tuple[str, dt.datetime]:
    """Split a megastream filename into its prefix and encoded timestamp.

    The spooler discovers and orders files by this timestamp, so a name it
    can't parse would be silently skipped at ingest time — fail here instead.
    """
    match = FILENAME_RE.match(name)
    if not match:
        sys.exit(f"FATAL: fixture filename {name} doesn't match *_YYYYMMDD_HHMMSS.db.zip")
    stamp = dt.datetime.strptime(match["date"] + match["time"], "%Y%m%d%H%M%S")
    return match["prefix"], stamp.replace(tzinfo=dt.UTC)


def parse_iso(ts: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def shift_iso(ts: str, delta: dt.timedelta) -> str:
    shifted = parse_iso(ts) + delta
    out = shifted.isoformat()
    if ts.endswith("Z"):
        out = out.replace("+00:00", "Z")
    return out


def shift_raw_post(raw: str, delta: dt.timedelta, delta_us: int) -> str:
    post = json.loads(raw)
    message = post.get("message") or {}
    if isinstance(message.get("time_us"), (int, float)):
        message["time_us"] = int(message["time_us"]) + delta_us
    record = ((message.get("commit") or {}).get("record")) or {}
    if isinstance(record.get("createdAt"), str):
        record["createdAt"] = shift_iso(record["createdAt"], delta)
    if isinstance(post.get("time_us"), (int, float)):
        post["time_us"] = int(post["time_us"]) + delta_us
    return json.dumps(post)


def load_topic_post_fixture(path: Path = TOPIC_POST_FIXTURE) -> dict:
    """Load and validate the small synthetic post added to every local seed."""
    try:
        fixture = json.loads(path.read_text())
        topics = fixture["inferences"]["text"]["message.commit.record.text"]["topic"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError) as exc:
        sys.exit(f"FATAL: invalid topic post fixture {path}: {exc}")

    for field in ("at_uri", "did", "text"):
        if not isinstance(fixture.get(field), str) or not fixture[field]:
            sys.exit(f"FATAL: topic post fixture {path} needs a non-empty {field}")
    if not isinstance(topics, dict) or not topics:
        sys.exit(f"FATAL: topic post fixture {path} needs at least one topic score")
    for label, score in topics.items():
        if (
            not isinstance(label, str)
            or not label
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not 0 <= score <= 1
        ):
            sys.exit(f"FATAL: invalid topic score in {path}: {label!r}={score!r}")
    return fixture


def write_topic_post_fixture(fixture: dict, created_at: dt.datetime) -> Path:
    """Write one production-shaped megastream archive carrying topic scores."""
    at_uri = fixture["at_uri"]
    did = fixture["did"]
    time_us = int(created_at.timestamp() * 1_000_000)
    filename = f"mega_jetstream_{created_at:%Y%m%d_%H%M%S}.db.zip"
    zip_path = OUT_DIR / filename
    db_path = OUT_DIR / filename.removesuffix(".zip")
    if zip_path.exists() or db_path.exists():
        sys.exit(f"FATAL: topic post fixture collides with {filename}")

    raw_post = {
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
                "rkey": at_uri.rsplit("/", 1)[-1],
                "record": {
                    "$type": "app.bsky.feed.post",
                    "text": fixture["text"],
                    "createdAt": created_at.isoformat().replace("+00:00", "Z"),
                },
            },
        },
    }

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
        conn.execute(
            "INSERT INTO enriched_posts"
            " (at_uri, did, time_us, raw_post, inferences, enriched_metadata, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                at_uri,
                did,
                time_us,
                json.dumps(raw_post),
                json.dumps(fixture["inferences"]),
                "{}",
                created_at.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(db_path, db_path.name)
    db_path.unlink()
    return zip_path


def rebase_db(src_zip: Path, delta: dt.timedelta, delta_us: int) -> Path:
    prefix, file_ts = parse_filename(src_zip.name)
    shifted_ts = file_ts + delta
    out_name = f"{prefix}{shifted_ts:%Y%m%d_%H%M%S}.db.zip"

    work_db = OUT_DIR / (out_name + ".tmp.db")
    if zipfile.is_zipfile(src_zip):
        with zipfile.ZipFile(src_zip) as zf:
            db_members = [m for m in zf.namelist() if m.endswith(".db")]
            if len(db_members) != 1:
                sys.exit(f"FATAL: expected one .db inside {src_zip.name}, got {db_members}")
            work_db.write_bytes(zf.read(db_members[0]))
    else:
        # The ingest service tolerates raw SQLite files with a .db.zip name.
        shutil.copyfile(src_zip, work_db)

    conn = sqlite3.connect(work_db)
    try:
        rows = conn.execute(
            "SELECT id, time_us, raw_post, created_at FROM enriched_posts"
        ).fetchall()
        for row_id, time_us, raw_post, created_at in rows:
            new_time_us = int(time_us) + delta_us if time_us is not None else None
            new_raw = shift_raw_post(raw_post, delta, delta_us) if raw_post else raw_post
            new_created = None
            if created_at:
                # SQLite CURRENT_TIMESTAMP format: "YYYY-MM-DD HH:MM:SS"
                parsed = dt.datetime.fromisoformat(str(created_at))
                new_created = (parsed + delta).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE enriched_posts SET time_us = ?, raw_post = ?, created_at = ? WHERE id = ?",
                (new_time_us, new_raw, new_created, row_id),
            )
        conn.commit()
    finally:
        conn.close()

    out_zip = OUT_DIR / out_name
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(work_db, out_name.removesuffix(".zip"))
    work_db.unlink()
    return out_zip


def rebase_jsonl(src: Path, dest: Path, delta: dt.timedelta, fields: tuple[str, ...]) -> int:
    count = 0
    with gzip.open(src, "rt") as fin, gzip.open(dest, "wt") as fout:
        for line in fin:
            if not line.strip():
                continue
            doc = json.loads(line)
            for field in fields:
                if isinstance(doc.get(field), str):
                    doc[field] = shift_iso(doc[field], delta)
            fout.write(json.dumps(doc) + "\n")
            count += 1
    return count


def main() -> None:
    manifest_path = FIXTURES_DIR / "manifest.json"
    if not manifest_path.exists():
        sys.exit(
            "FATAL: no fixtures found (fixtures/data/manifest.json missing). "
            "Generate one with fixtures/generate_sample.py — see devenv/README.md."
        )
    manifest = json.loads(manifest_path.read_text())

    now = dt.datetime.now(dt.UTC)
    window_end = parse_iso(manifest["window_end"])
    delta_seconds = max(0, int((now - dt.timedelta(hours=1) - window_end).total_seconds()))
    delta = dt.timedelta(seconds=delta_seconds)
    delta_us = delta_seconds * 1_000_000
    print(
        f"Rebasing fixtures: window_end={manifest['window_end']}, "
        f"shift=+{delta_seconds / 86400:.2f}d"
    )

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    db_zips = sorted(FIXTURES_DIR.glob("*.db.zip"))
    if not db_zips:
        sys.exit("FATAL: no *.db.zip fixture files in fixtures/data/")
    out_names = []
    for src in db_zips:
        out = rebase_db(src, delta, delta_us)
        out_names.append(out.name)
        print(f"  {src.name} -> {out.name}")

    # The published fixture predates Graze topics. Add one stable, synthetic
    # top-level post just after the captured window so every local seed can
    # verify the production ingestion path without republishing the large
    # binary fixture. Keep its at_uri in the manifest for a one-command lookup.
    topic_fixture = load_topic_post_fixture()
    last_file_ts = max(parse_filename(name)[1] for name in out_names)
    effective_window_end = (window_end + delta).replace(microsecond=0)
    topic_created_at = max(last_file_ts, effective_window_end) + dt.timedelta(seconds=1)
    topic_out = write_topic_post_fixture(topic_fixture, topic_created_at)
    out_names.append(topic_out.name)
    topic_scores = topic_fixture["inferences"]["text"]["message.commit.record.text"]["topic"]
    print(f"  {topic_out.name}: synthetic topic post {topic_fixture['at_uri']}")

    # megastream_ingest with no saved state initializes its cursor to "now" and
    # skips pre-existing files (spool semantics). Pre-write a state file whose
    # cursor sits just before the earliest rebased file so the whole fixture
    # set gets processed.
    earliest = min(parse_filename(name)[1] for name in out_names)
    cursor_us = int(earliest.timestamp() * 1_000_000) - 1_000_000
    (OUT_DIR / ".megastream_state.json").write_text(
        json.dumps({"last_time_us": cursor_us, "updated_at": now.isoformat()})
    )

    likes_src = FIXTURES_DIR / "likes.jsonl.gz"
    if likes_src.exists():
        n = rebase_jsonl(likes_src, OUT_DIR / "likes.jsonl.gz", delta, ("created_at", "indexed_at"))
        print(f"  likes.jsonl.gz: {n} likes rebased")
    else:
        print("  WARNING: no likes.jsonl.gz fixture; like-driven feeds will be empty")

    counts_src = FIXTURES_DIR / "like_counts.jsonl.gz"
    if counts_src.exists():
        shutil.copyfile(counts_src, OUT_DIR / "like_counts.jsonl.gz")

    manifest["rebased"] = {
        "delta_seconds": delta_seconds,
        "seeded_at": now.isoformat(),
        "effective_window_end": (window_end + delta).isoformat(),
    }
    manifest["topic_fixture"] = {
        "at_uri": topic_fixture["at_uri"],
        "scores": topic_scores,
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))

    personas = manifest.get("personas") or []
    if personas:
        PROBE_ENV.write_text(f"GE_PROBE_USER_DID={personas[0]['did']}\n")
        print(f"  probe persona: {personas[0]['did']}")

    print("Rebase complete")


if __name__ == "__main__":
    main()
