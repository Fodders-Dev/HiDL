"""Desk webhook persistence and account-link operations. No Telegram framework imports."""

from __future__ import annotations

import datetime as dt
import json
import secrets
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from utils.time import tzinfo_from_string, is_valid_timezone

from config import DeskIntegrationSettings, get_desk_settings

UTC = dt.timezone.utc
EVENT_TYPES = {
    "lesson.reminder_15m", "homework.assigned", "homework.due_24h",
    "homework.graded", "homework.submitted", "homework.submissions_digest",
}


def utc_iso(value: dt.datetime | None = None) -> str:
    value = value or dt.datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_datetime(value: Any) -> dt.datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid_datetime")
    raw = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError("invalid_datetime")
    return parsed.astimezone(UTC)


def validate_url(value: Any, *, optional: bool = True) -> str:
    if value in (None, "") and optional:
        return ""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("invalid_url")
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("invalid_url")
    return value


def validate_envelope(payload: Any, event_header: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or type(payload.get("schemaVersion")) is not int or payload.get("schemaVersion") != 1:
        raise ValueError("invalid_envelope")
    event_id = payload.get("eventId")
    kind = payload.get("type")
    recipient = payload.get("recipient")
    data = payload.get("data")
    if not isinstance(event_id, str) or not (1 <= len(event_id) <= 200) or not isinstance(event_header, str) or event_id != event_header:
        raise ValueError("event_id_mismatch")
    if not isinstance(kind, str) or kind not in EVENT_TYPES or not isinstance(recipient, dict) or not isinstance(data, dict):
        raise ValueError("invalid_envelope")
    desk_user_id = recipient.get("deskUserId")
    role = recipient.get("role")
    if not isinstance(desk_user_id, str) or not desk_user_id.strip() or len(desk_user_id) > 200 or not isinstance(role, str) or role not in {"student", "teacher"}:
        raise ValueError("invalid_recipient")
    parse_datetime(payload.get("occurredAt"))
    required = {
        "lesson.reminder_15m": ("lessonId", "title", "startsAt", "endsAt"),
        "homework.assigned": ("homeworkId", "title"),
        "homework.due_24h": ("homeworkId", "title", "dueAt"),
        "homework.graded": ("homeworkId", "title"),
        "homework.submitted": ("homeworkId", "studentName", "title"),
        "homework.submissions_digest": (),
    }[kind]
    for key in required:
        if not isinstance(data.get(key), str) or not data[key].strip() or len(data[key]) > 500:
            raise ValueError("invalid_data")
    if kind == "homework.assigned" and data.get("dueAt") is not None:
        if not isinstance(data["dueAt"], str) or not data["dueAt"].strip() or len(data["dueAt"]) > 500:
            raise ValueError("invalid_data")
    if kind in {"homework.submitted", "homework.submissions_digest"} and role != "teacher":
        raise ValueError("invalid_role")
    if kind in {"homework.assigned", "homework.due_24h", "homework.graded"} and role != "student":
        raise ValueError("invalid_role")
    if any(key in data and data[key] is not None and (not isinstance(data[key], str) or len(data[key]) > 500) for key in ("subject", "commentPreview", "text")):
        raise ValueError("invalid_data")
    if "grade" in data and data["grade"] is not None and (isinstance(data["grade"], bool) or not isinstance(data["grade"], (str, int, float)) or len(str(data["grade"])) > 100):
        raise ValueError("invalid_data")
    if kind == "homework.submissions_digest":
        items = data.get("submissions", data.get("items", []))
        if not isinstance(items, list) or len(items) > 500:
            raise ValueError("invalid_data")
        for item in items:
            if not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key].strip() or len(item[key]) > 500 for key in ("studentName", "title")):
                raise ValueError("invalid_data")
    if "url" in data:
        validate_url(data["url"])
    for key in ("startsAt", "endsAt", "dueAt"):
        if key in data and not (kind == "homework.assigned" and key == "dueAt" and data[key] is None):
            parse_datetime(data[key])
    if kind == "lesson.reminder_15m" and parse_datetime(data["endsAt"]) <= parse_datetime(data["startsAt"]):
        raise ValueError("invalid_data")
    return payload


async def create_link_token(conn, *, desk_user_id: str, role: str, display_name: str,
                            timezone: str, settings: DeskIntegrationSettings | None = None,
                            now: dt.datetime | None = None) -> dict[str, str]:
    cfg = settings or get_desk_settings()
    if not cfg.bot_username:
        raise RuntimeError("bot_username_not_configured")
    if not isinstance(desk_user_id, str) or not desk_user_id.strip() or len(desk_user_id) > 200:
        raise ValueError("invalid_desk_user_id")
    if not isinstance(role, str) or role not in {"student", "teacher"}:
        raise ValueError("invalid_role")
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 200:
        raise ValueError("invalid_display_name")
    if not isinstance(timezone, str) or not timezone.strip():
        raise ValueError("invalid_timezone")
    try:
        if not is_valid_timezone(timezone):
            raise ValueError("invalid_timezone")
        tzinfo_from_string(timezone)
    except (ZoneInfoNotFoundError, TypeError):
        raise ValueError("invalid_timezone")
    now_s = utc_iso(now)
    base_now = now or dt.datetime.now(UTC)
    if base_now.tzinfo is None:
        base_now = base_now.replace(tzinfo=UTC)
    expires = utc_iso(base_now.astimezone(UTC) + dt.timedelta(seconds=cfg.link_token_ttl_sec))
    token = secrets.token_urlsafe(32)
    await conn.execute(
        "INSERT INTO desk_link_tokens(token, desk_user_id, role, display_name, timezone, expires_at_utc, created_at) VALUES(?,?,?,?,?,?,?)",
        (token, desk_user_id.strip(), role, display_name.strip(), timezone, expires, now_s),
    )
    await conn.commit()
    bot = cfg.bot_username
    deep = f"{cfg.telegram_link_base_url.rstrip('/')}/{bot}?start=desk_{token}" if bot else ""
    return {"token": token, "deepLink": deep, "expiresAt": expires}


async def consume_link_token(conn, token: str, user_id: int) -> dict[str, Any]:
    """Consume once under an immediate transaction; caller owns the connection."""
    now = utc_iso()
    await conn.execute("BEGIN IMMEDIATE")
    try:
        cur = await conn.execute("SELECT * FROM desk_link_tokens WHERE token=?", (token,))
        row = await cur.fetchone()
        if not row:
            await conn.rollback()
            return {"status": "invalid"}
        r = dict(row)
        if r.get("consumed_at_utc"):
            await conn.rollback()
            return {"status": "consumed"}
        if r["expires_at_utc"] <= now:
            await conn.rollback()
            return {"status": "expired"}
        existing = await (await conn.execute("SELECT id,profile_mode FROM users WHERE telegram_id=?", (int(user_id),))).fetchone()
        if not existing:
            cur = await conn.execute(
                "INSERT INTO users(telegram_id,name,timezone,wake_up_time,sleep_time,profile_mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (int(user_id), r["display_name"], r["timezone"], "08:00", "23:00", "student" if r["role"] == "student" else "adult", now, now),
            )
            actual_user_id = int(cur.lastrowid)
            existing_mode = "student" if r["role"] == "student" else "adult"
            is_new = True
        else:
            actual_user_id = int(existing["id"])
            existing_mode = str(existing["profile_mode"] or "adult")
            is_new = False
        occupied = await (await conn.execute("SELECT user_id,desk_user_id FROM desk_links WHERE desk_user_id=? OR user_id=?", (r["desk_user_id"], actual_user_id))).fetchall()
        if any(int(x["user_id"]) != actual_user_id or str(x["desk_user_id"]) != r["desk_user_id"] for x in occupied):
            await conn.rollback()
            return {"status": "already_linked"}
        same_link = any(int(x["user_id"]) == actual_user_id and str(x["desk_user_id"]) == r["desk_user_id"] for x in occupied)
        if same_link:
            await conn.execute("UPDATE desk_links SET role=?,display_name=?,enabled=1,updated_at=? WHERE user_id=?", (r["role"], r["display_name"], now, actual_user_id))
        else:
            await conn.execute(
                "INSERT INTO desk_links(user_id,desk_user_id,role,display_name,enabled,linked_at,updated_at) VALUES(?,?,?,?,1,?,?)",
                (actual_user_id, r["desk_user_id"], r["role"], r["display_name"], now, now),
            )
        if r["role"] == "teacher":
            await conn.execute("UPDATE users SET profile_mode='adult',updated_at=? WHERE id=?", (now, actual_user_id))
        await conn.execute("UPDATE desk_link_tokens SET consumed_at_utc=?,consumed_by_user_id=? WHERE token=? AND consumed_at_utc IS NULL", (now, actual_user_id, token))
        offer = r["role"] == "student" and not is_new and existing_mode == "adult"
        await conn.commit()
        return {"status": "linked", "role": r["role"], "offer_student_mode": offer,
                "desk_user_id": r["desk_user_id"], "display_name": r["display_name"], "timezone": r["timezone"], "user_id": actual_user_id, "is_new_user": is_new}
    except Exception:
        await conn.rollback()
        raise


async def get_link_for_user(conn, user_id: int) -> dict[str, Any] | None:
    row = await (await conn.execute("SELECT * FROM desk_links WHERE user_id=? AND enabled=1", (int(user_id),))).fetchone()
    return dict(row) if row else None


async def get_link_for_desk_user(conn, desk_user_id: str) -> dict[str, Any] | None:
    row = await (await conn.execute("SELECT * FROM desk_links WHERE desk_user_id=? AND enabled=1", (desk_user_id,))).fetchone()
    return dict(row) if row else None


async def unlink_user(conn, user_id: int) -> bool:
    await conn.execute("BEGIN IMMEDIATE")
    try:
        row = await (await conn.execute("SELECT desk_user_id FROM desk_links WHERE user_id=?", (int(user_id),))).fetchone()
        if row:
            await conn.execute("UPDATE desk_link_tokens SET consumed_at_utc=? WHERE desk_user_id=? AND consumed_at_utc IS NULL", (utc_iso(), row["desk_user_id"]))
            await conn.execute("UPDATE desk_outbox SET status='suppressed_unlinked' WHERE user_id=? AND status IN ('pending','sending')", (int(user_id),))
        cur = await conn.execute("DELETE FROM desk_links WHERE user_id=?", (int(user_id),))
        await conn.commit()
        return cur.rowcount > 0
    except Exception:
        await conn.rollback()
        raise


async def unlink_desk_user(conn, desk_user_id: str) -> bool:
    await conn.execute("BEGIN IMMEDIATE")
    try:
        row = await (await conn.execute("SELECT user_id FROM desk_links WHERE desk_user_id=?", (desk_user_id,))).fetchone()
        await conn.execute("UPDATE desk_link_tokens SET consumed_at_utc=? WHERE desk_user_id=? AND consumed_at_utc IS NULL", (utc_iso(), desk_user_id))
        if row:
            await conn.execute("UPDATE desk_outbox SET status='suppressed_unlinked' WHERE user_id=? AND status IN ('pending','sending')", (row["user_id"],))
        cur = await conn.execute("DELETE FROM desk_links WHERE desk_user_id=?", (desk_user_id,))
        await conn.commit()
        return cur.rowcount > 0
    except Exception:
        await conn.rollback()
        raise


async def ingest_event(conn, payload: dict[str, Any], *, now: dt.datetime | None = None,
                       settings: DeskIntegrationSettings | None = None) -> dict[str, Any]:
    """Persist one event and its plan/outbox changes atomically. Caller should use isolated connection."""
    cfg = settings or get_desk_settings()
    event_id, kind = payload["eventId"], payload["type"]
    recipient, data = payload["recipient"], payload["data"]
    desk_uid, role = recipient["deskUserId"], recipient["role"]
    received = utc_iso(now)
    await conn.execute("BEGIN IMMEDIATE")
    try:
        prior = await (await conn.execute("SELECT 1 FROM desk_events WHERE event_id=?", (event_id,))).fetchone()
        if prior:
            await conn.rollback()
            return {"duplicate": True}
        link = await (await conn.execute("SELECT user_id,role FROM desk_links WHERE desk_user_id=? AND enabled=1", (desk_uid,))).fetchone()
        if not link or link["role"] != role:
            await conn.rollback()
            return {"not_linked": True}
        uid = int(link["user_id"])
        await conn.execute("INSERT INTO desk_events(event_id,desk_user_id,user_id,event_type,payload_json,received_at) VALUES(?,?,?,?,?,?)",
                           (event_id, desk_uid, uid, kind, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), received))
        await _apply_event(conn, uid, event_id, kind, data, received, cfg)
        await conn.commit()
        return {"duplicate": False, "user_id": uid}
    except Exception:
        await conn.rollback()
        raise


async def _apply_event(conn, uid: int, event_id: str, kind: str, data: dict[str, Any], now: str,
                       cfg: DeskIntegrationSettings) -> None:
    url = validate_url(data.get("url"))
    subject = str(data.get("subject") or "").strip()
    title = str(data.get("title") or "Задание").strip()
    user = await (await conn.execute("SELECT timezone FROM users WHERE id=?", (uid,))).fetchone()
    timezone = tzinfo_from_string((user["timezone"] if user else "UTC") or "UTC")
    if kind in {"homework.assigned", "homework.due_24h", "homework.graded"}:
        external = f"desk:hw:{data['homeworkId']}"
        has_due_date = data.get("dueAt") is not None
        existing = None
        if kind != "homework.assigned" or has_due_date:
            existing = await (await conn.execute("SELECT id,done,plan_id FROM day_plan_items WHERE user_id=? AND external_key=?", (uid, external))).fetchone()
        already_done = bool(existing and existing["done"])
        if kind == "homework.graded":
            if existing:
                await conn.execute("UPDATE day_plan_items SET done=1,updated_at=? WHERE id=?", (now, existing["id"]))
        elif (kind != "homework.assigned" or has_due_date) and (not existing or not existing["done"]):
            due = parse_datetime(data["dueAt"]).astimezone(timezone).date().isoformat()
            plan_id = await _get_or_create_plan_id(conn, uid, due, now)
            label = f"{subject}: {title}" if subject else title
            if existing:
                await conn.execute("UPDATE day_plan_items SET plan_id=?,title=?,category='desk',external_url=?,updated_at=? WHERE id=? AND done=0", (plan_id, label, url, now, existing["id"]))
            else:
                await conn.execute("INSERT INTO day_plan_items(plan_id,user_id,title,category,is_important,done,synced_to_today,external_key,external_url,created_at,updated_at) VALUES(?,?,?,'desk',1,0,0,?,?,?,?)", (plan_id, uid, label, external, url, now, now))
        if kind == "homework.assigned":
            msg, priority = f"📚 Новое задание{(' по ' + subject) if subject else ''}: {title}", "normal"
        elif kind == "homework.due_24h":
            msg, priority = f"🌿 Мягкое напоминание: завтра срок сдачи «{title}».", "normal"
        else:
            grade_value = data.get("grade")
            grade = "" if grade_value is None else str(grade_value).strip()
            comment = str(data.get("commentPreview") or "").strip()[:300]
            msg = f"✨ Проверили задание «{title}»" + (f": {grade}" if grade else "") + (f"\n{comment}" if comment else "")
            priority = "normal"
        if not (kind == "homework.due_24h" and already_done):
            await _enqueue(conn, uid, event_id, kind, priority, msg, url, "🔗 Открыть в Desk", now, now)
    elif kind == "lesson.reminder_15m":
        start, end = parse_datetime(data["startsAt"]), parse_datetime(data["endsAt"])
        local_start, local_end = start.astimezone(timezone), end.astimezone(timezone)
        date, st, en = local_start.date().isoformat(), local_start.strftime("%H:%M"), local_end.strftime("%H:%M")
        external = f"desk:lesson:{data['lessonId']}"
        cur = await conn.execute("SELECT id FROM schedule_events WHERE user_id=? AND external_key=?", (uid, external))
        row = await cur.fetchone()
        label = f"{subject}: {title}" if subject else title
        if row:
            await conn.execute("UPDATE schedule_events SET event_date=?,start_time=?,end_time=?,title=?,source='desk',category='desk',updated_at=? WHERE id=?", (date, st, en, label, now, row["id"]))
        else:
            await conn.execute("INSERT INTO schedule_events(user_id,event_date,start_time,end_time,title,category,source,external_key,created_at,updated_at) VALUES(?,?,?, ?,?,'desk','desk',?,?,?)", (uid, date, st, en, label, external, now, now))
        plan_id = await _get_or_create_plan_id(conn, uid, date, now)
        plan_item = await (await conn.execute("SELECT id,done FROM day_plan_items WHERE user_id=? AND external_key=?", (uid, external))).fetchone()
        if plan_item:
            if not plan_item["done"]:
                await conn.execute("UPDATE day_plan_items SET plan_id=?,title=?,external_url=?,updated_at=? WHERE id=?", (plan_id,label,url,now,plan_item["id"]))
        else:
            await conn.execute("INSERT INTO day_plan_items(plan_id,user_id,title,category,is_important,external_key,external_url,created_at,updated_at) VALUES(?,?,?,'desk',1,?,?,?,?)", (plan_id,uid,label,external,url,now,now))
        await _enqueue(conn,uid,event_id,kind,"urgent",f"🔔 Через 15 минут занятие: {label}",url,"🔗 Открыть в Desk",now,now)
    elif kind == "homework.submitted":
        student = str(data["studentName"]).strip()
        msg = f"📥 {student} сдал(а) «{title}»"
        batch = f"teacher_submit:{uid}"
        pending = await (await conn.execute("SELECT deliver_after_utc FROM desk_outbox WHERE batch_key=? AND status='pending' AND deliver_after_utc>? ORDER BY created_at,id LIMIT 1", (batch, now))).fetchone()
        now_value = now_dt(now)
        # Only join a future pending window. Ready backlog rows keep their stable
        # batch key and are separated by the scheduler's deadline grouping.
        first_due = pending["deliver_after_utc"] if pending else utc_iso(now_value + dt.timedelta(seconds=cfg.submit_batch_sec))
        await _enqueue(conn,uid,event_id,kind,"normal",msg,url,"🔗 Открыть в Desk",first_due,now,batch)
    elif kind == "homework.submissions_digest":
        items = data.get("submissions") or data.get("items") or []
        lines = [f"• {str(i.get('studentName') or 'Ученик')}: {str(i.get('title') or 'Задание')}" for i in items if isinstance(i,dict)]
        msg = "📋 Сводка сдач\n" + ("\n".join(lines) if lines else str(data.get("text") or "Есть обновления по сдачам."))
        await _enqueue(conn,uid,event_id,kind,"normal",msg,url,"🔗 Открыть в Desk",now,now)


def now_dt(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _get_or_create_plan_id(conn, user_id: int, plan_date: str, now: str) -> int:
    await conn.execute(
        "INSERT OR IGNORE INTO day_plans(user_id,plan_date,created_at,updated_at) VALUES(?,?,?,?)",
        (user_id, plan_date, now, now),
    )
    row = await (await conn.execute("SELECT id FROM day_plans WHERE user_id=? AND plan_date=?", (user_id, plan_date))).fetchone()
    return int(row["id"])


async def _enqueue(conn, uid: int, event_id: str, kind: str, priority: str, text: str,
                   url: str, button: str, due: str, created: str, batch: str = "") -> None:
    await conn.execute("INSERT INTO desk_outbox(user_id,event_id,event_type,priority,batch_key,text,url,button_label,deliver_after_utc,status,attempts,last_error,created_at) VALUES(?,?,?,?,?,?,?,?,?,'pending',0,'',?)",
                       (uid,event_id,kind,priority,batch,text,url,button,due,created))
