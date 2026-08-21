import firestore_tiers
from scoring import FirestoreTierA, ProfileSignal, ScoredRow


def _row(did: str, score: float) -> ScoredRow:
    profile = ProfileSignal(
        did=did,
        handle=None,
        created_at=None,
        followers_count=0,
        follows_count=0,
        posts_count=0,
        has_avatar=False,
        has_description=False,
        self_labels=[],
    )
    flags = {
        name: False
        for name in (
            "created_during_spike",
            "no_profile_content",
            "handle_looks_random",
            "self_declared_bot",
            "labeler_flagged",
        )
    }
    return ScoredRow(
        did=did, profile=profile, labels=None, firestore=None, flags=flags, score=score
    )


def test_user_doc_id_strips_plc_prefix():
    assert firestore_tiers.user_doc_id("did:plc:abc123") == "abc123"


def test_user_doc_id_passthrough_for_non_plc():
    assert firestore_tiers.user_doc_id("did:web:example.com") == "did:web:example.com"


def test_parse_user_doc_found():
    result = firestore_tiers.parse_user_doc(
        "did:plc:aaa",
        {
            "created_at": "2026-08-18T15:00:00Z",
            "last_seen_at": "2026-08-19T00:00:00Z",
            "social_radius": "1",
        },
    )
    assert result == FirestoreTierA(
        did="did:plc:aaa",
        found=True,
        created_at="2026-08-18T15:00:00Z",
        last_seen_at="2026-08-19T00:00:00Z",
        social_radius="1",
    )


def test_parse_user_doc_not_found():
    result = firestore_tiers.parse_user_doc("did:plc:aaa", None)
    assert result == FirestoreTierA(
        did="did:plc:aaa", found=False, created_at=None, last_seen_at=None, social_radius=None
    )


def test_select_tier_b_sample_includes_top_n_highest_scores():
    rows = [_row(f"did:plc:{i}", score=i / 10) for i in range(10)]
    sample = firestore_tiers.select_tier_b_sample(rows, top_n=3, control_n=0, seed=1)
    assert set(sample) == {"did:plc:9", "did:plc:8", "did:plc:7"}


def test_select_tier_b_sample_control_excludes_top_n_and_is_deterministic():
    rows = [_row(f"did:plc:{i}", score=i / 10) for i in range(10)]
    first = firestore_tiers.select_tier_b_sample(rows, top_n=2, control_n=3, seed=42)
    second = firestore_tiers.select_tier_b_sample(rows, top_n=2, control_n=3, seed=42)
    top = {"did:plc:9", "did:plc:8"}
    assert first == second
    assert top <= set(first)
    control = set(first) - top
    assert control.isdisjoint(top)
    assert len(control) == 3


def test_select_tier_b_sample_handles_fewer_rows_than_requested():
    rows = [_row("did:plc:0", score=0.5)]
    sample = firestore_tiers.select_tier_b_sample(rows, top_n=5, control_n=5, seed=0)
    assert sample == ["did:plc:0"]
