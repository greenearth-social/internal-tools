"""Aggregate parsed Cloud Run request records into endpoint/IP/UA breakdowns.

Usage (run as a script — traffic-audit has a hyphen in its directory name,
so it isn't an importable package; see user-analysis/README.md for the same
convention):
  python traffic-audit/report.py \
      --spike-start 2026-08-18T00:00:00Z --spike-end 2026-08-21T00:00:00Z \
      --baseline-start 2026-08-11T00:00:00Z --baseline-end 2026-08-12T00:00:00Z
"""

import argparse
import collections
import sys

from logs import RequestRecord, fetch_request_logs

DEFAULT_PROJECT = "greenearth-471522"
DEFAULT_SERVICE = "greenearth-api-prod"


def aggregate_by_field(records: list[RequestRecord], field: str) -> collections.Counter:
    return collections.Counter(getattr(r, field) for r in records)


def endpoint_shares(records: list[RequestRecord]) -> list[tuple[str, int, float]]:
    total = len(records)
    counts = aggregate_by_field(records, "endpoint")
    return [
        (endpoint, count, 100 * count / total if total else 0.0)
        for endpoint, count in counts.most_common()
    ]


def build_report(records: list[RequestRecord], label: str) -> str:
    total = len(records)
    lines = [f"=== {label}: {total:,} requests ==="]
    if not total:
        return "\n".join(lines)

    lines.append("\nBy endpoint:")
    for endpoint, count, pct in endpoint_shares(records)[:10]:
        lines.append(f"  {count:>7,} ({pct:5.1f}%)  {endpoint}")

    lines.append("\nBy user agent:")
    for ua, count in aggregate_by_field(records, "user_agent").most_common(10):
        lines.append(f"  {count:>7,}  {ua}")

    lines.append("\nBy host:")
    for host, count in aggregate_by_field(records, "host").most_common(10):
        lines.append(f"  {count:>7,}  {host}")

    ip_counts = aggregate_by_field(records, "remote_ip")
    lines.append(f"\nDistinct remote IPs: {len(ip_counts):,}")
    lines.append("Top remote IPs:")
    for ip, count in ip_counts.most_common(10):
        lines.append(f"  {count:>7,} ({100 * count / total:5.1f}%)  {ip}")

    lines.append("\nBy status:")
    for status, count in aggregate_by_field(records, "status").most_common(10):
        lines.append(f"  {count:>7,}  {status}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--spike-start", required=True)
    parser.add_argument("--spike-end", required=True)
    parser.add_argument("--baseline-start", default=None)
    parser.add_argument("--baseline-end", default=None)
    parser.add_argument(
        "--endpoint-filter", default=None, help='e.g. "getFeedSkeleton" to narrow the query'
    )
    parser.add_argument("--max-entries", type=int, default=200_000)
    args = parser.parse_args(argv)

    spike = fetch_request_logs(
        args.project,
        args.service,
        args.spike_start,
        args.spike_end,
        endpoint_filter=args.endpoint_filter,
        max_entries=args.max_entries,
    )
    print(build_report(spike, f"spike window {args.spike_start}..{args.spike_end}"))

    if args.baseline_start and args.baseline_end:
        baseline = fetch_request_logs(
            args.project,
            args.service,
            args.baseline_start,
            args.baseline_end,
            endpoint_filter=args.endpoint_filter,
            max_entries=args.max_entries,
        )
        print()
        print(build_report(baseline, f"baseline {args.baseline_start}..{args.baseline_end}"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
