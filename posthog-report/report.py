"""posthog-report: the last WEEKS whole weeks of MySky in two boxes, one row per metric:
its weeks as a strip of bars, last week's count, and the change from the week before.
README.md says what each row means.

    python report.py           print the boxes (what would be posted)
    python report.py --post    post them to DISCORD_WEBHOOK_URL, one code block each

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

# Our own uptime check asks for the feeds all day as this one account; it is not a user.
PROBE = "did:plc:s4tl2ajfsnstzuxtegl7r33g"
SEEN = "event = 'interactionSeen'"
# The Bluesky app asks for 30 posts when a feed is opened and for 8 to preview it on Explore.
FIRST_PAGE = f"event = 'feedLoaded' AND distinct_id != '{PROBE}' AND properties.has_cursor = false"
FEED = f"{FIRST_PAGE} AND toInt(properties.requested_limit) > 8"
EXPLORE = f"{FIRST_PAGE} AND toInt(properties.requested_limit) BETWEEN 2 AND 8"

# name -> HogQL aggregate over one week's events
COUNTS = {
    "Weekly active": f"count(distinct if({SEEN}, person_id, null))",
    "Feed loads": f"countIf({FEED})",
    "Feed users": f"count(distinct if({FEED}, person_id, null))",
    "Explore loads": f"countIf({EXPLORE})",
    "Explore users": f"count(distinct if({EXPLORE}, person_id, null))",
    "Likes": "countIf(event = 'interactionLike')",
    "Reposts": "countIf(event = 'interactionRepost')",
    "Replies": "countIf(event = 'interactionReply')",
    "Show more": "countIf(event = 'requestMore')",
    "Show less": "countIf(event = 'requestLess')",
}
LAST_WEEKS = "{0} >= toStartOfWeek(now(), 1) - INTERVAL {1} WEEK AND {0} < toStartOfWeek(now(), 1)"
# name -> HogQL returning (week, value); these need more than one pass over the events
QUERIES = {
    "Daily active (avg)": f"""
SELECT toStartOfWeek(day, 1) AS week, round(sum(people) / 7)
FROM (SELECT toDate(timestamp) AS day, count(distinct person_id) AS people
      FROM events WHERE {SEEN} AND {LAST_WEEKS.format("timestamp", WEEKS)} GROUP BY day)
GROUP BY week""",
    "New users": f"""
SELECT toStartOfWeek(began, 1) AS week, count()
FROM (SELECT person_id, min(timestamp) AS began FROM events WHERE {SEEN} GROUP BY person_id)
WHERE {LAST_WEEKS.format("began", WEEKS)}
GROUP BY week""",
}
# (title, its rows) per box, in the order posted
BOXES = [
    (
        "MySky · Weekly · Audience",
        [
            "Weekly active",
            "Daily active (avg)",
            "New users",
            "Feed loads",
            "Feed users",
            "Explore loads",
            "Explore users",
        ],
    ),
    ("MySky · Weekly · Engagement", ["Likes", "Reposts", "Replies", "Show more", "Show less"]),
]
FALL_IS_GOOD = {"Show less"}
NOTE = "Each histogram is normalized to its own max."
README = "https://github.com/greenearth-social/internal-tools/tree/main/posthog-report#readme"
LAST_BAR = "The last bar is the week of {}."  # filled with that week's Monday

Week = tuple[str, dict[str, int]]


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


def query(e: dict[str, str], sql: str) -> list[list]:
    """The rows PostHog returns for one HogQL query."""
    host = e.get("POSTHOG_HOST", "https://us.posthog.com").rstrip("/")
    reply = post_json(
        f"{host}/api/projects/{e['POSTHOG_PROJECT_ID']}/query/",
        {"query": {"kind": "HogQLQuery", "query": sql}},
        token=e["POSTHOG_API_KEY"],
    )
    return reply["results"]


def monday(value: object) -> str:
    """'MM-DD' out of the week's date as PostHog writes it."""
    return str(value)[5:10]


def weeks(e: dict[str, str]) -> list[Week]:
    """[(monday 'MM-DD', {metric: value})] for the last WEEKS whole weeks, oldest first."""
    cols = ",\n  ".join(f"{agg} AS c{i}" for i, agg in enumerate(COUNTS.values()))
    sql = f"""
SELECT toStartOfWeek(timestamp, 1) AS week,
  {cols}
FROM events
WHERE {LAST_WEEKS.format("timestamp", WEEKS)}
GROUP BY week ORDER BY week"""
    rows = [(monday(r[0]), dict(zip(COUNTS, r[1:], strict=True))) for r in query(e, sql)]
    for name, sql in QUERIES.items():
        found = {monday(week): int(value) for week, value in query(e, sql)}
        for week, values in rows:
            values[name] = found.get(week, 0)
    return rows


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


def table(rows: list[Week], names: list[str], color: bool = False) -> list[str]:
    """One line per metric, a blank line between them so the bars of two rows never touch."""
    label = max(len(name) for _, box_names in BOXES for name in box_names)  # same in every box
    grid = [[" " * label, f"{WEEKS} Week Summary", f"Week of {rows[-1][0]}", "Change vs Prev"]]
    colours: list[list[int | None]] = [[None] * 4]
    for name in names:
        series = [vals[name] for _, vals in rows]
        new, old = series[-1], series[-2] if len(series) > 1 else 0
        grid.append([name, bars(series), f"{new:,}", change(new, old)])
        colours.append([None, CYAN, None, verdict(new, old, name not in FALL_IS_GOOD)])
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
    if notes:
        lines = [*lines, "", *(x for note in notes for x in textwrap.wrap(note, inner))]
    width = inner + 2 * PAD
    top = "╭─ " + title + " " + "─" * (width - len(title) - 3) + "╮"
    mid = ["│" + " " * PAD + x + " " * (inner - shown(x) + PAD) + "│" for x in ["", *lines, ""]]
    return "\n".join([top, *mid, "╰" + "─" * width + "╯"])


def report(rows: list[Week], color: bool = False) -> list[str]:
    """Every box, plain for a terminal or coloured for Discord; the notes go under the last."""
    notes = [NOTE, LAST_BAR.format(rows[-1][0])]
    return [
        box(title, table(rows, names, color), notes if i == len(BOXES) - 1 else [])
        for i, (title, names) in enumerate(BOXES)
    ]


def messages(rows: list[Week]) -> list[str]:
    """One Discord message per box, each in an `ansi` code block, which keeps it monospace."""
    out = ["```ansi\n" + x + "\n```" for x in report(rows, color=True)]
    # Under the last box, outside the block so it is a link; <> stops Discord previewing it.
    out[-1] += f"\n[Row definitions (README)](<{README}>)"
    return out


def main():
    p = argparse.ArgumentParser(description="MySky weekly PostHog boxes")
    p.add_argument("--post", action="store_true", help="post to Discord instead of printing")
    a = p.parse_args()
    e = env()
    rows = weeks(e)
    if not rows:
        sys.exit("no events in the last weeks")
    contents = messages(rows)
    longest = max(len(x) for x in contents)
    if longest > DISCORD_LIMIT:
        sys.exit(f"a message is {longest} characters, Discord takes {DISCORD_LIMIT}")
    if not a.post:
        print("\n".join(report(rows)))
        return
    for content in contents:
        post_json(e["DISCORD_WEBHOOK_URL"], {"content": content})


if __name__ == "__main__":
    main()
