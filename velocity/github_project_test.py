from datetime import UTC, datetime

from . import github_project


def _node(field_values, content):
    return {
        "id": "PVTI_x",
        "fieldValues": {"nodes": field_values},
        "content": content,
    }


def test_parse_item_extracts_fields():
    # Points is a single-select; type comes from the issue's native issueType.
    node = _node(
        field_values=[
            {"field": {"name": "Status"}, "name": "Backlog"},
            {"field": {"name": "Points"}, "name": "5"},
        ],
        content={
            "title": "Do a thing",
            "url": "https://x/1",
            "closedAt": None,
            "issueType": {"name": "Bug"},
        },
    )
    item = github_project._parse_item(node)
    assert item.status == "Backlog"
    assert item.raw_type == "Bug"
    assert item.raw_points == 5.0
    assert item.title == "Do a thing"
    assert item.closed_at is None


def test_parse_item_reads_number_field_fallback_and_closed_at():
    node = _node(
        field_values=[{"field": {"name": "Estimate"}, "number": 3}],
        content={"title": "t", "url": None, "closedAt": "2026-06-16T10:00:00Z"},
    )
    item = github_project._parse_item(node)
    assert item.raw_points == 3.0
    assert item.closed_at == datetime(2026, 6, 16, 10, 0, tzinfo=UTC)


def test_parse_item_null_issue_type_is_none():
    node = _node(
        field_values=[{"field": {"name": "Points"}, "name": "2"}],
        content={"title": "t", "url": None, "closedAt": None, "issueType": None},
    )
    item = github_project._parse_item(node)
    assert item.raw_type is None
    assert item.raw_points == 2.0


def test_parse_item_reads_state_reason():
    node = _node(
        field_values=[],
        content={"title": "t", "url": None, "closedAt": None, "stateReason": "DUPLICATE"},
    )
    assert github_project._parse_item(node).state_reason == "DUPLICATE"
    # absent stateReason (e.g. PR/draft) -> None
    node2 = _node(field_values=[], content={"title": "t", "url": None})
    assert github_project._parse_item(node2).state_reason is None


def test_parse_item_skips_items_without_content():
    assert github_project._parse_item(_node([], content=None)) is None


def test_parse_item_handles_missing_fields():
    node = _node([], content={"title": "bare", "url": None})
    item = github_project._parse_item(node)
    assert item.status is None
    assert item.raw_type is None
    assert item.raw_points is None
