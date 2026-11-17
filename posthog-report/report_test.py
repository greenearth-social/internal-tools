import report

AUDIENCE, ENGAGEMENT = (names for _, names in report.BOXES)
NAMES = AUDIENCE + ENGAGEMENT
ROWS = [
    ("11-02", dict.fromkeys(NAMES, 10) | {"Likes": 40, "Replies": 5, "Show more": 0}),
    ("11-09", dict.fromkeys(NAMES, 10) | {"Likes": 30, "Replies": 0, "Show more": 8}),
]
ROWS[0][1]["Show less"], ROWS[1][1]["Show less"] = 2, 3


def test_every_row_is_fetched_and_every_fetched_metric_is_a_row():
    assert sorted(NAMES) == sorted([*report.COUNTS, *report.QUERIES])


def test_change_is_signed_percent_and_dot_without_a_base():
    assert report.change(150, 100) == "+50%"
    assert report.change(30, 40) == "-25%"
    assert report.change(10, 10) == "+0%"
    assert report.change(1, 0) == "·"


def test_bars_scale_to_the_highest_value_and_stay_flat_when_all_are_zero():
    assert report.bars([0, 50, 100]) == "▁▅█"
    assert report.bars([7, 7]) == "██"
    assert report.bars([0, 0]) == "▁▁"


def test_table_has_a_header_then_one_spaced_row_per_metric():
    lines = report.table(ROWS, ENGAGEMENT)
    assert lines[0].split() == "8 Week Summary Week of 11-09 Change vs Prev".split()
    assert lines[1::2] == [""] * len(ENGAGEMENT)
    assert [x.split() for x in lines[2::2]] == [
        ["Likes", "█▇", "30", "-25%"],
        ["Reposts", "██", "10", "+0%"],
        ["Replies", "█▁", "0", "-100%"],
        ["Show", "more", "▁█", "8", "·"],
        ["Show", "less", "▆█", "3", "+50%"],
    ]
    assert len({len(x) for x in lines if x}) == 1


def test_table_of_a_single_week_has_no_change_to_show():
    assert [x.split()[-1] for x in report.table(ROWS[:1], NAMES)[2::2]] == ["·"] * len(NAMES)


def test_boxes_are_one_width_and_only_the_last_carries_the_notes():
    first, last = report.report(ROWS)
    assert len({len(x) for x in (first + "\n" + last).splitlines()}) == 1
    assert report.NOTE not in first and report.NOTE in last


def test_colour_changes_nothing_but_the_escapes():
    coloured = "\n".join(report.report(ROWS, color=True))
    assert report.ESCAPE.sub("", coloured) == "\n".join(report.report(ROWS))


def test_change_is_green_when_good_yellow_when_bad_and_plain_when_flat():
    assert report.verdict(150, 100, True) == report.GREEN
    assert report.verdict(30, 40, True) == report.YELLOW
    assert report.verdict(3, 2, False) == report.YELLOW
    assert report.verdict(2, 3, False) == report.GREEN
    assert report.verdict(10, 10, True) is None
    assert report.verdict(8, 0, True) is None
    lines = report.table(ROWS, ENGAGEMENT, color=True)
    assert "\x1b[33m" in lines[2] and "\x1b[32m" not in lines[2]  # likes fell
    assert "\x1b[33m" in lines[-1] and "\x1b[32m" not in lines[-1]  # show less rose


def test_each_box_of_large_weeks_fits_in_one_discord_message():
    rows = [(f"01-{d:02}", dict.fromkeys(NAMES, 9_999_999)) for d in range(1, report.WEEKS + 1)]
    assert max(len(x) for x in report.messages(rows)) <= report.DISCORD_LIMIT


def test_the_last_message_ends_with_a_link_to_the_readme():
    first, last = report.messages(ROWS)
    assert first.endswith("```") and last.endswith(f"[Row definitions (README)](<{report.README}>)")


def test_weeks_merges_the_replies_of_every_query(monkeypatch):
    sent = []

    def fake_post_json(url, payload, token=None):
        sql = payload["query"]["query"]
        sent.append((url, token, sql))
        if sql in report.QUERIES.values():  # one value per week, the second week missing
            return {"results": [["2026-11-02", 7.0]]}
        counts = range(len(report.COUNTS))
        return {"results": [["2026-11-02T00:00:00Z", *counts], ["2026-11-09", *counts]]}

    monkeypatch.setattr(report, "post_json", fake_post_json)
    rows = report.weeks({"POSTHOG_PROJECT_ID": "42", "POSTHOG_API_KEY": "key"})
    assert [week for week, _ in rows] == ["11-02", "11-09"]
    assert all(sorted(values) == sorted(NAMES) for _, values in rows)
    assert rows[0][1]["Likes"] == list(report.COUNTS).index("Likes")
    assert [values["New users"] for _, values in rows] == [7, 0]
    assert {(url, token) for url, token, _ in sent} == {
        ("https://us.posthog.com/api/projects/42/query/", "key")
    }
    assert len(sent) == 1 + len(report.QUERIES)
