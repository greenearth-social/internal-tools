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


def has_status(item: "ProjectItem", status: str) -> bool:
    """Case-insensitive comparison of an item's Status field."""
    return (item.status or "").strip().lower() == status


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
