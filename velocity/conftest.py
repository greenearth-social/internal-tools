"""Shared test fixtures: a small factory for building ProjectItems."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from .models import ProjectItem

# A fixed "now" so week math in tests is deterministic.
# 2026-06-24 is a Wednesday -> its week starts Monday 2026-06-22.
NOW = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)


def make_item(
    id: str = "ITEM",
    title: str = "A task",
    status: str | None = None,
    raw_type: str | None = None,
    raw_points: float | None = None,
    closed_at: datetime | None = None,
    url: str | None = "https://example.test/1",
    state_reason: str | None = None,
) -> ProjectItem:
    return ProjectItem(
        id=id,
        title=title,
        url=url,
        status=status,
        raw_type=raw_type,
        raw_points=raw_points,
        closed_at=closed_at,
        state_reason=state_reason,
    )


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def item_factory():
    return make_item
