# posthog-report

Posts MySky's weekly PostHog numbers to Discord: one box, one row per whole
week (Monday to Sunday), the last 8 weeks, and the change from the week before.

```bash
python posthog-report/report.py           # print the box
python posthog-report/report.py --post    # post it to Discord
```

It needs `POSTHOG_API_KEY`, `POSTHOG_PROJECT_ID` and `DISCORD_WEBHOOK_URL`, from
the environment or from a gitignored `.env` beside `report.py`. It uses only the
standard library.

The weekly run is `.github/workflows/posthog-report.yml`, every Monday, with the
three values stored as repository secrets. It can also be started by hand from
the Actions tab. The workflow only ever posts: this repository is public and so
are its Action logs, so the numbers are never printed there.

The columns are the `COLUMNS` list at the top of `report.py`, one PostHog event
count each.
