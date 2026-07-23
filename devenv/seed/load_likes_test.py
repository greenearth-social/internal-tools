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
