import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, Request
import google.cloud.firestore as fs
from discord_utils import send_channel_message, format_ts
from firestore import get_current_oncall, get_user, create_alert
from runbooks import fetch_runbook

logger = logging.getLogger(__name__)

DISCORD_BOT_TOKEN = os.environ["GE_DISCORD_BOT_TOKEN"]
DISCORD_ONCALL_CHANNEL_ID = os.environ["GE_DISCORD_ONCALL_CHANNEL_ID"]
RUNBOOKS_BRANCH = os.environ.get("GE_ONCALL_RUNBOOKS_BRANCH", "main")


@asynccontextmanager
async def lifespan(application):
    application.state.db = fs.Client(project=os.environ["GE_FIRESTORE_PROJECT_ID"])
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/alert")
async def alert(request: Request):
    payload = await request.json()
    incident = payload.get("incident", {})

    if incident.get("state") != "open":
        return {"status": "ignored"}

    alert_id = incident["incident_id"]
    condition_name = incident.get("condition_name", "Unknown alert")
    severity = incident.get("severity", "WARNING").upper()
    started_at = datetime.fromtimestamp(incident.get("started_at", 0), tz=timezone.utc)
    summary = incident.get("summary", "")

    found, runbook_content = fetch_runbook(condition_name, RUNBOOKS_BRANCH)

    if severity == "CRITICAL":
        oncall = get_current_oncall(request.app.state.db)
        if oncall:
            user = get_user(request.app.state.db, oncall["user_id"])
            until_dt = datetime.fromisoformat(oncall["until"])
            oncall_line = (
                f"Oncall: {user['name']} (until {format_ts(until_dt)})"
                f" — use `/ack {alert_id}` to acknowledge"
            )
        else:
            oncall_line = "no oncall set — run `/oncall set` to assign someone"

        runbook_section = runbook_content if found else "No runbook found — run `/runbook add` to capture the fix."

        message = (
            f"🚨 **[CRITICAL]** {condition_name}\n"
            f"{oncall_line}\n"
            f"Alert ID: `{alert_id}` | {summary}\n\n"
            f"{runbook_section}"
        )

        try:
            create_alert(
                request.app.state.db, alert_id, condition_name,
                "critical", found, started_at
            )
        except Exception:
            logger.exception("Firestore write failed for alert %s", alert_id)

    else:
        runbook_section = runbook_content if found else "No runbook found."
        message = (
            f"⚠️ **[WARNING]** {condition_name}\n"
            f"Alert ID: `{alert_id}` | {summary}\n\n"
            f"{runbook_section}"
        )

    try:
        send_channel_message(DISCORD_ONCALL_CHANNEL_ID, DISCORD_BOT_TOKEN, message)
    except Exception:
        logger.exception("Failed to post alert %s to Discord", alert_id)

    return {"status": "ok"}
