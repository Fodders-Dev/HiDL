"""Сбор живого контекста дня и автообновляемого портрета привычек пользователя."""

from __future__ import annotations

import datetime
import html
import json
from typing import Any, Dict, List, Optional

from db import repositories as repo
from services import calendar_service, karma_service, pulse_service
from utils.rows import rows_to_dicts
from utils.profile import is_student_mode
from utils.time import format_time_local, local_date_str, parse_hhmm

DIET_LABELS = {
    "vegan": "веган",
    "vegetarian": "вегетарианец",
    "omnivore": "обычное",
}


def _naive_utc(dt: Optional[datetime.datetime] = None) -> datetime.datetime:
    if dt is None:
        return datetime.datetime.utcnow()
    if dt.tzinfo is not None:
        return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


def _parse_iso(raw: Optional[str]) -> Optional[datetime.datetime]:
    if not raw:
        return None
    try:
        return _naive_utc(datetime.datetime.fromisoformat(str(raw).strip()))
    except Exception:
        return None


def _minutes_until_sleep(
    local_hhmm: str, wake_hhmm: str, sleep_hhmm: str
) -> int:
    now_t = parse_hhmm(local_hhmm) or datetime.time(12, 0)
    wake_t = parse_hhmm(wake_hhmm) or datetime.time(8, 0)
    sleep_t = parse_hhmm(sleep_hhmm) or datetime.time(23, 0)
    now_m = now_t.hour * 60 + now_t.minute
    wake_m = wake_t.hour * 60 + wake_t.minute
    sleep_m = sleep_t.hour * 60 + sleep_t.minute

    if sleep_m <= wake_m:
        # Ночной отбой (например 00:30 при подъёме 08:00)
        if now_m >= wake_m:
            return (sleep_m + 24 * 60) - now_m
        return sleep_m - now_m
    return sleep_m - now_m


async def refresh_user_portrait_if_needed(
    db,
    user: Dict[str, Any],
    now_utc: Optional[datetime.datetime] = None,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """Детерминированно обновить краткий портрет привычек по истории последних 14 дней."""
    now = _naive_utc(now_utc)
    user_id = int(user["id"])
    tz = user.get("timezone") or "UTC"
    local_today = local_date_str(now, tz)

    memory = await repo.get_or_create_profile_memory(db, user_id)
    total_events = await repo.count_karma_events(db, user_id)
    last_ref = _parse_iso(memory.get("last_refreshed_at"))
    events_seen = int(memory.get("events_seen_count") or 0)

    needs_refresh = (
        force
        or not memory.get("last_refreshed_at")
        or last_ref is None
        or (now - last_ref) > datetime.timedelta(hours=24)
        or (total_events - events_seen) >= 5
    )
    if not needs_refresh:
        return memory

    since_date = (datetime.date.fromisoformat(local_today) - datetime.timedelta(days=14)).isoformat()
    karma_events = await repo.list_karma_events_since(db, user_id, since_date)
    checkins = await repo.list_pulse_checkins_since(db, user_id, since_date)
    karma_state = await repo.get_or_create_karma_state(db, user_id)
    wellness_row = await repo.get_wellness(db, user_id)
    meal_profile = str((dict(wellness_row) if wellness_row else {}).get("meal_profile") or "omnivore")
    pantry_rows = rows_to_dicts(await repo.list_pantry_items(db, user_id))
    journal_rows = rows_to_dicts(await repo.list_journal_entries(db, user_id, limit=25))

    patterns: List[str] = []
    bullets: List[str] = []

    # 1. Сон и ночной онлайн
    night_catches = sum(1 for e in karma_events if e.get("event_type") == "night_catch")
    sleep_ignores = sum(
        1 for e in karma_events if e.get("domain") == "sleep" and e.get("event_type") == "checkin_ignored"
    )
    sleep_debt = int(karma_state.get("sleep_debt") or 0)
    sleep_streak = int(karma_state.get("sleep_streak") or 0)
    if not is_student_mode(user) and (night_catches >= 1 or sleep_ignores >= 1 or sleep_debt >= 2):
        patterns.append("late_night_drift")
        bullets.append(
            f"• Сон: бывали срывы/поздний онлайн (ночных поимок: {night_catches}, долг сна: {sleep_debt})."
        )
    elif sleep_streak >= 3:
        patterns.append("sleep_steady")
        bullets.append(f"• Сон: держит график стабильно (серия {sleep_streak}).")
    else:
        bullets.append("• Сон: режим настраивается, явных затяжных срывов пока нет.")

    # 2. Еда и холодильник
    food_misses = sum(
        1
        for e in karma_events
        if e.get("domain") == "food" and e.get("event_type") in {"checkin_ignored", "checkin_skip"}
    )
    food_debt = int(karma_state.get("food_debt") or 0)
    diet_label = DIET_LABELS.get(meal_profile, meal_profile)
    if food_misses >= 2 or food_debt >= 2:
        patterns.append("skips_meals")
        bullets.append(
            f"• Еда ({diet_label}): периодически пропускает приёмы пищи (пропусков: {food_misses}, в холодильнике {len(pantry_rows)} поз.)."
        )
    else:
        bullets.append(
            f"• Еда ({diet_label}): в холодильнике {len(pantry_rows)} поз., явного голодного завала нет."
        )

    # 3. Ритм, переносы и хвосты
    snoozes_total = sum(int(c.get("snooze_count") or 0) for c in checkins)
    rollovers_total = sum(1 for e in karma_events if e.get("event_type") == "rollover")
    tasks_debt = int(karma_state.get("tasks_debt") or 0)
    if snoozes_total >= 3 or tasks_debt >= 2:
        patterns.append("frequent_snoozer")
        bullets.append(
            f"• Ритм: часто откладывает пинги/дела (переносов: {snoozes_total}, переносов на завтра: {rollovers_total})."
        )
    elif int(karma_state.get("tasks_streak") or 0) >= 3:
        patterns.append("steady_executor")
        bullets.append(f"• Ритм: закрывает дела ровно без лишних переносов.")

    # 4. Эмоциональные маркеры из недавних записей (без цитирования личного текста)
    fatigue_hits = 0
    anxiety_hits = 0
    for j in journal_rows:
        txt = str(j.get("user_text") or "").lower()
        if any(k in txt for k in ("устал", "нет сил", "выгорел", "выжат")):
            fatigue_hits += 1
        if any(k in txt for k in ("трево", "бессон", "не могу уснуть", "тяжело")):
            anxiety_hits += 1
    if fatigue_hits >= 2 or anxiety_hits >= 2:
        patterns.append("needs_low_friction")
        bullets.append(
            "• Состояние: в последних заметках проскакивает усталость/напряжение — лучше предлагать простые шаги до 10–15 мин."
        )

    portrait_text = "\n".join(bullets)
    patterns_json = json.dumps(patterns, ensure_ascii=False)
    await repo.update_profile_memory(
        db,
        user_id,
        portrait_text=portrait_text,
        patterns_json=patterns_json,
        last_refreshed_at=now.isoformat(),
        events_seen_count=total_events,
    )
    return {
        "user_id": user_id,
        "portrait_text": portrait_text,
        "patterns_json": patterns_json,
        "last_refreshed_at": now.isoformat(),
        "events_seen_count": total_events,
    }


async def build_user_day_snapshot(
    db,
    user: Dict[str, Any],
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Собрать единый снимок дня пользователя для ИИ-мозга и ситуативных советов."""
    now = _naive_utc(now_utc)
    user_id = int(user["id"])
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    local_hhmm = format_time_local(now, tz)
    local_hour = int(local_hhmm.split(":")[0])

    wake_hhmm, sleep_hhmm, is_weekend = pulse_service.get_effective_schedule(user, local_date)
    mins_to_sleep = _minutes_until_sleep(local_hhmm, wake_hhmm, sleep_hhmm)

    karma_state = await repo.get_or_create_karma_state(db, user_id)
    score = int(karma_state.get("score") if karma_state.get("score") is not None else 70)
    student = is_student_mode(user)
    karma_mode = karma_service.get_karma_mode(score, user)
    sick_active = karma_service.is_sick_freeze_active(user, now)

    checkins = await repo.list_pulse_checkins_for_date(db, user_id, local_date)
    slot_status = {str(c.get("slot_key")): str(c.get("status")) for c in checkins
                   if not student or c.get("slot_key") not in {"lunch", "dinner"}}
    wake_chk = next((c for c in checkins if str(c.get("slot_key")) == "wake"), None)
    late_wake_time = ""
    if wake_chk:
        try:
            wake_meta = json.loads(wake_chk.get("meta_json") or "{}")
            if isinstance(wake_meta, dict):
                late_wake_time = str(wake_meta.get("late_wake_at_local") or "").strip()
        except Exception:
            late_wake_time = ""

    plan_rows = rows_to_dicts(await repo.list_day_plan_items(db, user_id, local_date))
    pending_tasks = [p for p in plan_rows if not p.get("done")]
    done_tasks_count = sum(1 for p in plan_rows if p.get("done"))

    reminders = rows_to_dicts(await repo.list_custom_reminders(db, user_id))
    events_today = await calendar_service.get_unified_events_for_date(db, user_id, local_date)
    tomorrow_date = (datetime.date.fromisoformat(local_date) + datetime.timedelta(days=1)).isoformat()
    events_tomorrow = await calendar_service.get_unified_events_for_date(db, user_id, tomorrow_date)
    pantry_rows = rows_to_dicts(await repo.list_pantry_items(db, user_id))
    shopping_rows = [
        r for r in rows_to_dicts(await repo.list_shopping_items(db, user_id, scope="household"))
        if not r.get("is_bought")
    ]
    meal_plan_today = await repo.get_meal_plan_for_date(db, user_id, local_date)
    wellness = await repo.get_wellness(db, user_id)
    meal_profile = str((dict(wellness) if wellness else {}).get("meal_profile") or "omnivore")

    memory = await refresh_user_portrait_if_needed(db, user, now_utc=now)
    try:
        patterns = json.loads(memory.get("patterns_json") or "[]")
        if not isinstance(patterns, list):
            patterns = []
    except Exception:
        patterns = []
    if student:
        patterns = [p for p in patterns if p != "late_night_drift"]

    return {
        "local_date": local_date,
        "local_time": local_hhmm,
        "local_hour": local_hour,
        "timezone": tz,
        "is_weekend": is_weekend,
        "wake_up_time": wake_hhmm,
        "sleep_time": sleep_hhmm,
        "minutes_to_sleep": mins_to_sleep,
        "sleep_confirmed": bool(user.get("sleep_confirmed_at")),
        "sick_freeze_active": sick_active,
        "karma_score": score,
        "karma_mode": karma_mode,
        "karma_state": karma_state,
        "slot_status": slot_status,
        "late_wake_time": late_wake_time,
        "pending_tasks": pending_tasks,
        "done_tasks_count": done_tasks_count,
        "reminders": reminders[:5],
        "events_today": events_today[:8],
        "events_tomorrow": events_tomorrow[:5],
        "pantry_items": [] if student else pantry_rows,
        "shopping_unbought": [] if student else shopping_rows,
        "meal_plan_today": [] if student else meal_plan_today,
        "meal_profile": meal_profile,
        "student_mode": student,
        "portrait_text": memory.get("portrait_text") or "",
        "patterns": patterns,
    }


def format_context_for_llm(snapshot: Dict[str, Any]) -> str:
    """Сформировать компактный контекст жизни пользователя и директиву тона для LLM."""
    mode = snapshot["karma_mode"]
    student = bool(snapshot.get("student_mode"))
    mode_code = mode["code"]
    score = snapshot["karma_score"]
    state = snapshot["karma_state"]

    tone_directives = {
        "proud": "Тон ответа: 💚 тёплый, гордый и поддерживающий (человек держит ритм).",
        "care": "Тон ответа: 🙂 спокойная деловая забота без воды.",
        "strict": "Тон ответа: 😠 строгая мама — прямолинейно, с припоминанием просадок, без оскорблений.",
        "alarm": "Тон ответа: 🚨 жёсткий нажим и контроль (можно акценты КАПСОМ по делу срыва, без оскорблений).",
    }
    if student:
        tone_directives["strict"] = "Тон ответа: спокойный, бережный, без давления, стыда и нотаций."
        tone_directives["alarm"] = tone_directives["strict"]

    debts = []
    for k, label in (("sleep", "сон"), ("food", "еда"), ("tasks", "дела"), ("hygiene", "гигиена")):
        val = int(state.get(f"{k}_debt") or 0)
        if val > 0:
            debts.append(f"{label}={val}")
    debts_str = ", ".join(debts) if debts else "нет"

    pending = [str(t.get("title") or "") for t in snapshot["pending_tasks"][:6] if t.get("title")]
    pending_str = "; ".join(pending) if pending else "нет открытых задач"

    pantry_names = [str(p.get("name") or "") for p in snapshot["pantry_items"][:12] if p.get("name")]
    pantry_str = ", ".join(pantry_names) if pantry_names else "пусто"

    shop_names = [str(s.get("item_name") or "") for s in snapshot["shopping_unbought"][:6] if s.get("item_name")]
    shop_str = ", ".join(shop_names) if shop_names else "пусто"

    slot_st = snapshot.get("slot_status") or {}
    ate_note = "" if student else (
        "обед/ужин закрыт"
        if slot_st.get("lunch") == "done" or slot_st.get("dinner") == "done"
        else "еда сегодня ещё не отмечена"
    )
    mins_left = int(snapshot.get("minutes_to_sleep") or 0)
    sleep_left_str = (
        f"через {mins_left} мин"
        if mins_left >= 0
        else f"просрочен на {abs(mins_left)} мин!"
    )

    pulse_core = (
        "спокойный ритм дня"
        if student
        else f"{ate_note}, {'ночной отбой=подтверждён' if snapshot.get('sleep_confirmed') else 'ночной отбой=нет'}"
    )
    if snapshot.get("late_wake_time"):
        pulse_core += f", поздний подъём отмечен в {snapshot['late_wake_time']}"
    lines = [
        "=== Снимок дня пользователя ===",
        f"Сейчас: {snapshot['local_date']} {snapshot['local_time']} ({snapshot['timezone']}), подъём {snapshot['wake_up_time']}, отбой {snapshot['sleep_time']} ({sleep_left_str}).",
        f"Карма: {score}/100 ({mode['emoji']} {mode['label']}), долги: {debts_str}. {tone_directives.get(mode_code, '')}",
        f"Пульс: {pulse_core}, ЧП-заморозка={'да' if snapshot.get('sick_freeze_active') else 'нет'}.",
        f"Задачи на сегодня (открыто {len(snapshot['pending_tasks'])}, закрыто {snapshot['done_tasks_count']}): {pending_str}.",
    ]
    if student:
        lines.append("Профиль: ученик. Тон спокойный и бережный; не дави, не стыди и не используй нотации или КАПС.")
    else:
        lines.append(f"Питание: {DIET_LABELS.get(snapshot['meal_profile'], snapshot['meal_profile'])}. Холодильник: {pantry_str}. Покупки: {shop_str}.")
    meal_plan_today = snapshot.get("meal_plan_today") or []
    if meal_plan_today:
        slot_names = {"breakfast": "завтрак", "lunch": "обед", "dinner": "ужин"}
        meal_parts = [
            f"{slot_names.get(str(m.get('meal_slot')), m.get('meal_slot'))} — {m.get('title')}"
            + (" ✓" if m.get("status") == "cooked" else "")
            for m in meal_plan_today
            if m.get("title")
        ]
        if meal_parts:
            lines.append("Меню на сегодня: " + ", ".join(meal_parts) + ".")
    events_today = snapshot.get("events_today") or []
    if events_today:
        ev_parts = [
            ("весь день" if e.get("is_all_day") else f"{e.get('start_time')}–{e.get('end_time')}")
            + f" {e.get('title')}"
            for e in events_today[:5]
        ]
        lines.append("Календарь сегодня: " + "; ".join(ev_parts) + ".")
    early_tom = [
        e
        for e in (snapshot.get("events_tomorrow") or [])
        if not e.get("is_all_day") and str(e.get("start_time") or "99:99") < "10:00"
    ]
    if early_tom:
        e0 = early_tom[0]
        lines.append(f"Раннее событие завтра: {e0.get('start_time')} {e0.get('title')}.")
    last_ev = str(state.get("last_event_summary") or "").strip()
    if last_ev:
        lines.append(f"Последнее событие кармы: {last_ev}.")
    if snapshot.get("portrait_text"):
        lines.append("Портрет привычек:\n" + str(snapshot["portrait_text"]))
    return "\n".join(lines)


def format_portrait_card(
    user: Dict[str, Any],
    memory: Dict[str, Any],
    karma_state: Dict[str, Any],
) -> str:
    """Сформировать HTML-карточку «🧠 Портрет привычек» для Telegram."""
    score = int(karma_state.get("score") if karma_state.get("score") is not None else 70)
    mode = karma_service.get_karma_mode(score, user)
    portrait = str(memory.get("portrait_text") or "").strip()
    refreshed = str(memory.get("last_refreshed_at") or "")[:16].replace("T", " ")

    lines = [
        "<b>🧠 Портрет привычек HiDL</b>",
        f"{mode['emoji']} Сейчас карма: <b>{score}/100</b> ({html.escape(mode['label'])})",
        "",
    ]
    if portrait:
        lines.append(html.escape(portrait))
    else:
        lines.append("Пока мало истории — закрой пару чек-инов и я соберу живые паттерны.")
    if refreshed:
        lines.append("")
        lines.append(f"<i>Обновлено: {html.escape(refreshed)} UTC</i>")
    return "\n".join(lines)
