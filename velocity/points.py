"""Point and type normalization rules (pure).

Rules (from issue #1):

- A blank type is treated as ``feature``.
- Bugs are never assigned points -> 0 (they don't consume velocity).
- Any other item with no points is worth 2 points.
- Otherwise the item's point value is used as-is.
"""

from __future__ import annotations

from .models import ProjectItem

DEFAULT_TYPE = "feature"
BUG_TYPE = "bug"
UNPOINTED_DEFAULT = 2


def effective_type(item: ProjectItem) -> str:
    """Return the item's type, defaulting a blank/missing type to ``feature``."""
    raw = (item.raw_type or "").strip()
    return raw.lower() if raw else DEFAULT_TYPE


def normalize(item: ProjectItem) -> int:
    """Return the point value used for velocity and burndown math."""
    if effective_type(item) == BUG_TYPE:
        return 0
    if item.raw_points is None:
        return UNPOINTED_DEFAULT
    return int(round(item.raw_points))
