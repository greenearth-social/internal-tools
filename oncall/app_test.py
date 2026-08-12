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
    "data": {"name": "register"},
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
    mock_reg.assert_called_once_with(mock_db, "uid1", "Inseon", "inthree3")


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


def test_modal_submit_creates_pr_and_replies(client):
    with patch("app.create_runbook_pr", return_value="https://github.com/.../pull/42") as mock_pr:
        response = _post_interaction(client, MODAL_SUBMIT_PAYLOAD)
    assert response.status_code == 200
    content = response.json()["data"]["content"]
    assert "https://github.com/.../pull/42" in content
    mock_pr.assert_called_once_with(
        os.environ["GE_GITHUB_TOKEN"],
        "es-storage-high",
        "ES Storage > 80%",
        "## Steps\n1. Check.",
    )


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
