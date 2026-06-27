from .conftest import make_item
from . import points


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
