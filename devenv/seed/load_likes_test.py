from datetime import UTC

import load_likes
import pytest

# --------------------------------------------------------------------------
# bulk response handling
#
# A silently-ignored bulk failure would leave the environment partially
# seeded and looking fine, which is worse than a loud failure.
# --------------------------------------------------------------------------


def test_clean_response_reports_no_failures():
    assert load_likes.check_bulk_response({"errors": False, "items": []}) == 0


def test_errors_flag_false_short_circuits_even_with_items():
    resp = {"errors": False, "items": [{"index": {"error": {"type": "whatever"}}}]}
    assert load_likes.check_bulk_response(resp) == 0


def test_unexpected_bulk_error_is_fatal():
    resp = {
        "errors": True,
        "items": [{"index": {"error": {"type": "mapper_parsing_exception", "reason": "bad"}}}],
    }
    with pytest.raises(SystemExit):
        load_likes.check_bulk_response(resp)


def test_missing_document_is_fatal_by_default():
    # Only the like-count pass knows missing posts are acceptable; everything
    # else should still treat them as real errors.
    resp = {
        "errors": True,
        "items": [{"update": {"error": {"type": "document_missing_exception"}}}],
    }
    with pytest.raises(SystemExit):
        load_likes.check_bulk_response(resp)


def test_missing_document_is_counted_not_fatal_when_allowed():
    # Posts skipped by ingest legitimately have no document to update; the
    # like-count pass reports them instead of aborting the seed.
    resp = {
        "errors": True,
        "items": [
            {"update": {"error": {"type": "document_missing_exception"}}},
            {"update": {}},
        ],
    }
    assert load_likes.check_bulk_response(resp, allow_missing=True) == 1


def test_other_errors_stay_fatal_even_when_missing_is_allowed():
    resp = {
        "errors": True,
        "items": [
            {"update": {"error": {"type": "document_missing_exception"}}},
            {"update": {"error": {"type": "version_conflict_engine_exception"}}},
        ],
    }
    with pytest.raises(SystemExit):
        load_likes.check_bulk_response(resp, allow_missing=True)


def test_failure_count_covers_every_failed_item():
    resp = {
        "errors": True,
        "items": [{"update": {"error": {"type": "document_missing_exception"}}}] * 5,
    }
    assert load_likes.check_bulk_response(resp, allow_missing=True) == 5


def test_index_and_update_items_are_both_inspected():
    resp = {
        "errors": True,
        "items": [{"index": {"error": {"type": "document_missing_exception"}}}],
    }
    assert load_likes.check_bulk_response(resp, allow_missing=True) == 1


# --------------------------------------------------------------------------
# alias wiring
#
# posts-quality-* also matches the posts-* pattern, so a careless alias action
# would surface every quality post twice through posts_recent
# (greenearth-social/ingex#442). Elasticsearch also rejects a negative wildcard
# when the excluded index doesn't exist, so actions use resolved concrete names.
# --------------------------------------------------------------------------


def _capture_requests(monkeypatch, cat_result):
    calls = []

    def fake_request(method, path, body=None, ndjson=False):
        calls.append((method, path, body))
        if path.startswith("/_cat/indices/posts-"):
            return cat_result
        if path.endswith("/_count"):
            return {"count": 0}
        return {}

    monkeypatch.setattr(load_likes, "request", fake_request)
    return calls


def _alias_actions(calls):
    import json

    for method, path, body in calls:
        if method == "POST" and path == "/_aliases":
            return json.loads(body)["actions"]
    raise AssertionError(f"no /_aliases call in {calls}")


def test_posts_recent_excludes_the_quality_indexes(monkeypatch):
    calls = _capture_requests(
        monkeypatch,
        [
            {"index": "posts-quality-2026-w32"},
            {"index": "posts-2026-w32"},
            {"index": "posts-2026-w31"},
        ],
    )
    load_likes.update_posts_recent()

    actions = _alias_actions(calls)
    recent = next(a["add"] for a in actions if a["add"]["alias"] == "posts_recent")
    assert recent["indices"] == ["posts-2026-w31", "posts-2026-w32"]


def test_quality_alias_added_when_the_corpus_exists(monkeypatch):
    calls = _capture_requests(
        monkeypatch,
        [{"index": "posts-2026-w32"}, {"index": "posts-quality-2026-w32"}],
    )
    load_likes.update_posts_recent()

    actions = _alias_actions(calls)
    aliases = {a["add"]["alias"] for a in actions}
    assert aliases == {"posts_recent", "posts_recent_quality"}
    quality = next(a["add"] for a in actions if a["add"]["alias"] == "posts_recent_quality")
    assert quality["indices"] == ["posts-quality-2026-w32"]


def test_quality_alias_skipped_when_the_corpus_is_empty(monkeypatch):
    # A fresh environment has not run ingex's backfill_quality_index yet;
    # aliasing posts-quality-* would fail with index_not_found_exception.
    calls = _capture_requests(monkeypatch, [{"index": "posts-2026-w32"}])
    load_likes.update_posts_recent()

    aliases = {a["add"]["alias"] for a in _alias_actions(calls)}
    assert aliases == {"posts_recent"}


def test_missing_regular_posts_is_fatal(monkeypatch):
    calls = _capture_requests(monkeypatch, [{"index": "posts-quality-2026-w32"}])

    with pytest.raises(SystemExit, match="no posts-\\* indexes"):
        load_likes.update_posts_recent()

    assert all(path != "/_aliases" for _, path, _ in calls)


def test_empty_quality_backfill_publishes_a_queryable_alias(monkeypatch):
    from datetime import datetime

    class FrozenDateTime:
        @staticmethod
        def now(tz):
            assert tz is UTC
            # ISO week belongs to the previous year at this boundary.
            return datetime(2027, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(load_likes, "datetime", FrozenDateTime)
    calls = _capture_requests(monkeypatch, [{"index": "posts-2026-w53"}])
    load_likes.main(["--aliases-only"])

    assert ("PUT", "/posts-quality-2026-w53", b"{}") in calls
    actions = _alias_actions(calls)
    quality = next(a["add"] for a in actions if a["add"]["alias"] == "posts_recent_quality")
    assert quality["indices"] == ["posts-quality-2026-w53"]
    paths = [path for _, path, _ in calls]
    assert paths.index("/posts-quality-2026-w53") < paths.index("/_aliases")
    assert paths.index("/_aliases") < paths.index("/posts_recent_quality/_refresh")
    assert paths.index("/posts_recent_quality/_refresh") < paths.index(
        "/posts_recent_quality/_count"
    )


def test_aliases_only_refreshes_existing_quality_corpus_without_reloading_likes(monkeypatch):
    def must_not_reload():
        pytest.fail("final alias publication must not reload likes or like counts")

    monkeypatch.setattr(load_likes, "load_likes", must_not_reload)
    monkeypatch.setattr(load_likes, "apply_like_counts", must_not_reload)
    calls = _capture_requests(
        monkeypatch,
        [{"index": "posts-2026-w32"}, {"index": "posts-quality-2026-w32"}],
    )
    load_likes.main(["--aliases-only"])

    assert not any(method == "PUT" for method, _, _ in calls)
    actions = _alias_actions(calls)
    quality = next(a["add"] for a in actions if a["add"]["alias"] == "posts_recent_quality")
    assert quality["indices"] == ["posts-quality-2026-w32"]
    assert ("POST", "/posts_recent_quality/_refresh", None) in calls
    assert ("GET", "/posts_recent_quality/_count", None) in calls


def test_like_counts_and_regular_posts_are_visible_before_backfill(monkeypatch):
    events = []
    monkeypatch.setattr(load_likes, "load_likes", lambda: events.append("likes"))
    monkeypatch.setattr(load_likes, "apply_like_counts", lambda: events.append("counts"))

    def fake_request(method, path, body=None, ndjson=False):
        events.append(path)
        if path.startswith("/_cat/indices/posts-"):
            return [{"index": "posts-2026-w32"}]
        if path == "/_aliases":
            import json

            assert json.loads(body)["actions"] == [
                {"add": {"indices": ["posts-2026-w32"], "alias": "posts_recent"}}
            ]
        if path.endswith("/_count"):
            return {"count": 1}
        return {}

    monkeypatch.setattr(load_likes, "request", fake_request)
    load_likes.main([])

    assert events.index("counts") < events.index("/_aliases") < events.index("/posts/_refresh")
