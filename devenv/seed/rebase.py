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
- With GE_DEV_SYNTHETIC_TOPICS=1, each rebased post whose inference payload
  lacks a politics topic score also receives a deterministic synthetic one.
  The downloaded fixture is mounted read-only and is never modified.
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
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

FIXTURES_DIR = Path("/fixtures")
OUT_DIR = Path("/runtime/seed")
PROBE_ENV = Path("/runtime/probe.env")

FILENAME_RE = re.compile(r"^(?P<prefix>.*_)(?P<date>\d{8})_(?P<time>\d{6})\.db\.zip$")

SYNTHETIC_TOPICS_ENV = "GE_DEV_SYNTHETIC_TOPICS"
POLITICS_TOPIC_LABEL = "News & Social Concern"
SYNTHETIC_TOPIC_BUCKETS = (0.0, 0.25, 0.5, 0.75, 1.0)
SYNTHETIC_TOPIC_STRATEGY = "sha256-at-uri-mod-5-v1"


@dataclass
class SyntheticTopicStats:
    """Summary written to the rebased manifest for an enabled overlay."""

    injected_posts: int = 0
    preserved_posts: int = 0
    distribution: Counter[str] = field(default_factory=Counter)

    def record(self, score: float, *, injected: bool) -> None:
        if injected:
            self.injected_posts += 1
        else:
            self.preserved_posts += 1
        self.distribution[f"{score:.2f}"] += 1

    def manifest(self) -> dict:
        return {
            "enabled": True,
            "strategy": SYNTHETIC_TOPIC_STRATEGY,
            "label": POLITICS_TOPIC_LABEL,
            "injected_posts": self.injected_posts,
            "preserved_posts": self.preserved_posts,
            "distribution": dict(
                sorted(self.distribution.items(), key=lambda item: float(item[0]))
            ),
        }


def env_flag(name: str) -> bool:
    """Parse a boolean environment flag, rejecting ambiguous spellings."""
    value = os.environ.get(name, "").strip().lower()
    if value in {"", "0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    sys.exit(f"FATAL: {name} must be 1/0, true/false, yes/no, or on/off (got {value!r})")


def synthetic_topic_score(at_uri: str) -> float:
    """Return one of five stable politics scores for an AT URI."""
    digest = hashlib.sha256(at_uri.encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") % len(SYNTHETIC_TOPIC_BUCKETS)
    return SYNTHETIC_TOPIC_BUCKETS[bucket]


def _mapping_child(parent: dict, key: str, path: str) -> dict:
    value = parent.get(key)
    if value is None:
        value = {}
        parent[key] = value
    if not isinstance(value, dict):
        raise ValueError(f"inferences.{path} must be an object")
    return value


def inject_synthetic_topic_score(
    inferences_json: str | None, at_uri: str
) -> tuple[str, float, bool]:
    """Add the production-shaped politics score unless a valid one exists.

    Returns ``(json, score, injected)``. Existing inference branches and valid
    real politics scores are retained exactly; an invalid existing score is
    replaced so an explicitly synthetic seed always provides usable input.
    """
    try:
        inferences = json.loads(inferences_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"inferences is not valid JSON: {exc}") from exc
    if not isinstance(inferences, dict):
        raise ValueError("inferences must be a JSON object")

    text = _mapping_child(inferences, "text", "text")
    post_text = _mapping_child(
        text,
        "message.commit.record.text",
        "text.message.commit.record.text",
    )
    topics = _mapping_child(
        post_text,
        "topic",
        "text.message.commit.record.text.topic",
    )

    existing = topics.get(POLITICS_TOPIC_LABEL)
    if not isinstance(existing, bool) and isinstance(existing, (int, float)) and 0 <= existing <= 1:
        return inferences_json or "{}", float(existing), False

    score = synthetic_topic_score(at_uri)
    topics[POLITICS_TOPIC_LABEL] = score
    return json.dumps(inferences), score, True


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


def rebase_db(
    src_zip: Path,
    delta: dt.timedelta,
    delta_us: int,
    synthetic_topic_stats: SyntheticTopicStats | None = None,
) -> Path:
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
            "SELECT id, at_uri, time_us, raw_post, inferences, created_at FROM enriched_posts"
        ).fetchall()
        for row_id, at_uri, time_us, raw_post, inferences, created_at in rows:
            new_time_us = int(time_us) + delta_us if time_us is not None else None
            new_raw = shift_raw_post(raw_post, delta, delta_us) if raw_post else raw_post
            new_inferences = inferences
            if synthetic_topic_stats is not None and isinstance(at_uri, str) and at_uri:
                try:
                    new_inferences, score, injected = inject_synthetic_topic_score(
                        inferences,
                        at_uri,
                    )
                except ValueError as exc:
                    sys.exit(
                        f"FATAL: cannot add synthetic topics to {at_uri} in {src_zip.name}: {exc}"
                    )
                synthetic_topic_stats.record(score, injected=injected)
            new_created = None
            if created_at:
                # SQLite CURRENT_TIMESTAMP format: "YYYY-MM-DD HH:MM:SS"
                parsed = dt.datetime.fromisoformat(str(created_at))
                new_created = (parsed + delta).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE enriched_posts SET time_us = ?, raw_post = ?, inferences = ?, "
                "created_at = ? WHERE id = ?",
                (new_time_us, new_raw, new_inferences, new_created, row_id),
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

    synthetic_topics = env_flag(SYNTHETIC_TOPICS_ENV)
    synthetic_topic_stats = SyntheticTopicStats() if synthetic_topics else None
    if synthetic_topics:
        print(
            "Synthetic topics enabled: adding deterministic "
            f"{POLITICS_TOPIC_LABEL!r} scores to rebased copies only"
        )

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
        out = rebase_db(src, delta, delta_us, synthetic_topic_stats)
        out_names.append(out.name)
        print(f"  {src.name} -> {out.name}")

    if synthetic_topic_stats is not None:
        print(
            "  synthetic topics: "
            f"{synthetic_topic_stats.injected_posts} injected, "
            f"{synthetic_topic_stats.preserved_posts} existing scores preserved"
        )

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
    if synthetic_topic_stats is not None:
        manifest["synthetic_topics"] = synthetic_topic_stats.manifest()
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))

    personas = manifest.get("personas") or []
    if personas:
        PROBE_ENV.write_text(f"GE_PROBE_USER_DID={personas[0]['did']}\n")
        print(f"  probe persona: {personas[0]['did']}")

    print("Rebase complete")


if __name__ == "__main__":
    main()
