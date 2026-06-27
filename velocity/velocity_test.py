from datetime import date, datetime, timezone

from .conftest import NOW, make_item
from . import velocity


def _done(closed: datetime, **kw):
    return make_item(status="Done", closed_at=closed, **kw)


def test_week_start_returns_monday():
    # 2026-06-24 is a Wednesday
    assert velocity.week_start(date(2026, 6, 24)) == date(2026, 6, 22)
    # a Monday maps to itself
    assert velocity.week_start(date(2026, 6, 22)) == date(2026, 6, 22)


def test_current_week_start_excludes_partial_week_boundary():
    assert velocity.current_week_start(NOW) == date(2026, 6, 22)


def test_weekly_completed_points_buckets_and_ignores_non_done():
    items = [
        _done(datetime(2026, 6, 16, tzinfo=timezone.utc), raw_points=5),
        _done(datetime(2026, 6, 17, tzinfo=timezone.utc), raw_points=1),
        make_item(status="Backlog", raw_points=8),  # not done -> ignored
        _done(datetime(2026, 6, 16, tzinfo=timezone.utc), raw_type="bug", raw_points=3),
    ]
    totals = velocity.weekly_completed_points(items)
    assert totals == {date(2026, 6, 15): 6}  # 5 + 1, bug contributes 0


def test_completed_weeks_excludes_current_week_and_zero_fills():
    items = [
        _done(datetime(2026, 6, 16, tzinfo=timezone.utc), raw_points=5),  # wk 06-15
        _done(datetime(2026, 6, 23, tzinfo=timezone.utc), raw_points=99),  # current wk -> excluded
        _done(datetime(2026, 6, 2, tzinfo=timezone.utc), raw_points=2),  # wk 06-01
    ]
    weeks = velocity.completed_weeks(items, weeks=3, now=NOW)
    assert weeks == [
        (date(2026, 6, 1), 2),
        (date(2026, 6, 8), 0),
        (date(2026, 6, 15), 5),
    ]


def test_compute_velocity_is_mean_over_three_weeks():
    items = [
        _done(datetime(2026, 6, 16, tzinfo=timezone.utc), raw_points=5),
        _done(datetime(2026, 6, 9, tzinfo=timezone.utc), raw_points=3),
        _done(datetime(2026, 6, 2, tzinfo=timezone.utc), raw_points=2),
        _done(datetime(2026, 6, 23, tzinfo=timezone.utc), raw_points=99),  # current, excluded
    ]
    assert velocity.compute_velocity(items, weeks=3, now=NOW) == (5 + 3 + 2) / 3


def test_compute_velocity_zero_when_no_completions():
    assert velocity.compute_velocity([], weeks=3, now=NOW) == 0.0
