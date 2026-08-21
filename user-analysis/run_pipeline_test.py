from firestore_tiers import parse_user_doc
from run_pipeline import build_summary, format_csv_rows
from scoring import LabelInfo, ProfileSignal, score_row


def _row(did, created_during_spike=False, labeler_flagged=False):
    profile = ProfileSignal(
        did=did,
        handle="h.bsky.social",
        created_at=None,
        followers_count=1,
        follows_count=1,
        posts_count=1,
        has_avatar=True,
        has_description=True,
        self_labels=[],
    )
    labels = LabelInfo(did=did, labels=["spam"]) if labeler_flagged else None
    row = score_row(did, profile, labels, parse_user_doc(did, None))
    row.flags["created_during_spike"] = created_during_spike
    return row


def test_format_csv_rows_includes_flat_columns():
    rows = [_row("did:plc:aaa", labeler_flagged=True)]
    out = format_csv_rows(rows)
    assert out[0]["did"] == "did:plc:aaa"
    assert out[0]["score"] == rows[0].score
    assert out[0]["flag_labeler_flagged"] is True
    assert out[0]["handle"] == "h.bsky.social"


def test_format_csv_rows_handles_missing_profile():
    row = score_row("did:plc:zzz", None, None, parse_user_doc("did:plc:zzz", None))
    out = format_csv_rows([row])
    assert out[0]["handle"] is None
    assert out[0]["followers_count"] == ""


def test_build_summary_reports_totals_and_flag_breakdown():
    rows = [
        _row("did:plc:aaa", created_during_spike=True, labeler_flagged=True),
        _row("did:plc:bbb", created_during_spike=True),
        _row("did:plc:ccc"),
    ]
    summary = build_summary(rows, tier_b={})
    assert "Total DIDs analyzed: 3" in summary
    assert "created_during_spike: 2 (66.7%)" in summary
    assert "labeler_flagged: 1 (33.3%)" in summary


def test_build_summary_handles_empty_input():
    summary = build_summary([], tier_b={})
    assert "Total DIDs analyzed: 0" in summary
