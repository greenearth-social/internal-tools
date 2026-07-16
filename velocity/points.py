"""Point and type normalization rules (pure).

Rules (from issue #1):

- A blank type is treated as ``feature``.
- Bugs are never assigned points -> 0 (they don't consume velocity).
- Release markers (see below) are never assigned points -> 0.
- Any other item with no points is worth 2 points.
- Otherwise the item's point value is used as-is.
"""

from __future__ import annotations

import re

from .models import ProjectItem

DEFAULT_TYPE = "feature"
BUG_TYPE = "bug"
UNPOINTED_DEFAULT = 2

# Emoji that mark an item as a release marker rather than a real task: a rocket,
# any of the common check marks, or either ship. Markers carry 0 points; their
# position in the backlog is used to project a release date.
RELEASE_MARKER_CHARS = frozenset(
    {
        "\U0001f680",  # 🚀 rocket
        "✅",  # ✅ white heavy check mark
        "✔",  # ✔ heavy check mark
        "☑",  # ☑ ballot box with check
        "\U0001f6a2",  # 🚢 ship
        "\U0001f6f3",  # 🛳 passenger ship
    }
)


def is_release_marker(item: ProjectItem) -> bool:
    """True when the title contains a release-marker emoji."""
    return any(ch in item.title for ch in RELEASE_MARKER_CHARS)


_MARKER_NOISE = re.compile(r"[^\w\s/&.+-]")


def marker_label(title: str) -> str:
    """A readable label for a release marker: strip emoji/marker noise.

    e.g. "🚀🚢 🟩🟩 GA demarcator 🟩🟩" -> "GA demarcator".
    """
    return re.sub(r"\s+", " ", _MARKER_NOISE.sub("", title)).strip() or "release"


def effective_type(item: ProjectItem) -> str:
    """Return the item's type, defaulting a blank/missing type to ``feature``."""
    raw = (item.raw_type or "").strip()
    return raw.lower() if raw else DEFAULT_TYPE


def normalize(item: ProjectItem) -> int:
    """Return the point value used for velocity and burndown math."""
    if is_release_marker(item) or effective_type(item) == BUG_TYPE:
        return 0
    if item.raw_points is None:
        return UNPOINTED_DEFAULT
    return int(round(item.raw_points))


def is_estimated(item: ProjectItem) -> bool:
    """True when the normalized points are auto-assigned rather than real.

    This is the unpointed-non-bug case where we fall back to
    ``UNPOINTED_DEFAULT``. Bugs and release markers (deterministically 0) and
    items with a real point value are not "estimated".
    """
    if is_release_marker(item) or effective_type(item) == BUG_TYPE:
        return False
    return item.raw_points is None
