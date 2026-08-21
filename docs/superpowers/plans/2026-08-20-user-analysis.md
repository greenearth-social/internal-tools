# User Analysis (api#426 bot investigation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a rerunnable `internal-tools/user-analysis/` pipeline that scores the 129,235 DIDs from the 08-18 growth spike for bot/inauthentic likelihood, using public Bluesky API profile data, a skywatch.blue labeler cross-check, and tiered prod Firestore activity data.

**Architecture:** A tested pure-logic core (`dids.py`, `scoring.py`) plus four I/O stage modules (`profiles.py`, `labeler.py`, `firestore_tiers.py`) that each expose a pure, unit-tested parsing/logic layer and an untested network-calling orchestrator function, wired together by a `run_pipeline.py` CLI that supports `--sample N` for cheap end-to-end validation before the full 129k-DID run.

**Tech Stack:** Python 3.13, pipenv, stdlib `urllib`/`json`/`csv` for the two Bluesky HTTP integrations (matching ingex#466's dependency-free scripts), `google-cloud-firestore` for the Firestore stages, pytest/ruff/pyright per `internal-tools` conventions.

**Spec:** `docs/superpowers/specs/2026-08-20-user-analysis-design.md`

## Global Constraints

- Stdlib only for `profiles.py` and `labeler.py` (no new HTTP dependency) — matches ingex#466's scripts, which this reuses techniques from.
- `google-cloud-firestore` is the one new dependency, needed only by `firestore_tiers.py`.
- **`user-analysis` has a hyphen in its directory name, so it cannot be a real Python package** (no `__init__.py`, no relative imports — `from . import x` raises `ImportError: attempted relative import with no known parent package` under pytest with a hyphenated directory, confirmed by hand). Follow `devenv/`'s existing pattern instead (see the comment already in `pyproject.toml`): every module uses bare top-level imports (`import dids`, `from scoring import ProfileSignal`), and scripts are run directly (`python user-analysis/run_pipeline.py`), never via `python -m`. pytest's default "prepend" import mode adds each test file's own directory to `sys.path`, which is what makes the bare imports resolve during test collection too.
- Tests live co-located as `<module>_test.py` (not a `tests/` dir), matching `velocity/` and `devenv/`.
- Raw CSVs and all generated data/checkpoints/outputs live under `user-analysis/data/`, which is already gitignored (`api.issue.426` branch, commit `3ff2105`) — never add `-f` to git add anything under it.
- `pyproject.toml`'s `testpaths` and `[tool.pyright] include` must list `"user-analysis"` before its tests will run in CI.
- Every stage's orchestrator function must be resumable/checkpointable or cheap enough to just rerun — no stage should force redoing a multi-hour public-API sweep because of an unrelated crash later in the pipeline.

---

## Task 1: Package scaffolding + DID loading

**Files:**
- Create: `user-analysis/dids.py`
- Test: `user-analysis/dids_test.py`
- Modify: `pyproject.toml` (`testpaths`, `[tool.pyright] include`)

**Interfaces:**
- Produces: `load_dids(csv_paths: list[Path]) -> list[str]` — reads one `distinct_id` column per CSV, returns deduped DIDs in first-seen order.
- Produces: `chunk(items: list[str], size: int) -> list[list[str]]` — generic batching helper, reused by every later stage that needs to batch DIDs (public API batches of 25, Firestore `get_all()` batches of 500).

- [ ] **Step 1: Write the failing tests**

```python
# user-analysis/dids_test.py
from pathlib import Path

import dids


def _write_csv(tmp_path: Path, name: str, rows: list[str]) -> Path:
    path = tmp_path / name
    path.write_text("distinct_id\n" + "\n".join(rows) + "\n")
    return path


def test_load_dids_dedupes_across_files(tmp_path):
    a = _write_csv(tmp_path, "a.csv", ["did:plc:aaa", "did:plc:bbb"])
    b = _write_csv(tmp_path, "b.csv", ["did:plc:bbb", "did:plc:ccc"])
    assert dids.load_dids([a, b]) == ["did:plc:aaa", "did:plc:bbb", "did:plc:ccc"]


def test_load_dids_skips_blank_rows(tmp_path):
    a = _write_csv(tmp_path, "a.csv", ["did:plc:aaa", "", "did:plc:bbb"])
    assert dids.load_dids([a]) == ["did:plc:aaa", "did:plc:bbb"]


def test_chunk_splits_into_fixed_size_groups():
    assert dids.chunk(["a", "b", "c", "d", "e"], 2) == [["a", "b"], ["c", "d"], ["e"]]


def test_chunk_empty_input_returns_empty_list():
    assert dids.chunk([], 25) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pipenv run pytest user-analysis/dids_test.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dids'`

- [ ] **Step 3: Implement**

```python
# user-analysis/dids.py
"""Load and dedupe the DID list exported from Posthog for the 08-18 spike."""

import csv
from pathlib import Path


def load_dids(csv_paths: list[Path]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for path in csv_paths:
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                did = (row.get("distinct_id") or "").strip()
                if did and did not in seen:
                    seen.add(did)
                    ordered.append(did)
    return ordered


def chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pipenv run pytest user-analysis/dids_test.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Wire pyproject.toml and verify lint/type-check**

Edit `pyproject.toml`:

```toml
testpaths = ["velocity", "devenv", "user-analysis"]
```

```toml
[tool.pyright]
include = ["velocity", "devenv", "user-analysis"]
```

Run: `pipenv run ruff check user-analysis && pipenv run ruff format --check user-analysis && pipenv run pyright user-analysis`
Expected: all clean

- [ ] **Step 6: Commit**

```bash
git add user-analysis/dids.py user-analysis/dids_test.py pyproject.toml
git commit -m "Add user-analysis DID loading/deduping"
```

---

## Task 2: Scoring core (data types + pure flag functions)

**Files:**
- Create: `user-analysis/scoring.py`
- Test: `user-analysis/scoring_test.py`

**Interfaces:**
- Consumes: nothing (pure, no dependency on Task 1's runtime — only stdlib).
- Produces (used by Tasks 3-6), all importable as `from scoring import <name>`:
  - `ProfileSignal` dataclass: `did, handle, created_at, followers_count, follows_count, posts_count, has_avatar, has_description, self_labels, fetch_error` (fields typed below).
  - `LabelInfo` dataclass: `did, labels: list[str]`.
  - `FirestoreTierA` dataclass: `did, found: bool, created_at, last_seen_at, social_radius`.
  - `ScoredRow` dataclass: `did, profile, labels, firestore, flags: dict[str, bool], score: float`.
  - `compute_flags(profile: ProfileSignal | None, labels: LabelInfo | None) -> dict[str, bool]`
  - `composite_score(flags: dict[str, bool]) -> float`
  - `score_row(did: str, profile: ProfileSignal | None, labels: LabelInfo | None, firestore: FirestoreTierA | None) -> ScoredRow`
  - `FLAG_WEIGHTS: dict[str, int]` — the 5 flag names as keys.

- [ ] **Step 1: Write the failing tests**

```python
# user-analysis/scoring_test.py
import scoring
from scoring import FirestoreTierA, LabelInfo, ProfileSignal


def _profile(**overrides) -> ProfileSignal:
    base = dict(
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
    base.update(overrides)
    return ProfileSignal(**base)


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
    profile = _profile(created_at="2026-08-18T15:00:00.000Z", has_avatar=False,
                        has_description=False, posts_count=0)
    labels = LabelInfo(did="did:plc:aaa", labels=["spam"])
    firestore = FirestoreTierA(did="did:plc:aaa", found=True, created_at="2026-08-18T15:05:00Z",
                                last_seen_at="2026-08-18T15:06:00Z", social_radius="1")
    row = scoring.score_row("did:plc:aaa", profile, labels, firestore)
    assert row.did == "did:plc:aaa"
    assert row.profile is profile
    assert row.labels is labels
    assert row.firestore is firestore
    assert row.flags["labeler_flagged"] is True
    assert row.score > 0.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pipenv run pytest user-analysis/scoring_test.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scoring'`

- [ ] **Step 3: Implement**

```python
# user-analysis/scoring.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pipenv run pytest user-analysis/scoring_test.py -v`
Expected: PASS (19 tests)

- [ ] **Step 5: Lint/type-check and commit**

Run: `pipenv run ruff check user-analysis && pipenv run ruff format --check user-analysis && pipenv run pyright user-analysis`

```bash
git add user-analysis/scoring.py user-analysis/scoring_test.py
git commit -m "Add pure bot-likelihood scoring logic"
```

---

## Task 3: Stage 1 — public API profile signals

**Files:**
- Create: `user-analysis/profiles.py`
- Test: `user-analysis/profiles_test.py`

**Interfaces:**
- Consumes: `dids.chunk` (Task 1, `from dids import chunk`), `scoring.ProfileSignal` (Task 2, `from scoring import ProfileSignal`).
- Produces (used by Task 6), importable as `from profiles import <name>`:
  - `parse_profile(raw: dict) -> ProfileSignal` — pure.
  - `load_checkpoint(path: Path) -> dict[str, ProfileSignal]` — pure file I/O, testable with `tmp_path`.
  - `append_checkpoint(path: Path, signals: list[ProfileSignal]) -> None` — pure file I/O, testable with `tmp_path`.
  - `run_stage1(all_dids: list[str], checkpoint_path: Path, batch_size: int = 25, checkpoint_every: int = 40) -> dict[str, ProfileSignal]` — network orchestrator, not unit tested (validated via `run_pipeline.py --sample`).

- [ ] **Step 1: Write the failing tests**

```python
# user-analysis/profiles_test.py
from pathlib import Path

import profiles
from scoring import ProfileSignal

_RAW_PROFILE_FULL = {
    "did": "did:plc:aaa",
    "handle": "realname.bsky.social",
    "displayName": "Real Name",
    "description": "hi",
    "avatar": "https://example.com/a.jpg",
    "createdAt": "2026-01-01T00:00:00.000Z",
    "followersCount": 12,
    "followsCount": 34,
    "postsCount": 56,
    "labels": [{"val": "bot", "src": "did:plc:aaa"}],
}

_RAW_PROFILE_BARE = {
    "did": "did:plc:bbb",
    "handle": "bbb.bsky.social",
    "followersCount": 0,
    "followsCount": 0,
    "postsCount": 0,
}


def test_parse_profile_extracts_full_fields():
    signal = profiles.parse_profile(_RAW_PROFILE_FULL)
    assert signal.did == "did:plc:aaa"
    assert signal.handle == "realname.bsky.social"
    assert signal.created_at == "2026-01-01T00:00:00.000Z"
    assert signal.followers_count == 12
    assert signal.follows_count == 34
    assert signal.posts_count == 56
    assert signal.has_avatar is True
    assert signal.has_description is True
    assert signal.self_labels == ["bot"]
    assert signal.fetch_error is None


def test_parse_profile_handles_missing_optional_fields():
    signal = profiles.parse_profile(_RAW_PROFILE_BARE)
    assert signal.did == "did:plc:bbb"
    assert signal.created_at is None
    assert signal.has_avatar is False
    assert signal.has_description is False
    assert signal.self_labels == []


def test_checkpoint_roundtrip(tmp_path):
    path = tmp_path / "checkpoint.jsonl"
    signals = [profiles.parse_profile(_RAW_PROFILE_FULL), profiles.parse_profile(_RAW_PROFILE_BARE)]
    profiles.append_checkpoint(path, signals)
    loaded = profiles.load_checkpoint(path)
    assert set(loaded) == {"did:plc:aaa", "did:plc:bbb"}
    assert loaded["did:plc:aaa"].handle == "realname.bsky.social"


def test_load_checkpoint_missing_file_returns_empty(tmp_path):
    assert profiles.load_checkpoint(tmp_path / "missing.jsonl") == {}


def test_append_checkpoint_is_additive(tmp_path):
    path = tmp_path / "checkpoint.jsonl"
    profiles.append_checkpoint(path, [profiles.parse_profile(_RAW_PROFILE_FULL)])
    profiles.append_checkpoint(path, [profiles.parse_profile(_RAW_PROFILE_BARE)])
    loaded = profiles.load_checkpoint(path)
    assert set(loaded) == {"did:plc:aaa", "did:plc:bbb"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pipenv run pytest user-analysis/profiles_test.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'profiles'`

- [ ] **Step 3: Implement**

```python
# user-analysis/profiles.py
"""Stage 1: batch account signals from the public Bluesky AppView.

Unauthenticated `app.bsky.actor.getProfiles` (public.api.bsky.app), 25 DIDs
per call. Retry/backoff mirrors ingex#466's enumerate_labeler.py. Progress
checkpoints to disk as JSONL so an interrupted run resumes instead of
restarting a multi-hour sweep over 129k DIDs.
"""

import dataclasses
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dids import chunk
from scoring import ProfileSignal

PUBLIC_APPVIEW = "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfiles"


def parse_profile(raw: dict) -> ProfileSignal:
    return ProfileSignal(
        did=raw["did"],
        handle=raw.get("handle"),
        created_at=raw.get("createdAt"),
        followers_count=raw.get("followersCount", 0),
        follows_count=raw.get("followsCount", 0),
        posts_count=raw.get("postsCount", 0),
        has_avatar=bool(raw.get("avatar")),
        has_description=bool(raw.get("description")),
        self_labels=[lbl["val"] for lbl in raw.get("labels", []) if lbl.get("val")],
    )


def load_checkpoint(path: Path) -> dict[str, ProfileSignal]:
    if not path.exists():
        return {}
    out: dict[str, ProfileSignal] = {}
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            out[data["did"]] = ProfileSignal(**data)
    return out


def append_checkpoint(path: Path, signals: list[ProfileSignal]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for signal in signals:
            fh.write(json.dumps(dataclasses.asdict(signal)) + "\n")


def _fetch_batch(dids_batch: list[str], attempts: int = 4) -> list[dict]:
    query = urllib.parse.urlencode([("actors", d) for d in dids_batch])
    url = f"{PUBLIC_APPVIEW}?{query}"
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return json.load(resp).get("profiles", [])
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(2 * (attempt + 1))
    raise last  # type: ignore[misc]


def run_stage1(
    all_dids: list[str],
    checkpoint_path: Path,
    batch_size: int = 25,
    checkpoint_every: int = 40,
) -> dict[str, ProfileSignal]:
    known = load_checkpoint(checkpoint_path)
    pending = [d for d in all_dids if d not in known]
    print(f"stage1: {len(known):,} already checkpointed, {len(pending):,} to fetch",
          file=sys.stderr)

    buffer: list[ProfileSignal] = []
    for i, batch in enumerate(chunk(pending, batch_size), start=1):
        raw_profiles = _fetch_batch(batch)
        found_dids = set()
        for raw in raw_profiles:
            signal = parse_profile(raw)
            known[signal.did] = signal
            buffer.append(signal)
            found_dids.add(signal.did)
        for did in batch:
            if did not in found_dids:
                signal = ProfileSignal(
                    did=did, handle=None, created_at=None, followers_count=0,
                    follows_count=0, posts_count=0, has_avatar=False,
                    has_description=False, self_labels=[], fetch_error="not_found",
                )
                known[did] = signal
                buffer.append(signal)

        if i % checkpoint_every == 0:
            append_checkpoint(checkpoint_path, buffer)
            buffer = []
            print(f"stage1: {i * batch_size:,} DIDs processed", file=sys.stderr)

    if buffer:
        append_checkpoint(checkpoint_path, buffer)
    return known
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pipenv run pytest user-analysis/profiles_test.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Lint/type-check and commit**

Run: `pipenv run ruff check user-analysis && pipenv run ruff format --check user-analysis && pipenv run pyright user-analysis`

```bash
git add user-analysis/profiles.py user-analysis/profiles_test.py
git commit -m "Add Stage 1: public API profile signal fetching"
```

---

## Task 4: Stage 2 — skywatch.blue labeler cross-check

**Files:**
- Create: `user-analysis/labeler.py`
- Test: `user-analysis/labeler_test.py`

**Interfaces:**
- Consumes: `scoring.LabelInfo` (Task 2, `from scoring import LabelInfo`).
- Produces (used by Task 6), importable as `from labeler import <name>`:
  - `apply_label_page(page_labels: list[dict], by_val: dict[str, set[str]]) -> tuple[int, int]` — pure, returns `(seen, retracted)` counts; mutates `by_val` in place.
  - `intersect_dids(did_set: set[str], by_val: dict[str, set[str]]) -> dict[str, LabelInfo]` — pure.
  - `resolve_labeler_endpoint(actor: str) -> tuple[str, str]` — network, untested (ported from ingex#466).
  - `enumerate_labeler(labeler: str = DEFAULT_LABELER, values: set[str] | None = None, max_pages: int = 100000) -> dict[str, set[str]]` — network orchestrator, untested (validated via `run_pipeline.py --sample`).
  - `DEFAULT_LABELER = "skywatch.blue"`

- [ ] **Step 1: Write the failing tests**

```python
# user-analysis/labeler_test.py
import labeler
from scoring import LabelInfo


def test_apply_label_page_adds_account_labels():
    by_val: dict[str, set[str]] = {}
    seen, retracted = labeler.apply_label_page(
        [{"uri": "did:plc:aaa", "val": "spam"}, {"uri": "did:plc:bbb", "val": "spam"}],
        by_val,
    )
    assert seen == 2
    assert retracted == 0
    assert by_val == {"spam": {"did:plc:aaa", "did:plc:bbb"}}


def test_apply_label_page_drops_post_level_labels():
    by_val: dict[str, set[str]] = {}
    labeler.apply_label_page(
        [{"uri": "at://did:plc:aaa/app.bsky.feed.post/xyz", "val": "spam"}], by_val
    )
    assert by_val == {}


def test_apply_label_page_handles_retraction():
    by_val = {"spam": {"did:plc:aaa"}}
    seen, retracted = labeler.apply_label_page(
        [{"uri": "did:plc:aaa", "val": "spam", "neg": True}], by_val
    )
    assert seen == 1
    assert retracted == 1
    assert by_val == {"spam": set()}


def test_apply_label_page_skips_labels_without_value():
    by_val: dict[str, set[str]] = {}
    seen, retracted = labeler.apply_label_page([{"uri": "did:plc:aaa"}], by_val)
    assert seen == 1
    assert by_val == {}


def test_intersect_dids_returns_only_matches_with_labels():
    by_val = {"spam": {"did:plc:aaa"}, "amplifier": {"did:plc:aaa", "did:plc:zzz"}}
    result = labeler.intersect_dids({"did:plc:aaa", "did:plc:bbb"}, by_val)
    assert set(result) == {"did:plc:aaa"}
    assert isinstance(result["did:plc:aaa"], LabelInfo)
    assert sorted(result["did:plc:aaa"].labels) == ["amplifier", "spam"]


def test_intersect_dids_empty_when_no_overlap():
    by_val = {"spam": {"did:plc:zzz"}}
    assert labeler.intersect_dids({"did:plc:aaa"}, by_val) == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pipenv run pytest user-analysis/labeler_test.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'labeler'`

- [ ] **Step 3: Implement**

```python
# user-analysis/labeler.py
"""Stage 2: cross-check the DID set against a labeler's account-level labels.

Ports enumerate_labeler.py's wholesale-enumeration approach from ingex#466:
pull the labeler's entire queryLabels feed once (O(labeler size)) and
intersect locally, rather than looking up each DID individually (ingex#466
notes per-DID lookups get rate-limited past ~45k lookups against a similar
labeler).
"""

import collections
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from scoring import LabelInfo

PLC_DIRECTORY = "https://plc.directory/"
PUBLIC_APPVIEW = "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile?actor="
DEFAULT_LABELER = "skywatch.blue"


def _fetch_json(url: str, timeout: int = 30, attempts: int = 4) -> dict:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(2 * (attempt + 1))
    raise last  # type: ignore[misc]


def resolve_labeler_endpoint(actor: str) -> tuple[str, str]:
    did = actor
    if not actor.startswith("did:"):
        did = _fetch_json(PUBLIC_APPVIEW + urllib.parse.quote(actor)).get("did")
        if not did:
            raise ValueError(f"could not resolve handle {actor}")
    doc = _fetch_json(PLC_DIRECTORY + did)
    for service in doc.get("service") or []:
        if service.get("id", "").endswith("atproto_labeler"):
            return did, service["serviceEndpoint"].rstrip("/")
    raise ValueError(f"{actor} declares no #atproto_labeler service")


def apply_label_page(page_labels: list[dict], by_val: dict[str, set[str]]) -> tuple[int, int]:
    seen = retracted = 0
    for label in page_labels:
        seen += 1
        uri, val = label.get("uri") or "", label.get("val")
        if not val or not uri.startswith("did:"):
            continue
        by_val.setdefault(val, set())
        if label.get("neg"):
            by_val[val].discard(uri)
            retracted += 1
        else:
            by_val[val].add(uri)
    return seen, retracted


def enumerate_labeler(
    labeler: str = DEFAULT_LABELER,
    values: set[str] | None = None,
    max_pages: int = 100000,
) -> dict[str, set[str]]:
    did, endpoint = resolve_labeler_endpoint(labeler)
    print(f"labeler: {labeler} ({did}) at {endpoint}", file=sys.stderr)

    by_val: dict[str, set[str]] = collections.defaultdict(set)
    cursor = None
    pages = seen_total = 0
    while pages < max_pages:
        url = f"{endpoint}/xrpc/com.atproto.label.queryLabels?uriPatterns=*&limit=250"
        if cursor:
            url += f"&cursor={urllib.parse.quote(str(cursor))}"
        data = _fetch_json(url)
        page_labels = data.get("labels") or []
        if not page_labels:
            break
        seen, _ = apply_label_page(page_labels, by_val)
        seen_total += seen
        if values is not None:
            for val in list(by_val):
                if val not in values:
                    del by_val[val]
        next_cursor = data.get("cursor")
        pages += 1
        if pages % 200 == 0:
            print(f"labeler: {seen_total:,} labels seen, cursor {next_cursor}", file=sys.stderr)
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return dict(by_val)


def intersect_dids(did_set: set[str], by_val: dict[str, set[str]]) -> dict[str, LabelInfo]:
    hits: dict[str, list[str]] = collections.defaultdict(list)
    for val, val_dids in by_val.items():
        for did in val_dids & did_set:
            hits[did].append(val)
    return {did: LabelInfo(did=did, labels=sorted(vals)) for did, vals in hits.items()}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pipenv run pytest user-analysis/labeler_test.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Lint/type-check and commit**

Run: `pipenv run ruff check user-analysis && pipenv run ruff format --check user-analysis && pipenv run pyright user-analysis`

```bash
git add user-analysis/labeler.py user-analysis/labeler_test.py
git commit -m "Add Stage 2: skywatch.blue labeler cross-check"
```

---

## Task 5: Stage 4 — tiered Firestore activity data

**Files:**
- Create: `user-analysis/firestore_tiers.py`
- Test: `user-analysis/firestore_tiers_test.py`
- Modify: `Pipfile` (add `google-cloud-firestore`), `Pipfile.lock` (regenerate)

**Interfaces:**
- Consumes: `dids.chunk` (Task 1, `from dids import chunk`), `scoring.FirestoreTierA`, `scoring.ScoredRow` (Task 2, `from scoring import ...`).
- Produces (used by Task 6), importable as `from firestore_tiers import <name>`:
  - `user_doc_id(did: str) -> str` — pure.
  - `parse_user_doc(did: str, doc_dict: dict | None) -> FirestoreTierA` — pure.
  - `select_tier_b_sample(scored_rows: list[ScoredRow], top_n: int = 500, control_n: int = 500, seed: int = 0) -> list[str]` — pure, deterministic.
  - `run_tier_a(all_dids: list[str], project: str, database: str) -> dict[str, FirestoreTierA]` — network orchestrator, untested.
  - `run_tier_b(sample_dids: list[str], project: str, database: str) -> dict[str, dict]` — network orchestrator, untested; returns raw subcollection docs per DID for the summary stage to inspect.

- [ ] **Step 1: Write the failing tests**

```python
# user-analysis/firestore_tiers_test.py
import firestore_tiers
from scoring import FirestoreTierA, ProfileSignal, ScoredRow, score_row


def _row(did: str, score: float) -> ScoredRow:
    profile = ProfileSignal(
        did=did, handle=None, created_at=None, followers_count=0, follows_count=0,
        posts_count=0, has_avatar=False, has_description=False, self_labels=[],
    )
    flags = {name: False for name in ("created_during_spike", "no_profile_content",
                                       "handle_looks_random", "self_declared_bot",
                                       "labeler_flagged")}
    return ScoredRow(did=did, profile=profile, labels=None, firestore=None,
                      flags=flags, score=score)


def test_user_doc_id_strips_plc_prefix():
    assert firestore_tiers.user_doc_id("did:plc:abc123") == "abc123"


def test_user_doc_id_passthrough_for_non_plc():
    assert firestore_tiers.user_doc_id("did:web:example.com") == "did:web:example.com"


def test_parse_user_doc_found():
    result = firestore_tiers.parse_user_doc(
        "did:plc:aaa",
        {"created_at": "2026-08-18T15:00:00Z", "last_seen_at": "2026-08-19T00:00:00Z",
         "social_radius": "1"},
    )
    assert result == FirestoreTierA(
        did="did:plc:aaa", found=True, created_at="2026-08-18T15:00:00Z",
        last_seen_at="2026-08-19T00:00:00Z", social_radius="1",
    )


def test_parse_user_doc_not_found():
    result = firestore_tiers.parse_user_doc("did:plc:aaa", None)
    assert result == FirestoreTierA(
        did="did:plc:aaa", found=False, created_at=None, last_seen_at=None, social_radius=None,
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pipenv run pytest user-analysis/firestore_tiers_test.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'firestore_tiers'`

- [ ] **Step 3: Add the dependency and implement**

Run: `pipenv install google-cloud-firestore`

```python
# user-analysis/firestore_tiers.py
"""Stage 4: prod Firestore activity data, tiered by cost.

Tier A reads users/{doc_id} for every DID (cheap: one batched get_all() pass).
Tier B reads feed_activity/interactions/seen_posts subcollections for a
sample only — interactions isn't indexed for a user_did-filtered
collection-group query today, and per-user subcollection reads at 129k scale
would be far more expensive than a sample already answers the "do they
behave like a real client" question for.

Requires GE_FIRESTORE_PROJECT / GE_FIRESTORE_DATABASE and application-default
credentials, same as api/scripts/apikeys.py and api/scripts/feed_debug.py.
"""

import random
import sys

from google.cloud import firestore

from dids import chunk
from scoring import FirestoreTierA, ScoredRow

PLC_PREFIX = "did:plc:"


def user_doc_id(did: str) -> str:
    return did[len(PLC_PREFIX):] if did.startswith(PLC_PREFIX) else did


def parse_user_doc(did: str, doc_dict: dict | None) -> FirestoreTierA:
    if doc_dict is None:
        return FirestoreTierA(did=did, found=False, created_at=None, last_seen_at=None,
                               social_radius=None)
    return FirestoreTierA(
        did=did,
        found=True,
        created_at=doc_dict.get("created_at"),
        last_seen_at=doc_dict.get("last_seen_at"),
        social_radius=doc_dict.get("social_radius"),
    )


def select_tier_b_sample(
    scored_rows: list[ScoredRow],
    top_n: int = 500,
    control_n: int = 500,
    seed: int = 0,
) -> list[str]:
    ranked = sorted(scored_rows, key=lambda r: r.score, reverse=True)
    top = [r.did for r in ranked[:top_n]]
    remainder = [r.did for r in ranked[top_n:]]
    rng = random.Random(seed)
    control = rng.sample(remainder, min(control_n, len(remainder)))
    return top + control


def run_tier_a(all_dids: list[str], project: str, database: str) -> dict[str, FirestoreTierA]:
    db = firestore.Client(project=project, database=database)
    results: dict[str, FirestoreTierA] = {}
    for batch in chunk(all_dids, 500):
        refs = [db.collection("users").document(user_doc_id(did)) for did in batch]
        found_by_ref = {}
        for snapshot in db.get_all(refs):
            if snapshot.exists:
                found_by_ref[snapshot.id] = snapshot.to_dict()
        for did in batch:
            results[did] = parse_user_doc(did, found_by_ref.get(user_doc_id(did)))
        print(f"tier A: {len(results):,}/{len(all_dids):,} DIDs", file=sys.stderr)
    return results


def run_tier_b(sample_dids: list[str], project: str, database: str) -> dict[str, dict]:
    db = firestore.Client(project=project, database=database)
    results: dict[str, dict] = {}
    for did in sample_dids:
        doc_id = user_doc_id(did)
        user_ref = db.collection("users").document(doc_id)
        results[did] = {
            "feed_activity": [d.to_dict() for d in user_ref.collection("feed_activity").stream()],
            "interactions": [
                d.to_dict()
                for d in db.collection("interactions").where("user_did", "==", did).stream()
            ],
            "seen_posts": [d.to_dict() for d in user_ref.collection("seen_posts").stream()],
        }
    print(f"tier B: pulled behavioral data for {len(results):,} sampled DIDs", file=sys.stderr)
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pipenv run pytest user-analysis/firestore_tiers_test.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Lint/type-check and commit**

Run: `pipenv run ruff check user-analysis && pipenv run ruff format --check user-analysis && pipenv run pyright user-analysis`

```bash
git add user-analysis/firestore_tiers.py user-analysis/firestore_tiers_test.py Pipfile Pipfile.lock
git commit -m "Add Stage 4: tiered Firestore activity data"
```

---

## Task 6: Pipeline orchestration + CSV/summary output

**Files:**
- Create: `user-analysis/run_pipeline.py`
- Test: `user-analysis/run_pipeline_test.py`

**Interfaces:**
- Consumes: everything from Tasks 1-5, all via bare top-level imports (`from dids import load_dids`, `from firestore_tiers import ...`, `from labeler import ...`, `from profiles import run_stage1`, `from scoring import ...`).
- Produces: `format_csv_rows(rows: list[ScoredRow]) -> list[dict]` (pure), `build_summary(rows: list[ScoredRow], tier_b: dict[str, dict]) -> str` (pure), `main(argv: list[str] | None = None) -> int` (CLI entrypoint).

- [ ] **Step 1: Write the failing tests**

```python
# user-analysis/run_pipeline_test.py
from firestore_tiers import parse_user_doc
from run_pipeline import build_summary, format_csv_rows
from scoring import LabelInfo, ProfileSignal, score_row


def _row(did, created_during_spike=False, labeler_flagged=False):
    profile = ProfileSignal(
        did=did, handle="h.bsky.social", created_at=None, followers_count=1,
        follows_count=1, posts_count=1, has_avatar=True, has_description=True,
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pipenv run pytest user-analysis/run_pipeline_test.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'run_pipeline'`

- [ ] **Step 3: Implement**

```python
# user-analysis/run_pipeline.py
"""Orchestrates Stages 1-4 and writes the scored CSV + findings summary.

Usage (run as a script, not via `-m` — see the hyphenated-directory note in
the plan/spec for why):
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

FLAG_NAMES = ("created_during_spike", "no_profile_content", "handle_looks_random",
              "self_declared_bot", "labeler_flagged")


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
    parser.add_argument("--summary-out", type=Path,
                         default=Path("user-analysis/data/summary.md"))
    parser.add_argument("--checkpoint", type=Path,
                         default=Path("user-analysis/data/profiles_checkpoint.jsonl"))
    parser.add_argument("--labeler", default=DEFAULT_LABELER)
    parser.add_argument("--sample", type=int, default=0,
                         help="only process the first N DIDs (0 = all)")
    parser.add_argument("--tier-b-top-n", type=int, default=500)
    parser.add_argument("--tier-b-control-n", type=int, default=500)
    parser.add_argument("--firestore-project", default="greenearth-471522")
    parser.add_argument("--firestore-database", default="greenearth-prod")
    parser.add_argument("--skip-firestore", action="store_true",
                         help="skip Stages 4A/4B (public API + labeler only)")
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
        score_row(did, profiles.get(did), labels.get(did), firestore_a.get(did))
        for did in all_dids
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pipenv run pytest user-analysis/run_pipeline_test.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Lint/type-check and commit**

Run: `pipenv run ruff check user-analysis && pipenv run ruff format --check user-analysis && pipenv run pyright user-analysis`

```bash
git add user-analysis/run_pipeline.py user-analysis/run_pipeline_test.py
git commit -m "Add pipeline orchestration and CSV/summary output"
```

---

## Task 7: README + full test suite + small-sample end-to-end validation

**Files:**
- Create: `user-analysis/README.md`

**Interfaces:** none (documentation + validation task).

- [ ] **Step 1: Write the README**

```markdown
# user-analysis

Bot/inauthentic-user analysis for the 08-18 growth spike (api#426). Scores
the 129,235 DIDs exported from Posthog (`data/*-users.csv`, gitignored)
against public Bluesky API profile signals, a skywatch.blue labeler
cross-check, and tiered prod Firestore activity data.

See `../docs/superpowers/specs/2026-08-20-user-analysis-design.md` for the
full design.

This directory's name has a hyphen, so it is **not** an importable Python
package: every module uses bare top-level imports (`import dids`, not
`from . import dids`), matching `../devenv/`'s existing convention for
standalone script directories. Run scripts directly (`python
user-analysis/run_pipeline.py`), never via `python -m`.

## Usage

```bash
# From the internal-tools repo root:
pipenv run pytest user-analysis

# Validate end-to-end on a small slice first (skips Firestore by default
# unless you have gcloud auth login'd against prod):
pipenv run python user-analysis/run_pipeline.py --sample 500 --skip-firestore

# Full run, public API + labeler only:
pipenv run python user-analysis/run_pipeline.py

# Full run including tiered Firestore data (needs `gcloud auth login` and
# read access to greenearth-471522/greenearth-prod):
pipenv run python user-analysis/run_pipeline.py --tier-b-top-n 500 --tier-b-control-n 500
```

Outputs land in `data/`: `user_analysis.csv` (per-DID signals/flags/score),
`summary.md` (aggregate findings), `profiles_checkpoint.jsonl` (resumable
Stage 1 progress — safe to delete to force a full re-fetch).
```

- [ ] **Step 2: Run the full test suite**

Run: `pipenv run pytest user-analysis -v`
Expected: PASS (all tests from Tasks 1-6)

- [ ] **Step 3: Run full lint/type-check across the package**

Run: `pipenv run ruff check user-analysis && pipenv run ruff format --check user-analysis && pipenv run pyright user-analysis`
Expected: clean

- [ ] **Step 4: Validate end-to-end on a small sample (public API + labeler only)**

Run: `pipenv run python user-analysis/run_pipeline.py --sample 50 --skip-firestore --out user-analysis/data/sample_50.csv --summary-out user-analysis/data/sample_50_summary.md`
Expected: completes without error; `sample_50.csv` has 50 data rows; `sample_50_summary.md` prints a non-empty flag breakdown. Report the summary contents back for review — this is the checkpoint before running Stage 4 (Firestore) or the full 129k-DID sweep, both of which need explicit go-ahead (Firestore needs `gcloud auth login`; the full sweep takes hours).

- [ ] **Step 5: Commit**

```bash
git add user-analysis/README.md
git commit -m "Add user-analysis README"
```

---

## After Task 7

Do not proceed past the Task 7 sample validation without checking in — the
next steps (running Stage 4 against prod Firestore, then the full
129,235-DID sweep, then sharing `summary.md` for review, then opening the
draft PR) all depend on what the small-sample run shows and on prod GCP
auth being available. Stop and report the sample results here first.
