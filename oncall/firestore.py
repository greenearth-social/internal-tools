from datetime import UTC, datetime


def register_user(db, user_id: str, name: str, discord_handle: str) -> None:
    db.collection("oncall_users").document(user_id).set(
        {"name": name, "discord_handle": discord_handle}
    )


def get_user(db, user_id: str) -> dict | None:
    doc = db.collection("oncall_users").document(user_id).get()
    return doc.to_dict() if doc.exists else None


def get_current_oncall(db) -> dict | None:
    doc = db.collection("oncall_schedule").document("current").get()
    return doc.to_dict() if doc.exists else None


def set_current_oncall(db, user_id: str, until: datetime) -> None:
    db.collection("oncall_schedule").document("current").set(
        {"user_id": user_id, "until": until.isoformat()}
    )


def create_alert(
    db, alert_id: str, policy_name: str, severity: str, runbook_found: bool, fired_at: datetime
) -> None:
    db.collection("oncall_alerts").document(alert_id).set(
        {
            "policy_name": policy_name,
            "severity": severity,
            "fired_at": fired_at.isoformat(),
            "status": "open",
            "acked_by": None,
            "acked_at": None,
            "resolved_at": None,
            "runbook_found": runbook_found,
        }
    )


def ack_alert(db, alert_id: str, user_id: str) -> dict | None:
    ref = db.collection("oncall_alerts").document(alert_id)
    doc = ref.get()
    if not doc.exists:
        return None
    ref.update(
        {
            "status": "acked",
            "acked_by": user_id,
            "acked_at": datetime.now(UTC).isoformat(),
        }
    )
    return doc.to_dict()


def resolve_alert(db, alert_id: str) -> dict | None:
    ref = db.collection("oncall_alerts").document(alert_id)
    doc = ref.get()
    if not doc.exists:
        return None
    ref.update(
        {
            "status": "resolved",
            "resolved_at": datetime.now(UTC).isoformat(),
        }
    )
    return doc.to_dict()


def get_stale_alerts(db, threshold: datetime) -> list[dict]:
    docs = (
        db.collection("oncall_alerts")
        .where("status", "==", "open")
        .where("severity", "==", "critical")
        .get()
    )
    stale = []
    for doc in docs:
        data = doc.to_dict()
        fired_at = datetime.fromisoformat(data["fired_at"])
        if fired_at < threshold:
            stale.append({"id": doc.id, **data})
    return stale
