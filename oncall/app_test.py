import json
import os
from unittest.mock import MagicMock, patch


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


GCP_CRITICAL_PAYLOAD = {
    "incident": {
        "incident_id": "inc-abc123",
        "condition_name": "ES Storage > 80%",
        "severity": "CRITICAL",
        "state": "open",
        "started_at": 1723320000,
        "summary": "Storage at 85%",
    }
}

GCP_WARNING_PAYLOAD = {
    "incident": {
        "incident_id": "inc-warn01",
        "condition_name": "Ingest Freshness",
        "severity": "WARNING",
        "state": "open",
        "started_at": 1723320000,
        "summary": "Ingest is slow",
    }
}

GCP_CLOSED_PAYLOAD = {
    "incident": {
        "incident_id": "inc-abc123",
        "condition_name": "ES Storage > 80%",
        "severity": "CRITICAL",
        "state": "closed",
        "started_at": 1723320000,
        "summary": "Resolved",
    }
}


def test_alert_critical_posts_to_discord_and_stores(client, mock_db):
    oncall_doc = MagicMock()
    oncall_doc.exists = True
    oncall_doc.to_dict.return_value = {"user_id": "uid1", "until": "2026-08-15T23:59:59+00:00"}
    user_doc = MagicMock()
    user_doc.exists = True
    user_doc.to_dict.return_value = {"name": "Inseon", "discord_handle": "inthree3"}
    mock_db.collection.return_value.document.return_value.get.side_effect = [oncall_doc, user_doc]

    with (
        patch("app.fetch_runbook", return_value=(True, "## Steps\n1. Check storage.")),
        patch("app.send_channel_message") as mock_send,
        patch("app.create_alert") as mock_create,
    ):
        response = client.post("/alert", json=GCP_CRITICAL_PAYLOAD)

    assert response.status_code == 200
    mock_send.assert_called_once()
    sent_content = mock_send.call_args[0][2]
    assert "CRITICAL" in sent_content
    assert "Inseon" in sent_content
    assert "inc-abc123" in sent_content
    mock_create.assert_called_once()


def test_alert_warning_posts_without_mention_no_firestore(client, mock_db):
    with (
        patch("app.fetch_runbook", return_value=(False, "")),
        patch("app.send_channel_message") as mock_send,
        patch("app.create_alert") as mock_create,
    ):
        response = client.post("/alert", json=GCP_WARNING_PAYLOAD)

    assert response.status_code == 200
    mock_send.assert_called_once()
    sent_content = mock_send.call_args[0][2]
    assert "WARNING" in sent_content
    assert "@" not in sent_content
    mock_create.assert_not_called()


def test_alert_closed_is_ignored(client):
    with patch("app.send_channel_message") as mock_send:
        response = client.post("/alert", json=GCP_CLOSED_PAYLOAD)
    assert response.status_code == 200
    mock_send.assert_not_called()


def test_alert_critical_no_oncall_posts_without_mention(client, mock_db):
    oncall_doc = MagicMock()
    oncall_doc.exists = False
    mock_db.collection.return_value.document.return_value.get.return_value = oncall_doc

    with (
        patch("app.fetch_runbook", return_value=(False, "")),
        patch("app.send_channel_message") as mock_send,
        patch("app.create_alert"),
    ):
        response = client.post("/alert", json=GCP_CRITICAL_PAYLOAD)

    assert response.status_code == 200
    sent_content = mock_send.call_args[0][2]
    assert "no oncall set" in sent_content


def test_alert_critical_oncall_user_missing_still_alerts(client, mock_db):
    """Test that missing user doc doesn't crash /alert CRITICAL path — falls back to user_id."""
    oncall_val = {"user_id": "uid-missing", "until": "2026-08-15T23:59:59+00:00"}
    with (
        patch("app.get_current_oncall", return_value=oncall_val),
        patch("app.get_user", return_value=None),
        patch("app.fetch_runbook", return_value=(False, "")),
        patch("app.send_channel_message") as mock_send,
        patch("app.create_alert"),
    ):
        response = client.post("/alert", json=GCP_CRITICAL_PAYLOAD)

    assert response.status_code == 200
    mock_send.assert_called_once()
    sent_content = mock_send.call_args[0][2]
    assert "CRITICAL" in sent_content
    assert "uid-missing" in sent_content  # Falls back to user_id when user doc is None


# ---------------------------------------------------------------------------
# Discord interactions tests
# ---------------------------------------------------------------------------

DISCORD_PUBLIC_KEY = os.environ["GE_DISCORD_PUBLIC_KEY"]


def _discord_headers(body: bytes) -> dict:
    # For tests we bypass real Ed25519 by patching verify_discord_request
    return {
        "X-Signature-Ed25519": "aa" * 64,
        "X-Signature-Timestamp": "1234567890",
    }


PING_PAYLOAD = {"type": 1}

REGISTER_PAYLOAD = {
    "type": 2,
    "data": {
        "name": "register",
        "options": [{"name": "github_handle", "value": "inseon-hwang"}],
    },
    "member": {"user": {"id": "uid1", "username": "inthree3", "global_name": "Inseon"}},
}

ONCALL_WHO_PAYLOAD = {
    "type": 2,
    "data": {
        "name": "oncall",
        "options": [{"name": "who", "type": 1, "options": []}],
    },
    "member": {"user": {"id": "uid1", "username": "inthree3", "global_name": "Inseon"}},
}

ONCALL_SET_PAYLOAD = {
    "type": 2,
    "data": {
        "name": "oncall",
        "options": [
            {
                "name": "set",
                "type": 1,
                "options": [
                    {"name": "user", "value": "uid2"},
                    {"name": "until", "value": "2026-08-15"},
                ],
            }
        ],
        "resolved": {
            "users": {"uid2": {"id": "uid2", "username": "raindrift", "global_name": "Ian"}},
        },
    },
    "member": {"user": {"id": "uid1", "username": "inthree3", "global_name": "Inseon"}},
}


def _post_interaction(client, payload):
    body = json.dumps(payload).encode()
    with patch("app.verify_discord_request", return_value=True):
        return client.post(
            "/discord/interactions",
            content=body,
            headers={**_discord_headers(body), "Content-Type": "application/json"},
        )


def test_discord_ping_returns_pong(client):
    response = _post_interaction(client, PING_PAYLOAD)
    assert response.status_code == 200
    assert response.json()["type"] == 1


def test_discord_invalid_signature_returns_401(client):
    body = json.dumps(PING_PAYLOAD).encode()
    with patch("app.verify_discord_request", return_value=False):
        response = client.post(
            "/discord/interactions",
            content=body,
            headers={**_discord_headers(body), "Content-Type": "application/json"},
        )
    assert response.status_code == 401


def test_register_stores_user_and_replies(client, mock_db):
    with patch("app.register_user") as mock_reg:
        response = _post_interaction(client, REGISTER_PAYLOAD)
    assert response.status_code == 200
    data = response.json()
    assert data["type"] == 4
    assert "Inseon" in data["data"]["content"]
    assert "inseon-hwang" in data["data"]["content"]
    mock_reg.assert_called_once_with(mock_db, "uid1", "Inseon", "inthree3", "inseon-hwang")


def test_oncall_who_no_oncall_set(client, mock_db):
    mock_db.collection.return_value.document.return_value.get.return_value.exists = False
    response = _post_interaction(client, ONCALL_WHO_PAYLOAD)
    assert response.status_code == 200
    assert "no oncall" in response.json()["data"]["content"].lower()


def test_oncall_set_stores_and_replies(client, mock_db):
    with patch("app.set_current_oncall") as mock_set:
        response = _post_interaction(client, ONCALL_SET_PAYLOAD)
    assert response.status_code == 200
    data = response.json()
    assert data["type"] == 4
    assert "Ian" in data["data"]["content"]
    mock_set.assert_called_once()
    args = mock_set.call_args[0]
    assert args[1] == "uid2"
    assert args[2].year == 2026 and args[2].month == 8 and args[2].day == 15


ACK_PAYLOAD = {
    "type": 2,
    "data": {
        "name": "ack",
        "options": [{"name": "alert_id", "value": "inc-abc123"}],
    },
    "member": {"user": {"id": "uid1", "username": "inthree3", "global_name": "Inseon"}},
}

RESOLVE_PAYLOAD_WITH_RUNBOOK = {
    "type": 2,
    "data": {
        "name": "resolve",
        "options": [{"name": "alert_id", "value": "inc-abc123"}],
    },
    "member": {"user": {"id": "uid1", "username": "inthree3", "global_name": "Inseon"}},
}

RESOLVE_PAYLOAD_NO_RUNBOOK = {
    "type": 2,
    "data": {
        "name": "resolve",
        "options": [{"name": "alert_id", "value": "inc-abc123"}],
    },
    "member": {"user": {"id": "uid1", "username": "inthree3", "global_name": "Inseon"}},
}


def test_ack_updates_alert_and_replies(client, mock_db):
    ack_val = {"status": "open", "policy_name": "es-storage-high"}
    with patch("app.ack_alert", return_value=ack_val) as mock_ack:
        response = _post_interaction(client, ACK_PAYLOAD)
    assert response.status_code == 200
    content = response.json()["data"]["content"]
    assert "Acknowledged" in content
    assert "Inseon" in content
    mock_ack.assert_called_once_with(mock_db, "inc-abc123", "uid1")


def test_ack_unknown_alert_replies_gracefully(client, mock_db):
    with patch("app.ack_alert", return_value=None):
        response = _post_interaction(client, ACK_PAYLOAD)
    assert response.status_code == 200
    assert "not found" in response.json()["data"]["content"].lower()


def test_resolve_with_runbook_confirms(client, mock_db):
    with patch("app.resolve_alert", return_value={"status": "acked", "runbook_found": True}):
        response = _post_interaction(client, RESOLVE_PAYLOAD_WITH_RUNBOOK)
    assert response.status_code == 200
    assert "Resolved" in response.json()["data"]["content"]
    assert "runbook add" not in response.json()["data"]["content"]


def test_resolve_without_runbook_prompts_add(client, mock_db):
    with patch("app.resolve_alert", return_value={"status": "acked", "runbook_found": False}):
        response = _post_interaction(client, RESOLVE_PAYLOAD_NO_RUNBOOK)
    assert response.status_code == 200
    assert "/runbook add" in response.json()["data"]["content"]


# ---------------------------------------------------------------------------
# /runbook add modal + modal submit tests
# ---------------------------------------------------------------------------

RUNBOOK_ADD_PAYLOAD = {
    "type": 2,
    "data": {
        "name": "runbook",
        "options": [{"name": "add", "type": 1, "options": []}],
    },
    "member": {"user": {"id": "uid1", "username": "inthree3", "global_name": "Inseon"}},
}

MODAL_SUBMIT_PAYLOAD = {
    "type": 5,
    "token": "int_token_abc",
    "data": {
        "custom_id": "runbook_add_modal",
        "components": [
            {"type": 1, "components": [{"custom_id": "policy_name", "value": "es-storage-high"}]},
            {"type": 1, "components": [{"custom_id": "title", "value": "ES Storage > 80%"}]},
            {"type": 1, "components": [{"custom_id": "content", "value": "## Steps\n1. Check."}]},
        ],
    },
    "member": {"user": {"id": "uid1", "username": "inthree3", "global_name": "Inseon"}},
}


def test_runbook_add_returns_modal(client):
    response = _post_interaction(client, RUNBOOK_ADD_PAYLOAD)
    assert response.status_code == 200
    data = response.json()
    assert data["type"] == 9  # MODAL
    assert data["data"]["custom_id"] == "runbook_add_modal"


def test_modal_submit_returns_deferred_and_schedules_finalize(client, mock_db):
    with patch("app._schedule_finalize") as mock_sched:
        response = _post_interaction(client, MODAL_SUBMIT_PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {"type": 5}  # DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE
    mock_sched.assert_called_once_with(
        db=mock_db,
        interaction_token="int_token_abc",
        submitter_user_id="uid1",
        policy_name="es-storage-high",
        title="ES Storage > 80%",
        content="## Steps\n1. Check.",
    )


# ---------------------------------------------------------------------------
# _finalize_runbook_pr tests
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402

import app as app_module  # noqa: E402

_PR_RESULT = {
    "html_url": "https://github.com/greenearth-social/internal-tools/pull/42",
    "node_id": "PR_kwDO_abc",
    "number": 42,
}

_USERS = [
    {
        "user_id": "uid1",
        "name": "Inseon",
        "discord_handle": "inthree3",
        "github_handle": "inseon-hwang",
    },
    {"user_id": "uid2", "name": "Ian", "discord_handle": "raindrift", "github_handle": "ian-gh"},
    {"user_id": "uid3", "name": "Max", "discord_handle": "maxdisc", "github_handle": "max-gh"},
]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _finalize(mock_db, submitter_user_id="uid1"):
    return app_module._finalize_runbook_pr(
        db=mock_db,
        interaction_token="int_token_abc",
        submitter_user_id=submitter_user_id,
        policy_name="es-storage-high",
        title="ES Storage > 80%",
        content="## Steps\n1. Check.",
    )


def test_finalize_happy_path_posts_success(mock_db):
    with (
        patch("app.create_runbook_pr", return_value=_PR_RESULT) as mock_pr,
        patch("app.list_registered_users", return_value=_USERS),
        patch("app.request_pr_reviewers") as mock_reviewers,
        patch("app.add_pr_to_project", return_value="PVTI_new") as mock_add,
        patch("app.set_project_item_status") as mock_status,
        patch("app.edit_original_interaction_response") as mock_edit,
    ):
        _run(_finalize(mock_db))

    mock_pr.assert_called_once()
    mock_reviewers.assert_called_once_with(os.environ["GE_GITHUB_TOKEN"], 42, ["ian-gh", "max-gh"])
    mock_add.assert_called_once_with(
        os.environ["GE_GITHUB_TOKEN"],
        os.environ["GE_ONCALL_RUNBOOK_PROJECT_ID"],
        "PR_kwDO_abc",
    )
    mock_status.assert_called_once_with(
        os.environ["GE_GITHUB_TOKEN"],
        os.environ["GE_ONCALL_RUNBOOK_PROJECT_ID"],
        "PVTI_new",
        os.environ["GE_ONCALL_RUNBOOK_STATUS_FIELD_ID"],
        os.environ["GE_ONCALL_RUNBOOK_STATUS_INREVIEW_OPTION_ID"],
    )
    mock_edit.assert_called_once()
    followup_content = mock_edit.call_args.args[2]
    assert followup_content.startswith("✓ Runbook PR opened:")
    assert _PR_RESULT["html_url"] in followup_content


def test_finalize_excludes_submitter_from_reviewers(mock_db):
    with (
        patch("app.create_runbook_pr", return_value=_PR_RESULT),
        patch("app.list_registered_users", return_value=_USERS),
        patch("app.request_pr_reviewers") as mock_reviewers,
        patch("app.add_pr_to_project", return_value="PVTI_new"),
        patch("app.set_project_item_status"),
        patch("app.edit_original_interaction_response"),
    ):
        _run(_finalize(mock_db, submitter_user_id="uid2"))  # Ian submits

    mock_reviewers.assert_called_once_with(
        os.environ["GE_GITHUB_TOKEN"], 42, ["inseon-hwang", "max-gh"]
    )


def test_finalize_pr_creation_failure_reports_failure(mock_db):
    with (
        patch("app.create_runbook_pr", side_effect=RuntimeError("boom")),
        patch("app.request_pr_reviewers") as mock_reviewers,
        patch("app.add_pr_to_project") as mock_add,
        patch("app.edit_original_interaction_response") as mock_edit,
    ):
        _run(_finalize(mock_db))

    mock_reviewers.assert_not_called()
    mock_add.assert_not_called()
    mock_edit.assert_called_once()
    assert "Failed to open PR" in mock_edit.call_args.args[2]


def test_finalize_reviewer_failure_reported_but_pr_still_succeeds(mock_db):
    with (
        patch("app.create_runbook_pr", return_value=_PR_RESULT),
        patch("app.list_registered_users", return_value=_USERS),
        patch("app.request_pr_reviewers", side_effect=RuntimeError("no perms")),
        patch("app.add_pr_to_project", return_value="PVTI_new"),
        patch("app.set_project_item_status"),
        patch("app.edit_original_interaction_response") as mock_edit,
    ):
        _run(_finalize(mock_db))

    content = mock_edit.call_args.args[2]
    assert _PR_RESULT["html_url"] in content
    assert "reviewers" in content
    assert content.startswith("⚠")


def test_finalize_project_failure_reported_but_pr_still_succeeds(mock_db):
    with (
        patch("app.create_runbook_pr", return_value=_PR_RESULT),
        patch("app.list_registered_users", return_value=_USERS),
        patch("app.request_pr_reviewers"),
        patch("app.add_pr_to_project", side_effect=RuntimeError("GraphQL boom")),
        patch("app.set_project_item_status"),
        patch("app.edit_original_interaction_response") as mock_edit,
    ):
        _run(_finalize(mock_db))

    content = mock_edit.call_args.args[2]
    assert _PR_RESULT["html_url"] in content
    assert "project" in content


def test_finalize_no_registered_users_skips_reviewer_request(mock_db):
    """Solo submitter — reviewer pool empty after filtering out submitter."""
    with (
        patch("app.create_runbook_pr", return_value=_PR_RESULT),
        patch("app.list_registered_users", return_value=[_USERS[0]]),
        patch("app.request_pr_reviewers") as mock_reviewers,
        patch("app.add_pr_to_project", return_value="PVTI_new"),
        patch("app.set_project_item_status"),
        patch("app.edit_original_interaction_response") as mock_edit,
    ):
        _run(_finalize(mock_db))

    mock_reviewers.assert_not_called()
    assert mock_edit.call_args.args[2].startswith("✓")


# ---------------------------------------------------------------------------
# /check-escalations tests
# ---------------------------------------------------------------------------


def test_check_escalations_pings_for_stale_alerts(client, mock_db):
    stale = [
        {
            "id": "inc-abc123",
            "policy_name": "ES Storage > 80%",
            "fired_at": "2026-08-10T19:00:00+00:00",
            "severity": "critical",
            "status": "open",
        }
    ]
    oncall = {"user_id": "uid1", "until": "2026-08-15T23:59:59+00:00"}

    with (
        patch("app.get_stale_alerts", return_value=stale),
        patch("app.get_current_oncall", return_value=oncall),
        patch("app.get_user", return_value={"name": "Inseon", "discord_handle": "inthree3"}),
        patch("app.send_channel_message") as mock_send,
    ):
        response = client.post("/check-escalations")

    assert response.status_code == 200
    mock_send.assert_called_once()
    content = mock_send.call_args[0][2]
    assert "inc-abc123" in content
    assert "Inseon" in content


def test_check_escalations_no_stale_alerts_sends_nothing(client, mock_db):
    with (
        patch("app.get_stale_alerts", return_value=[]),
        patch("app.send_channel_message") as mock_send,
    ):
        response = client.post("/check-escalations")
    assert response.status_code == 200
    mock_send.assert_not_called()
