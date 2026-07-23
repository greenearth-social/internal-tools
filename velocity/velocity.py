"""Velocity computation (pure).

Velocity is the average number of points completed per week over the last few
*completed* weeks. A task counts as completed in the week its issue was closed
(``closed_at``), provided its Status is Done.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from . import points
from .models import STATUS_DONE, ProjectItem, has_status, is_completed_work

DEFAULT_VELOCITY_WEEKS = 3


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def week_start(d: datetime | date) -> date:
    """Return the Monday (date) of the week containing ``d``."""
    if isinstance(d, datetime):
        d = d.date()
    return d - timedelta(days=d.weekday())


def current_week_start(now: datetime | None = None) -> date:
    """Monday of the current (partial) week."""
    return week_start(_now(now))


def weekly_completed_points(items: list[ProjectItem]) -> dict[date, int]:
    """Map each week (Monday) to the points completed in it.

    Only Done items with a ``closed_at`` are counted, and tasks closed as
    "not planned" (wontfix) or "duplicate" are excluded since they delivered no
    work.
    """
    totals: dict[date, int] = defaultdict(int)
    for item in items:
        if not has_status(item, STATUS_DONE) or item.closed_at is None:
            continue
        if not is_completed_work(item):
            continue
        totals[week_start(item.closed_at)] += points.normalize(item)
    return dict(totals)


def completed_weeks(
    items: list[ProjectItem],
    weeks: int = DEFAULT_VELOCITY_WEEKS,
    now: datetime | None = None,
) -> list[tuple[date, int]]:
    """Return the (week_start, points) for the last ``weeks`` completed weeks.

    The current, still-in-progress week is excluded. Weeks with no completed
    work are included as zeros so the average isn't inflated.
    """
    totals = weekly_completed_points(items)
    last_complete = current_week_start(now) - timedelta(days=7)
    result: list[tuple[date, int]] = []
    for i in range(weeks):
        wk = last_complete - timedelta(days=7 * i)
        result.append((wk, totals.get(wk, 0)))
    result.reverse()
    return result


def compute_velocity(
    items: list[ProjectItem],
    weeks: int = DEFAULT_VELOCITY_WEEKS,
    now: datetime | None = None,
) -> float:
    """Average completed points per week over the last ``weeks`` weeks."""
    recent = completed_weeks(items, weeks, now)
    if not recent:
        return 0.0
    return sum(pts for _, pts in recent) / len(recent)
