import base64
import datetime as dt
import gzip
import json
import re
import sqlite3
import struct
import zipfile
import zlib

import generate_sample as gs


def _like(author="did:plc:liker00000000000000000", subject_author="did:plc:author0000000000000000"):
    return {
        "at_uri": f"at://{author}/app.bsky.feed.like/3abcdefghijkl",
        "subject_uri": f"at://{subject_author}/app.bsky.feed.post/3zyxwvutsrqpo",
        "author_did": author,
        "created_at": "2026-07-22T10:00:00Z",
        "indexed_at": "2026-07-22T10:00:01Z",
    }


# --------------------------------------------------------------------------
# embedding codec
#
# Cross-language contract: ingex's Go decoder reads these strings. A
# round-trip against our own encoder would pass even if the format were
# wrong, so decode explicitly the way the Go side documents it —
# base85 (RFC 1924) -> zlib -> little-endian float32.
# --------------------------------------------------------------------------


def test_embedding_encodes_in_the_format_the_go_decoder_expects():
    vector = [0.5, -0.25, 0.125, 0.0]
    encoded = gs.encode_embedding(vector)

    raw = zlib.decompress(base64.b85decode(encoded))
    decoded = list(struct.unpack(f"<{len(raw) // 4}f", raw))

    assert decoded == vector


def test_embedding_round_trips_at_realistic_size_and_precision():
    vector = [i / 997.0 - 0.5 for i in range(384)]
    raw = zlib.decompress(base64.b85decode(gs.encode_embedding(vector)))
    decoded = struct.unpack(f"<{len(raw) // 4}f", raw)

    assert len(decoded) == 384
    # float32 storage, so compare with tolerance rather than exactly.
    assert all(abs(a - b) < 1e-6 for a, b in zip(decoded, vector, strict=True))


# --------------------------------------------------------------------------
# timestamps
# --------------------------------------------------------------------------


def test_parse_iso_treats_naive_timestamps_as_utc():
    assert gs.parse_iso("2026-07-22T10:00:00").tzinfo == dt.UTC


def test_parse_iso_accepts_both_z_and_offset_forms():
    assert gs.parse_iso("2026-07-22T10:00:00Z") == gs.parse_iso("2026-07-22T10:00:00+00:00")


def test_iso_z_emits_z_suffix_not_offset():
    # ES index templates declare `format: iso8601`; keep output shaped like the
    # timestamps ingest already writes.
    out = gs.iso_z(dt.datetime(2026, 7, 22, 10, 0, tzinfo=dt.UTC))
    assert out == "2026-07-22T10:00:00Z"


def test_iso_z_converts_other_zones_to_utc():
    tz = dt.timezone(dt.timedelta(hours=-7))
    out = gs.iso_z(dt.datetime(2026, 7, 22, 3, 0, tzinfo=tz))
    assert out == "2026-07-22T10:00:00Z"


# --------------------------------------------------------------------------
# raw_post construction — megastream_ingest parses this shape
# --------------------------------------------------------------------------


def _built_post(text="hello world", quote=None):
    created = dt.datetime(2026, 7, 22, 10, 0, tzinfo=dt.UTC)
    raw = gs.build_raw_post(
        "at://did:plc:author0000000000000000/app.bsky.feed.post/3abcdefghijkl",
        "did:plc:author0000000000000000",
        text,
        created,
        quote,
    )
    return json.loads(raw)


def test_raw_post_carries_the_fields_ingest_reads():
    post = _built_post()
    commit = post["message"]["commit"]
    assert commit["operation"] == "create"
    assert commit["collection"] == "app.bsky.feed.post"
    assert commit["record"]["text"] == "hello world"
    assert commit["record"]["createdAt"] == "2026-07-22T10:00:00Z"


def test_raw_post_time_us_matches_created_at():
    post = _built_post()
    expected = int(dt.datetime(2026, 7, 22, 10, 0, tzinfo=dt.UTC).timestamp() * 1_000_000)
    assert post["message"]["time_us"] == expected
    assert post["time_us"] == expected


def test_raw_post_rkey_matches_the_at_uri():
    post = _built_post()
    assert post["message"]["commit"]["rkey"] == "3abcdefghijkl"


def test_quote_post_is_exposed_where_ingest_looks_for_it():
    quote = "at://did:plc:other000000000000000000/app.bsky.feed.post/3qqqqqqqqqqqq"
    post = _built_post(quote=quote)
    assert post["hydrated_metadata"]["quote_post"]["uri"] == quote


def test_hydrated_metadata_is_omitted_when_there_is_no_quote():
    assert "hydrated_metadata" not in _built_post()


# --------------------------------------------------------------------------
# megastream chunk writing
# --------------------------------------------------------------------------


def _posts(count, start=dt.datetime(2026, 7, 22, 10, 0, tzinfo=dt.UTC)):
    out = []
    for i in range(count):
        created = start + dt.timedelta(seconds=i)
        uri = f"at://did:plc:author0000000000000000/app.bsky.feed.post/3post{i:08d}"
        out.append(
            {
                "at_uri": uri,
                "did": "did:plc:author0000000000000000",
                "created_at": created,
                "raw_post": gs.build_raw_post(
                    uri, "did:plc:author0000000000000000", "x", created, None
                ),
                "inferences": json.dumps({"text_embeddings": {gs.EMBED_MODEL: "abc"}}),
            }
        )
    return out


def test_chunk_filenames_are_ordered_and_parseable(tmp_path):
    # The spooler discovers and orders files by the timestamp in the filename,
    # so collisions or out-of-order names silently drop data.
    names = gs.write_megastream_chunks(tmp_path, _posts(5))
    assert names == sorted(names)
    for name in names:
        # Must match ingex's ParseMegastreamFilenameTimestamp regex —
        # ^mega_jetstream_(\d{8})_(\d{6})\.db\.zip$ — or the spooler skips it.
        match = re.match(r"^mega_jetstream_(\d{8})_(\d{6})\.db\.zip$", name)
        assert match, name
        dt.datetime.strptime(match[1] + match[2], "%Y%m%d%H%M%S")


def test_chunks_hold_every_post_with_the_schema_ingest_queries(tmp_path):
    gs.write_megastream_chunks(tmp_path, _posts(3))
    (zip_path,) = list(tmp_path.glob("*.db.zip"))

    with zipfile.ZipFile(zip_path) as zf:
        (member,) = [m for m in zf.namelist() if m.endswith(".db")]
        db_bytes = zf.read(member)
    db_file = tmp_path / "extracted.db"
    db_file.write_bytes(db_bytes)

    conn = sqlite3.connect(db_file)
    try:
        rows = conn.execute(
            "SELECT at_uri, did, time_us, raw_post, inferences FROM enriched_posts ORDER BY time_us"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 3
    assert all(json.loads(row[3])["message"]["commit"]["operation"] == "create" for row in rows)


def test_posts_are_split_across_chunks_at_the_row_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(gs, "CHUNK_ROWS", 2)
    names = gs.write_megastream_chunks(tmp_path, _posts(5))
    assert len(names) == 3


def test_identical_timestamps_do_not_collide_into_one_filename(tmp_path, monkeypatch):
    # Two chunks whose last post shares a second would otherwise produce the
    # same filename and one would overwrite the other.
    monkeypatch.setattr(gs, "CHUNK_ROWS", 2)
    same = dt.datetime(2026, 7, 22, 10, 0, tzinfo=dt.UTC)
    posts = _posts(4, start=same)
    for post in posts:
        post["created_at"] = same
    names = gs.write_megastream_chunks(tmp_path, posts)
    assert len(names) == len(set(names))


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------


def test_liker_identities_are_written_through_unchanged(tmp_path):
    # Fixtures deliberately carry real liker DIDs (see the generate_sample
    # docstring): the follow-driven generators and Bluesky API lookups only
    # work if these resolve to actual accounts.
    like = _like(author="did:plc:gzgq3r5wddxsorpmu4tauxo7")
    gs.write_fixture_set(
        tmp_path,
        "prod-es",
        _posts(1),
        [like],
        [],
        [],
        dt.datetime(2026, 7, 22, tzinfo=dt.UTC),
        dt.datetime(2026, 7, 23, tzinfo=dt.UTC),
        0,
    )
    written = json.loads(gzip.decompress((tmp_path / "likes.jsonl.gz").read_bytes()).decode())
    assert written["author_did"] == "did:plc:gzgq3r5wddxsorpmu4tauxo7"
    assert written["at_uri"] == like["at_uri"]


def test_manifest_counts_match_the_written_data(tmp_path):
    posts, likes = _posts(3), [_like(), _like()]
    gs.write_fixture_set(
        tmp_path,
        "prod-es",
        posts,
        likes,
        [],
        [{"did": "did:plc:x", "likes": 2}],
        dt.datetime(2026, 7, 22, tzinfo=dt.UTC),
        dt.datetime(2026, 7, 23, tzinfo=dt.UTC),
        7,
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["counts"]["posts"] == 3
    assert manifest["counts"]["likes"] == 2
    assert manifest["counts"]["cohort"] == 7
    # seed/rebase.py reads window_end to compute the shift.
    assert manifest["window_end"] == "2026-07-23T00:00:00Z"


def test_regenerating_into_a_directory_clears_old_chunks(tmp_path, monkeypatch):
    # Leftover chunks from a previous, larger sample would be ingested too,
    # mixing two unrelated post/like graphs.
    monkeypatch.setattr(gs, "CHUNK_ROWS", 2)
    args = (
        dt.datetime(2026, 7, 22, tzinfo=dt.UTC),
        dt.datetime(2026, 7, 23, tzinfo=dt.UTC),
        0,
    )
    gs.write_fixture_set(tmp_path, "prod-es", _posts(6), [], [], [], *args)
    gs.write_fixture_set(tmp_path, "prod-es", _posts(2), [], [], [], *args)

    assert len(list(tmp_path.glob("*.db.zip"))) == 1
