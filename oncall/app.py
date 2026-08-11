import os
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from fastapi import FastAPI, Request, Response
import google.cloud.firestore as fs
from discord_utils import send_channel_message, format_ts, verify_discord_request
from firestore import get_current_oncall, get_user, create_alert, register_user, set_current_oncall, ack_alert, resolve_alert, get_stale_alerts
from runbooks import fetch_runbook
from github_utils import create_runbook_pr

logger = logging.getLogger(__name__)

DISCORD_BOT_TOKEN = os.environ["GE_DISCORD_BOT_TOKEN"]
DISCORD_ONCALL_CHANNEL_ID = os.environ["GE_DISCORD_ONCALL_CHANNEL_ID"]
DISCORD_PUBLIC_KEY = os.environ["GE_DISCORD_PUBLIC_KEY"]
RUNBOOKS_BRANCH = os.environ.get("GE_ONCALL_RUNBOOKS_BRANCH", "main")
GITHUB_TOKEN = os.environ["GE_GITHUB_TOKEN"]

# Interaction types
_PING = 1
_APPLICATION_COMMAND = 2
_MODAL_SUBMIT = 5

# Response types
_PONG = 1
_MESSAGE = 4
_MODAL = 9

ESCALATION_THRESHOLD_MINUTES = 15


@asynccontextmanager
async def lifespan(application):
    application.state.db = fs.Client(project=os.environ["GE_FIRESTORE_PROJECT_ID"])
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/check-escalations")
async def check_escalations(request: Request):
    db = request.app.state.db
    threshold = datetime.now(timezone.utc) - timedelta(minutes=ESCALATION_THRESHOLD_MINUTES)
    stale = get_stale_alerts(db, threshold)

    oncall = get_current_oncall(db) if stale else None
    oncall_name = None
    if oncall:
        user = get_user(db, oncall["user_id"])
        oncall_name = user["name"] if user else oncall["user_id"]

    for alert in stale:
        alert_id = alert["id"]
        policy_name = alert.get("policy_name", "Unknown alert")
        fired_at = datetime.fromisoformat(alert["fired_at"])
        minutes_ago = int((datetime.now(timezone.utc) - fired_at).total_seconds() / 60)
        mention = f"**{oncall_name}**" if oncall_name else "*(no oncall set)*"
        message = (
            f"⚠️ Unacknowledged critical alert: **{policy_name}** (fired {minutes_ago}min ago)\n"
            f"{mention} — please `/ack {alert_id}`"
        )
        try:
            send_channel_message(DISCORD_ONCALL_CHANNEL_ID, DISCORD_BOT_TOKEN, message)
        except Exception:
            logger.exception("Failed to send escalation ping for alert %s", alert_id)

    return {"escalated": len(stale)}


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


# ---------------------------------------------------------------------------
# Discord interaction helpers
# ---------------------------------------------------------------------------

def _interaction_response(rtype: int, content: str = "", **extra) -> dict:
    if rtype == _PONG:
        return {"type": _PONG}
    return {"type": rtype, "data": {"content": content, **extra}}


def _end_of_week_utc() -> datetime:
    today = date.today()
    days_until_sunday = (6 - today.weekday()) % 7 or 7
    end = today + timedelta(days=days_until_sunday)
    return datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)


@app.post("/discord/interactions")
async def discord_interactions(request: Request):
    body = await request.body()
    sig = request.headers.get("X-Signature-Ed25519", "")
    ts = request.headers.get("X-Signature-Timestamp", "")
    if not verify_discord_request(DISCORD_PUBLIC_KEY, sig, ts, body):
        return Response(status_code=401)

    payload = await request.json()
    itype = payload.get("type")

    if itype == _PING:
        return _interaction_response(_PONG)

    if itype == _APPLICATION_COMMAND:
        return await _handle_command(request, payload)

    if itype == _MODAL_SUBMIT:
        return await _handle_modal_submit(request, payload)

    return _interaction_response(_MESSAGE, "Unknown interaction type.")


async def _handle_command(request: Request, payload: dict) -> dict:
    db = request.app.state.db
    name = payload["data"]["name"]
    member = payload.get("member", {})
    user = member.get("user", {})
    user_id = user.get("id", "")
    username = user.get("username", "")
    display_name = user.get("global_name") or username

    if name == "register":
        register_user(db, user_id, display_name, username)
        return _interaction_response(_MESSAGE, f"Registered **{display_name}** in the oncall system.")

    if name == "oncall":
        subcommand = payload["data"]["options"][0]
        sub_name = subcommand["name"]

        if sub_name == "who":
            oncall = get_current_oncall(db)
            if not oncall:
                return _interaction_response(_MESSAGE, "No oncall set — run `/oncall set` to assign someone.")
            oncall_user = get_user(db, oncall["user_id"])
            until_dt = datetime.fromisoformat(oncall["until"])
            name_str = oncall_user["name"] if oncall_user else oncall["user_id"]
            return _interaction_response(
                _MESSAGE,
                f"**Current oncall:** {name_str} — until {format_ts(until_dt)}",
            )

        if sub_name == "set":
            opts = {o["name"]: o["value"] for o in subcommand.get("options", [])}
            target_user_id = opts["user"]
            resolved_users = payload["data"].get("resolved", {}).get("users", {})
            target_info = resolved_users.get(target_user_id, {})
            target_name = target_info.get("global_name") or target_info.get("username", target_user_id)

            if "until" in opts:
                y, m, d = (int(p) for p in opts["until"].split("-"))
                until_dt = datetime(y, m, d, 23, 59, 59, tzinfo=timezone.utc)
            else:
                until_dt = _end_of_week_utc()

            set_current_oncall(db, target_user_id, until_dt)
            return _interaction_response(
                _MESSAGE,
                f"**{target_name}** is now oncall until {format_ts(until_dt)}",
            )

    if name == "ack":
        alert_id = payload["data"]["options"][0]["value"]
        old = ack_alert(db, alert_id, user_id)
        if old is None:
            return _interaction_response(_MESSAGE, f"Alert `{alert_id}` not found.")
        return _interaction_response(
            _MESSAGE,
            f"✓ Acknowledged by **{display_name}** — escalation stopped."
        )

    if name == "resolve":
        alert_id = payload["data"]["options"][0]["value"]
        old = resolve_alert(db, alert_id)
        if old is None:
            return _interaction_response(_MESSAGE, f"Alert `{alert_id}` not found.")
        if not old.get("runbook_found", True):
            return _interaction_response(
                _MESSAGE,
                f"✓ Resolved. No runbook matched this alert — run `/runbook add` to capture the fix."
            )
        return _interaction_response(_MESSAGE, "✓ Resolved.")

    if name == "runbook":
        subcommand = payload["data"]["options"][0]
        if subcommand["name"] == "add":
            return {
                "type": _MODAL,
                "data": {
                    "custom_id": "runbook_add_modal",
                    "title": "Add Runbook",
                    "components": [
                        {"type": 1, "components": [{"type": 4, "custom_id": "policy_name",
                            "label": "Alert policy name", "style": 1,
                            "placeholder": "es-storage-high", "required": True}]},
                        {"type": 1, "components": [{"type": 4, "custom_id": "title",
                            "label": "Title", "style": 1,
                            "placeholder": "ES Storage > 80%", "required": True}]},
                        {"type": 1, "components": [{"type": 4, "custom_id": "content",
                            "label": "Content (Markdown)", "style": 2,
                            "placeholder": "## Likely cause\n...\n\n## Steps\n1. ...",
                            "required": True, "max_length": 3000}]},
                    ],
                },
            }

    return _interaction_response(_MESSAGE, f"Unknown command: {name}")


async def _handle_modal_submit(request: Request, payload: dict) -> dict:
    if payload["data"]["custom_id"] != "runbook_add_modal":
        return _interaction_response(_MESSAGE, "Unknown modal.")

    fields = {
        c["components"][0]["custom_id"]: c["components"][0]["value"]
        for c in payload["data"]["components"]
    }
    policy_name = fields["policy_name"]
    title = fields["title"]
    content = fields["content"]

    try:
        pr_url = create_runbook_pr(GITHUB_TOKEN, policy_name, title, content)
        return _interaction_response(_MESSAGE, f"✓ Runbook PR opened: {pr_url}")
    except Exception:
        logger.exception("Failed to create runbook PR")
        return _interaction_response(_MESSAGE, "Failed to open PR — check logs.")
