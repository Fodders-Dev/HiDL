"""Единый «Пульс дня» (5 чекпоинтов), эскалация, честные переносы и ночной детектор."""

from __future__ import annotations

import datetime
import html
import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from db import repositories as repo
from services import calendar_service, karma_service
from utils.time import format_time_local, local_date_str, parse_hhmm, tzinfo_from_string
from utils.profile import is_student_mode

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

SLOT_TITLES: Dict[str, str] = {
    "wake": "Подъём",
    "breakfast": "Завтрак",
    "lunch": "Обед",
    "afternoon": "День и вода",
    "dinner": "Ужин",
    "winddown": "Вечерний финиш",
    "bedtime": "Отбой",
}

SLOT_DEFAULT_REWARDS: Dict[str, int] = {
    "wake": 4,
    "breakfast": 3,
    "lunch": 3,
    "afternoon": 3,
    "dinner": 3,
    "winddown": 3,
    "bedtime": 5,
}


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
        return _naive_utc(datetime.datetime.fromisoformat(str(raw).strip()))
    except Exception:
        return None


def _normalize_hhmm(raw: Optional[str], fallback: str) -> str:
    parsed = parse_hhmm(str(raw or "").strip())
    if parsed is not None:
        return parsed.strftime("%H:%M")
    fb = parse_hhmm(fallback)
    return fb.strftime("%H:%M") if fb else "08:00"


def _hhmm_to_min(hhmm: str) -> int:
    parsed = parse_hhmm(str(hhmm or "").strip())
    if parsed is None:
        return 8 * 60
    return parsed.hour * 60 + parsed.minute


def _min_to_hhmm(minutes: int) -> str:
    m = int(minutes) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def _local_minutes_to_utc(
    local_date: str, minute_offset_from_midnight: int, tz_str: str
) -> datetime.datetime:
    base_local = datetime.datetime.combine(
        datetime.date.fromisoformat(local_date), datetime.time(0, 0)
    ) + datetime.timedelta(minutes=int(minute_offset_from_midnight))
    tzinfo = tzinfo_from_string(tz_str)
    aware = base_local.replace(tzinfo=tzinfo)
    return aware.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _load_meta(checkin: Dict[str, Any]) -> Dict[str, Any]:
    raw = checkin.get("meta_json")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _card_ref(checkin: Optional[Dict[str, Any]]) -> Tuple[Optional[int], Optional[int]]:
    row = checkin or {}
    return row.get("card_chat_id"), row.get("card_message_id")


def _transition_snapshot(checkin: Dict[str, Any]) -> Dict[str, Any]:
    """Fields that make a scheduled-send CAS fail after any newer state/card update."""
    names = ("status", "ping_count", "snooze_count", "next_ping_at_utc", "card_chat_id", "card_message_id")
    return {name: checkin.get(name) for name in names if name in checkin}


async def _already_resolved_result(
    db, user: Dict[str, Any], checkin: Dict[str, Any], *, is_wake: bool = False
) -> Dict[str, Any]:
    state = await repo.get_or_create_karma_state(db, int(user["id"]))
    raw_score = state.get("score")
    score = int(70 if raw_score is None else raw_score)
    status = str(checkin.get("status") or "")
    return {
        "ok": True,
        "already_resolved": True,
        "already_done": status == "done",
        "status": status,
        "checkin": checkin,
        "previous_card": _card_ref(checkin),
        "is_wake": is_wake or str(checkin.get("slot_key") or "") == "wake",
        "karma": {
            "score_after": score,
            "delta": 0,
            "mode": karma_service.get_karma_mode(score, user),
        },
    }


async def _clear_saved_card(bot, card_ref: Tuple[Optional[int], Optional[int]]) -> None:
    chat_id, message_id = card_ref
    if chat_id is None or message_id is None:
        return
    from utils.sender import safe_delete_or_clear_markup

    await safe_delete_or_clear_markup(bot, chat_id, message_id)


def get_effective_schedule(
    user: Dict[str, Any], local_date: str
) -> Tuple[str, str, bool]:
    """Вернуть (wake_HHMM, sleep_HHMM, is_weekend) с учётом выходных и нормализации H:MM."""
    dt = datetime.date.fromisoformat(local_date)
    is_weekend = dt.weekday() >= 5

    wd_wake = _normalize_hhmm(user.get("wake_up_time"), "08:00")
    wd_sleep = _normalize_hhmm(user.get("sleep_time"), "23:00")

    if not is_weekend:
        return wd_wake, wd_sleep, False

    raw_we_wake = str(user.get("weekend_wake_up_time") or "").strip()
    raw_we_sleep = str(user.get("weekend_sleep_time") or "").strip()
    we_wake = _normalize_hhmm(raw_we_wake, wd_wake) if parse_hhmm(raw_we_wake) else wd_wake
    we_sleep = _normalize_hhmm(raw_we_sleep, wd_sleep) if parse_hhmm(raw_we_sleep) else wd_sleep
    return we_wake, we_sleep, True


def compute_daily_slots(
    user: Dict[str, Any],
    wellness: Optional[Dict[str, Any]],
    local_date: str,
) -> List[Dict[str, Any]]:
    """Рассчитать 6 слотов единого Пульса дня в UTC для локальной даты."""
    tz = user.get("timezone") or "UTC"
    wake_hhmm, sleep_hhmm, is_weekend = get_effective_schedule(user, local_date)
    wake_min = _hhmm_to_min(wake_hhmm)
    sleep_min = _hhmm_to_min(sleep_hhmm)
    # Если отбой после полуночи (например 00:30), считаем относительно текущих суток пробуждения.
    sleep_rel = sleep_min if sleep_min > wake_min else sleep_min + 24 * 60

    w = dict(wellness) if wellness else {}
    meal_parts = [p.strip() for p in str(w.get("meal_times") or "").split(",") if p.strip()]
    valid_meals = [p for p in meal_parts if parse_hhmm(p)]
    water_parts = [p.strip() for p in str(w.get("water_times") or "").split(",") if p.strip()]

    lunch_default = 13 * 60
    dinner_default = 19 * 60 + 30
    bf_rel: Optional[int] = None

    if len(valid_meals) >= 3:
        lunch_raw = _hhmm_to_min(valid_meals[1])
        lunch_rel = lunch_raw if lunch_raw > wake_min else lunch_default
        lunch_rel = max(wake_min + 90, min(sleep_rel - 240, lunch_rel))

        dinner_raw = _hhmm_to_min(valid_meals[2])
        dinner_rel = dinner_raw if dinner_raw > wake_min else dinner_raw + 24 * 60
        dinner_rel = min(sleep_rel - 90, max(lunch_rel + 180, dinner_rel))

        bf_raw = _hhmm_to_min(valid_meals[0])
        bf_cand = bf_raw if bf_raw > wake_min else wake_min + 30
        bf_rel = max(wake_min + 20, min(lunch_rel - 60, bf_cand))
    else:
        if valid_meals:
            lunch_raw = _hhmm_to_min(valid_meals[0])
            lunch_rel = lunch_raw if lunch_raw > wake_min else lunch_default
        else:
            lunch_rel = max(wake_min + 120, lunch_default)
        lunch_rel = max(wake_min + 90, min(sleep_rel - 240, lunch_rel))

        if len(valid_meals) >= 2:
            dinner_raw = _hhmm_to_min(valid_meals[1])
            dinner_rel = dinner_raw if dinner_raw > wake_min else dinner_raw + 24 * 60
        else:
            dinner_rel = dinner_default
        dinner_rel = min(sleep_rel - 90, max(lunch_rel + 180, dinner_rel))

    afternoon_default = 16 * 60
    afternoon_rel = afternoon_default
    if len(water_parts) >= 2 and parse_hhmm(water_parts[1]):
        cand = _hhmm_to_min(water_parts[1])
        if lunch_rel + 90 <= cand <= dinner_rel - 90:
            afternoon_rel = cand
    afternoon_rel = min(dinner_rel - 60, max(lunch_rel + 60, afternoon_rel))

    winddown_rel = max(dinner_rel + 15, sleep_rel - 60)
    bedtime_rel = max(winddown_rel + 15, sleep_rel - 15)

    specs = [("wake", "sleep", wake_min), ("afternoon", "tasks", afternoon_rel),
             ("winddown", "tasks", winddown_rel), ("bedtime", "sleep", bedtime_rel)]
    if not is_student_mode(user):
        insert_idx = 1
        if bf_rel is not None:
            specs.insert(insert_idx, ("breakfast", "food", bf_rel))
            insert_idx += 1
        specs.insert(insert_idx, ("lunch", "food", lunch_rel))
        specs.insert(insert_idx + 2, ("dinner", "food", dinner_rel))
    out: List[Dict[str, Any]] = []
    for slot_key, domain, offset_min in specs:
        sched_utc = _local_minutes_to_utc(local_date, offset_min, tz)
        out.append(
            {
                "slot_key": slot_key,
                "domain": domain,
                "local_hhmm": _min_to_hhmm(offset_min),
                "scheduled_at_utc": sched_utc,
                "is_weekend": is_weekend,
            }
        )
    return out


def _next_wake_utc_after(
    user: Dict[str, Any], after_utc: datetime.datetime
) -> datetime.datetime:
    tz = user.get("timezone") or "UTC"
    base_date_str = local_date_str(after_utc, tz)
    base_date = datetime.date.fromisoformat(base_date_str)
    for add_days in (0, 1):
        d_str = (base_date + datetime.timedelta(days=add_days)).isoformat()
        wake_hhmm, _, _ = get_effective_schedule(user, d_str)
        candidate = _local_minutes_to_utc(d_str, _hhmm_to_min(wake_hhmm), tz)
        if candidate > after_utc:
            return candidate
    return after_utc + datetime.timedelta(hours=8)


def _confirmed_sleep_active(
    user: Dict[str, Any], now_utc: datetime.datetime
) -> bool:
    confirmed_at = _parse_iso_utc(user.get("sleep_confirmed_at"))
    if confirmed_at is None:
        return False
    elapsed = now_utc - confirmed_at
    if elapsed.total_seconds() < 0 or elapsed > datetime.timedelta(hours=10):
        return False
    next_wake = _next_wake_utc_after(user, confirmed_at)
    return now_utc < (next_wake - datetime.timedelta(minutes=15))


async def _gather_slot_context(
    db, user: Dict[str, Any], local_date: str, slot_key: str
) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {}
    if slot_key in {"wake", "winddown", "afternoon"}:
        plan_items = await repo.list_day_plan_items(db, int(user["id"]), local_date)
        pending_items = [dict(i) for i in plan_items if not i["done"]]
        ctx["pending_titles"] = [str(i.get("title") or "") for i in pending_items if i.get("title")]
        ctx["pending_count"] = len(pending_items)
    if slot_key in {"dinner", "winddown"}:
        shop_rows = await repo.list_shopping_items(db, int(user["id"]), scope="household")
        unbought = [dict(r) for r in shop_rows if not r["is_bought"]]
        ctx["unbought_count"] = len(unbought)
    if slot_key in {"wake", "breakfast", "lunch", "dinner"}:
        meal_rows = await repo.get_meal_plan_for_date(db, int(user["id"]), local_date)
        if slot_key == "wake":
            bfast = next(
                (m for m in meal_rows if m.get("meal_slot") == "breakfast" and m.get("status") != "cooked"),
                None,
            )
            if bfast:
                ctx["planned_breakfast"] = bfast
        else:
            meal = next(
                (m for m in meal_rows if m.get("meal_slot") == slot_key and m.get("status") != "cooked"),
                None,
            )
            if meal:
                ctx["planned_meal"] = meal
    return ctx


def build_pulse_message(
    checkin: Dict[str, Any],
    karma_state: Dict[str, Any],
    *,
    level: int,
    is_snooze_reminder: bool = False,
    context: Optional[Dict[str, Any]] = None,
    user: Optional[Dict[str, Any]] = None,
) -> Tuple[str, InlineKeyboardMarkup]:
    """Собрать текст и кнопки для чек-ина Пульса с учётом кармы и уровня эскалации."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    cid = int(checkin["id"])
    slot_key = str(checkin.get("slot_key") or "")
    domain = str(checkin.get("domain") or "tasks")
    meta = _load_meta(checkin)
    score = int(karma_state.get("score") if karma_state.get("score") is not None else 70)
    mode = karma_service.get_karma_mode(score, user)
    if is_student_mode(user):
        level = 1
    mode_code = mode["code"]
    ctx = context or {}
    debt_note = "" if is_student_mode(user) else karma_service.domain_debt_callout(karma_state, domain=domain)
    last_summary = str(karma_state.get("last_event_summary") or "")

    header_badge = f"{mode['emoji']} Карма {score}/100"
    pending_titles = ctx.get("pending_titles") or []
    pending_count = int(ctx.get("pending_count") or 0)
    unbought_count = int(ctx.get("unbought_count") or 0)

    if is_snooze_reminder:
        base_title = html.escape(str(meta.get("title") or SLOT_TITLES.get(slot_key, "Дело")))
        text = (
            f"⏰ <b>Напоминаю по переносу: {base_title}</b> ({header_badge})\n"
            f"Время вышло — давай закроем это прямо сейчас."
        )
        if level >= 2 or mode_code in {"strict", "alarm"}:
            text += "\nХватит откладывать — потом сложнее разгребать."
    elif slot_key == "wake":
        if level == 1:
            if mode_code == "proud":
                intro = "☀️ <b>Доброе утро!</b> Ты в отличной форме — поднимаемся мягко и держим темп."
            elif mode_code == "strict":
                intro = "☀️ <b>ПОРА ВСТАВАТЬ.</b> Карма просела — сегодняшнее утро надо закрыть чётко."
            elif mode_code == "alarm":
                intro = "🚨 <b>ПОДЪЁМ! ВСТАВАЙ СЕЙЧАС.</b> Режим на дне, хватит просыпать!"
            else:
                intro = "☀️ <b>Доброе утро!</b> Пора подниматься: стакан воды, зубы и старт дня."
            lines = [intro, f"{header_badge} · {mode['label']}"]
            if "ноч" in last_summary.lower() or "игнор" in last_summary.lower():
                lines.append(f"💬 Помню: {html.escape(last_summary)}.")
            elif debt_note:
                lines.append(f"💬 {html.escape(debt_note)}")
            if pending_titles:
                shown = ", ".join(html.escape(t) for t in pending_titles[:3])
                lines.append(f"📌 В плане: {shown}")
            planned_bf = ctx.get("planned_breakfast")
            if isinstance(planned_bf, dict) and planned_bf.get("title"):
                t_bf = int(planned_bf.get("time_minutes") or 10)
                lines.append(f"🥣 Завтрак по плану: <b>{html.escape(str(planned_bf['title']))}</b> ({t_bf} мин)")
            text = "\n".join(lines)
        elif level == 2:
            text = (
                f"⏰ <b>Просыпайся! Ты ещё не отметился в Пульсе подъёма.</b> ({header_badge})\n"
                f"Нажми кнопку, умойся и выпей воды — не скатывай день с утра."
            )
        else:
            text = (
                f"🚨 <b>ТРЕТИЙ СИГНАЛ: ВСТАВАЙ НЕМЕДЛЕННО!</b> ({header_badge})\n"
                f"ЕСЛИ ПРОИГНОРИРУЕШЬ И ЭТО — ЗАФИКСИРУЮ ПРОПУСК И СРЕЖУ КАРМУ."
            )
    elif slot_key == "breakfast":
        if level == 1:
            intro = (
                "🥣 <b>Время завтрака!</b> Закинь что-нибудь в топку, чтобы день пошёл бодрее."
                if mode_code in {"proud", "care"}
                else "🥣 <b>ЗАВТРАК.</b> Поешь нормально с утра, чтобы не провалиться по энергии днём."
            )
            lines = [intro, header_badge]
            planned_bf = ctx.get("planned_meal")
            if isinstance(planned_bf, dict) and planned_bf.get("title"):
                t_bf = int(planned_bf.get("time_minutes") or 10)
                lines.append(f"🥣 По плану на завтрак: <b>{html.escape(str(planned_bf['title']))}</b> ({t_bf} мин)")
            if debt_note:
                lines.append(f"💬 {html.escape(debt_note)}")
            text = "\n".join(lines)
        elif level == 2:
            text = (
                f"🥣 <b>Мягко напоминаю про завтрак</b> ({header_badge})\n"
                f"Если уже поел(а) — отметь или занеси в КБЖУ в 1 тап."
            )
        else:
            text = (
                f"🚨 <b>ЗАВТРАК ПРОПУЩЕН!</b> ({header_badge})\n"
                f"Отметься сейчас или перенеси, чтобы не терять карму."
            )
    elif slot_key == "lunch":
        if level == 1:
            intro = (
                "🍲 <b>Обед!</b> Ты поел или хотя бы собираешься поесть?"
                if mode_code in {"proud", "care"}
                else "🍽 <b>ОБЕД. Не вздумай пропустить приём пищи.</b>"
            )
            lines = [intro, header_badge]
            planned_lunch = ctx.get("planned_meal")
            if isinstance(planned_lunch, dict) and planned_lunch.get("title"):
                t_lu = int(planned_lunch.get("time_minutes") or 20)
                lines.append(f"🍽 По плану на обед: <b>{html.escape(str(planned_lunch['title']))}</b> ({t_lu} мин)")
            if debt_note:
                lines.append(f"💬 {html.escape(debt_note)}")
            text = "\n".join(lines)
        elif level == 2:
            text = (
                f"🍽 <b>Я жду ответа по обеду!</b> ({header_badge})\n"
                f"Голодный мозг всё равно тормозит — поешь или нажми перенос."
            )
        else:
            text = (
                f"🚨 <b>ПОСЛЕДНЕЕ ПРЕДУПРЕЖДЕНИЕ ПО ОБЕДУ!</b> ({header_badge})\n"
                f"ИДЁШЬ ЕСТЬ ИЛИ ФИКСИРУЮ ИГНОР И МИНУС КАРМУ?"
            )
    elif slot_key == "afternoon":
        if level == 1:
            intro = (
                "💧 <b>Дневная сверка курса:</b> выпей стакан воды и проверь главное дело."
                if mode_code in {"proud", "care"}
                else "📋 <b>Дневной контроль:</b> стакан воды прямо сейчас и отчёт, как идут дела."
            )
            lines = [intro, header_badge]
            if pending_titles:
                lines.append(f"🎯 Главное: {html.escape(pending_titles[0])}")
            text = "\n".join(lines)
        elif level == 2:
            text = (
                f"💧 <b>Вода и сверка дня!</b> Ты не ответил(а). ({header_badge})\n"
                f"Глотнул воды, сделал 5 минут по делу — и поехали дальше."
            )
        else:
            text = (
                f"🚨 <b>ЭЙ! СВЕРКА ДНЯ ВИСИТ БЕЗ ОТВЕТА!</b> ({header_badge})\n"
                f"ВЫПЕЙ ВОДЫ И НАЖМИ КНОПКУ, ИНАЧЕ ЗАСЧИТАЮ ИГНОР."
            )
    elif slot_key == "dinner":
        if level == 1:
            intro = (
                "🥗 <b>Время ужина.</b> Что будешь есть вечером?"
                if mode_code in {"proud", "care"}
                else "🍽 <b>УЖИН!</b> Надо нормально поесть, чтобы не срываться ночью к холодильнику."
            )
            lines = [intro, header_badge]
            planned_dinner = ctx.get("planned_meal")
            if isinstance(planned_dinner, dict) and planned_dinner.get("title"):
                t_din = int(planned_dinner.get("time_minutes") or 20)
                lines.append(f"🍽 По плану на ужин: <b>{html.escape(str(planned_dinner['title']))}</b> ({t_din} мин)")
            if unbought_count:
                lines.append(f"🛒 В списке покупок сейчас пунктов: {unbought_count}.")
            if debt_note:
                lines.append(f"💬 {html.escape(debt_note)}")
            text = "\n".join(lines)
        elif level == 2:
            text = (
                f"🥗 <b>Напоминаю про ужин!</b> ({header_badge})\n"
                f"Поешь сейчас, чтобы не жевать в полночь."
            )
        else:
            text = (
                f"🚨 <b>УЖИН ПРОИГНОРИРОВАН!</b> ({header_badge})\n"
                f"ОТМЕТЬСЯ ПО УЖИНУ СЕЙЧАС ИЛИ ПОЛУЧИШЬ ШТРАФ ПО КАРМЕ."
            )
    elif slot_key == "winddown":
        tail_line = (
            f"\n📌 Незакрытых дел в плане дня: <b>{pending_count}</b> — можешь перекинуть их на завтра в 1 клик."
            if pending_count
            else "\n✅ Хвостов в плане дня нет — красавчик."
        )
        if level == 1:
            text = (
                f"🌇 <b>За час до отбоя — сворачиваем день.</b> ({header_badge})\n"
                f"Почисти зубы, сгрузи хвосты и начинай тормозить.{tail_line}"
            )
        elif level == 2:
            text = (
                f"😠 <b>До сна меньше часа!</b> ({header_badge})\n"
                f"Пора закругляться с делами и идти в ванную.{tail_line}"
            )
        else:
            text = (
                f"🚨 <b>ЗАКРЫВАЙ НОУТ / ДЕЛА, СКОРО ОТБОЙ!</b> ({header_badge})\n"
                f"ПЕРЕНОСИ ХВОСТЫ НА ЗАВТРА И ЧИСТИ ЗУБЫ.{tail_line}"
            )
    elif slot_key == "bedtime":
        if level == 1:
            if mode_code == "proud":
                intro = "🌙 <b>Пора спать!</b> Молодец за сегодняшний день — убирай мобилу и отдыхай."
            elif mode_code == "strict":
                intro = "🌙 <b>ОТБОЙ!</b> Сон уже с долгами — убирай телефон прямо сейчас."
            elif mode_code == "alarm":
                intro = "🚨 <b>УБИРАЙ ТЕЛЕФОН И ЛОЖИСЬ СПАТЬ!</b> Хватит гробить режим!"
            else:
                intro = "🌙 <b>Отбой!</b> Убирай мобилу, ставь на зарядку и ложись спать."
            lines = [intro, header_badge]
            if debt_note:
                lines.append(f"💬 {html.escape(debt_note)}")
            text = "\n".join(lines)
        elif level == 2:
            text = (
                f"😠 <b>Ты всё ещё не лёг!</b> ({header_badge})\n"
                f"Каждый лишний час залипания украдёт завтрашние силы. Убирай телефон!"
            )
        else:
            text = (
                f"🚨 <b>НОЧНАЯ ТРЕВОГА: УБЕРИ ТЕЛЕФОН И СПИ СЕЙЧАС ЖЕ!</b> ({header_badge})\n"
                f"ПОСЛЕДНИЙ ШАНС ОТМЕТИТЬ ОТБОЙ БЕЗ ЖЁСТКОГО ШТРАФА КАРМЫ!"
            )
    else:
        title = html.escape(str(meta.get("title") or "Чек-ин"))
        text = (
            f"🔔 <b>{title}</b> ({header_badge})\n"
            f"Проверяем статус — закрой сейчас или честно перенеси."
        )

    # Клавиатура слота
    if slot_key == "wake":
        rows = [
            [InlineKeyboardButton(text="☀️ Я встал", callback_data=f"pulse:done:{cid}")],
            [
                InlineKeyboardButton(text="⏰ Ещё 10 мин", callback_data=f"pulse:snooze:{cid}:10"),
                InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"pulse:skip:{cid}"),
            ],
        ]
    elif slot_key == "breakfast":
        rows = [
            [
                InlineKeyboardButton(text="✅ Позавтракал", callback_data=f"pulse:done:{cid}"),
                InlineKeyboardButton(text="➕ Записать в КБЖУ", callback_data="kbju:add"),
            ],
            [
                InlineKeyboardButton(text="🍳 Что приготовить?", callback_data="kitchen:quick_pick"),
                InlineKeyboardButton(text="⏰ Через 30 мин", callback_data=f"pulse:snooze:{cid}:30"),
            ],
            [
                InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"pulse:skip:{cid}"),
            ],
        ]
    elif slot_key == "lunch":
        rows = [
            [
                InlineKeyboardButton(text="✅ Поел", callback_data=f"pulse:done:{cid}"),
                InlineKeyboardButton(text="➕ Записать в КБЖУ", callback_data="kbju:add"),
            ],
            [
                InlineKeyboardButton(text="🍳 Что приготовить?", callback_data="kitchen:quick_pick"),
                InlineKeyboardButton(text="⏰ Через 30 мин", callback_data=f"pulse:snooze:{cid}:30"),
            ],
            [
                InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"pulse:skip:{cid}"),
            ],
        ]
    elif slot_key == "afternoon":
        rows = [
            [InlineKeyboardButton(text="💧 Вода + в строю", callback_data=f"pulse:done:{cid}")],
            [
                InlineKeyboardButton(text="⏰ Через 30 мин", callback_data=f"pulse:snooze:{cid}:30"),
                InlineKeyboardButton(text="🆘 Нет сил", callback_data="help:menu"),
            ],
        ]
    elif slot_key == "dinner":
        rows = [
            [
                InlineKeyboardButton(text="✅ Поужинал", callback_data=f"pulse:done:{cid}"),
                InlineKeyboardButton(text="➕ Записать в КБЖУ", callback_data="kbju:add"),
            ],
            [
                InlineKeyboardButton(text="🍳 Что приготовить?", callback_data="kitchen:quick_pick"),
                InlineKeyboardButton(text="🛒 Покупки", callback_data="kitchen:shoplist"),
            ],
            [
                InlineKeyboardButton(text="⏰ Через 30 мин", callback_data=f"pulse:snooze:{cid}:30"),
                InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"pulse:skip:{cid}"),
            ],
        ]
    elif slot_key == "winddown":
        rows = [
            [InlineKeyboardButton(text="➡️ Перенести хвосты на завтра", callback_data=f"pulse:rollover:{cid}")],
            [
                InlineKeyboardButton(text="🪥 Зубы почистил", callback_data=f"pulse:hygiene:{cid}:teeth"),
                InlineKeyboardButton(text="✅ Сворачиваю день", callback_data=f"pulse:done:{cid}"),
            ],
            [InlineKeyboardButton(text="⏰ Ещё 20 мин", callback_data=f"pulse:snooze:{cid}:20")],
        ]
    elif slot_key == "bedtime":
        rows = [
            [InlineKeyboardButton(text="🌙 Убрал мобилу, ложусь", callback_data=f"pulse:bedtime_ok:{cid}")],
            [
                InlineKeyboardButton(text="⏰ Ещё 20 мин", callback_data=f"pulse:snooze:{cid}:20"),
                InlineKeyboardButton(text="😅 Позже (+45 мин)", callback_data=f"pulse:snooze:{cid}:45"),
            ],
        ]
    else:
        rows = [
            [InlineKeyboardButton(text="✅ Сделал", callback_data=f"pulse:done:{cid}")],
            [
                InlineKeyboardButton(text="⏰ Ещё 20 мин", callback_data=f"pulse:snooze:{cid}:20"),
                InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"pulse:skip:{cid}"),
            ],
        ]

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_wake_checklist_keyboard(
    checkin_id: int,
    meta: Optional[Dict[str, Any]] = None,
    *,
    is_student: bool = False,
) -> InlineKeyboardMarkup:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    m = meta or {}
    water_mark = "✅" if m.get("water_done") else "⬜️"
    teeth_mark = "✅" if m.get("teeth_done") else "⬜️"
    rows = [
        [
            InlineKeyboardButton(
                text=f"{water_mark} 💧 Стакан воды (+2)",
                callback_data=f"pulse:hygiene:{checkin_id}:water",
            ),
            InlineKeyboardButton(
                text=f"{teeth_mark} 🪥 Почистил зубы (+2)",
                callback_data=f"pulse:hygiene:{checkin_id}:teeth",
            ),
        ],
    ]
    if not is_student:
        rows.append(
            [
                InlineKeyboardButton(text="🥣 Записать завтрак", callback_data="kbju:add"),
                InlineKeyboardButton(text="🍳 Что на завтрак?", callback_data="kitchen:quick_pick"),
            ]
        )
    rows.append([InlineKeyboardButton(text="📅 План на сегодня", callback_data="today:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _maybe_schedule_breakfast_after_wake(
    db,
    user: Dict[str, Any],
    now: datetime.datetime,
) -> Optional[str]:
    """Мягко запланировать чек-ин завтрака через 25 мин после подъёма (без штрафа при пропуске)."""
    if is_student_mode(user):
        return None
    uid = int(user["id"])
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)

    if await repo.get_pulse_checkin(db, uid, local_date, "breakfast"):
        return None
    meals_today = await repo.list_meal_logs(db, uid, local_date)
    if any(str(m.get("meal_slot") or "") == "breakfast" for m in meals_today):
        return None

    wellness_row = await repo.get_wellness(db, uid)
    wellness = dict(wellness_row) if wellness_row else None
    slots = compute_daily_slots(user, wellness, local_date)
    lunch_slot = next((s for s in slots if s["slot_key"] == "lunch"), None)

    bf_utc = now + datetime.timedelta(minutes=25)
    if lunch_slot and bf_utc > lunch_slot["scheduled_at_utc"] - datetime.timedelta(minutes=30):
        return None

    await repo.upsert_pulse_checkin(
        db,
        uid,
        local_date,
        "breakfast",
        "food",
        bf_utc.isoformat(),
        bf_utc.isoformat(),
        status="pending",
        meta_json=json.dumps({"after_wake": True, "gentle": True}, ensure_ascii=False),
    )
    return format_time_local(bf_utc, tz)


async def complete_checkin(
    db,
    user: Dict[str, Any],
    checkin_id: int,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Отметить слот Пульса выполненным и начислить карму."""
    now = _naive_utc(now_utc)
    checkin = await repo.get_pulse_checkin_by_id(db, checkin_id)
    if not checkin or int(checkin["user_id"]) != int(user["id"]):
        return {"ok": False, "error": "Чек-ин не найден."}

    slot_key = str(checkin.get("slot_key") or "")
    if slot_key == "bedtime":
        return await confirm_bedtime(db, user, checkin_id=checkin_id, now_utc=now)

    domain = str(checkin.get("domain") or "tasks")
    meta = _load_meta(checkin)
    delta = SLOT_DEFAULT_REWARDS.get(slot_key, 3)
    title = meta.get("title") or SLOT_TITLES.get(slot_key, slot_key)

    changed, updated = await repo.transition_pulse_checkin(
        db,
        checkin_id,
        user_id=int(user["id"]),
        allowed_statuses=("pending", "snoozed"),
        status="done",
        resolved_at_utc=now.isoformat(),
    )
    if not changed:
        current_chk = updated or checkin
        if slot_key == "wake" and str(current_chk.get("status") or "") == "missed":
            tz = user.get("timezone") or "UTC"
            local_hhmm = format_time_local(now, tz)
            await repo.clear_user_sleep_confirmed(db, int(user["id"]))
            await repo.update_sleep_ping(db, int(user["id"]), "morning", local_date_str(now, tz))
            current_meta = _load_meta(current_chk)
            current_meta["late_wake_at_local"] = local_hhmm
            current_meta["late_wake_at_utc"] = now.isoformat()
            await repo.update_pulse_checkin(
                db,
                checkin_id,
                meta_json=json.dumps(current_meta, ensure_ascii=False),
            )
            current_chk = await repo.get_pulse_checkin_by_id(db, checkin_id) or current_chk
            bf_hhmm = await _maybe_schedule_breakfast_after_wake(db, user, now)
            res = await _already_resolved_result(db, user, current_chk, is_wake=True)
            res["late_wake_recorded"] = True
            res["local_time"] = local_hhmm
            if bf_hhmm:
                res["breakfast_scheduled_hhmm"] = bf_hhmm
            return res
        return await _already_resolved_result(db, user, current_chk, is_wake=slot_key == "wake")
    winning = updated or checkin
    bf_hhmm: Optional[str] = None
    if slot_key == "wake":
        await repo.clear_user_sleep_confirmed(db, int(user["id"]))
        tz = user.get("timezone") or "UTC"
        await repo.update_sleep_ping(db, int(user["id"]), "morning", local_date_str(now, tz))
        bf_hhmm = await _maybe_schedule_breakfast_after_wake(db, user, now)

    karma_res = await karma_service.record_karma_event(
        db,
        user,
        domain=domain,
        event_type="checkin_done",
        delta=delta,
        reason=f"Закрыт чек-ин «{title}»",
        now_utc=now,
    )
    out: Dict[str, Any] = {
        "ok": True,
        "already_done": False,
        "checkin": winning,
        "karma": karma_res,
        "is_wake": slot_key == "wake",
        "previous_card": _card_ref(winning),
    }
    if bf_hhmm:
        out["breakfast_scheduled_hhmm"] = bf_hhmm
    return out


async def close_open_food_pulse_slot(
    db,
    user: Dict[str, Any],
    meal_slot: str,
    *,
    now_utc: Optional[datetime.datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Закрыть открытый пищевой слот Пульса (breakfast/lunch/dinner), если пользователь записал еду."""
    slot = str(meal_slot or "").strip().lower()
    if slot not in {"breakfast", "lunch", "dinner"}:
        return None
    now = _naive_utc(now_utc)
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    chk = await repo.get_pulse_checkin(db, int(user["id"]), local_date, slot)
    if not chk or chk.get("status") not in {"pending", "snoozed"}:
        return None
    return await complete_checkin(db, user, int(chk["id"]), now_utc=now)


async def confirm_bedtime(
    db,
    user: Dict[str, Any],
    checkin_id: Optional[int] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Подтвердить отбой («убрал мобилу, ложусь») и включить ночной контроль."""
    now = _naive_utc(now_utc)
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    user_id = int(user["id"])

    checkin = (
        await repo.get_pulse_checkin_by_id(db, checkin_id)
        if checkin_id is not None
        else await repo.get_pulse_checkin(db, user_id, local_date, "bedtime")
    )
    if checkin_id is not None and checkin is None:
        return {"ok": False, "error": "Чек-ин не найден."}
    if checkin is None:
        checkin = await repo.upsert_pulse_checkin(
            db,
            user_id,
            local_date,
            "bedtime",
            "sleep",
            now.isoformat(),
            now.isoformat(),
            status="pending",
            meta_json=json.dumps({"manual": True}, ensure_ascii=False),
        )

    if int(checkin.get("user_id") or 0) != user_id:
        return {"ok": False, "error": "Чек-ин не найден."}

    changed, updated = await repo.transition_pulse_checkin(
        db,
        int(checkin["id"]),
        user_id=user_id,
        allowed_statuses=("pending", "snoozed"),
        status="done",
        resolved_at_utc=now.isoformat(),
    )
    if not changed:
        return await _already_resolved_result(db, user, updated or checkin, is_wake=False)
    winning = updated or checkin
    await repo.update_user_sleep_confirmed(db, user_id, local_date, now.isoformat())
    await repo.update_sleep_ping(db, user_id, "evening", local_date)

    karma_res = await karma_service.record_karma_event(
        db,
        user,
        domain="sleep",
        event_type="bedtime_confirmed",
        delta=5,
        reason="Вовремя убрал телефон и лёг спать",
        now_utc=now,
    )

    return {
        "ok": True,
        "already_done": False,
        "checkin": winning,
        "karma": karma_res,
        "previous_card": _card_ref(winning),
    }


async def record_hygiene_tick(
    db,
    user: Dict[str, Any],
    checkin_id: int,
    kind: str,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Идемпотентная отметка воды/зубов внутри чек-ина."""
    now = _naive_utc(now_utc)
    checkin = await repo.get_pulse_checkin_by_id(db, checkin_id)
    if not checkin or int(checkin["user_id"]) != int(user["id"]):
        return {"ok": False, "error": "Чек-ин не найден."}

    key_map = {
        "water": ("water_done", "Выпил утренний стакан воды"),
        "teeth": ("teeth_done", "Почистил зубы"),
    }
    if kind not in key_map:
        return {"ok": False, "error": "Неизвестная отметка гигиены."}

    flag_key, reason = key_map[kind]
    meta = _load_meta(checkin)
    if meta.get(flag_key):
        state = await repo.get_or_create_karma_state(db, int(user["id"]))
        return {
            "ok": True,
            "already_marked": True,
            "meta": meta,
            "score_after": int(state.get("score") or 70),
            "delta": 0,
        }

    meta[flag_key] = True
    await repo.update_pulse_checkin(
        db,
        checkin_id,
        meta_json=json.dumps(meta, ensure_ascii=False),
    )
    karma_res = await karma_service.record_karma_event(
        db,
        user,
        domain="hygiene",
        event_type=f"hygiene_{kind}",
        delta=2,
        reason=reason,
        now_utc=now,
    )
    return {
        "ok": True,
        "already_marked": False,
        "meta": meta,
        "score_after": karma_res["score_after"],
        "delta": karma_res["delta"],
        "reason": reason,
    }


async def snooze_checkin(
    db,
    user: Dict[str, Any],
    checkin_id: int,
    minutes: int,
    now_utc: Optional[datetime.datetime] = None,
    *,
    card_chat_id: Optional[int] = None,
    card_message_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Честный перенос чек-ина на N минут (1-й бесплатно, 2-й+ со штрафом -3)."""
    now = _naive_utc(now_utc)
    checkin = await repo.get_pulse_checkin_by_id(db, checkin_id)
    if not checkin or int(checkin["user_id"]) != int(user["id"]):
        return {"ok": False, "error": "Чек-ин не найден."}

    card_ref = _card_ref(checkin)
    callback_has_card = card_chat_id is not None and card_message_id is not None
    if callback_has_card and checkin.get("status") == "snoozed":
        return await _already_resolved_result(db, user, checkin)
    if callback_has_card and card_ref[1] is not None and card_ref != (card_chat_id, card_message_id):
        return await _already_resolved_result(db, user, checkin)

    mins = max(1, min(180, int(minutes)))
    new_snooze_count = int(checkin.get("snooze_count") or 0) + 1
    next_ping = now + datetime.timedelta(minutes=mins)
    tz = user.get("timezone") or "UTC"
    next_local_hhmm = format_time_local(next_ping, tz)
    domain = str(checkin.get("domain") or "tasks")
    meta = _load_meta(checkin)
    title = meta.get("title") or SLOT_TITLES.get(str(checkin.get("slot_key") or ""), "Чек-ин")

    expected = _transition_snapshot(checkin)
    fields = {}
    if callback_has_card:
        fields.update(card_chat_id=card_chat_id, card_message_id=card_message_id)
    changed, updated = await repo.transition_pulse_checkin(
        db,
        checkin_id,
        user_id=int(user["id"]),
        allowed_statuses=("pending", "snoozed"),
        expected_fields=expected,
        status="snoozed",
        snooze_count=new_snooze_count,
        next_ping_at_utc=next_ping.isoformat(),
        resolved_at_utc=None,
        **fields,
    )
    if not changed:
        return await _already_resolved_result(db, user, updated or checkin)
    winning = updated or checkin

    if new_snooze_count >= 2:
        karma_res = await karma_service.record_karma_event(
            db,
            user,
            domain=domain,
            event_type="checkin_snooze_repeat",
            delta=-3,
            reason=f"Повторный перенос «{title}» (+{mins} мин)",
            now_utc=now,
        )
    else:
        state = await repo.get_or_create_karma_state(db, int(user["id"]))
        karma_res = {
            "score_after": int(state.get("score") or 70),
            "delta": 0,
            "frozen": False,
        }

    return {
        "ok": True,
        "checkin": winning,
        "minutes": mins,
        "next_local_hhmm": next_local_hhmm,
        "next_ping_at_utc": next_ping.isoformat(),
        "snooze_count": new_snooze_count,
        "karma": karma_res,
        "previous_card": _card_ref(winning),
        "is_wake": str(winning.get("slot_key") or "") == "wake",
    }


async def skip_checkin(
    db,
    user: Dict[str, Any],
    checkin_id: int,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Честный пропуск чек-ина (-4 кармы, мягче чем игнор)."""
    now = _naive_utc(now_utc)
    checkin = await repo.get_pulse_checkin_by_id(db, checkin_id)
    if not checkin or int(checkin["user_id"]) != int(user["id"]):
        return {"ok": False, "error": "Чек-ин не найден."}

    domain = str(checkin.get("domain") or "tasks")
    meta = _load_meta(checkin)
    title = meta.get("title") or SLOT_TITLES.get(str(checkin.get("slot_key") or ""), "Чек-ин")

    changed, updated = await repo.transition_pulse_checkin(
        db,
        checkin_id,
        user_id=int(user["id"]),
        allowed_statuses=("pending", "snoozed"),
        status="skipped",
        resolved_at_utc=now.isoformat(),
    )
    if not changed:
        return await _already_resolved_result(db, user, updated or checkin)
    winning = updated or checkin
    karma_res = await karma_service.record_karma_event(
        db,
        user,
        domain=domain,
        event_type="checkin_skip",
        delta=-4,
        reason=f"Пропущен чек-ин «{title}»",
        now_utc=now,
    )
    return {
        "ok": True,
        "checkin": winning,
        "karma": karma_res,
        "previous_card": _card_ref(winning),
        "is_wake": str(winning.get("slot_key") or "") == "wake",
    }


async def schedule_adhoc_snooze(
    db,
    user: Dict[str, Any],
    slot_key: str,
    domain: str,
    minutes: int,
    title: str,
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Создать или сдвинуть реальный отложенный пинг в pulse_checkins (для legacy-кнопок)."""
    now = _naive_utc(now_utc)
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    user_id = int(user["id"])
    mins = max(1, min(180, int(minutes)))
    next_ping = now + datetime.timedelta(minutes=mins)

    existing = await repo.get_pulse_checkin(db, user_id, local_date, slot_key)
    meta = {"title": title}
    if existing:
        if existing.get("status") == "done":
            await repo.update_pulse_checkin(
                db,
                int(existing["id"]),
                status="snoozed",
                snooze_count=int(existing.get("snooze_count") or 0) + 1,
                next_ping_at_utc=next_ping.isoformat(),
                resolved_at_utc=None,
                meta_json=json.dumps(meta, ensure_ascii=False),
            )
            return {
                "ok": True,
                "checkin_id": int(existing["id"]),
                "next_local_hhmm": format_time_local(next_ping, tz),
                "minutes": mins,
            }
        res = await snooze_checkin(db, user, int(existing["id"]), mins, now_utc=now)
        return {
            "ok": res["ok"],
            "checkin_id": int(existing["id"]),
            "next_local_hhmm": res.get("next_local_hhmm") or format_time_local(next_ping, tz),
            "minutes": mins,
            "already_resolved": res.get("already_resolved", False),
        }

    created = await repo.upsert_pulse_checkin(
        db,
        user_id,
        local_date,
        slot_key,
        domain,
        now.isoformat(),
        next_ping.isoformat(),
        status="snoozed",
        meta_json=json.dumps(meta, ensure_ascii=False),
    )
    await repo.update_pulse_checkin(
        db,
        int(created["id"]),
        status="snoozed",
        snooze_count=1,
        next_ping_at_utc=next_ping.isoformat(),
    )
    return {
        "ok": True,
        "checkin_id": int(created["id"]),
        "next_local_hhmm": format_time_local(next_ping, tz),
        "minutes": mins,
    }


async def check_night_violation(
    db,
    user: Dict[str, Any],
    now_utc: Optional[datetime.datetime] = None,
) -> Optional[str]:
    """Поймать пользователя за ночным онлайном после подтверждённого отбоя."""
    if is_student_mode(user):
        return None
    if not user.get("pulse_enabled", 1):
        return None
    confirmed_at = _parse_iso_utc(user.get("sleep_confirmed_at"))
    if confirmed_at is None:
        return None

    now = _naive_utc(now_utc)
    elapsed = now - confirmed_at
    if elapsed < datetime.timedelta(minutes=5) or elapsed > datetime.timedelta(hours=10):
        return None

    next_wake = _next_wake_utc_after(user, confirmed_at)
    if now >= (next_wake - datetime.timedelta(minutes=30)):
        return None

    tz = user.get("timezone") or "UTC"
    tzinfo = tzinfo_from_string(tz)
    confirmed_local = confirmed_at.replace(tzinfo=datetime.timezone.utc).astimezone(tzinfo)
    now_local = now.replace(tzinfo=datetime.timezone.utc).astimezone(tzinfo)
    confirmed_date_str = confirmed_local.date().isoformat()
    conf_wake_hhmm, conf_sleep_hhmm, _ = get_effective_schedule(user, confirmed_date_str)
    conf_wake_min = _hhmm_to_min(conf_wake_hhmm)
    conf_sleep_min = _hhmm_to_min(conf_sleep_hhmm)
    conf_local_min = confirmed_local.hour * 60 + confirmed_local.minute
    # Защищаемся от случайного дневного нажатия «Ложусь спать» в 14:00: ловим только в вечерне-ночное окно.
    is_night_confirm = (
        confirmed_local.hour >= 20
        or conf_local_min < conf_wake_min
        or (conf_sleep_min > conf_wake_min and conf_local_min >= conf_sleep_min - 90)
    )
    if not is_night_confirm:
        return None

    last_catch = _parse_iso_utc(user.get("night_catch_last_at"))
    if last_catch is not None and (now - last_catch) < datetime.timedelta(minutes=30):
        return None

    await repo.update_user_night_catch(db, int(user["id"]), now.isoformat())
    local_hhmm = now_local.strftime("%H:%M")

    if karma_service.is_sick_freeze_active(user, now):
        return (
            f"🌙 <b>Вижу тебя в сети в {local_hhmm}.</b>\n"
            f"Штраф не ставлю из-за заморозки «Болею», но постарайся отложить телефон и поспать."
        )

    karma_res = await karma_service.record_karma_event(
        db,
        user,
        domain="sleep",
        event_type="night_catch",
        delta=-5,
        reason=f"Ночной онлайн после отбоя ({local_hhmm})",
        now_utc=now,
    )
    score = karma_res["score_after"]
    mode_code = karma_res["mode"]["code"]
    if mode_code == "alarm":
        return (
            f"🚨 <b>ПОПАЛСЯ! {local_hhmm} НА ЧАСАХ!</b>\n"
            f"ТЫ ОБЕЩАЛ УБРАТЬ ТЕЛЕФОН И СПАТЬ! <b>-5 кармы</b> (теперь {score}/100).\n"
            f"Сообщение я сохранила — А ТЕПЕРЬ НЕМЕДЛЕННО УБИРАЙ МОБИЛУ!"
        )
    if mode_code == "strict":
        return (
            f"😠 <b>ПОПАЛСЯ В НОЧНОМ ОНЛАЙНЕ ({local_hhmm})!</b>\n"
            f"Ты же нажал «Убрал мобилу, ложусь». <b>-5 кармы</b> (теперь {score}/100).\n"
            f"Записала твоё сообщение — а теперь выключай экран и спи."
        )
    return (
        f"👀 <b>Попался! На часах {local_hhmm}, а ты же уже подтвердил отбой.</b>\n"
        f"<b>-5 кармы</b> за срыв режима (теперь {score}/100). Доделаю запись ниже — и спать!"
    )


async def send_test_pulse_ping(
    db,
    bot: Bot,
    user: Dict[str, Any],
    now_utc: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Отправить живой тестовый пинг Пульса дня (включая ночной сон/тест кнопок)."""
    now = _naive_utc(now_utc)
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    user_id = int(user["id"])

    existing = await repo.get_pulse_checkin(db, user_id, local_date, "adhoc_test")
    meta = json.dumps({"title": "Пробный чек-ин Пульса"}, ensure_ascii=False)
    if existing:
        checkin = existing
    else:
        checkin = await repo.upsert_pulse_checkin(
            db,
            user_id,
            local_date,
            "adhoc_test",
            "tasks",
            now.isoformat(),
            (now + datetime.timedelta(minutes=20)).isoformat(),
            # Do not let a failed first test send become a scheduled test reminder.
            status="skipped",
            meta_json=meta,
        )

    state = await repo.get_or_create_karma_state(db, user_id)
    text, kb = build_pulse_message(checkin, state, level=1, is_snooze_reminder=False, user=user)
    old_card = _card_ref(checkin)
    sent_msg = await _safe_bot_send(bot, user, local_date, text, reply_markup=kb)
    if sent_msg is None:
        return {**dict(checkin), "sent": False}
    sent_chat_id = getattr(getattr(sent_msg, "chat", None), "id", None)
    sent_message_id = getattr(sent_msg, "message_id", None)
    changed, updated = await repo.transition_pulse_checkin(
        db,
        int(checkin["id"]),
        user_id=user_id,
        # adhoc_test is a separate test slot; it must never reopen a real pulse slot.
        allowed_statuses=("pending", "snoozed", "done", "skipped", "ignored"),
        expected_fields=_transition_snapshot(checkin),
        status="pending",
        ping_count=1,
        snooze_count=0,
        last_ping_at_utc=now.isoformat(),
        next_ping_at_utc=(now + datetime.timedelta(minutes=20)).isoformat(),
        resolved_at_utc=None,
        card_chat_id=sent_chat_id,
        card_message_id=sent_message_id,
    )
    if not changed:
        await _clear_saved_card(bot, (sent_chat_id, sent_message_id))
        return {**dict(updated or checkin), "sent": False}
    if old_card[0] is not None and old_card[1] is not None and old_card != (sent_chat_id, sent_message_id):
        await _clear_saved_card(bot, old_card)
    return {**dict(updated or checkin), "sent": True}


async def _safe_bot_send(
    bot: Bot,
    user: Dict[str, Any],
    local_date: str,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Optional[Any]:
    telegram_id = user.get("telegram_id")
    if not telegram_id:
        return None
    try:
        return await bot.send_message(chat_id=telegram_id, text=text, reply_markup=reply_markup)
    except Exception:
        return None


async def process_pulse_tick(
    db,
    bot: Bot,
    now_utc: Optional[datetime.datetime] = None,
) -> int:
    """Главный 60-секундный тик единого Пульса дня: создание слотов, дожим и эскалация."""
    now = _naive_utc(now_utc)
    users = await repo.list_users(db)
    user_map: Dict[int, Dict[str, Any]] = {}
    sent_or_resolved = 0

    for user_row in users:
        user = dict(user_row)
        uid = int(user["id"])
        user_map[uid] = user
        if not user.get("pulse_enabled", 1):
            continue
        if user.get("quiet_mode"):
            continue
        tz = user.get("timezone") or "UTC"
        local_date = local_date_str(now, tz)
        if user.get("pause_until") and local_date <= str(user["pause_until"] or ""):
            continue

        wellness_row = await repo.get_wellness(db, uid)
        wellness = dict(wellness_row) if wellness_row else None
        slots = compute_daily_slots(user, wellness, local_date)
        wake_slot = next((s for s in slots if s["slot_key"] == "wake"), None)

        # Сброс подтверждённого ночного сна при наступлении нового времени подъёма.
        confirmed_at = _parse_iso_utc(user.get("sleep_confirmed_at"))
        if confirmed_at and wake_slot:
            wake_utc = wake_slot["scheduled_at_utc"]
            if wake_utc > confirmed_at and now >= wake_utc:
                await repo.clear_user_sleep_confirmed(db, uid)
                user["sleep_confirmed_at"] = None
                user["sleep_confirmed_date"] = None

        sleep_locked = _confirmed_sleep_active(user, now)
        for slot in slots:
            if sleep_locked and slot["slot_key"] != "wake":
                continue
            sched_utc = slot["scheduled_at_utc"]
            # Окно создания 120 минут защищает от шквала просроченных утренних пингов при рестарте вечером.
            if sched_utc <= now <= sched_utc + datetime.timedelta(minutes=120):
                await repo.upsert_pulse_checkin(
                    db,
                    uid,
                    local_date,
                    slot["slot_key"],
                    slot["domain"],
                    sched_utc.isoformat(),
                    sched_utc.isoformat(),
                )

    due_checkins = await repo.list_due_pulse_checkins(db, now.isoformat())
    for checkin in due_checkins:
        uid = int(checkin["user_id"])
        user = user_map.get(uid)
        if not user:
            row = await repo.get_user(db, uid)
            if not row:
                continue
            user = dict(row)
            user_map[uid] = user

        if not user.get("pulse_enabled", 1) or user.get("quiet_mode"):
            continue
        tz = user.get("timezone") or "UTC"
        local_date = local_date_str(now, tz)
        if user.get("pause_until") and local_date <= str(user["pause_until"] or ""):
            continue

        slot_key = str(checkin.get("slot_key") or "")
        if is_student_mode(user) and slot_key in {"breakfast", "lunch", "dinner"}:
            changed, transitioned = await repo.transition_pulse_checkin(
                db, int(checkin["id"]), user_id=uid, allowed_statuses=("pending", "snoozed"),
                expected_fields=_transition_snapshot(checkin), status="skipped",
                resolved_at_utc=now.isoformat(), card_chat_id=None, card_message_id=None,
            )
            if changed:
                await _clear_saved_card(bot, _card_ref(checkin))
            continue
        if _confirmed_sleep_active(user, now) and slot_key != "wake":
            continue

        if slot_key in {"breakfast", "lunch", "afternoon", "dinner", "winddown"}:
            busy_ev = await calendar_service.get_active_busy_event(db, user, now)
            if busy_ev and busy_ev.get("end_utc"):
                end_utc = busy_ev["end_utc"]
                defer_utc = end_utc + datetime.timedelta(minutes=5)
                if defer_utc > now:
                    await repo.transition_pulse_checkin(
                        db, int(checkin["id"]), user_id=uid, allowed_statuses=("pending", "snoozed"),
                        expected_fields=_transition_snapshot(checkin), next_ping_at_utc=defer_utc.isoformat()
                    )
                    continue

        status = str(checkin.get("status") or "pending")
        ping_count = int(checkin.get("ping_count") or 0)
        if is_student_mode(user) and status == "pending" and ping_count >= 1:
            changed, transitioned = await repo.transition_pulse_checkin(
                db, int(checkin["id"]), user_id=uid, allowed_statuses=("pending",),
                expected_fields=_transition_snapshot(checkin), status="skipped",
                resolved_at_utc=now.isoformat(), card_chat_id=None, card_message_id=None,
            )
            if changed:
                await _clear_saved_card(bot, _card_ref(checkin))
            continue
        sick_active = karma_service.is_sick_freeze_active(user, now)
        cid = int(checkin["id"])

        # В заморозке «Болею» эскалация мягкая: не дожимаем 2-м и 3-м уровнями и не штрафуем за игнор.
        if status == "pending" and ping_count >= 1 and sick_active:
            changed, transitioned = await repo.transition_pulse_checkin(
                db, cid, user_id=uid, allowed_statuses=("pending",),
                expected_fields=_transition_snapshot(checkin), status="skipped",
                resolved_at_utc=now.isoformat(), card_chat_id=None, card_message_id=None,
            )
            if changed:
                await _clear_saved_card(bot, _card_ref(checkin))
            continue

        # Мягкий завтрак после подъёма не дожимаем 3-м сигналом и не штрафуем по карме.
        meta_chk = _load_meta(checkin)
        if status == "pending" and slot_key == "breakfast" and meta_chk.get("gentle") and ping_count >= 2:
            changed, transitioned = await repo.transition_pulse_checkin(
                db, cid, user_id=uid, allowed_statuses=("pending",),
                expected_fields=_transition_snapshot(checkin), status="skipped",
                resolved_at_utc=now.isoformat(), card_chat_id=None, card_message_id=None,
            )
            if changed:
                await _clear_saved_card(bot, _card_ref(checkin))
            continue

        if status == "pending" and ping_count >= 3:
            domain = str(checkin.get("domain") or "tasks")
            meta = _load_meta(checkin)
            title = meta.get("title") or SLOT_TITLES.get(slot_key, slot_key)
            old_card = _card_ref(checkin)
            changed, transitioned = await repo.transition_pulse_checkin(
                db, cid, user_id=uid, allowed_statuses=("pending",),
                expected_fields=_transition_snapshot(checkin), status="ignored",
                resolved_at_utc=now.isoformat(), card_chat_id=None, card_message_id=None,
            )
            if not changed:
                continue
            await _clear_saved_card(bot, old_card)
            karma_res = await karma_service.record_karma_event(
                db,
                user,
                domain=domain,
                event_type="checkin_ignored",
                delta=-10,
                reason=f"Игнор чек-ина «{title}»",
                now_utc=now,
            )
            msg = (
                f"❌ <b>Чек-ин «{html.escape(str(title))}» пропущен (игнор 3/3).</b>\n"
                f"Карма: <b>{karma_res['delta']}</b> → <b>{karma_res['score_after']}/100</b>. Я это запомнила."
            )
            await _safe_bot_send(bot, user, local_date, msg)
            sent_or_resolved += 1
            continue

        karma_state = await repo.get_or_create_karma_state(db, uid)
        slot_ctx = await _gather_slot_context(db, user, str(checkin.get("local_date") or local_date), slot_key)
        is_snooze = status == "snoozed"
        level = min(3, max(1, ping_count + 1))

        text, kb = build_pulse_message(
            checkin,
            karma_state,
            level=level,
            is_snooze_reminder=is_snooze,
            context=slot_ctx,
            user=user,
        )
        old_card = _card_ref(checkin)
        sent_msg = await _safe_bot_send(bot, user, local_date, text, reply_markup=kb)
        if sent_msg is None:
            continue

        new_ping_count = max(1, ping_count + 1)
        step_min = 15 if (slot_key in {"wake", "bedtime"} and new_ping_count == 1) else 20
        next_due = now + datetime.timedelta(minutes=step_min)
        sent_chat_id = getattr(getattr(sent_msg, "chat", None), "id", None)
        sent_message_id = getattr(sent_msg, "message_id", None)
        changed, transitioned = await repo.transition_pulse_checkin(
            db,
            cid,
            user_id=uid,
            allowed_statuses=("pending", "snoozed"),
            expected_fields=_transition_snapshot(checkin),
            status="pending",
            ping_count=new_ping_count,
            last_ping_at_utc=now.isoformat(),
            next_ping_at_utc=next_due.isoformat(),
            card_chat_id=sent_chat_id,
            card_message_id=sent_message_id,
        )
        if not changed:
            await _clear_saved_card(bot, (sent_chat_id, sent_message_id))
            continue
        if old_card[1] is not None and old_card[1] != sent_message_id:
            await _clear_saved_card(bot, old_card)
        sent_or_resolved += 1

    return sent_or_resolved
