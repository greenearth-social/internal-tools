import report

LEFT, RIGHT = report.SIDES
NAMES = LEFT + RIGHT
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


def test_side_has_a_header_then_one_line_per_metric_all_one_width():
    lines = report.side(ROWS, RIGHT, color=False)
    assert [x.split() for x in lines] == [
        ["8", "Weeks", "11-09", "vs", "Prev"],
        ["Likes", "█▇", "30", "-25%"],
        ["Reposts", "██", "10", "+0%"],
        ["Replies", "█▁", "0", "-100%"],
        ["Show", "more", "▁█", "8", "·"],
        ["Show", "less", "▆█", "3", "+50%"],
        ["Setting", "changes", "██", "10", "+0%"],
    ]
    assert len({len(x) for x in lines}) == 1


def test_side_of_a_single_week_has_no_change_to_show():
    assert [x.split()[-1] for x in report.side(ROWS[:1], LEFT, False)[1:]] == ["·"] * len(LEFT)


def test_table_sets_the_sides_either_side_of_one_unbroken_rule():
    lines = report.table(ROWS)
    assert len(lines) == 2 * (1 + max(len(LEFT), len(RIGHT))) - 1
    assert len({x.index("│") for x in lines}) == 1
    assert lines[2].split("│")[0].split()[:2] == ["Weekly", "active"]
    assert lines[2].split("│")[1].split()[0] == "Likes"
    assert lines[-1].split("│")[1] == ""  # the right side is the shorter one


def test_report_is_the_table_between_two_rules_with_the_notes_under_them():
    lines = report.report(ROWS).splitlines()
    assert lines[0].startswith(f"╭─ {report.TITLE} ─") and lines[0].endswith("╮")
    assert lines[-3].startswith("╰") and len(lines[-3]) == len(lines[0])
    assert [x.strip() for x in lines[-2:]] == [report.NOTE, report.LAST_BAR.format("11-09")]
    assert max(len(x) for x in lines[1:-3]) == len(lines[0]) - report.PAD - 1


def test_colour_changes_nothing_but_the_escapes():
    assert report.ESCAPE.sub("", report.report(ROWS, color=True)) == report.report(ROWS)


def test_change_is_green_when_good_yellow_when_bad_and_plain_when_flat():
    assert report.verdict(150, 100, True) == report.GREEN
    assert report.verdict(30, 40, True) == report.YELLOW
    assert report.verdict(3, 2, False) == report.YELLOW
    assert report.verdict(2, 3, False) == report.GREEN
    assert report.verdict(10, 10, True) is None
    assert report.verdict(8, 0, True) is None
    lines = report.side(ROWS, RIGHT, color=True)
    assert "\x1b[33m" in lines[1] and "\x1b[32m" not in lines[1]  # likes fell
    assert "\x1b[33m" in lines[-2] and "\x1b[32m" not in lines[-2]  # show less rose


def test_a_report_of_large_weeks_fits_in_one_discord_message():
    rows = [(f"01-{d:02}", dict.fromkeys(NAMES, 9_999_999)) for d in range(1, report.WEEKS + 1)]
    assert len(report.message(rows)) <= report.DISCORD_LIMIT


def test_the_message_ends_with_a_link_to_the_readme():
    assert report.message(ROWS).endswith(f"[Row definitions (README)](<{report.README}>)")


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
