"""Fetch and parse Cloud Run request logs for the api service.

Used to characterize the source of unexpected traffic (endpoint, IP,
User-Agent) against a baseline — see api#426 next-step 3.
"""

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


def fetch_request_logs(
    project: str,
    service: str,
    start: str,
    end: str,
    endpoint_filter: str | None = None,
    page_size: int = 1000,
    max_entries: int = 200_000,
) -> list[RequestRecord]:
    from google.cloud import logging as cloud_logging

    client = cloud_logging.Client(project=project)
    filter_parts = [
        'resource.type="cloud_run_revision"',
        f'resource.labels.service_name="{service}"',
        f'timestamp>="{start}"',
        f'timestamp<"{end}"',
    ]
    if endpoint_filter:
        filter_parts.append(f'httpRequest.requestUrl:"{endpoint_filter}"')
    filter_str = " ".join(filter_parts)

    records: list[RequestRecord] = []
    for entry in client.list_entries(filter_=filter_str, page_size=page_size):
        timestamp = entry.timestamp.isoformat() if entry.timestamp else ""
        record = parse_http_request(entry.http_request, timestamp)
        if record:
            records.append(record)
        if len(records) >= max_entries:
            break
    return records
