import datetime as dt
import gzip
import hashlib
import json
import sqlite3
import zipfile

import pytest
import rebase

HOUR = dt.timedelta(hours=1)
DAY = dt.timedelta(days=1)


# --------------------------------------------------------------------------
# filename parsing
#
# Shared by rebase_db (to shift the name) and main (to place the ingest
# cursor before the earliest file), so a mistake here either misnames output
# or makes the whole fixture set get skipped at ingest.
# --------------------------------------------------------------------------


def test_parse_filename_splits_prefix_and_timestamp():
    prefix, stamp = rebase.parse_filename("mega_jetstream_20260722_100000.db.zip")
    assert prefix == "mega_jetstream_"
    assert stamp == dt.datetime(2026, 7, 22, 10, 0, tzinfo=dt.UTC)


def test_parse_filename_returns_an_aware_utc_timestamp():
    # Compared against other aware datetimes in main(); a naive one would raise.
    _, stamp = rebase.parse_filename("mega_jetstream_20260722_100000.db.zip")
    assert stamp.tzinfo is not None
    assert stamp.utcoffset() == dt.timedelta(0)


@pytest.mark.parametrize(
    "name",
    [
        "mega_jetstream_2026072_100000.db.zip",  # short date
        "mega_jetstream_20260722_1000.db.zip",  # short time
        "mega_jetstream_20260722_100000.db",  # not a .db.zip
        "likes.jsonl.gz",
    ],
)
def test_parse_filename_rejects_names_the_spooler_cannot_read(name):
    with pytest.raises(SystemExit):
        rebase.parse_filename(name)


# --------------------------------------------------------------------------
# timestamp shifting
# --------------------------------------------------------------------------


def test_shift_iso_moves_the_timestamp_by_the_delta():
    assert rebase.shift_iso("2026-07-22T10:00:00Z", DAY) == "2026-07-23T10:00:00Z"


def test_shift_iso_preserves_the_z_suffix():
    # ES index templates declare `format: iso8601`; both forms parse, but the
    # rebased data should look like what ingest writes.
    assert rebase.shift_iso("2026-07-22T10:00:00Z", HOUR).endswith("Z")


def test_shift_iso_preserves_offset_form_when_input_used_one():
    assert rebase.shift_iso("2026-07-22T10:00:00+00:00", HOUR) == "2026-07-22T11:00:00+00:00"


def test_shift_iso_handles_sub_second_precision():
    # Real ES timestamps carry milliseconds; dropping them would collapse
    # ordering between posts written in the same second.
    assert rebase.shift_iso("2026-07-22T10:00:00.123456Z", HOUR) == "2026-07-22T11:00:00.123456Z"


def test_zero_delta_is_a_no_op():
    assert rebase.shift_iso("2026-07-22T10:00:00Z", dt.timedelta(0)) == "2026-07-22T10:00:00Z"


# --------------------------------------------------------------------------
# raw_post shifting
#
# megastream_ingest reads created_at from message.commit.record.createdAt and
# orders on time_us. Both have to move together or posts land in ES with
# timestamps that disagree with their ordering.
# --------------------------------------------------------------------------


def _raw_post(created="2026-07-22T10:00:00Z", time_us=1784714400000000):
    return json.dumps(
        {
            "at_uri": "at://did:plc:author0000000000000000/app.bsky.feed.post/3abc",
            "did": "did:plc:author0000000000000000",
            "time_us": time_us,
            "message": {
                "did": "did:plc:author0000000000000000",
                "kind": "commit",
                "time_us": time_us,
                "commit": {
                    "operation": "create",
                    "collection": "app.bsky.feed.post",
                    "record": {"$type": "app.bsky.feed.post", "text": "hi", "createdAt": created},
                },
            },
        }
    )


def test_shift_raw_post_moves_created_at():
    out = json.loads(rebase.shift_raw_post(_raw_post(), DAY, int(DAY.total_seconds() * 1e6)))
    assert out["message"]["commit"]["record"]["createdAt"] == "2026-07-23T10:00:00Z"


def test_shift_raw_post_moves_both_time_us_fields_consistently():
    delta_us = int(DAY.total_seconds() * 1e6)
    out = json.loads(rebase.shift_raw_post(_raw_post(), DAY, delta_us))
    assert out["message"]["time_us"] == 1784714400000000 + delta_us
    assert out["time_us"] == 1784714400000000 + delta_us


def test_shift_raw_post_keeps_time_us_and_created_at_in_agreement():
    delta_us = int(DAY.total_seconds() * 1e6)
    out = json.loads(rebase.shift_raw_post(_raw_post(), DAY, delta_us))
    from_created = rebase.parse_iso(out["message"]["commit"]["record"]["createdAt"])
    from_time_us = dt.datetime.fromtimestamp(out["message"]["time_us"] / 1e6, dt.UTC)
    assert abs((from_created - from_time_us).total_seconds()) < 1


def test_shift_raw_post_leaves_content_alone():
    out = json.loads(rebase.shift_raw_post(_raw_post(), DAY, 0))
    assert out["message"]["commit"]["record"]["text"] == "hi"
    assert out["at_uri"].endswith("/3abc")


def test_shift_raw_post_tolerates_missing_timestamp_fields():
    # Deletes and account events have no record/createdAt; they must pass
    # through rather than crash the whole rebase.
    minimal = json.dumps({"message": {"kind": "commit", "commit": {"operation": "delete"}}})
    out = json.loads(rebase.shift_raw_post(minimal, DAY, 1))
    assert out["message"]["commit"]["operation"] == "delete"


# --------------------------------------------------------------------------
# likes rebasing
# --------------------------------------------------------------------------


def test_rebase_jsonl_shifts_only_the_named_fields(tmp_path):
    src, dest = tmp_path / "in.jsonl.gz", tmp_path / "out.jsonl.gz"
    like = {
        "at_uri": "at://did:plc:liker00000000000000000/app.bsky.feed.like/3xyz",
        "subject_uri": "at://did:plc:author0000000000000000/app.bsky.feed.post/3abc",
        "author_did": "did:plc:liker00000000000000000",
        "created_at": "2026-07-22T10:00:00Z",
        "indexed_at": "2026-07-22T10:00:05Z",
    }
    with gzip.open(src, "wt") as f:
        f.write(json.dumps(like) + "\n")

    count = rebase.rebase_jsonl(src, dest, DAY, ("created_at", "indexed_at"))

    (out,) = [json.loads(line) for line in gzip.open(dest, "rt")]
    assert count == 1
    assert out["created_at"] == "2026-07-23T10:00:00Z"
    assert out["indexed_at"] == "2026-07-23T10:00:05Z"
    assert out["author_did"] == like["author_did"]
    assert out["at_uri"] == like["at_uri"]


def test_rebase_jsonl_preserves_like_after_post_ordering(tmp_path):
    # A uniform shift is what keeps "the like happened 5s after the post" true;
    # this is the property the whole rebase design rests on.
    src, dest = tmp_path / "in.jsonl.gz", tmp_path / "out.jsonl.gz"
    stamps = ["2026-07-22T10:00:00Z", "2026-07-22T10:00:05Z", "2026-07-22T18:30:00Z"]
    with gzip.open(src, "wt") as f:
        for stamp in stamps:
            f.write(json.dumps({"created_at": stamp}) + "\n")

    rebase.rebase_jsonl(src, dest, DAY, ("created_at",))

    shifted = [rebase.parse_iso(json.loads(line)["created_at"]) for line in gzip.open(dest, "rt")]
    original = [rebase.parse_iso(s) for s in stamps]
    # ragged on purpose: each list is zipped against its own tail
    gaps_before = [b - a for a, b in zip(original, original[1:], strict=False)]
    gaps_after = [b - a for a, b in zip(shifted, shifted[1:], strict=False)]
    assert gaps_before == gaps_after


def test_rebase_jsonl_skips_blank_lines(tmp_path):
    src, dest = tmp_path / "in.jsonl.gz", tmp_path / "out.jsonl.gz"
    with gzip.open(src, "wt") as f:
        f.write(json.dumps({"created_at": "2026-07-22T10:00:00Z"}) + "\n\n")
    assert rebase.rebase_jsonl(src, dest, DAY, ("created_at",)) == 1


# --------------------------------------------------------------------------
# database rebasing
# --------------------------------------------------------------------------


def _make_db_zip(path, created, time_us, sqlite_created="2026-07-22 10:00:00"):
    db_file = path.parent / "build.db"
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(
            """
            CREATE TABLE enriched_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at_uri TEXT, did TEXT, time_us INTEGER,
                raw_post TEXT, inferences TEXT, enriched_metadata TEXT,
                created_at TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO enriched_posts"
            " (at_uri, did, time_us, raw_post, inferences, enriched_metadata, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "at://did:plc:author0000000000000000/app.bsky.feed.post/3abc",
                "did:plc:author0000000000000000",
                time_us,
                _raw_post(created, time_us),
                "{}",
                "{}",
                sqlite_created,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(db_file, path.name.removesuffix(".zip"))
    db_file.unlink()


def _read_rows(zip_path, tmp_path):
    with zipfile.ZipFile(zip_path) as zf:
        (member,) = [m for m in zf.namelist() if m.endswith(".db")]
        extracted = tmp_path / "check.db"
        extracted.write_bytes(zf.read(member))
    conn = sqlite3.connect(extracted)
    try:
        return conn.execute("SELECT time_us, raw_post, created_at FROM enriched_posts").fetchall()
    finally:
        conn.close()


def test_rebase_db_shifts_the_filename_timestamp(tmp_path, monkeypatch):
    # The spooler orders and filters files by this timestamp, so it has to move
    # with the data or freshly-rebased files look old and get skipped.
    monkeypatch.setattr(rebase, "OUT_DIR", tmp_path / "out")
    rebase.OUT_DIR.mkdir()
    src = tmp_path / "mega_jetstream_20260722_100000.db.zip"
    _make_db_zip(src, "2026-07-22T10:00:00Z", 1784714400000000)

    out = rebase.rebase_db(src, DAY, int(DAY.total_seconds() * 1e6))

    assert out.name == "mega_jetstream_20260723_100000.db.zip"


def test_rebase_db_shifts_every_timestamp_column(tmp_path, monkeypatch):
    monkeypatch.setattr(rebase, "OUT_DIR", tmp_path / "out")
    rebase.OUT_DIR.mkdir()
    src = tmp_path / "mega_jetstream_20260722_100000.db.zip"
    time_us = 1784714400000000
    _make_db_zip(src, "2026-07-22T10:00:00Z", time_us)

    delta_us = int(DAY.total_seconds() * 1e6)
    out = rebase.rebase_db(src, DAY, delta_us)

    (row,) = _read_rows(out, tmp_path)
    shifted_time_us, raw_post, sqlite_created = row
    assert shifted_time_us == time_us + delta_us
    record = json.loads(raw_post)["message"]["commit"]["record"]
    assert record["createdAt"] == "2026-07-23T10:00:00Z"
    assert sqlite_created == "2026-07-23 10:00:00"


def test_rebase_db_accepts_a_bare_sqlite_file_named_db_zip(tmp_path, monkeypatch):
    # Megastream archives are sometimes uncompressed despite the extension;
    # ingest tolerates that, so the rebase has to as well.
    monkeypatch.setattr(rebase, "OUT_DIR", tmp_path / "out")
    rebase.OUT_DIR.mkdir()
    real_zip = tmp_path / "mega_jetstream_20260722_100000.db.zip"
    _make_db_zip(real_zip, "2026-07-22T10:00:00Z", 1784714400000000)
    with zipfile.ZipFile(real_zip) as zf:
        (member,) = [m for m in zf.namelist() if m.endswith(".db")]
        payload = zf.read(member)
    bare = tmp_path / "mega_jetstream_20260722_110000.db.zip"
    bare.write_bytes(payload)

    out = rebase.rebase_db(bare, DAY, int(DAY.total_seconds() * 1e6))

    assert _read_rows(out, tmp_path)


def test_rebase_db_rejects_filenames_the_spooler_cannot_parse(tmp_path, monkeypatch):
    monkeypatch.setattr(rebase, "OUT_DIR", tmp_path / "out")
    rebase.OUT_DIR.mkdir()
    bad = tmp_path / "not-a-megastream-file.db.zip"
    _make_db_zip(bad, "2026-07-22T10:00:00Z", 1784714400000000)

    with pytest.raises(SystemExit):
        rebase.rebase_db(bad, DAY, 0)


# Graph captures can be months newer than the posts they accompany. Their
# own capture endpoint must anchor the shift, while likes stay shared with ES.
def _graph_source(tmp_path, monkeypatch, *, graph=True):
    source = tmp_path / "fixtures"
    source.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(rebase, "FIXTURES_DIR", source)
    monkeypatch.setattr(rebase, "OUT_DIR", runtime / "seed")
    monkeypatch.setattr(rebase, "PROBE_ENV", runtime / "probe.env")
    post_time = rebase.parse_iso("2026-09-14T17:00:00Z")
    chunk = source / "mega_jetstream_20260914_170000.db.zip"
    _make_db_zip(
        chunk, post_time.isoformat(), int(post_time.timestamp() * 1e6), "2026-09-14 17:00:00"
    )
    author = "did:plc:author0000000000000000"
    liker = "did:plc:liker"
    manifest = {
        "source": "prod-es",
        "window_end": "2026-09-14T18:00:00Z",
        "personas": [{"did": liker}],
        "dev_users": [],
    }
    (source / "manifest.json").write_text(json.dumps(manifest))
    _write_jsonl(
        source / "likes.jsonl.gz",
        [
            {
                "at_uri": f"at://{liker}/app.bsky.feed.like/l1",
                "author_did": liker,
                "subject_uri": f"at://{author}/app.bsky.feed.post/3abc",
                "created_at": "2026-09-14T17:05:00Z",
                "indexed_at": "2026-09-14T17:05:01Z",
            }
        ],
    )
    if not graph:
        return source
    graph_dir = source / "graph"
    graph_dir.mkdir()
    captured = "2026-11-17T12:00:00.123456Z"
    _write_jsonl(
        graph_dir / "actors.jsonl.gz",
        [
            {
                "did": author,
                "status": "active",
                "status_at": None,
                "first_seen_at": captured,
                "is_root": False,
                "in_graph": True,
            },
            {
                "did": liker,
                "status": "active",
                "status_at": captured,
                "first_seen_at": captured,
                "is_root": True,
                "in_graph": True,
            },
            {
                "did": "did:plc:missing",
                "status": None,
                "status_at": None,
                "first_seen_at": None,
                "is_root": False,
                "in_graph": False,
            },
        ],
    )
    _write_jsonl(
        graph_dir / "follows.jsonl.gz",
        [
            {
                "follower_did": liker,
                "subject_did": author,
                "rkey": f"f{i}",
                "created_at": captured if i == 0 else None,
                "indexed_at": captured,
                "deleted_at": None,
                "seq": 42 + i,
            }
            for i in range(2)
        ],
    )
    graph_manifest = {
        "schema_version": 1,
        "source": "postgres",
        "selection": {
            "roots": "likers-personas-dev-users",
            "direction": "outgoing",
            "hops": 1,
            "active_only": True,
        },
        "capture_started_at": captured,
        "capture_finished_at": captured,
        "base_files": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (source / "manifest.json", source / "likes.jsonl.gz", chunk)
        },
        "likes_file": "../likes.jsonl.gz",
        "counts": {"roots": 1, "actors": 3, "follows": 2, "likes": 1, "missing_roots": 0},
    }
    (graph_dir / "manifest.json").write_text(json.dumps(graph_manifest))
    assert rebase.validate_graph_fixture(source) == []
    return source


def _write_jsonl(path, rows):
    with gzip.open(path, "wt") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


def _jsonl_rows(path):
    with gzip.open(path, "rt") as stream:
        return [json.loads(line) for line in stream]


def test_graph_uses_its_capture_end_and_shares_exact_rebased_likes(tmp_path, monkeypatch):
    source = _graph_source(tmp_path, monkeypatch)
    original_manifest = json.loads((source / "graph" / "manifest.json").read_text())

    rebase.main([])

    base = json.loads((rebase.OUT_DIR / "manifest.json").read_text())
    graph = json.loads((rebase.OUT_DIR / "graph" / "manifest.json").read_text())
    effective = rebase.parse_iso(base["rebased"]["effective_window_end"])
    delta = effective - rebase.parse_iso(original_manifest["capture_finished_at"])
    assert graph["rebased"]["delta_seconds"] == delta.total_seconds()
    assert graph["rebased"]["delta_seconds"] != base["rebased"]["delta_seconds"]
    assert graph["rebased"]["seeded_at"] == base["rebased"]["seeded_at"]
    assert graph["rebased"]["effective_capture_finished_at"] == effective.isoformat()
    assert graph["base_files"] == original_manifest["base_files"]
    shared_likes = rebase.OUT_DIR / "likes.jsonl.gz"
    assert graph["rebased"]["likes_sha256"] == hashlib.sha256(shared_likes.read_bytes()).hexdigest()
    assert graph["rebased"]["files_sha256"] == {
        name: hashlib.sha256((rebase.OUT_DIR / "graph" / name).read_bytes()).hexdigest()
        for name in ("actors.jsonl.gz", "follows.jsonl.gz")
    }
    assert not (rebase.OUT_DIR / "graph" / "likes.jsonl.gz").exists()
    (like,) = _jsonl_rows(shared_likes)
    assert rebase.parse_iso(like["created_at"]) == effective - dt.timedelta(minutes=55)


def test_graph_negative_shift_preserves_microseconds_nulls_and_record_identity(
    tmp_path, monkeypatch
):
    source = _graph_source(tmp_path, monkeypatch)
    output = tmp_path / "out"
    output.mkdir()
    likes = b"the shared rebased likes bytes"
    (output / "likes.jsonl.gz").write_bytes(likes)
    effective = rebase.parse_iso("2026-11-17T11:00:00.654321Z")
    seeded = effective + HOUR

    rebase.rebase_graph(source, output, effective, seeded)

    manifest = json.loads((output / "graph" / "manifest.json").read_text())
    assert manifest["rebased"]["delta_seconds"] == -3599.469135
    assert manifest["rebased"]["likes_sha256"] == hashlib.sha256(likes).hexdigest()
    for name, fields in (
        ("actors", ("status_at", "first_seen_at")),
        ("follows", ("created_at", "indexed_at", "deleted_at")),
    ):
        originals = _jsonl_rows(source / "graph" / f"{name}.jsonl.gz")
        shifted = _jsonl_rows(output / "graph" / f"{name}.jsonl.gz")
        assert (
            manifest["rebased"]["files_sha256"][f"{name}.jsonl.gz"]
            == hashlib.sha256((output / "graph" / f"{name}.jsonl.gz").read_bytes()).hexdigest()
        )
        for before, after in zip(originals, shifted, strict=True):
            for key, value in before.items():
                if key in fields and value is not None:
                    assert rebase.parse_iso(after[key]) == effective
                else:
                    assert after[key] == value


@pytest.mark.parametrize("args", [[], ["--check"]])
def test_invalid_graph_leaves_existing_runtime_untouched(tmp_path, monkeypatch, args):
    source = _graph_source(tmp_path, monkeypatch)
    rebase.OUT_DIR.mkdir()
    saved = rebase.OUT_DIR / "existing-data"
    saved.write_text("keep")
    (source / "graph" / "actors.jsonl.gz").unlink()

    with pytest.raises(SystemExit, match="graph fixture validation failed"):
        rebase.main(args)

    assert saved.read_text() == "keep"
    assert not rebase.PROBE_ENV.exists()


def test_check_validates_without_writing_runtime(tmp_path, monkeypatch):
    _graph_source(tmp_path, monkeypatch)
    rebase.main(["--check"])
    assert not rebase.OUT_DIR.exists()
    assert not rebase.PROBE_ENV.exists()


def test_graphless_fixture_clears_stale_runtime_graph_and_still_rebases(tmp_path, monkeypatch):
    _graph_source(tmp_path, monkeypatch, graph=False)
    stale = rebase.OUT_DIR / "graph"
    stale.mkdir(parents=True)
    (stale / "manifest.json").write_text("stale")

    rebase.main([])

    assert not stale.exists()
    assert (rebase.OUT_DIR / "manifest.json").exists()
    assert (rebase.OUT_DIR / "likes.jsonl.gz").exists()
    assert rebase.PROBE_ENV.read_text() == "GE_PROBE_USER_DID=did:plc:liker\n"
