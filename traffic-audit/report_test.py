from logs import RequestRecord
from report import aggregate_by_field, build_report, endpoint_shares


def _rec(
    endpoint="/xrpc/app.bsky.feed.getFeedSkeleton",
    host="api.greenearth.social",
    status=200,
    remote_ip="1.2.3.4",
    user_agent="BskyAppView",
):
    return RequestRecord(
        timestamp="2026-08-19T12:00:00Z",
        host=host,
        endpoint=endpoint,
        status=status,
        remote_ip=remote_ip,
        user_agent=user_agent,
    )


def test_aggregate_by_field_counts_occurrences():
    records = [_rec(endpoint="/a"), _rec(endpoint="/a"), _rec(endpoint="/b")]
    counts = aggregate_by_field(records, "endpoint")
    assert counts == {"/a": 2, "/b": 1}


def test_endpoint_shares_computes_percentages():
    records = [_rec(endpoint="/a"), _rec(endpoint="/a"), _rec(endpoint="/a"), _rec(endpoint="/b")]
    shares = endpoint_shares(records)
    assert shares[0] == ("/a", 3, 75.0)
    assert shares[1] == ("/b", 1, 25.0)


def test_endpoint_shares_empty_input():
    assert endpoint_shares([]) == []


def test_build_report_includes_total_and_breakdowns():
    records = [
        _rec(user_agent="BskyAppView", remote_ip="1.1.1.1"),
        _rec(user_agent="BskyAppView", remote_ip="1.1.1.1"),
        _rec(user_agent="Google-Cloud-Scheduler", remote_ip="2.2.2.2"),
    ]
    report = build_report(records, "test window")
    assert "test window: 3 requests" in report
    assert "BskyAppView" in report
    assert "Google-Cloud-Scheduler" in report
    assert "Distinct remote IPs: 2" in report


def test_build_report_handles_empty_input():
    report = build_report([], "empty window")
    assert "empty window: 0 requests" in report
