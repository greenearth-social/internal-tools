import labeler
from scoring import LabelInfo


def test_apply_label_page_adds_account_labels():
    by_val: dict[str, set[str]] = {}
    seen, retracted = labeler.apply_label_page(
        [{"uri": "did:plc:aaa", "val": "spam"}, {"uri": "did:plc:bbb", "val": "spam"}],
        by_val,
    )
    assert seen == 2
    assert retracted == 0
    assert by_val == {"spam": {"did:plc:aaa", "did:plc:bbb"}}


def test_apply_label_page_drops_post_level_labels():
    by_val: dict[str, set[str]] = {}
    labeler.apply_label_page(
        [{"uri": "at://did:plc:aaa/app.bsky.feed.post/xyz", "val": "spam"}], by_val
    )
    assert by_val == {}


def test_apply_label_page_handles_retraction():
    by_val = {"spam": {"did:plc:aaa"}}
    seen, retracted = labeler.apply_label_page(
        [{"uri": "did:plc:aaa", "val": "spam", "neg": True}], by_val
    )
    assert seen == 1
    assert retracted == 1
    assert by_val == {"spam": set()}


def test_apply_label_page_skips_labels_without_value():
    by_val: dict[str, set[str]] = {}
    seen, retracted = labeler.apply_label_page([{"uri": "did:plc:aaa"}], by_val)
    assert seen == 1
    assert by_val == {}


def test_intersect_dids_returns_only_matches_with_labels():
    by_val = {"spam": {"did:plc:aaa"}, "amplifier": {"did:plc:aaa", "did:plc:zzz"}}
    result = labeler.intersect_dids({"did:plc:aaa", "did:plc:bbb"}, by_val)
    assert set(result) == {"did:plc:aaa"}
    assert isinstance(result["did:plc:aaa"], LabelInfo)
    assert sorted(result["did:plc:aaa"].labels) == ["amplifier", "spam"]


def test_intersect_dids_empty_when_no_overlap():
    by_val = {"spam": {"did:plc:zzz"}}
    assert labeler.intersect_dids({"did:plc:aaa"}, by_val) == {}
