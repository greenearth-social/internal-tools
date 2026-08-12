from datetime import UTC, datetime
from unittest.mock import MagicMock

from firestore import (
    ack_alert,
    create_alert,
    get_current_oncall,
    get_stale_alerts,
    get_user,
    register_user,
    resolve_alert,
    set_current_oncall,
)

NOW = datetime(2026, 8, 10, 20, 0, 0, tzinfo=UTC)


def _mock_db_with_doc(data: dict | None):
    db = MagicMock()
    doc = MagicMock()
    doc.exists = data is not None
    doc.to_dict.return_value = data
    db.collection.return_value.document.return_value.get.return_value = doc
    return db


def test_register_user_sets_document():
    db = MagicMock()
    register_user(db, "user123", "Inseon", "inthree3")
    db.collection("oncall_users").document("user123").set.assert_called_once_with(
        {"name": "Inseon", "discord_handle": "inthree3"}
    )


def test_get_user_returns_dict_when_exists():
    db = _mock_db_with_doc({"name": "Inseon", "discord_handle": "inthree3"})
    result = get_user(db, "user123")
    assert result == {"name": "Inseon", "discord_handle": "inthree3"}


def test_get_user_returns_none_when_missing():
    db = _mock_db_with_doc(None)
    assert get_user(db, "missing") is None


def test_get_current_oncall_returns_none_when_missing():
    db = _mock_db_with_doc(None)
    assert get_current_oncall(db) is None


def test_set_current_oncall_stores_utc_iso():
    db = MagicMock()
    set_current_oncall(db, "user123", NOW)
    db.collection("oncall_schedule").document("current").set.assert_called_once_with(
        {"user_id": "user123", "until": "2026-08-10T20:00:00+00:00"}
    )


def test_create_alert_stores_open_status():
    db = MagicMock()
    create_alert(db, "inc-001", "es-storage-high", "critical", True, NOW)
    db.collection("oncall_alerts").document("inc-001").set.assert_called_once_with(
        {
            "policy_name": "es-storage-high",
            "severity": "critical",
            "fired_at": "2026-08-10T20:00:00+00:00",
            "status": "open",
            "acked_by": None,
            "acked_at": None,
            "resolved_at": None,
            "runbook_found": True,
        }
    )


def test_ack_alert_returns_none_when_not_found():
    db = _mock_db_with_doc(None)
    assert ack_alert(db, "missing", "user123") is None


def test_ack_alert_updates_status():
    doc_data = {"status": "open", "policy_name": "es-storage-high"}
    db = _mock_db_with_doc(doc_data)
    result = ack_alert(db, "inc-001", "user123")
    assert result == doc_data
    ref = db.collection("oncall_alerts").document("inc-001")
    update_call = ref.update.call_args[0][0]
    assert update_call["status"] == "acked"
    assert update_call["acked_by"] == "user123"
    assert "acked_at" in update_call


def test_resolve_alert_updates_status():
    doc_data = {"status": "acked", "policy_name": "es-storage-high"}
    db = _mock_db_with_doc(doc_data)
    result = resolve_alert(db, "inc-001")
    assert result == doc_data
    ref = db.collection("oncall_alerts").document("inc-001")
    update_call = ref.update.call_args[0][0]
    assert update_call["status"] == "resolved"
    assert "resolved_at" in update_call


def test_get_stale_alerts_filters_by_threshold():
    db = MagicMock()
    old_doc = MagicMock()
    old_doc.id = "inc-001"
    old_doc.to_dict.return_value = {
        "status": "open",
        "severity": "critical",
        "fired_at": "2026-08-10T19:00:00+00:00",
        "policy_name": "es-storage-high",
    }
    recent_doc = MagicMock()
    recent_doc.id = "inc-002"
    recent_doc.to_dict.return_value = {
        "status": "open",
        "severity": "critical",
        "fired_at": "2026-08-10T20:30:00+00:00",
        "policy_name": "es-storage-high",
    }
    (db.collection.return_value.where.return_value.where.return_value.get.return_value) = [
        old_doc,
        recent_doc,
    ]

    threshold = datetime(2026, 8, 10, 20, 15, 0, tzinfo=UTC)
    stale = get_stale_alerts(db, threshold)
    assert len(stale) == 1
    assert stale[0]["id"] == "inc-001"
