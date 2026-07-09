from datetime import date, datetime, timezone

from .conftest import NOW, make_item
from . import burndown


def _backlog(points_, **kw):
    return make_item(status="Backlog", raw_points=points_, **kw)


def test_completed_history_zero_filled_and_chronological():
    items = [
        make_item(status="Done", closed_at=datetime(2026, 6, 16, tzinfo=timezone.utc), raw_points=4),
    ]
    hist = burndown.completed_history(items, weeks_back=3, now=NOW)
    assert hist == [
        (date(2026, 6, 8), 0),
        (date(2026, 6, 15), 4),
        (date(2026, 6, 22), 0),
    ]


def test_backlog_total_only_counts_backlog_status():
    items = [
        _backlog(3),
        _backlog(None),  # unpointed -> 2
        _backlog(8, raw_type="bug"),  # bug -> 0
        make_item(status="Done", raw_points=99),  # not backlog
    ]
    assert burndown.backlog_total(items) == 3 + 2 + 0


def test_project_burndown_counts_down_to_zero():
    items = [_backlog(5), _backlog(5)]  # total 10
    series = burndown.project_burndown(items, velocity=5, now=NOW)
    assert series == [
        (date(2026, 6, 22), 10.0),
        (date(2026, 6, 29), 5.0),
        (date(2026, 7, 6), 0.0),
    ]


def test_project_burndown_handles_zero_velocity():
    items = [_backlog(5)]
    series = burndown.project_burndown(items, velocity=0, now=NOW)
    assert series == [(date(2026, 6, 22), 5.0)]


def test_assign_tasks_to_weeks_fills_by_capacity():
    items = [_backlog(3, id="a"), _backlog(3, id="b"), _backlog(3, id="c")]
    assigned = burndown.assign_tasks_to_weeks(items, velocity=5, now=NOW)
    weeks = [wk for wk, _ in assigned]
    assert weeks == [date(2026, 6, 22), date(2026, 6, 29), date(2026, 6, 29)]


def test_upcoming_weeks_windows_and_groups():
    items = [_backlog(5, id="a"), _backlog(5, id="b"), _backlog(5, id="c"), _backlog(5, id="d")]
    assignments = burndown.assign_tasks_to_weeks(items, velocity=5, now=NOW)
    grouped = burndown.upcoming_weeks(assignments, weeks=2, now=NOW)
    weeks = [wk for wk, _ in grouped]
    ids = [[it.id for it in items_] for _, items_ in grouped]
    # Only the first 2 weeks are returned; tasks c & d (weeks 3-4) are dropped.
    assert weeks == [date(2026, 6, 22), date(2026, 6, 29)]
    assert ids == [["a"], ["b"]]


def test_upcoming_weeks_includes_empty_weeks_in_window():
    items = [_backlog(3, id="only")]
    assignments = burndown.assign_tasks_to_weeks(items, velocity=5, now=NOW)
    grouped = burndown.upcoming_weeks(assignments, weeks=3, now=NOW)
    assert [len(items_) for _, items_ in grouped] == [1, 0, 0]


def test_assign_tasks_zero_point_bug_does_not_consume_capacity():
    items = [
        _backlog(None, id="bug", raw_type="bug"),  # 0 pts, rides current week
        _backlog(5, id="x"),
        _backlog(5, id="y"),
    ]
    assigned = burndown.assign_tasks_to_weeks(items, velocity=5, now=NOW)
    by_id = {item.id: wk for wk, item in assigned}
    assert by_id["bug"] == date(2026, 6, 22)
    assert by_id["x"] == date(2026, 6, 22)
    assert by_id["y"] == date(2026, 6, 29)
