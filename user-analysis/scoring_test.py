import dataclasses

import scoring
from scoring import FirestoreTierA, LabelInfo, ProfileSignal

_DEFAULT_PROFILE = ProfileSignal(
    did="did:plc:aaa",
    handle="realname.bsky.social",
    created_at="2026-01-01T00:00:00.000Z",
    followers_count=10,
    follows_count=10,
    posts_count=5,
    has_avatar=True,
    has_description=True,
    self_labels=[],
    fetch_error=None,
)


def _profile(**overrides) -> ProfileSignal:
    return dataclasses.replace(_DEFAULT_PROFILE, **overrides)


def test_flag_created_during_spike_true_inside_window():
    p = _profile(created_at="2026-08-18T15:00:00.000Z")
    flags = scoring.compute_flags(p, None)
    assert flags["created_during_spike"] is True


def test_flag_created_during_spike_false_outside_window():
    p = _profile(created_at="2025-01-01T00:00:00.000Z")
    flags = scoring.compute_flags(p, None)
    assert flags["created_during_spike"] is False


def test_flag_created_during_spike_false_when_unknown():
    p = _profile(created_at=None)
    flags = scoring.compute_flags(p, None)
    assert flags["created_during_spike"] is False


def test_flag_no_profile_content_true_when_empty():
    p = _profile(has_avatar=False, has_description=False, posts_count=0)
    flags = scoring.compute_flags(p, None)
    assert flags["no_profile_content"] is True


def test_flag_no_profile_content_false_when_any_present():
    p = _profile(has_avatar=False, has_description=False, posts_count=3)
    flags = scoring.compute_flags(p, None)
    assert flags["no_profile_content"] is False


def test_flag_handle_looks_random_true_for_consonant_run():
    p = _profile(handle="xkqvbzjpwn.bsky.social")
    flags = scoring.compute_flags(p, None)
    assert flags["handle_looks_random"] is True


def test_flag_handle_looks_random_false_for_wordlike_handle():
    p = _profile(handle="johnsmith.bsky.social")
    flags = scoring.compute_flags(p, None)
    assert flags["handle_looks_random"] is False


def test_flag_handle_looks_random_false_when_missing():
    p = _profile(handle=None)
    flags = scoring.compute_flags(p, None)
    assert flags["handle_looks_random"] is False


def test_flag_self_declared_bot_true_when_label_present():
    p = _profile(self_labels=["bot"])
    flags = scoring.compute_flags(p, None)
    assert flags["self_declared_bot"] is True


def test_flag_self_declared_bot_false_when_absent():
    p = _profile(self_labels=[])
    flags = scoring.compute_flags(p, None)
    assert flags["self_declared_bot"] is False


def test_flag_labeler_flagged_true_when_labels_present():
    flags = scoring.compute_flags(_profile(), LabelInfo(did="did:plc:aaa", labels=["spam"]))
    assert flags["labeler_flagged"] is True


def test_flag_labeler_flagged_false_when_no_label_info():
    flags = scoring.compute_flags(_profile(), None)
    assert flags["labeler_flagged"] is False


def test_flag_labeler_flagged_false_when_empty_label_list():
    flags = scoring.compute_flags(_profile(), LabelInfo(did="did:plc:aaa", labels=[]))
    assert flags["labeler_flagged"] is False


def test_compute_flags_all_false_when_profile_missing():
    flags = scoring.compute_flags(None, None)
    assert flags == {
        "created_during_spike": False,
        "no_profile_content": False,
        "handle_looks_random": False,
        "self_declared_bot": False,
        "labeler_flagged": False,
    }


def test_composite_score_zero_when_no_flags():
    assert scoring.composite_score({k: False for k in scoring.FLAG_WEIGHTS}) == 0.0


def test_composite_score_one_when_all_flags():
    assert scoring.composite_score({k: True for k in scoring.FLAG_WEIGHTS}) == 1.0


def test_composite_score_weights_labeler_flagged_highest():
    only_labeler = {k: False for k in scoring.FLAG_WEIGHTS}
    only_labeler["labeler_flagged"] = True
    only_handle = {k: False for k in scoring.FLAG_WEIGHTS}
    only_handle["handle_looks_random"] = True
    assert scoring.composite_score(only_labeler) > scoring.composite_score(only_handle)


def test_score_row_bundles_inputs_and_computed_score():
    profile = _profile(
        created_at="2026-08-18T15:00:00.000Z",
        has_avatar=False,
        has_description=False,
        posts_count=0,
    )
    labels = LabelInfo(did="did:plc:aaa", labels=["spam"])
    firestore = FirestoreTierA(
        did="did:plc:aaa",
        found=True,
        created_at="2026-08-18T15:05:00Z",
        last_seen_at="2026-08-18T15:06:00Z",
        social_radius="1",
    )
    row = scoring.score_row("did:plc:aaa", profile, labels, firestore)
    assert row.did == "did:plc:aaa"
    assert row.profile is profile
    assert row.labels is labels
    assert row.firestore is firestore
    assert row.flags["labeler_flagged"] is True
    assert row.score > 0.5
