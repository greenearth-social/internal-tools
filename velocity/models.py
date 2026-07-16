"""Shared data model for a single GitHub Project item.

Kept in its own module so the pure-logic modules and the I/O module can both
import it without a circular dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Status field values we care about (compared case-insensitively).
STATUS_DONE = "done"
STATUS_BACKLOG = "backlog"

# Statuses that make up the "Backlog - Unified" project view (view #13): all
# remaining, non-Icebox, non-Done work. This is the set the burndown treats as
# the backlog.
BACKLOG_STATUSES = frozenset({STATUS_BACKLOG, "in progress", "in review", "on hold"})

# GitHub issue close reasons that mean the task was NOT actually completed
# (compared case-insensitively). "wontfix" closes as NOT_PLANNED; duplicates
# close as DUPLICATE. Points from these should not count toward velocity.
INCOMPLETE_CLOSE_REASONS = frozenset({"not_planned", "duplicate"})


def has_status(item: "ProjectItem", status: str) -> bool:
    """Case-insensitive comparison of an item's Status field."""
    return (item.status or "").strip().lower() == status


def in_backlog(item: "ProjectItem") -> bool:
    """True when the item is part of the unified backlog (see BACKLOG_STATUSES)."""
    return (item.status or "").strip().lower() in BACKLOG_STATUSES


def is_completed_work(item: "ProjectItem") -> bool:
    """False when the item was closed for a non-completion reason.

    Tasks closed as "not planned" (wontfix) or "duplicate" delivered no work, so
    they should be excluded from velocity/history. Items with no close reason
    (e.g. merged PRs, draft items) are treated as completed.
    """
    return (item.state_reason or "").strip().lower() not in INCOMPLETE_CLOSE_REASONS


@dataclass(frozen=True)
class ProjectItem:
    """A single card on the project board.

    ``raw_type`` / ``raw_points`` are the values as read from the project's
    custom fields (``None`` when unset). Normalization (the "blank type means
    feature", "bugs are 0 points", "unpointed is 2 points" rules) lives in
    ``points.py`` so it stays pure and testable.
    """

    id: str
    title: str
    url: str | None
    status: str | None
    raw_type: str | None
    raw_points: float | None
    closed_at: datetime | None
    # GitHub issue close reason: COMPLETED / NOT_PLANNED / DUPLICATE / None.
    state_reason: str | None = None
