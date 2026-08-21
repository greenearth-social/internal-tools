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
    print(
        f"stage1: {len(known):,} already checkpointed, {len(pending):,} to fetch",
        file=sys.stderr,
    )

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
                    did=did,
                    handle=None,
                    created_at=None,
                    followers_count=0,
                    follows_count=0,
                    posts_count=0,
                    has_avatar=False,
                    has_description=False,
                    self_labels=[],
                    fetch_error="not_found",
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
