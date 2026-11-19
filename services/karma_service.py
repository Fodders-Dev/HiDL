"""RDR2-система кармы, учёта проседаний по сферам и динамического тона HiDL."""

from __future__ import annotations

import datetime
import html
from typing import Any, Dict, List, Optional, Tuple

from db import repositories as repo
from utils.time import format_time_local, local_date_str

SICK_FREEZE_MONTHLY_LIMIT = 2
SICK_FREEZE_HOURS = 24

DOMAIN_LABELS: Dict[str, Tuple[str, str]] = {
    "sleep": ("😴", "Сон и режим"),
    "food": ("🍽", "Еда"),
    "tasks": ("📋", "Дела и ритм"),
    "hygiene": ("🫧", "Вода и гигиена"),
}

TRACKED_DOMAINS = tuple(DOMAIN_LABELS.keys())

KARMA_MODES: Tuple[Dict[str, Any], ...] = (
    {
        "min_score": 80,
        "code": "proud",
        "label": "Горжусь тобой",
        "emoji": "💚",
        "desc": "Тёплая поддержка и доверие: держишь ритм как человек.",
    },
    {
        "min_score": 50,
        "code": "care",
        "label": "Спокойная забота",
        "emoji": "🙂",
        "desc": "Ровный деловой тон: держит фокус и мягко страхует.",
    },
    {
        "min_score": 25,
        "code": "strict",
        "label": "Строгая мама",
        "emoji": "😠",
        "desc": "Ворчит, припоминает долги и не даёт спускать режим на тормозах.",
    },
    {
        "min_score": 0,
        "code": "alarm",
        "label": "ТРЕВОГА / КАПС",
        "emoji": "🚨",
        "desc": "Жёсткий нажим и контроль каждого шага, чтобы вытащить тебя обратно в колею.",
    },
)


def _naive_utc(dt: Optional[datetime.datetime] = None) -> datetime.datetime:
    if dt is None:
        return datetime.datetime.utcnow()
    if dt.tzinfo is not None:
        return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


def _parse_iso_utc(raw: Optional[str]) -> Optional[datetime.datetime]:
    if not raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(raw).strip())
        return _naive_utc(dt)
    except Exception:
        return None


def get_karma_mode(score: int) -> Dict[str, Any]:
    """Вернуть режим характера HiDL по текущему значению кармы 0–100."""
    clamped = max(0, min(100, int(score)))
    for mode in KARMA_MODES:
        if clamped >= mode["min_score"]:
            return dict(mode)
    return dict(KARMA_MODES[-1])


def render_karma_bar(score: int) -> str:
    """Компактный 10-сегментный индикатор шкалы кармы."""
    clamped = max(0, min(100, int(score)))
    filled = round(clamped / 10)
    filled = max(0, min(10, filled))
    return f"[{'▓' * filled}{'░' * (10 - filled)}] {clamped}/100"


def is_sick_freeze_active(
    user: Dict[str, Any], now_utc: Optional[datetime.datetime] = None
) -> bool:
    """Проверить, активна ли 24-часовая заморозка штрафов «Болею / форс-мажор»."""
    until = _parse_iso_utc(user.get("sick_until_utc"))
    if until is None:
        return False
    return _naive_utc(now_utc) < until


def sick_freeze_remaining_uses(
    user: Dict[str, Any], now_utc: Optional[datetime.datetime] = None
) -> Tuple[int, int]:
    """Вернуть (использовано_в_этом_месяце, лимит) в таймзоне пользователя."""
    now = _naive_utc(now_utc)
    tz = user.get("timezone") or "UTC"
    current_ym = local_date_str(now, tz)[:7]
    stored_ym = str(user.get("sick_uses_month") or "")
    used = int(user.get("sick_uses_count") or 0) if stored_ym == current_ym else 0
    return max(0, used), SICK_FREEZE_MONTHLY_LIMIT


async def activate_sick_freeze(
    db, user: Dict[str, Any], now_utc: Optional[datetime.datetime] = None
) -> Dict[str, Any]:
    """Активировать заморозку штрафов на 24 часа (не более 2 раз в месяц)."""
    now = _naive_utc(now_utc)
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    current_ym = local_date[:7]

    if is_sick_freeze_active(user, now):
        until = _parse_iso_utc(user.get("sick_until_utc"))
        return {
            "ok": False,
            "already_active": True,
            "limit_reached": False,
            "until_utc": until.isoformat() if until else user.get("sick_until_utc"),
            "until_local": format_time_local(until, tz) if until else "",
        }

    used, limit = sick_freeze_remaining_uses(user, now)
    if used >= limit:
        return {
            "ok": False,
            "already_active": False,
            "limit_reached": True,
            "used": used,
            "limit": limit,
        }

    until = now + datetime.timedelta(hours=SICK_FREEZE_HOURS)
    new_used = used + 1
    until_iso = until.isoformat()
    await repo.update_user_sick_freeze(db, int(user["id"]), until_iso, current_ym, new_used)

    state = await repo.get_or_create_karma_state(db, int(user["id"]))
    score_now = int(state.get("score") or 70)
    await repo.insert_karma_event(
        db,
        int(user["id"]),
        local_date,
        "sleep",
        "sick_freeze",
        0,
        score_now,
        "Включена заморозка «Болею / форс-мажор» на 24ч",
        created_at=now.isoformat(),
    )
    return {
        "ok": True,
        "already_active": False,
        "limit_reached": False,
        "sick_until_utc": until_iso,
        "until_local": format_time_local(until, tz),
        "used": new_used,
        "limit": limit,
        "remaining": max(0, limit - new_used),
    }


def domain_debt_callout(
    state: Dict[str, Any], domain: Optional[str] = None
) -> str:
    """Короткая фраза-упрёк или похвала с учётом долгов и серий по сферам."""
    sleep_debt = int(state.get("sleep_debt") or 0)
    food_debt = int(state.get("food_debt") or 0)
    tasks_debt = int(state.get("tasks_debt") or 0)
    hygiene_debt = int(state.get("hygiene_debt") or 0)

    debts = [
        ("sleep", sleep_debt, "сон уже просел несколько раз — пора выравниваться"),
        ("food", food_debt, "ты снова забиваешь на еду"),
        ("tasks", tasks_debt, "копятся пропуски по делам и ритму"),
        ("hygiene", hygiene_debt, "проседают вода и базовая гигиена"),
    ]
    if domain:
        specific = [d for d in debts if d[0] == domain and d[1] >= 2]
        if specific:
            return f"У тебя по этой сфере уже накоплен долг ({specific[0][1]}): {specific[0][2]}."

    worst = max(debts, key=lambda x: x[1])
    if worst[1] >= 2:
        others_ok = sum(1 for k, v, _ in debts if k != worst[0] and v == 0)
        if others_ok >= 2:
            return f"По остальному держишься неплохо, но {worst[2]}."
        return f"Главный провал сейчас — {worst[2]}."

    score = int(state.get("score") or 70)
    streaks = [int(state.get(f"{d}_streak") or 0) for d in TRACKED_DOMAINS]
    if score >= 80 and max(streaks, default=0) >= 3:
        return "Хорошая серия без срывов — так держать, человек."
    return ""


async def record_karma_event(
    db,
    user: Dict[str, Any],
    *,
    domain: str,
    event_type: str,
    delta: int,
    reason: str,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Применить событие дисциплины к карме 0–100 и долгам/сериям по сфере."""
    now = _naive_utc(now_utc)
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    user_id = int(user["id"])

    norm_domain = domain if domain in TRACKED_DOMAINS else "tasks"
    state = await repo.get_or_create_karma_state(db, user_id)
    old_score = int(state.get("score") if state.get("score") is not None else 70)
    old_mode = get_karma_mode(old_score)

    frozen = delta < 0 and is_sick_freeze_active(user, now)
    actual_delta = 0 if frozen else int(delta)
    new_score = max(0, min(100, old_score + actual_delta))
    new_mode = get_karma_mode(new_score)

    debt_col = f"{norm_domain}_debt"
    streak_col = f"{norm_domain}_streak"
    old_debt = int(state.get(debt_col) or 0)
    old_streak = int(state.get(streak_col) or 0)

    if actual_delta > 0:
        new_streak = old_streak + 1
        new_debt = max(0, old_debt - 1)
    elif actual_delta < 0:
        new_streak = 0
        debt_inc = 2 if event_type in {"checkin_ignored", "night_catch"} else 1
        new_debt = min(10, old_debt + debt_inc)
    else:
        new_streak = old_streak
        new_debt = old_debt

    event_reason = (
        f"[заморозка ЧП] {reason}" if frozen else reason
    )
    summary = f"{event_reason} ({actual_delta:+d})" if actual_delta != 0 else event_reason

    await repo.update_karma_state(
        db,
        user_id,
        score=new_score,
        **{
            debt_col: new_debt,
            streak_col: new_streak,
            "last_event_summary": summary[:220],
            "updated_at": now.isoformat(),
        },
    )
    await repo.insert_karma_event(
        db,
        user_id,
        local_date,
        norm_domain,
        event_type,
        actual_delta,
        new_score,
        event_reason,
        created_at=now.isoformat(),
    )

    updated_state = await repo.get_or_create_karma_state(db, user_id)
    return {
        "score_before": old_score,
        "score_after": new_score,
        "delta": actual_delta,
        "frozen": frozen,
        "mode": new_mode,
        "mode_changed": old_mode["code"] != new_mode["code"],
        "state": updated_state,
    }


def format_karma_screen(
    user: Dict[str, Any],
    state: Dict[str, Any],
    recent_events: List[Dict[str, Any]],
    checkins_today: List[Dict[str, Any]],
    now_utc: Optional[datetime.datetime] = None,
    *,
    effective_schedule: Optional[Tuple[str, str, bool]] = None,
) -> str:
    """Собрать HTML-экран раздела «🌙 Сон и карма»."""
    now = _naive_utc(now_utc)
    tz = user.get("timezone") or "UTC"
    score = int(state.get("score") if state.get("score") is not None else 70)
    mode = get_karma_mode(score)
    bar = render_karma_bar(score)

    wd_wake = str(user.get("wake_up_time") or "08:00")
    wd_sleep = str(user.get("sleep_time") or "23:00")
    we_wake = str(user.get("weekend_wake_up_time") or "").strip()
    we_sleep = str(user.get("weekend_sleep_time") or "").strip()

    sched_lines = [f"• Будни: подъём {html.escape(wd_wake)} · отбой {html.escape(wd_sleep)}"]
    if we_wake and we_sleep:
        sched_lines.append(f"• Выходные: подъём {html.escape(we_wake)} · отбой {html.escape(we_sleep)}")
    else:
        sched_lines.append("• Выходные: как в будни (можно задать отдельно)")
    if effective_schedule:
        eff_w, eff_s, is_we = effective_schedule
        day_tag = "выходной" if is_we else "будни"
        sched_lines.append(f"• Сегодня ({day_tag}): <b>{html.escape(eff_w)} – {html.escape(eff_s)}</b>")

    if user.get("sleep_confirmed_at"):
        sched_lines.append("• Ночной статус: 🛌 <b>Отбой подтверждён (телефон убран)</b>")

    if is_sick_freeze_active(user, now):
        until = _parse_iso_utc(user.get("sick_until_utc"))
        until_str = format_time_local(until, tz) if until else ""
        sched_lines.append(f"• Заморозка ЧП: 🤒 <b>активна до {html.escape(until_str)}</b> (штрафы кармы отключены)")
    else:
        used, limit = sick_freeze_remaining_uses(user, now)
        sched_lines.append(f"• Заморозка «Болею 24ч»: осталось {max(0, limit - used)}/{limit} в этом месяце")

    pulse_on = bool(user.get("pulse_enabled", 1))
    legacy_on = bool(user.get("legacy_pings_enabled", 0))
    sched_lines.append(
        f"• Пульс дня: {'✅ вкл' if pulse_on else '⏸ выкл'} · старые авто-пинги: {'✅ вкл' if legacy_on else '🔕 выкл'}"
    )

    domain_lines = []
    for dom_key, (emoji, label) in DOMAIN_LABELS.items():
        debt = int(state.get(f"{dom_key}_debt") or 0)
        streak = int(state.get(f"{dom_key}_streak") or 0)
        debt_badge = f"долг {debt} ⚠️" if debt >= 2 else (f"долг {debt}" if debt == 1 else "ровно ✅")
        domain_lines.append(f"{emoji} {label}: {debt_badge} · серия {streak}")

    slot_titles = {
        "wake": "Подъём",
        "lunch": "Обед",
        "afternoon": "День",
        "dinner": "Ужин",
        "winddown": "Вечер (-60м)",
        "bedtime": "Отбой (-15м)",
    }
    status_icons = {
        "done": "✅",
        "pending": "⏳",
        "snoozed": "⏰",
        "skipped": "⏭",
        "ignored": "❌",
    }
    today_items = []
    for row in checkins_today:
        sk = str(row.get("slot_key") or "")
        if sk not in slot_titles:
            continue
        icon = status_icons.get(str(row.get("status") or "pending"), "⏳")
        today_items.append(f"{icon} {slot_titles[sk]}")

    callout = domain_debt_callout(state)
    lines = [
        f"<b>🌙 Сон, режим и карма</b>",
        f"{mode['emoji']} <b>Режим: {html.escape(mode['label'])}</b>",
        f"<code>{html.escape(bar)}</code>",
        f"<i>{html.escape(mode['desc'])}</i>",
    ]
    if callout:
        lines.append(f"💬 {html.escape(callout)}")
    lines.append("")
    lines.append("<b>🕒 Режим сна и пульс:</b>")
    lines.extend(sched_lines)
    lines.append("")
    lines.append("<b>📊 Сферы дисциплины:</b>")
    lines.extend(domain_lines)
    if today_items:
        lines.append("")
        lines.append("<b>💓 Чек-ины сегодня:</b> " + " · ".join(today_items))

    if recent_events:
        lines.append("")
        lines.append("<b>📜 Последние события кармы:</b>")
        for ev in recent_events[:4]:
            delta = int(ev.get("delta") or 0)
            sign = f"{delta:+d}" if delta != 0 else "0"
            lines.append(
                f"• <code>{sign}</code> {html.escape(str(ev.get('reason') or ''))} → {int(ev.get('score_after') or 0)}"
            )

    return "\n".join(lines)
