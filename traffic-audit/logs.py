"""Fetch and parse Cloud Run request logs for the api service.

Used to characterize the source of unexpected traffic (endpoint, IP,
User-Agent) against a baseline — see api#426 next-step 3.
"""

import time
import urllib.parse
from dataclasses import dataclass


@dataclass
class RequestRecord:
    timestamp: str
    host: str
    endpoint: str
    status: int | None
    remote_ip: str | None
    user_agent: str | None


def parse_http_request(http_request: dict | None, timestamp: str) -> RequestRecord | None:
    if not http_request or not http_request.get("requestUrl"):
        return None
    parsed = urllib.parse.urlparse(http_request["requestUrl"])
    return RequestRecord(
        timestamp=timestamp,
        host=parsed.netloc,
        endpoint=parsed.path,
        status=http_request.get("status"),
        remote_ip=http_request.get("remoteIp"),
        user_agent=http_request.get("userAgent"),
    )


def build_filter(service: str, start: str, end: str, endpoint_filter: str | None = None) -> str:
    parts = [
        'resource.type="cloud_run_revision"',
        f'resource.labels.service_name="{service}"',
        f'timestamp>="{start}"',
        f'timestamp<"{end}"',
    ]
    if endpoint_filter:
        parts.append(f'httpRequest.requestUrl:"{endpoint_filter}"')
    return " ".join(parts)


def fetch_request_logs(
    project: str,
    service: str,
    start: str,
    end: str,
    endpoint_filter: str | None = None,
    page_size: int = 1000,
    max_entries: int = 200_000,
    max_retries: int = 8,
    backoff_seconds: float = 65.0,
) -> list[RequestRecord]:
    """Fetch and parse request logs, resuming past Cloud Logging's read-quota 429s.

    ``ReadRequestsPerMinutePerProject`` is 60/min — a multi-day window with
    thousands of entries needs hundreds of paged calls, so hitting the quota
    mid-fetch is expected, not exceptional. On ``ResourceExhausted`` this
    backs off and resumes from the last successfully parsed entry's
    timestamp (``timestamp>=``), which can re-fetch that one boundary entry
    — acceptable for aggregate traffic analysis, not exact-count billing.
    """
    from google.api_core.exceptions import ResourceExhausted
    from google.cloud import logging as cloud_logging

    client = cloud_logging.Client(project=project)
    records: list[RequestRecord] = []
    window_start = start
    attempt = 0

    while True:
        filter_str = build_filter(service, window_start, end, endpoint_filter)
        try:
            for entry in client.list_entries(filter_=filter_str, page_size=page_size):
                timestamp = entry.timestamp.isoformat() if entry.timestamp else ""
                record = parse_http_request(entry.http_request, timestamp)
                if record:
                    records.append(record)
                    if timestamp:
                        window_start = timestamp
                if len(records) >= max_entries:
                    return records
            return records
        except ResourceExhausted:
            attempt += 1
            if attempt > max_retries:
                raise
            time.sleep(backoff_seconds)
