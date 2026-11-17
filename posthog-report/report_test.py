import report

ROWS = [
    ("11-02", [100, 40, 10, 5, 0, 2]),
    ("11-09", [150, 30, 10, 0, 8, 3]),
]


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
    lines = report.table(ROWS)
    assert lines[0].split() == "8 Week Summary Week of 11-09 Change vs Prev".split()
    assert lines[1::2] == [""] * len(report.COLUMNS)
    assert [x.split() for x in lines[2::2]] == [
        ["Active", "▆█", "150", "+50%"],
        ["Likes", "█▇", "30", "-25%"],
        ["Reposts", "██", "10", "+0%"],
        ["Replies", "█▁", "0", "-100%"],
        ["Show", "more", "▁█", "8", "·"],
        ["Show", "less", "▆█", "3", "+50%"],
    ]
    assert len({len(x) for x in lines if x}) == 1


def test_table_of_a_single_week_has_no_change_to_show():
    assert [x.split()[-1] for x in report.table(ROWS[:1])[2::2]] == ["·"] * len(report.COLUMNS)


def test_box_edges_line_up():
    text = report.report(ROWS)
    assert len({len(x) for x in text.splitlines()}) == 1


def test_colour_changes_nothing_but_the_escapes():
    assert report.ESCAPE.sub("", report.report(ROWS, color=True)) == report.report(ROWS)


def test_change_is_green_when_good_yellow_when_bad_and_plain_when_flat():
    assert report.verdict(150, 100, True) == report.GREEN
    assert report.verdict(30, 40, True) == report.YELLOW
    assert report.verdict(3, 2, False) == report.YELLOW
    assert report.verdict(2, 3, False) == report.GREEN
    assert report.verdict(10, 10, True) is None
    assert report.verdict(8, 0, True) is None
    lines = report.table(ROWS, color=True)
    assert "\x1b[32m" in lines[2] and "\x1b[33m" not in lines[2]  # active rose
    assert "\x1b[33m" in lines[-1] and "\x1b[32m" not in lines[-1]  # show less rose


def test_a_full_report_of_large_weeks_fits_in_one_discord_message():
    rows = [(f"01-{d:02}", [9_999_999] * len(report.COLUMNS)) for d in range(1, report.WEEKS + 1)]
    assert len(report.message(rows)) <= report.DISCORD_LIMIT


def test_weeks_reads_the_query_reply(monkeypatch):
    sent = {}

    def fake_post_json(url, payload, token=None):
        sent.update(url=url, token=token, sql=payload["query"]["query"])
        return {"results": [["2026-11-02T00:00:00Z", *ROWS[0][1]], ["2026-11-09", *ROWS[1][1]]]}

    monkeypatch.setattr(report, "post_json", fake_post_json)
    e = {"POSTHOG_PROJECT_ID": "42", "POSTHOG_API_KEY": "key"}
    assert report.weeks(e) == ROWS
    assert sent["url"] == "https://us.posthog.com/api/projects/42/query/"
    assert sent["token"] == "key"
    assert "interactionSeen" in sent["sql"]
