"""Backlog burndown: historical completion, forward projection, and a
per-week assignment of which backlog tasks finish when. All pure.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from . import points
from .models import ProjectItem, in_backlog
from .velocity import current_week_start, weekly_completed_points

DEFAULT_HISTORY_WEEKS = 8  # ~2 months


def completed_history(
    items: list[ProjectItem],
    min_weeks: int = DEFAULT_HISTORY_WEEKS,
    now: datetime | None = None,
) -> list[tuple[date, int]]:
    """Points completed per week, chronological and zero-filled.

    Spans from the earliest week with completed work (or ``min_weeks`` back,
    whichever is earlier) through the last *completed* week. The current,
    partial week is excluded because it isn't counted toward velocity yet.
    """
    totals = weekly_completed_points(items)
    end = current_week_start(now) - timedelta(days=7)  # last completed week
    earliest_data = min([wk for wk in totals if wk <= end], default=end)
    floor = end - timedelta(days=7 * (min_weeks - 1))
    start = min(earliest_data, floor)

    weeks: list[date] = []
    wk = start
    while wk <= end:
        weeks.append(wk)
        wk += timedelta(days=7)
    return [(wk, totals.get(wk, 0)) for wk in weeks]


def backlog_items(items: list[ProjectItem]) -> list[ProjectItem]:
    """Unified-backlog items, preserving input (priority) order."""
    return [item for item in items if in_backlog(item)]


def backlog_total(items: list[ProjectItem]) -> int:
    """Total normalized points remaining in the backlog."""
    return sum(points.normalize(item) for item in backlog_items(items))


def project_burndown(
    items: list[ProjectItem],
    velocity: float,
    now: datetime | None = None,
) -> list[tuple[date, float]]:
    """Forward weekly remaining-points series from now until the backlog is 0.

    Index 0 is the current week at the full backlog total. With a non-positive
    velocity the backlog can't be projected, so only the starting point is
    returned.
    """
    total = backlog_total(items)
    start = current_week_start(now)
    if velocity <= 0 or total <= 0:
        return [(start, float(total))]
    weeks_needed = math.ceil(total / velocity)
    return [
        (start + timedelta(days=7 * w), max(total - velocity * w, 0.0))
        for w in range(weeks_needed + 1)
    ]


def assign_tasks_to_weeks(
    backlog_in_priority_order: list[ProjectItem],
    velocity: float,
    now: datetime | None = None,
) -> list[tuple[date, ProjectItem]]:
    """Project which week each backlog task is completed in.

    Walks the backlog in priority order, treating each week as ``velocity``
    points of capacity; a task is assigned to the week in which its cumulative
    point total finishes. Zero-point bugs don't consume capacity, so they ride
    along in whatever week the cursor currently sits.
    """
    start = current_week_start(now)
    assignments: list[tuple[date, ProjectItem]] = []
    cumulative = 0
    for item in backlog_in_priority_order:
        cumulative += points.normalize(item)
        if velocity <= 0:
            week_index = 1
        else:
            week_index = max(math.ceil(cumulative / velocity - 1e-9), 1)
        assignments.append((start + timedelta(days=7 * (week_index - 1)), item))
    return assignments


def upcoming_weeks(
    assignments: list[tuple[date, ProjectItem]],
    weeks: int = 4,
    now: datetime | None = None,
) -> list[tuple[date, list[ProjectItem]]]:
    """Group task assignments into the next ``weeks`` weeks, oldest first.

    Tasks projected beyond the window are dropped. Weeks inside the window with
    no tasks are still included (as empty lists) so the picture is complete.
    Item order within each week is preserved (priority order).
    """
    window = [current_week_start(now) + timedelta(days=7 * i) for i in range(weeks)]
    grouped: dict[date, list[ProjectItem]] = {wk: [] for wk in window}
    for wk, item in assignments:
        if wk in grouped:
            grouped[wk].append(item)
    return [(wk, grouped[wk]) for wk in window]


def is_task(item: ProjectItem) -> bool:
    """True for real backlog tasks (i.e. not release markers)."""
    return not points.is_release_marker(item)


def release_marker_dates(
    backlog_in_priority_order: list[ProjectItem],
    velocity: float,
    now: datetime | None = None,
) -> list[tuple[date, ProjectItem]]:
    """Projected release date for each release marker in the backlog.

    A marker itself is worth 0 points, so its assigned week is exactly the week
    all higher-priority work above it is projected to finish. Returned in
    backlog (priority) order.
    """
    return [
        (wk, item)
        for wk, item in assign_tasks_to_weeks(backlog_in_priority_order, velocity, now)
        if points.is_release_marker(item)
    ]
