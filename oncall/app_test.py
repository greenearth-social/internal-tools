from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock


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
    mock_db.collection.return_value.document.return_value.get.side_effect = [
        oncall_doc, user_doc
    ]

    with patch("app.fetch_runbook", return_value=(True, "## Steps\n1. Check storage.")), \
         patch("app.send_channel_message") as mock_send, \
         patch("app.create_alert") as mock_create:
        response = client.post("/alert", json=GCP_CRITICAL_PAYLOAD)

    assert response.status_code == 200
    mock_send.assert_called_once()
    sent_content = mock_send.call_args[0][2]
    assert "CRITICAL" in sent_content
    assert "Inseon" in sent_content
    assert "inc-abc123" in sent_content
    mock_create.assert_called_once()


def test_alert_warning_posts_without_mention_no_firestore(client, mock_db):
    with patch("app.fetch_runbook", return_value=(False, "")), \
         patch("app.send_channel_message") as mock_send, \
         patch("app.create_alert") as mock_create:
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

    with patch("app.fetch_runbook", return_value=(False, "")), \
         patch("app.send_channel_message") as mock_send, \
         patch("app.create_alert"):
        response = client.post("/alert", json=GCP_CRITICAL_PAYLOAD)

    assert response.status_code == 200
    sent_content = mock_send.call_args[0][2]
    assert "no oncall set" in sent_content
