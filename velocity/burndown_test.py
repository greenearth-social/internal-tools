from datetime import UTC, date, datetime

from . import burndown
from .conftest import NOW, make_item


def _backlog(points_, **kw):
    return make_item(status="Backlog", raw_points=points_, **kw)


def test_completed_history_spans_to_last_completed_week():
    # NOW = 2026-06-24 (Wed) -> current week 06-22, last completed week 06-15.
    items = [
        make_item(status="Done", closed_at=datetime(2026, 6, 16, tzinfo=UTC), raw_points=4),
    ]
    hist = burndown.completed_history(items, min_weeks=3, now=NOW)
    # ends at 06-15 (current partial week 06-22 excluded), zero-filled back min_weeks
    assert hist == [
        (date(2026, 6, 1), 0),
        (date(2026, 6, 8), 0),
        (date(2026, 6, 15), 4),
    ]


def test_completed_history_extends_back_for_older_data():
    items = [
        make_item(status="Done", closed_at=datetime(2026, 4, 20, tzinfo=UTC), raw_points=2),
        make_item(status="Done", closed_at=datetime(2026, 6, 16, tzinfo=UTC), raw_points=4),
    ]
    hist = burndown.completed_history(items, min_weeks=3, now=NOW)
    weeks = [wk for wk, _ in hist]
    # spans from the older data week (04-20) through the last completed week (06-15)
    assert weeks[0] == date(2026, 4, 20)
    assert weeks[-1] == date(2026, 6, 15)
    assert dict(hist)[date(2026, 4, 20)] == 2
    assert dict(hist)[date(2026, 6, 15)] == 4


def test_backlog_total_counts_unified_view_statuses():
    items = [
        _backlog(3),
        _backlog(None),  # unpointed -> 2
        _backlog(8, raw_type="bug"),  # bug -> 0
        make_item(status="In Progress", raw_points=5),  # in unified backlog
        make_item(status="On Hold", raw_points=1),  # in unified backlog
        make_item(status="Done", raw_points=99),  # excluded
        make_item(status="Icebox", raw_points=99),  # excluded
    ]
    assert burndown.backlog_total(items) == 3 + 2 + 0 + 5 + 1


def test_release_marker_dates_use_position_not_points():
    # velocity 5; a marker sits after 5 pts of work, so it lands in week 1
    items = [
        _backlog(5, id="a"),
        make_item(status="Backlog", title="🚀 GA demarcator ✅", id="mark"),
        _backlog(5, id="b"),
    ]
    markers = burndown.release_marker_dates(items, velocity=5, now=NOW)
    assert len(markers) == 1
    wk, item = markers[0]
    assert item.id == "mark"
    assert wk == date(2026, 6, 22)  # everything above (5 pts) done in week 1
    # the marker contributes no points to the backlog total
    assert burndown.backlog_total(items) == 10


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
