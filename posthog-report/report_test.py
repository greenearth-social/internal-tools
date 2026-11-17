import report

ROWS = [
    ("11-02", [100, 40, 10, 5, 8, 2, 30, 0]),
    ("11-09", [150, 30, 10, 0, 8, 3, 45, 1]),
]


def test_change_is_signed_percent_and_dot_without_a_base():
    assert report.change(150, 100) == "+50%"
    assert report.change(30, 40) == "-25%"
    assert report.change(10, 10) == "+0%"
    assert report.change(1, 0) == "·"


def test_table_has_one_row_per_week_then_a_rule_and_the_change_row():
    lines = report.table(ROWS)
    assert [x.split()[0] for x in lines] == ["week", "11-02", "11-09", "─" * len(lines[0]), "Δ"]
    assert lines[-1].split() == ["Δ", "+50%", "-25%", "+0%", "-100%", "+0%", "+50%", "+50%", "·"]
    assert len({len(x) for x in lines}) == 1


def test_table_of_a_single_week_has_no_change_row():
    assert [x.split()[0] for x in report.table(ROWS[:1])] == ["week", "11-02"]


def test_box_edges_line_up():
    text = report.box("title", report.table(ROWS), report.NOTE)
    assert len({len(x) for x in text.splitlines()}) == 1


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
