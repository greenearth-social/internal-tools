import logs


def test_parse_http_request_extracts_fields():
    http_request = {
        "requestUrl": "https://api.greenearth.social/xrpc/app.bsky.feed.getFeedSkeleton?feed=at://did:plc:x/app.bsky.feed.generator/your-feed&limit=8",
        "status": 200,
        "remoteIp": "149.97.184.194",
        "userAgent": "BskyAppView",
    }
    record = logs.parse_http_request(http_request, "2026-08-19T12:00:02Z")
    assert record is not None
    assert record.timestamp == "2026-08-19T12:00:02Z"
    assert record.host == "api.greenearth.social"
    assert record.endpoint == "/xrpc/app.bsky.feed.getFeedSkeleton"
    assert record.status == 200
    assert record.remote_ip == "149.97.184.194"
    assert record.user_agent == "BskyAppView"


def test_parse_http_request_returns_none_when_missing():
    assert logs.parse_http_request(None, "2026-08-19T12:00:02Z") is None


def test_parse_http_request_returns_none_when_no_request_url():
    assert logs.parse_http_request({"status": 200}, "2026-08-19T12:00:02Z") is None


def test_parse_http_request_handles_run_app_host():
    http_request = {
        "requestUrl": "https://greenearth-api-prod-oef7fsaama-ue.a.run.app/xrpc/app.bsky.feed.getFeedSkeleton?feed=x&limit=30",
        "status": 200,
        "remoteIp": "34.116.28.36",
        "userAgent": "Google-Cloud-Scheduler",
    }
    record = logs.parse_http_request(http_request, "2026-08-19T12:00:01Z")
    assert record is not None
    assert record.host == "greenearth-api-prod-oef7fsaama-ue.a.run.app"
    assert record.endpoint == "/xrpc/app.bsky.feed.getFeedSkeleton"


def test_build_filter_without_endpoint_filter():
    filter_str = logs.build_filter(
        "greenearth-api-prod", "2026-08-18T00:00:00Z", "2026-08-21T00:00:00Z"
    )
    assert filter_str == (
        'resource.type="cloud_run_revision" '
        'resource.labels.service_name="greenearth-api-prod" '
        'timestamp>="2026-08-18T00:00:00Z" '
        'timestamp<"2026-08-21T00:00:00Z"'
    )


def test_build_filter_with_endpoint_filter():
    filter_str = logs.build_filter(
        "greenearth-api-prod", "2026-08-18T00:00:00Z", "2026-08-21T00:00:00Z", "getFeedSkeleton"
    )
    assert filter_str.endswith('httpRequest.requestUrl:"getFeedSkeleton"')
