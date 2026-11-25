from datetime import UTC, datetime, timedelta


def register_user(db, user_id: str, name: str, discord_handle: str, github_handle: str) -> None:
    db.collection("oncall_users").document(user_id).set(
        {
            "name": name,
            "discord_handle": discord_handle,
            "github_handle": github_handle,
        }
    )


def get_user(db, user_id: str) -> dict | None:
    doc = db.collection("oncall_users").document(user_id).get()
    return doc.to_dict() if doc.exists else None


def list_registered_users(db) -> list[dict]:
    docs = db.collection("oncall_users").get()
    return [{"user_id": d.id, **d.to_dict()} for d in docs]


def sync_rotation_queue(db, current_user_id: str | None = None) -> list[str]:
    """Computes the rotation queue of registered users following current_user_id."""
    users = list_registered_users(db)
    user_ids = [u["user_id"] for u in users]
    if not user_ids:
        return []
    if current_user_id and current_user_id in user_ids:
        idx = user_ids.index(current_user_id)
        return user_ids[idx + 1 :] + user_ids[:idx]
    return user_ids


def get_current_oncall(db, now: datetime | None = None) -> dict | None:
    doc = db.collection("oncall_schedule").document("current").get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    if not isinstance(data, dict):
        return None
    until_str = data.get("until")
    if isinstance(until_str, str):
        try:
            until_dt = datetime.fromisoformat(until_str)
            curr_now = now or datetime.now(UTC)
            if curr_now >= until_dt:
                new_data, _ = rotate_oncall_schedule(db, curr_now)
                if new_data:
                    return new_data
        except (ValueError, TypeError):
            pass
    return data


def set_current_oncall(
    db, user_id: str, until: datetime, rotation_queue: list[str] | None = None
) -> None:
    data = {"user_id": user_id, "until": until.isoformat()}
    if rotation_queue is not None:
        data["rotation_queue"] = rotation_queue
    db.collection("oncall_schedule").document("current").set(data)


def rotate_oncall_schedule(db, now: datetime | None = None) -> tuple[dict | None, dict | None]:
    """Rotates the oncall shift to the next engineer in round-robin order.

    Returns (new_oncall_data, prev_oncall_data) if rotation happened, or (None, None).
    """
    doc_ref = db.collection("oncall_schedule").document("current")
    doc = doc_ref.get()
    if not doc.exists:
        return None, None

    current_data = doc.to_dict()
    if not isinstance(current_data, dict):
        return None, None

    until_str = current_data.get("until")
    if not isinstance(until_str, str):
        return None, None

    try:
        until_dt = datetime.fromisoformat(until_str)
    except (ValueError, TypeError):
        return None, None

    curr_now = now or datetime.now(UTC)
    if curr_now < until_dt:
        return None, None

    prev_user_id = current_data.get("user_id")
    raw_queue = current_data.get("rotation_queue")

    users = list_registered_users(db)
    registered_ids = [u["user_id"] for u in users]
    if not registered_ids:
        return None, None

    if isinstance(raw_queue, list) and raw_queue:
        active_queue = [uid for uid in raw_queue if uid in registered_ids and uid != prev_user_id]
        for uid in registered_ids:
            if uid != prev_user_id and uid not in active_queue:
                active_queue.append(uid)
    else:
        active_queue = sync_rotation_queue(db, prev_user_id)

    today = curr_now.date()
    days_until_sunday = (6 - today.weekday()) % 7 or 7
    end = today + timedelta(days=days_until_sunday)
    new_until = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC)

    if not active_queue:
        if prev_user_id and prev_user_id in registered_ids:
            new_data = {
                "user_id": prev_user_id,
                "until": new_until.isoformat(),
                "rotation_queue": [],
            }
            doc_ref.set(new_data)
            return new_data, current_data
        return None, None

    next_user_id = active_queue[0]
    new_queue = active_queue[1:]
    if prev_user_id and prev_user_id in registered_ids:
        new_queue.append(prev_user_id)

    new_data = {
        "user_id": next_user_id,
        "until": new_until.isoformat(),
        "rotation_queue": new_queue,
    }
    doc_ref.set(new_data)
    return new_data, current_data


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
