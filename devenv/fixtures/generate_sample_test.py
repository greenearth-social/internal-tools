import argparse
import base64
import datetime as dt
import email.message
import gzip
import io
import json
import re
import sqlite3
import struct
import zipfile
import zlib

import generate_sample as gs
import pytest


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


# --------------------------------------------------------------------------
# ES transport retries
#
# A prod-es run is tens of minutes of requests over a kubectl port-forward,
# which drops connections routinely. These guard that a blip costs a retry
# rather than the whole run.
# --------------------------------------------------------------------------


def _es_client(monkeypatch):
    monkeypatch.setenv("GE_ELASTICSEARCH_URL", "https://localhost:9200")
    monkeypatch.setenv("GE_ELASTICSEARCH_API_KEY", "k")
    return gs.EsClient()


def test_search_retries_a_dropped_connection_and_succeeds(monkeypatch):
    client = _es_client(monkeypatch)
    monkeypatch.setattr(gs.time, "sleep", lambda _: None)
    calls = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"hits": {"hits": []}}'

    def fake_urlopen(*_a, **_k):
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionResetError("connection reset by peer")
        return _Resp()

    monkeypatch.setattr(gs.urllib.request, "urlopen", fake_urlopen)
    assert client.search("posts", {}) == {"hits": {"hits": []}}
    assert len(calls) == 3


def test_search_gives_up_after_max_attempts(monkeypatch):
    client = _es_client(monkeypatch)
    monkeypatch.setattr(gs.time, "sleep", lambda _: None)

    def always_reset(*_a, **_k):
        raise ConnectionResetError("connection reset by peer")

    monkeypatch.setattr(gs.urllib.request, "urlopen", always_reset)
    with pytest.raises(SystemExit) as exc:
        client.search("posts", {})
    assert "after" in str(exc.value)


def test_search_does_not_retry_a_client_error(monkeypatch):
    # A 4xx is a real answer (bad key, bad query); retrying repeats the same
    # mistake slowly instead of failing fast.
    client = _es_client(monkeypatch)
    monkeypatch.setattr(gs.time, "sleep", lambda _: None)
    calls = []

    def unauthorized(*_a, **_k):
        calls.append(1)
        raise gs.urllib.error.HTTPError(
            "u", 401, "Unauthorized", email.message.Message(), io.BytesIO(b"nope")
        )

    monkeypatch.setattr(gs.urllib.request, "urlopen", unauthorized)
    with pytest.raises(SystemExit):
        client.search("posts", {})
    assert len(calls) == 1


def test_search_retries_a_gateway_error(monkeypatch):
    client = _es_client(monkeypatch)
    monkeypatch.setattr(gs.time, "sleep", lambda _: None)
    calls = []

    def bad_gateway(*_a, **_k):
        calls.append(1)
        raise gs.urllib.error.HTTPError(
            "u", 503, "Unavailable", email.message.Message(), io.BytesIO(b"busy")
        )

    monkeypatch.setattr(gs.urllib.request, "urlopen", bad_gateway)
    with pytest.raises(SystemExit):
        client.search("posts", {})
    assert len(calls) == gs.ES_MAX_ATTEMPTS


# --------------------------------------------------------------------------
# dev-team accounts
#
# A fixture whose like graph doesn't contain the team's own DIDs gives an
# empty, unpersonalized feed to anyone who signs into the devenv frontend as
# themselves (internal-tools#22). These cover the two ways that regresses
# quietly: dev likes never being fetched, and dev likes being fetched but
# then discarded because their subjects lost the hydration cap.
# --------------------------------------------------------------------------


def _dev_users_file(tmp_path, users):
    path = tmp_path / "dev_users.json"
    path.write_text(json.dumps({"users": users}))
    return path


def test_dev_users_load_with_their_checked_in_dids_and_no_network(tmp_path, monkeypatch):
    def no_network(*_a, **_k):
        raise AssertionError("resolving should not happen when the did is already recorded")

    monkeypatch.setattr(gs.urllib.request, "urlopen", no_network)
    path = _dev_users_file(
        tmp_path,
        [
            {"handle": "a.bsky.social", "did": "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa"},
            {"handle": "b.bsky.social", "did": "did:plc:bbbbbbbbbbbbbbbbbbbbbbbb"},
        ],
    )

    users = gs.load_dev_users(path)

    assert [u["did"] for u in users] == [
        "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa",
        "did:plc:bbbbbbbbbbbbbbbbbbbbbbbb",
    ]
    assert users[0]["handle"] == "a.bsky.social"


def test_dev_user_with_only_a_handle_is_resolved(tmp_path, monkeypatch):
    # Adding a teammate should take one line, not a manual DID lookup.
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"did": "did:plc:cccccccccccccccccccccccc"}'

    monkeypatch.setattr(gs.urllib.request, "urlopen", lambda *_a, **_k: _Resp())
    path = _dev_users_file(tmp_path, [{"handle": "new.bsky.social"}])

    assert gs.load_dev_users(path) == [
        {"handle": "new.bsky.social", "did": "did:plc:cccccccccccccccccccccccc"}
    ]


def test_dev_user_that_cannot_be_resolved_fails_the_run(tmp_path, monkeypatch):
    def unreachable(*_a, **_k):
        raise gs.urllib.error.URLError("no route to host")

    monkeypatch.setattr(gs.urllib.request, "urlopen", unreachable)
    path = _dev_users_file(tmp_path, [{"handle": "gone.bsky.social"}])

    with pytest.raises(SystemExit) as exc:
        gs.load_dev_users(path)
    assert "gone.bsky.social" in str(exc.value)


def test_duplicate_dev_dids_are_collapsed(tmp_path):
    path = _dev_users_file(
        tmp_path,
        [
            {"handle": "old.bsky.social", "did": "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa"},
            {"handle": "renamed.bsky.social", "did": "did:plc:aaaaaaaaaaaaaaaaaaaaaaaa"},
        ],
    )

    assert len(gs.load_dev_users(path)) == 1


def test_empty_dev_user_list_fails_rather_than_silently_skipping(tmp_path):
    # Silently generating a fixture with no dev users is the bug, not a mode.
    with pytest.raises(SystemExit):
        gs.load_dev_users(_dev_users_file(tmp_path, []))


def test_the_checked_in_dev_user_list_is_usable():
    users = gs.load_dev_users(gs.DEV_USERS_FILE)

    assert len(users) >= 1
    assert all(u["did"].startswith("did:plc:") for u in users)


def test_dev_users_are_recorded_in_the_manifest(tmp_path):
    gs.write_fixture_set(
        tmp_path,
        "prod-es",
        _posts(1),
        [_like()],
        [],
        [{"did": "did:plc:persona", "likes": 9}],
        dt.datetime(2026, 7, 22, tzinfo=dt.UTC),
        dt.datetime(2026, 7, 23, tzinfo=dt.UTC),
        1,
        [{"handle": "a.bsky.social", "did": "did:plc:aaaa", "likes": 4}],
    )

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["dev_users"] == [{"handle": "a.bsky.social", "did": "did:plc:aaaa", "likes": 4}]


def test_summarize_dev_users_reports_accounts_with_no_history(capsys):
    rows = gs.summarize_dev_users(
        [
            {"handle": "active.bsky.social", "did": "did:plc:active"},
            {"handle": "quiet.bsky.social", "did": "did:plc:quiet"},
        ],
        gs.Counter({"did:plc:active": 3}),
    )

    assert rows == [
        {"handle": "active.bsky.social", "did": "did:plc:active", "likes": 3},
        {"handle": "quiet.bsky.social", "did": "did:plc:quiet", "likes": 0},
    ]
    # An account with no likes gets an unpersonalized feed weeks later, when
    # nobody remembers how the fixture was built. Say it now.
    assert "quiet.bsky.social" in capsys.readouterr().out


# --- prod-es sampling, against a fake cluster -----------------------------

DEV_DID = "did:plc:devuser00000000000000000"
DEV_USERS = [{"handle": "dev.bsky.social", "did": DEV_DID}]
WINDOW_END = dt.datetime(2026, 7, 23, tzinfo=dt.UTC)


def _es_post(uri, created, like_count=0):
    return {
        "at_uri": uri,
        "author_did": "did:plc:author0000000000000000",
        "content": "hello",
        "created_at": gs.iso_z(created),
        "like_count": like_count,
        "embeddings": {"all_MiniLM_L12_v2": [0.1] * 4},
    }


def _es_like(author, subject, created):
    return {
        "at_uri": f"at://{author}/app.bsky.feed.like/{subject.rsplit('/', 1)[-1]}",
        "subject_uri": subject,
        "author_did": author,
        "created_at": gs.iso_z(created),
        "indexed_at": gs.iso_z(created),
    }


def _matches(doc, query):
    for clause in query["bool"]["filter"]:
        if "range" in clause:
            field, bounds = next(iter(clause["range"].items()))
            value = gs.parse_iso(doc[field])
            if value < gs.parse_iso(bounds["gte"]) or value >= gs.parse_iso(bounds["lt"]):
                return False
        elif "exists" in clause:
            if not doc.get("embeddings"):
                return False
        elif "terms" in clause:
            field, values = next(iter(clause["terms"].items()))
            if doc.get(field) not in values:
                return False
        else:
            raise AssertionError(f"unhandled clause {clause}")
    return True


class _FakeEs:
    """Just enough Elasticsearch to run run_prod_es end to end."""

    def __init__(self, posts, likes):
        self.posts, self.likes = posts, likes

    def _docs(self, index):
        return self.posts if index == "posts" else self.likes

    def search(self, index, body):
        if body.get("size") == 0 and "aggs" in body:
            terms = body["aggs"]["likers"]["terms"]
            counts = gs.Counter(
                like["author_did"] for like in self.likes if _matches(like, body["query"])
            )
            return {
                "aggregations": {
                    "likers": {
                        "buckets": [
                            {"key": did, "doc_count": n}
                            for did, n in counts.most_common(terms["size"])
                            if n >= terms["min_doc_count"]
                        ]
                    }
                }
            }
        hits = [d for d in self._docs(index) if _matches(d, body["query"])]
        return {"hits": {"hits": [{"_source": d} for d in hits[: body["size"]]]}}

    def scan(self, index, query, source, page_size=1000):
        for doc in self._docs(index):
            if _matches(doc, query):
                yield {"_source": doc}


def _prod_es_args(**overrides):
    defaults = {
        "window_end": gs.iso_z(WINDOW_END),
        "window_hours": 1,
        # Small enough that the dangler-hydration cap (max_posts // 2) bites,
        # which is where dev-team likes get silently dropped.
        "max_posts": 4,
        "min_cohort_likes": 1,
        "max_cohort": 300,
        "dev_user_lookback_days": 30,
        "personas": 5,
    }
    return argparse.Namespace(**{**defaults, **overrides})


def _fake_cluster():
    """One in-window post, three popular danglers, one old dev-only dangler."""
    in_window = _es_post(
        "at://a/app.bsky.feed.post/inwindow", WINDOW_END - dt.timedelta(minutes=30)
    )
    danglers = [
        _es_post(f"at://a/app.bsky.feed.post/dangler{i}", WINDOW_END - dt.timedelta(days=2))
        for i in range(3)
    ]
    dev_subject = _es_post(
        "at://a/app.bsky.feed.post/devsubject", WINDOW_END - dt.timedelta(days=25)
    )

    likes = []
    for i in range(3):  # cohort likers, most popular dangler first
        liker = f"did:plc:liker{i}0000000000000000000"
        for dangler in danglers[: 3 - i]:
            likes.append(_es_like(liker, dangler["at_uri"], WINDOW_END - dt.timedelta(minutes=10)))
    # The dev account liked one post, 25 days ago — outside the sample window
    # and unpopular, so both the window filter and the cap would lose it.
    likes.append(_es_like(DEV_DID, dev_subject["at_uri"], WINDOW_END - dt.timedelta(days=25)))

    return _FakeEs([in_window, *danglers, dev_subject], likes)


def _run_prod_es(tmp_path, monkeypatch, dev_users, **overrides):
    fake = _fake_cluster()
    monkeypatch.setattr(gs, "EsClient", lambda: fake)
    gs.run_prod_es(_prod_es_args(**overrides), tmp_path, dev_users)
    return json.loads((tmp_path / "manifest.json").read_text()), [
        json.loads(line)
        for line in gzip.decompress((tmp_path / "likes.jsonl.gz").read_bytes())
        .decode()
        .splitlines()
    ]


def test_dev_user_history_survives_sampling(tmp_path, monkeypatch):
    manifest, likes = _run_prod_es(tmp_path, monkeypatch, DEV_USERS)

    # The like is older than the sample window and its subject is the least
    # popular dangler — it only survives if dev likes get their own lookback
    # and jump the hydration queue.
    assert [like["author_did"] for like in likes].count(DEV_DID) == 1
    assert manifest["dev_users"] == [{"handle": "dev.bsky.social", "did": DEV_DID, "likes": 1}]


def test_dev_users_do_not_take_persona_slots(tmp_path, monkeypatch):
    # The probe persona should be the densest history in the fixture, not
    # whichever of us liked the most posts last month.
    manifest, _ = _run_prod_es(tmp_path, monkeypatch, DEV_USERS)

    assert DEV_DID not in [persona["did"] for persona in manifest["personas"]]
    assert manifest["personas"]


def test_no_dev_users_leaves_the_sample_untouched(tmp_path, monkeypatch):
    manifest, likes = _run_prod_es(tmp_path, monkeypatch, [])

    assert DEV_DID not in [like["author_did"] for like in likes]
    assert manifest["dev_users"] == []


# --- megastream-files mode ------------------------------------------------


def test_megastream_mode_gives_dev_users_a_synthesized_history(tmp_path, monkeypatch):
    # The offline path invents its whole like graph, so dev accounts get an
    # invented history too — logging in as yourself has to work here as well.
    monkeypatch.setattr(gs, "read_megastream_posts", lambda _: _posts(20))
    args = argparse.Namespace(
        input=str(tmp_path),
        window_hours=48,
        cohort=3,
        min_likes=2,
        max_likes=4,
        seed=1,
        personas=5,
    )

    gs.run_megastream_files(args, tmp_path, DEV_USERS)

    manifest = json.loads((tmp_path / "manifest.json").read_text())
    (dev_row,) = manifest["dev_users"]
    assert dev_row["did"] == DEV_DID
    assert dev_row["likes"] >= args.min_likes
    assert DEV_DID not in [persona["did"] for persona in manifest["personas"]]
