"""Pure scoring logic: turn raw signals into bot-likelihood flags and a score.

Kept dependency-free and network-free on purpose — this is the part of the
pipeline worth unit-testing carefully, per the design spec
(docs/superpowers/specs/2026-08-20-user-analysis-design.md).
"""

import datetime as dt
import re
from dataclasses import dataclass

SPIKE_START = dt.datetime(2026, 8, 17, tzinfo=dt.UTC)
SPIKE_END = dt.datetime(2026, 8, 19, tzinfo=dt.UTC)

# Weight of each flag in the composite score. Labeler verdicts are a
# third-party judgment call, so they carry the most weight; a randomish
# handle alone is the weakest signal (plenty of real users get one from
# bsky.social's auto-suggest).
FLAG_WEIGHTS = {
    "labeler_flagged": 3,
    "created_during_spike": 2,
    "no_profile_content": 2,
    "self_declared_bot": 1,
    "handle_looks_random": 1,
}

_VOWELS = set("aeiou")


@dataclass
class ProfileSignal:
    did: str
    handle: str | None
    created_at: str | None
    followers_count: int
    follows_count: int
    posts_count: int
    has_avatar: bool
    has_description: bool
    self_labels: list[str]
    fetch_error: str | None = None


@dataclass
class LabelInfo:
    did: str
    labels: list[str]


@dataclass
class FirestoreTierA:
    did: str
    found: bool
    created_at: str | None
    last_seen_at: str | None
    social_radius: str | None


@dataclass
class ScoredRow:
    did: str
    profile: ProfileSignal | None
    labels: LabelInfo | None
    firestore: FirestoreTierA | None
    flags: dict[str, bool]
    score: float


def _parse_created_at(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _flag_created_during_spike(profile: ProfileSignal | None) -> bool:
    if profile is None:
        return False
    created = _parse_created_at(profile.created_at)
    if created is None:
        return False
    return SPIKE_START <= created <= SPIKE_END


def _flag_no_profile_content(profile: ProfileSignal | None) -> bool:
    if profile is None:
        return False
    return not profile.has_avatar and not profile.has_description and profile.posts_count == 0


def _flag_handle_looks_random(profile: ProfileSignal | None) -> bool:
    if profile is None or not profile.handle:
        return False
    local = profile.handle.split(".")[0]
    if len(local) < 8 or not re.fullmatch(r"[a-z0-9]+", local):
        return False
    run = max_run = 0
    for ch in local:
        if ch.isalpha() and ch not in _VOWELS:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    return max_run >= 5


def _flag_self_declared_bot(profile: ProfileSignal | None) -> bool:
    if profile is None:
        return False
    return "bot" in profile.self_labels


def _flag_labeler_flagged(labels: LabelInfo | None) -> bool:
    return bool(labels and labels.labels)


def compute_flags(profile: ProfileSignal | None, labels: LabelInfo | None) -> dict[str, bool]:
    return {
        "created_during_spike": _flag_created_during_spike(profile),
        "no_profile_content": _flag_no_profile_content(profile),
        "handle_looks_random": _flag_handle_looks_random(profile),
        "self_declared_bot": _flag_self_declared_bot(profile),
        "labeler_flagged": _flag_labeler_flagged(labels),
    }


def composite_score(flags: dict[str, bool]) -> float:
    total = sum(FLAG_WEIGHTS.values())
    earned = sum(weight for name, weight in FLAG_WEIGHTS.items() if flags.get(name))
    return earned / total


def score_row(
    did: str,
    profile: ProfileSignal | None,
    labels: LabelInfo | None,
    firestore: FirestoreTierA | None,
) -> ScoredRow:
    flags = compute_flags(profile, labels)
    return ScoredRow(
        did=did,
        profile=profile,
        labels=labels,
        firestore=firestore,
        flags=flags,
        score=composite_score(flags),
    )
