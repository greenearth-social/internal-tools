"""posthog-report: the last WEEKS whole weeks of MySky in one box, one row per week,
oldest at the top, and the change from the week before under it.

    python report.py           print the box (what would be posted)
    python report.py --post    post it to DISCORD_WEBHOOK_URL as a code block

Keys come from the environment, or from .env beside this file when not set there:
POSTHOG_API_KEY, POSTHOG_PROJECT_ID, DISCORD_WEBHOOK_URL, POSTHOG_HOST (default US cloud).
A week runs Monday to Sunday in the PostHog project's timezone; the week in progress is left out.

Standard library only, so the scheduled run needs no install step.
"""

import argparse
import json
import os
import sys
import textwrap
import urllib.request
from pathlib import Path
from typing import Any

WEEKS = 8
PAD = 2  # columns between the box's edge and its contents
DISCORD_LIMIT = 2000  # characters Discord accepts in one message
# Discord turns away urllib's default User-Agent, so name ourselves.
USER_AGENT = "greenearth-posthog-report/1.0"

# (header, HogQL aggregate) per column, in the order printed
COLUMNS = [
    ("active", "count(distinct if(event = 'interactionSeen', person_id, null))"),
    ("likes", "countIf(event = 'interactionLike')"),
    ("reposts", "countIf(event = 'interactionRepost')"),
    ("replies", "countIf(event = 'interactionReply')"),
    ("more", "countIf(event = 'requestMore')"),
    ("less", "countIf(event = 'requestLess')"),
    ("sign-ins", "countIf(event = 'signInCompleted')"),
    ("failed", "countIf(event = 'signInFailed')"),
]
NOTE = (
    "active = people who saw a post · more / less = 'show me more / less like this' "
    "· sign-ins = completed, failed = sign-in errors · Δ = last week vs the one before"
)

Week = tuple[str, list[int]]


def env() -> dict[str, str]:
    """The environment, with .env filling in whatever it does not set."""
    vals = {}
    path = Path(__file__).with_name(".env")
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip("'\"")
    return {**vals, **os.environ}


def post_json(url: str, payload: dict, token: str | None = None) -> Any:
    """POST the payload as JSON; the parsed reply, or None when there is no body."""
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=120) as r:  # raises on any non-2xx reply
        body = r.read()
    return json.loads(body) if body else None


def weeks(e: dict[str, str]) -> list[Week]:
    """[(monday 'MM-DD', [value per column])] for the last WEEKS whole weeks, oldest first."""
    cols = ",\n  ".join(f"{agg} AS c{i}" for i, (_, agg) in enumerate(COLUMNS))
    sql = f"""
SELECT toStartOfWeek(timestamp, 1) AS week,
  {cols}
FROM events
WHERE timestamp >= toStartOfWeek(now(), 1) - INTERVAL {WEEKS} WEEK
  AND timestamp < toStartOfWeek(now(), 1)
GROUP BY week ORDER BY week"""
    host = e.get("POSTHOG_HOST", "https://us.posthog.com").rstrip("/")
    reply = post_json(
        f"{host}/api/projects/{e['POSTHOG_PROJECT_ID']}/query/",
        {"query": {"kind": "HogQLQuery", "query": sql}},
        token=e["POSTHOG_API_KEY"],
    )
    return [(str(row[0])[5:10], list(row[1:])) for row in reply["results"]]


def change(new: int, old: int) -> str:
    """Signed percent change, '·' when there is nothing to compare against."""
    return "·" if not old else f"{100 * (new - old) / old:+.0f}%"


def table(rows: list[Week]) -> list[str]:
    """Plain columns, week left, numbers right, with a Δ row under a rule."""
    grid = [["week", *(h for h, _ in COLUMNS)]]
    grid += [[w, *(f"{v:,}" for v in vals)] for w, vals in rows]
    if len(rows) > 1:
        grid.append(["Δ", *(change(n, o) for n, o in zip(rows[-1][1], rows[-2][1], strict=True))])
    widths = [max(len(r[i]) for r in grid) for i in range(len(grid[0]))]

    def line(cells: list[str]) -> str:
        return "  ".join(
            f"{c:<{w}}" if i == 0 else f"{c:>{w}}"
            for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        )

    out = [line(r) for r in grid]
    if len(rows) > 1:
        out.insert(-1, "─" * len(out[0]))
    return out


def box(title: str, lines: list[str], note: str) -> str:
    """The lines in a rounded box with the title set into its top edge, the note wrapped under."""
    inner = max(len(title) + 2, *(len(x) for x in lines))
    lines = [*lines, "", *textwrap.wrap(note, inner)]
    width = inner + 2 * PAD
    top = "╭─ " + title + " " + "─" * (width - len(title) - 3) + "╮"
    mid = ["│" + " " * PAD + x.ljust(inner) + " " * PAD + "│" for x in ["", *lines, ""]]
    return "\n".join([top, *mid, "╰" + "─" * width + "╯"])


def message(rows: list[Week]) -> str:
    """The Discord message: the box in a code block, so it keeps its monospace shape."""
    return "```\n" + box("MySky · weekly, Mon–Sun", table(rows), NOTE) + "\n```"


def main():
    p = argparse.ArgumentParser(description="MySky weekly PostHog box")
    p.add_argument("--post", action="store_true", help="post to Discord instead of printing")
    a = p.parse_args()
    e = env()
    rows = weeks(e)
    if not rows:
        sys.exit("no events in the last weeks")
    content = message(rows)
    if len(content) > DISCORD_LIMIT:
        sys.exit(f"message is {len(content)} characters, Discord takes {DISCORD_LIMIT}")
    if not a.post:
        print(content.removeprefix("```\n").removesuffix("\n```"))
        return
    post_json(e["DISCORD_WEBHOOK_URL"], {"content": content})


if __name__ == "__main__":
    main()
