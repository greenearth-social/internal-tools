import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import google.cloud.firestore as fs
from discord_utils import (
    edit_original_interaction_response,
    format_ts,
    send_channel_message,
    verify_discord_request,
)
from fastapi import FastAPI, Request, Response
from firestore import (
    ack_alert,
    create_alert,
    get_current_oncall,
    get_stale_alerts,
    get_user,
    list_registered_users,
    register_user,
    resolve_alert,
    rotate_oncall_schedule,
    set_current_oncall,
    sync_rotation_queue,
)
from github_utils import (
    add_pr_to_project,
    create_runbook_pr,
    request_pr_reviewers,
    set_project_item_status,
)
from runbooks import fetch_runbook, runbook_url

logger = logging.getLogger(__name__)

DISCORD_APPLICATION_ID = os.environ["GE_DISCORD_APPLICATION_ID"]
DISCORD_BOT_TOKEN = os.environ["GE_DISCORD_BOT_TOKEN"]
DISCORD_ONCALL_CHANNEL_ID = os.environ["GE_DISCORD_ONCALL_CHANNEL_ID"]
DISCORD_PUBLIC_KEY = os.environ["GE_DISCORD_PUBLIC_KEY"]
RUNBOOKS_BRANCH = os.environ.get("GE_ONCALL_RUNBOOKS_BRANCH", "main")
GITHUB_TOKEN = os.environ["GE_GITHUB_TOKEN"]
RUNBOOK_PROJECT_ID = os.environ["GE_ONCALL_RUNBOOK_PROJECT_ID"]
RUNBOOK_STATUS_FIELD_ID = os.environ["GE_ONCALL_RUNBOOK_STATUS_FIELD_ID"]
RUNBOOK_STATUS_INREVIEW_OPTION_ID = os.environ["GE_ONCALL_RUNBOOK_STATUS_INREVIEW_OPTION_ID"]

# Interaction types
_PING = 1
_APPLICATION_COMMAND = 2
_MODAL_SUBMIT = 5

# Response types
_PONG = 1
_MESSAGE = 4
_DEFERRED_MESSAGE = 5
_MODAL = 9

ESCALATION_THRESHOLD_MINUTES = 15


@asynccontextmanager
async def lifespan(application):
    # Explicit project + database. Local dev routes to the emulator via
    # GE_FIRESTORE_EMULATOR_HOST regardless of either value.
    application.state.db = fs.Client(
        project=os.environ["GE_FIRESTORE_PROJECT_ID"],
        database=os.environ["GE_FIRESTORE_DATABASE_ID"],
    )
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


def check_and_rotate_oncall(db, now: datetime | None = None) -> dict | None:
    """Checks if current oncall shift has expired, transitions to the next engineer,
    and posts a hand-off announcement in #oncall.
    """
    try:
        new_oncall, prev_oncall = rotate_oncall_schedule(db, now)
    except Exception:
        logger.exception("Failed checking or rotating oncall schedule")
        return None

    if not new_oncall:
        return None

    prev_user_id = prev_oncall.get("user_id") if prev_oncall else None
    next_user_id = new_oncall.get("user_id")

    prev_user = get_user(db, prev_user_id) if prev_user_id else None
    next_user = get_user(db, next_user_id) if next_user_id else None

    prev_name = prev_user["name"] if prev_user else (prev_user_id or "Unknown")
    next_name = next_user["name"] if next_user else (next_user_id or "Unknown")
    until_dt = datetime.fromisoformat(new_oncall["until"])

    message = (
        f"🔄 **Oncall Rotation Hand-off**\n"
        f"**{prev_name}**'s shift has ended. "
        f"**{next_name}** is now oncall until {format_ts(until_dt)}."
    )
    try:
        send_channel_message(DISCORD_ONCALL_CHANNEL_ID, DISCORD_BOT_TOKEN, message)
    except Exception:
        logger.exception("Failed to post oncall rotation hand-off to Discord")

    return new_oncall


@app.post("/check-rotation")
async def check_rotation(request: Request):
    rotated = check_and_rotate_oncall(request.app.state.db)
    return {"status": "ok", "rotated": rotated is not None}


@app.post("/check-escalations")
async def check_escalations(request: Request):
    db = request.app.state.db
    check_and_rotate_oncall(db)
    threshold = datetime.now(UTC) - timedelta(minutes=ESCALATION_THRESHOLD_MINUTES)
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
        minutes_ago = int((datetime.now(UTC) - fired_at).total_seconds() / 60)
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
    # Runbooks and /runbook add use policy display names, not metric condition names.
    policy_name = incident.get("policy_name") or incident.get("condition_name", "Unknown alert")
    severity = incident.get("severity", "WARNING").upper()
    started_at = datetime.fromtimestamp(incident.get("started_at", 0), tz=UTC)
    summary = incident.get("summary", "")

    found, _ = fetch_runbook(policy_name, RUNBOOKS_BRANCH)
    rb_url = runbook_url(policy_name, RUNBOOKS_BRANCH)

    if severity == "CRITICAL":
        oncall = get_current_oncall(request.app.state.db)
        if oncall:
            user = get_user(request.app.state.db, oncall["user_id"])
            name_str = user["name"] if user else oncall["user_id"]
            until_dt = datetime.fromisoformat(oncall["until"])
            oncall_line = (
                f"Oncall: {name_str} (until {format_ts(until_dt)})"
                f" - use `/ack {alert_id}` to acknowledge"
            )
        else:
            oncall_line = "no oncall set - run `/oncall set` to assign someone"

        runbook_section = (
            f"Runbook: {rb_url}"
            if found
            else "No runbook found - run `/runbook add` to capture the fix."
        )

        message = (
            f"🚨 **[CRITICAL]** {policy_name}\n"
            f"{oncall_line}\n"
            f"Alert ID: `{alert_id}` | {summary}\n\n"
            f"{runbook_section}"
        )

        try:
            create_alert(request.app.state.db, alert_id, policy_name, "critical", found, started_at)
        except Exception:
            logger.exception("Firestore write failed for alert %s", alert_id)

    else:
        runbook_section = f"Runbook: {rb_url}" if found else "No runbook found."
        message = (
            f"⚠️ **[WARNING]** {policy_name}\n"
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


def _end_of_week_utc(now: datetime | None = None) -> datetime:
    today = (now or datetime.now(UTC)).date()
    days_until_sunday = (6 - today.weekday()) % 7 or 7
    end = today + timedelta(days=days_until_sunday)
    return datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC)


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
        opts = {o["name"]: o["value"] for o in payload["data"].get("options", [])}
        github_handle = opts["github_handle"]
        register_user(db, user_id, display_name, username, github_handle)
        return _interaction_response(
            _MESSAGE,
            f"Registered **{display_name}** (GitHub: `{github_handle}`) in the oncall system.",
        )

    if name == "oncall":
        subcommand = payload["data"]["options"][0]
        sub_name = subcommand["name"]

        if sub_name == "who":
            check_and_rotate_oncall(db)
            oncall = get_current_oncall(db)
            if not oncall:
                return _interaction_response(
                    _MESSAGE, "No oncall set — run `/oncall set` to assign someone."
                )
            oncall_user = get_user(db, oncall["user_id"])
            until_dt = datetime.fromisoformat(oncall["until"])
            name_str = oncall_user["name"] if oncall_user else oncall["user_id"]

            queue = oncall.get("rotation_queue")
            if not queue:
                queue = sync_rotation_queue(db, oncall["user_id"])

            if queue:
                next_user = get_user(db, queue[0])
                next_name = next_user["name"] if next_user else queue[0]
                next_str = f"\n**Next in line:** {next_name}"
            else:
                next_str = ""

            return _interaction_response(
                _MESSAGE,
                f"**Current oncall:** {name_str} — until {format_ts(until_dt)}{next_str}",
            )

        if sub_name == "set":
            opts = {o["name"]: o["value"] for o in subcommand.get("options", [])}
            target_user_id = opts["user"]
            resolved_users = payload["data"].get("resolved", {}).get("users", {})
            target_info = resolved_users.get(target_user_id, {})
            target_name = target_info.get("global_name") or target_info.get(
                "username", target_user_id
            )

            target_user = get_user(db, target_user_id)
            if target_user is None:
                return _interaction_response(
                    _MESSAGE,
                    f"**{target_name}** is not registered in the oncall system. "
                    "They must run `/register` first.",
                )

            if isinstance(target_user, dict) and isinstance(target_user.get("name"), str):
                target_name = target_user["name"]

            if "until" in opts:
                y, m, d = (int(p) for p in opts["until"].split("-"))
                until_dt = datetime(y, m, d, 23, 59, 59, tzinfo=UTC)
            else:
                until_dt = _end_of_week_utc()

            queue = sync_rotation_queue(db, target_user_id)
            set_current_oncall(db, target_user_id, until_dt, rotation_queue=queue)

            if queue:
                next_user = get_user(db, queue[0])
                next_name = next_user["name"] if next_user else queue[0]
                next_str = f" Next in line: **{next_name}**."
            else:
                next_str = ""

            return _interaction_response(
                _MESSAGE,
                f"**{target_name}** is now oncall until {format_ts(until_dt)}.{next_str}",
            )

        if sub_name == "list":
            check_and_rotate_oncall(db)
            registered = list_registered_users(db)
            if not registered:
                return _interaction_response(
                    _MESSAGE,
                    "No registered oncall engineers. Use `/register` to join the rotation.",
                )

            users_by_id = {u["user_id"]: u for u in registered}
            oncall = get_current_oncall(db)
            current_uid = oncall.get("user_id") if oncall else None

            lines = ["📋 **Oncall Engineers & Rotation Order**\n"]

            if current_uid and current_uid in users_by_id:
                curr_user = users_by_id[current_uid]
                until_dt = datetime.fromisoformat(oncall["until"])
                c_disc = curr_user.get("discord_handle", "")
                c_gh = curr_user.get("github_handle", "")
                lines.append(
                    f"**Current Oncall:**\n"
                    f"👉 **{curr_user['name']}** (@{c_disc} | GitHub: `{c_gh}`) — "
                    f"until {format_ts(until_dt)}\n"
                )
            else:
                lines.append("*(No current oncall set — run `/oncall set` to assign someone)*\n")

            queue = oncall.get("rotation_queue") if oncall else None
            if not queue and current_uid:
                queue = sync_rotation_queue(db, current_uid)
            if not queue:
                queue = [uid for uid in users_by_id if uid != current_uid]

            if queue:
                lines.append("**Upcoming Rotation Sequence:**")
                for i, uid in enumerate(queue, 1):
                    u = users_by_id.get(uid)
                    if u:
                        u_disc = u.get("discord_handle", "")
                        u_gh = u.get("github_handle", "")
                        lines.append(f"{i}. **{u['name']}** (@{u_disc} | GitHub: `{u_gh}`)")
                    else:
                        lines.append(f"{i}. `{uid}`")
            elif len(registered) == 1 and current_uid:
                lines.append("*(No other engineers in rotation queue)*")

            return _interaction_response(_MESSAGE, "\n".join(lines))

    if name == "ack":
        alert_id = payload["data"]["options"][0]["value"]
        old = ack_alert(db, alert_id, user_id)
        if old is None:
            return _interaction_response(_MESSAGE, f"Alert `{alert_id}` not found.")
        return _interaction_response(
            _MESSAGE, f"✓ Acknowledged by **{display_name}** — escalation stopped."
        )

    if name == "resolve":
        alert_id = payload["data"]["options"][0]["value"]
        old = resolve_alert(db, alert_id)
        if old is None:
            return _interaction_response(_MESSAGE, f"Alert `{alert_id}` not found.")
        if not old.get("runbook_found", True):
            return _interaction_response(
                _MESSAGE,
                "✓ Resolved. No runbook matched this alert — run `/runbook add` to capture the fix.",  # noqa: E501
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
                        {
                            "type": 1,
                            "components": [
                                {
                                    "type": 4,
                                    "custom_id": "policy_name",
                                    "label": "Alert policy name",
                                    "style": 1,
                                    "placeholder": "feed-low-fulfillment",
                                    "required": True,
                                }
                            ],
                        },
                        {
                            "type": 1,
                            "components": [
                                {
                                    "type": 4,
                                    "custom_id": "title",
                                    "label": "Title",
                                    "style": 1,
                                    "placeholder": "ES Storage > 80%",
                                    "required": True,
                                }
                            ],
                        },
                        {
                            "type": 1,
                            "components": [
                                {
                                    "type": 4,
                                    "custom_id": "content",
                                    "label": "Content (Markdown)",
                                    "style": 2,
                                    "placeholder": "## Likely cause\n...\n\n## Steps\n1. ...",
                                    "required": True,
                                    "max_length": 3000,
                                }
                            ],
                        },
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
    submitter = payload.get("member", {}).get("user") or payload.get("user", {})
    _schedule_finalize(
        db=request.app.state.db,
        interaction_token=payload["token"],
        submitter_user_id=submitter.get("id", ""),
        policy_name=fields["policy_name"],
        title=fields["title"],
        content=fields["content"],
    )
    return {"type": _DEFERRED_MESSAGE}


def _schedule_finalize(**kwargs) -> None:
    """Kick off the background finalize task.

    Wrapped in its own function so tests can patch this instead of
    wrestling with asyncio.create_task and unawaited coroutines.
    """
    asyncio.create_task(_finalize_runbook_pr(**kwargs))


def _reviewer_handles(db, submitter_user_id: str) -> list[str]:
    handles = []
    for user in list_registered_users(db):
        if user["user_id"] == submitter_user_id:
            continue
        handle = user.get("github_handle")
        if handle:
            handles.append(handle)
    return handles


def _followup(interaction_token: str, content: str) -> None:
    try:
        edit_original_interaction_response(DISCORD_APPLICATION_ID, interaction_token, content)
    except Exception:
        logger.exception("Failed to send Discord follow-up")


async def _finalize_runbook_pr(
    db,
    interaction_token: str,
    submitter_user_id: str,
    policy_name: str,
    title: str,
    content: str,
) -> None:
    try:
        pr = create_runbook_pr(GITHUB_TOKEN, policy_name, title, content)
    except Exception:
        logger.exception("Failed to create runbook PR")
        _followup(interaction_token, "Failed to open PR — check logs.")
        return

    pr_url = pr["html_url"]
    warnings: list[str] = []

    reviewers = _reviewer_handles(db, submitter_user_id)
    if reviewers:
        try:
            request_pr_reviewers(GITHUB_TOKEN, pr["number"], reviewers)
        except Exception:
            logger.exception("Failed to request reviewers for PR %s", pr_url)
            warnings.append("couldn't attach reviewers")

    try:
        item_id = add_pr_to_project(GITHUB_TOKEN, RUNBOOK_PROJECT_ID, pr["node_id"])
        set_project_item_status(
            GITHUB_TOKEN,
            RUNBOOK_PROJECT_ID,
            item_id,
            RUNBOOK_STATUS_FIELD_ID,
            RUNBOOK_STATUS_INREVIEW_OPTION_ID,
        )
    except Exception:
        logger.exception("Failed to attach PR %s to project", pr_url)
        warnings.append("couldn't attach to project")

    if warnings:
        message = (
            f"⚠ Runbook PR opened: {pr_url} — but "
            + " and ".join(warnings)
            + ". Please fix manually."
        )
    else:
        message = f"✓ Runbook PR opened: {pr_url}"
    _followup(interaction_token, message)
