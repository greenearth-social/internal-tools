"""Orchestrates Stages 1-4 and writes the scored CSV + findings summary.

Usage (run as a script, not via `-m` — user-analysis has a hyphen in its
directory name, so it isn't an importable Python package; see the README):
  python user-analysis/run_pipeline.py --data-dir user-analysis/data \
      --out user-analysis/data/user_analysis.csv \
      --summary-out user-analysis/data/summary.md

  # Validate end-to-end on a small slice before the full 129k-DID run:
  python user-analysis/run_pipeline.py --sample 500 --skip-firestore
"""

import argparse
import csv
import sys
from pathlib import Path

from dids import load_dids
from firestore_tiers import run_tier_a, run_tier_b, select_tier_b_sample
from labeler import DEFAULT_LABELER, enumerate_labeler, intersect_dids
from profiles import run_stage1
from scoring import ScoredRow, score_row

FLAG_NAMES = (
    "created_during_spike",
    "no_profile_content",
    "handle_looks_random",
    "self_declared_bot",
    "labeler_flagged",
)


def format_csv_rows(rows: list[ScoredRow]) -> list[dict]:
    out = []
    for row in rows:
        profile = row.profile
        firestore = row.firestore
        record = {
            "did": row.did,
            "score": row.score,
            "handle": profile.handle if profile else None,
            "created_at": profile.created_at if profile else None,
            "followers_count": profile.followers_count if profile else "",
            "follows_count": profile.follows_count if profile else "",
            "posts_count": profile.posts_count if profile else "",
            "firestore_found": firestore.found if firestore else False,
            "firestore_first_seen": firestore.created_at if firestore else None,
            "firestore_last_seen": firestore.last_seen_at if firestore else None,
            "labeler_labels": ",".join(row.labels.labels) if row.labels else "",
        }
        for flag in FLAG_NAMES:
            record[f"flag_{flag}"] = row.flags[flag]
        out.append(record)
    return out


def write_csv(rows: list[ScoredRow], path: Path) -> None:
    records = format_csv_rows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(records[0].keys()) if records else ["did"])
        writer.writeheader()
        writer.writerows(records)


def build_summary(rows: list[ScoredRow], tier_b: dict[str, dict]) -> str:
    total = len(rows)
    lines = [f"Total DIDs analyzed: {total}", ""]
    if total:
        lines.append("Flag breakdown:")
        for flag in FLAG_NAMES:
            count = sum(1 for r in rows if r.flags[flag])
            lines.append(f"  {flag}: {count} ({100 * count / total:.1f}%)")
        lines.append("")
        scored_high = sum(1 for r in rows if r.score >= 0.5)
        lines.append(f"Score >= 0.5: {scored_high} ({100 * scored_high / total:.1f}%)")
    if tier_b:
        lines.append("")
        lines.append(f"Tier B behavioral sample: {len(tier_b)} DIDs")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("user-analysis/data"))
    parser.add_argument("--out", type=Path, default=Path("user-analysis/data/user_analysis.csv"))
    parser.add_argument("--summary-out", type=Path, default=Path("user-analysis/data/summary.md"))
    parser.add_argument(
        "--checkpoint", type=Path, default=Path("user-analysis/data/profiles_checkpoint.jsonl")
    )
    parser.add_argument("--labeler", default=DEFAULT_LABELER)
    parser.add_argument(
        "--sample", type=int, default=0, help="only process the first N DIDs (0 = all)"
    )
    parser.add_argument("--tier-b-top-n", type=int, default=500)
    parser.add_argument("--tier-b-control-n", type=int, default=500)
    parser.add_argument("--firestore-project", default="greenearth-471522")
    parser.add_argument("--firestore-database", default="greenearth-prod")
    parser.add_argument(
        "--skip-firestore",
        action="store_true",
        help="skip Stages 4A/4B (public API + labeler only)",
    )
    args = parser.parse_args(argv)

    csv_paths = sorted(args.data_dir.glob("*-users.csv"))
    all_dids = load_dids(csv_paths)
    if args.sample:
        all_dids = all_dids[: args.sample]
    print(f"loaded {len(all_dids):,} DIDs from {len(csv_paths)} files", file=sys.stderr)

    profiles = run_stage1(all_dids, args.checkpoint)
    by_val = enumerate_labeler(args.labeler)
    labels = intersect_dids(set(all_dids), by_val)

    firestore_a = {}
    tier_b = {}
    if not args.skip_firestore:
        firestore_a = run_tier_a(all_dids, args.firestore_project, args.firestore_database)

    rows = [
        score_row(did, profiles.get(did), labels.get(did), firestore_a.get(did)) for did in all_dids
    ]

    if not args.skip_firestore:
        sample_dids = select_tier_b_sample(rows, args.tier_b_top_n, args.tier_b_control_n)
        tier_b = run_tier_b(sample_dids, args.firestore_project, args.firestore_database)

    write_csv(rows, args.out)
    summary = build_summary(rows, tier_b)
    args.summary_out.parent.mkdir(parents=True, exist_ok=True)
    args.summary_out.write_text(summary + "\n")
    print(summary)
    print(f"\nwrote {args.out} and {args.summary_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
