from . import points
from .conftest import make_item


def test_blank_type_defaults_to_feature():
    assert points.effective_type(make_item(raw_type=None)) == "feature"
    assert points.effective_type(make_item(raw_type="   ")) == "feature"


def test_type_is_lowercased():
    assert points.effective_type(make_item(raw_type="Bug")) == "bug"


def test_bugs_are_zero_points_even_when_pointed():
    assert points.normalize(make_item(raw_type="bug", raw_points=8)) == 0


def test_unpointed_non_bug_defaults_to_two():
    assert points.normalize(make_item(raw_type="feature", raw_points=None)) == 2
    # blank type counts as feature, so also 2
    assert points.normalize(make_item(raw_type=None, raw_points=None)) == 2


def test_pointed_item_uses_its_value():
    assert points.normalize(make_item(raw_type="feature", raw_points=5)) == 5


def test_fractional_points_are_rounded():
    assert points.normalize(make_item(raw_points=2.6)) == 3


def test_is_estimated_only_for_unpointed_non_bugs():
    assert points.is_estimated(make_item(raw_type="feature", raw_points=None)) is True
    assert points.is_estimated(make_item(raw_type=None, raw_points=None)) is True
    # real points -> not estimated
    assert points.is_estimated(make_item(raw_points=3)) is False
    # bugs are deterministically 0, not an estimate
    assert points.is_estimated(make_item(raw_type="bug", raw_points=None)) is False


def test_release_marker_detected_by_emoji():
    for title in ("🚀 GA release", "Ship it ✅", "🚢 v2", "🛳 big boat", "done ✔"):
        assert points.is_release_marker(make_item(title=title)) is True
    assert points.is_release_marker(make_item(title="Normal task")) is False
    # matches the real board item
    assert points.is_release_marker(make_item(title="🚀🚢 GA demarcator ✅")) is True


def test_release_markers_are_zero_points_and_not_estimated():
    marker = make_item(title="🚀 GA", raw_points=None)
    assert points.normalize(marker) == 0
    assert points.is_estimated(marker) is False
    # even if someone pointed it
    assert points.normalize(make_item(title="🚀 GA", raw_points=8)) == 0


def test_marker_label_strips_emoji_noise():
    assert points.marker_label("🚀🚢 🟩🟩 GA demarcator 🟩🟩") == "GA demarcator"
    assert points.marker_label("✅ v1.2 release") == "v1.2 release"
    assert points.marker_label("🚀") == "release"  # nothing left -> fallback
