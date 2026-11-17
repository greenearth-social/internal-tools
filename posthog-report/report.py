"""posthog-report: the last WEEKS whole weeks of MySky in one box, one row per metric:
its weeks as a strip of bars, last week's count, and the change from the week before.

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
import re
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

BARS = "▁▂▃▄▅▆▇█"
# The colours a Discord `ansi` code block understands, by their escape number.
GREEN, YELLOW, CYAN = 32, 33, 36
ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

# (name, HogQL aggregate, whether a rise is good news) per metric, in the order printed
COLUMNS = [
    ("Active", "count(distinct if(event = 'interactionSeen', person_id, null))", True),
    ("Likes", "countIf(event = 'interactionLike')", True),
    ("Reposts", "countIf(event = 'interactionRepost')", True),
    ("Replies", "countIf(event = 'interactionReply')", True),
    ("Show more", "countIf(event = 'requestMore')", True),
    ("Show less", "countIf(event = 'requestLess')", False),
]
NOTE = "Each histogram is normalized to its own max."
LAST_BAR = "The last bar is the week of {}."  # filled with that week's Monday

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
    cols = ",\n  ".join(f"{agg} AS c{i}" for i, (_, agg, _) in enumerate(COLUMNS))
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


def bars(values: list[int]) -> str:
    """One bar per value, each as tall as its share of the highest one."""
    top = max(values)
    return "".join(
        BARS[min(len(BARS) - 1, len(BARS) * v // top)] if top else BARS[0] for v in values
    )


def paint(text: str, colour: int | None) -> str:
    """The text in that colour, or as it is when there is none."""
    return f"\x1b[{colour}m{text}\x1b[0m" if colour else text


def shown(text: str) -> int:
    """How many columns the text takes once its colour escapes are left out."""
    return len(ESCAPE.sub("", text))


def verdict(new: int, old: int, rise_is_good: bool) -> int | None:
    """Green for a change in the good direction, yellow for the other, nothing when flat."""
    if not old or new == old:
        return None
    return GREEN if (new > old) == rise_is_good else YELLOW


def table(rows: list[Week], color: bool = False) -> list[str]:
    """One line per metric, a blank line between them so the bars of two rows never touch."""
    grid = [["", f"{WEEKS} Week Summary", f"Week of {rows[-1][0]}", "Change vs Prev"]]
    colours: list[list[int | None]] = [[None] * 4]
    for i, (name, _, rise_is_good) in enumerate(COLUMNS):
        series = [vals[i] for _, vals in rows]
        new, old = series[-1], series[-2] if len(series) > 1 else 0
        grid.append([name, bars(series), f"{new:,}", change(new, old)])
        colours.append([None, CYAN, None, verdict(new, old, rise_is_good)])
    widths = [max(len(r[i]) for r in grid) for i in range(len(grid[0]))]
    out = []
    for cells, tints in zip(grid, colours, strict=True):
        padded = [
            f"{c:<{w}}" if i < 2 else f"{c:>{w}}"
            for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        ]
        if color:
            padded = [paint(c, t) for c, t in zip(padded, tints, strict=True)]
        out += ["  ".join(padded), ""]
    return out[:-1]


def box(title: str, lines: list[str], notes: list[str]) -> str:
    """The lines in a rounded box with the title set into its top edge, each note wrapped under."""
    inner = max(len(title) + 2, *(shown(x) for x in lines))
    lines = [*lines, "", *(x for note in notes for x in textwrap.wrap(note, inner))]
    width = inner + 2 * PAD
    top = "╭─ " + title + " " + "─" * (width - len(title) - 3) + "╮"
    mid = ["│" + " " * PAD + x + " " * (inner - shown(x) + PAD) + "│" for x in ["", *lines, ""]]
    return "\n".join([top, *mid, "╰" + "─" * width + "╯"])


def report(rows: list[Week], color: bool = False) -> str:
    """The whole box, plain for a terminal or coloured for Discord."""
    notes = [NOTE, LAST_BAR.format(rows[-1][0])]
    return box("MySky · Weekly", table(rows, color), notes)


def message(rows: list[Week]) -> str:
    """The Discord message: the coloured box in an `ansi` code block, which keeps it monospace."""
    return "```ansi\n" + report(rows, color=True) + "\n```"


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
        print(report(rows))
        return
    post_json(e["DISCORD_WEBHOOK_URL"], {"content": content})


if __name__ == "__main__":
    main()
