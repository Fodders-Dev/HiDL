"""Сервис обработки входящих голосовых и свободных текстовых сообщений."""

from __future__ import annotations

import datetime
import html
import json
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from llm_client import LLMClient, client as default_llm_client
from services import (
    advice_service,
    calendar_service,
    context_builder,
    fitness_nutrition,
    karma_service,
    kitchen_ai,
    pulse_service,
    stt,
)
from services.inbox_parser import (
    InboxParseResult,
    ParsedInboxItem,
    analyze_inbox_text,
    compute_one_time_last_sent,
    resolve_temporal_expression,
)
from utils.pantry import format_quantity
from utils.rows import rows_to_dicts
from utils.time import format_date_display, format_time_local, local_date_str, parse_hhmm
from utils.profile import is_student_mode

KIND_LABELS = {
    "task": ("✅", "Задача"),
    "reminder": ("🔔", "Напоминание"),
    "calendar_event": ("📅", "Календарь"),
    "shopping": ("🛒", "Покупки"),
    "shopping_remove": ("🗑", "Убрано из покупок"),
    "pantry_add": ("🧊", "В холодильник"),
    "pantry_remove": ("🍽", "Убрано из холодильника"),
    "pulse_log": ("💓", "Режим и карма"),
    "rollover_tasks": ("➡️", "Перенос хвостов"),
    "shift_bedtime": ("🌙", "Сдвиг отбоя"),
    "shift_wake": ("⏰", "Подъём"),
    "task_delete": ("🗑", "Удалена задача"),
    "reminder_delete": ("🗑", "Удалено напоминание"),
    "meal_log": ("🔥", "КБЖУ"),
    "weight_log": ("⚖", "Весы"),
    "workout_log": ("🏋️", "Тренировка"),
    "pending_meal": ("🍽", "Уточнить еду"),
    "expense": ("💸", "Трата"),
    "diary": ("📓", "Дневник"),
    "idea": ("💡", "Идея"),
    "done": ("✔", "Сделано"),
    "pending_clarification": ("❓", "Уточнить"),
}


@dataclass
class InboxProcessOutcome:
    inbox_id: int
    status: str
    is_duplicate: bool
    source: str
    raw_text: str = ""
    actions: list[dict[str, Any]] = field(default_factory=list)
    clarifications: list[dict[str, Any]] = field(default_factory=list)
    llm_error: Optional[str] = None
    stt_error: Optional[str] = None
    used_llm: bool = False
    reply_text: str = ""
    reply_markup: Optional[InlineKeyboardMarkup] = None


def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


def _load_payload(action: dict[str, Any]) -> dict[str, Any]:
    raw = action.get("payload_json")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _format_amount(amount: Any) -> str:
    try:
        val = float(amount)
    except (TypeError, ValueError):
        return str(amount or 0)
    return str(int(val)) if val.is_integer() else f"{val:g}"


def _format_action_line(action: dict[str, Any]) -> str:
    kind = action.get("item_kind") or "diary"
    icon, label = KIND_LABELS.get(kind, ("📝", "Запись"))
    title = _esc(action.get("title") or "")
    payload = _load_payload(action)

    if kind == "reminder":
        d = payload.get("resolved_date")
        t = payload.get("resolved_time")
        when = f"{format_date_display(d)} {t}".strip() if d else (t or "")
        return f"{icon} {label}: {title}" + (f" — {when}" if when else "")
    if kind == "calendar_event":
        d = payload.get("resolved_date")
        st_t = payload.get("start_time") or ""
        en_t = payload.get("end_time") or ""
        span = f"{st_t}–{en_t}" if st_t and en_t else st_t
        when = f"{format_date_display(d)} {span}".strip() if d else span
        g_note = " (Google Calendar)" if payload.get("google_synced") else ""
        return f"{icon} {label}: {title}" + (f" — {when}" if when else "") + g_note
    if kind == "shopping":
        qty = payload.get("quantity", 1)
        unit = payload.get("unit", "шт")
        return f"{icon} {label}: {title} ({_esc(format_quantity(qty, unit))})"
    if kind == "expense":
        amt_str = _format_amount(payload.get("amount", 0))
        cat = _esc(payload.get("category") or action.get("title") or "другое")
        return f"💸 Трата: {amt_str} ₽ ({cat})"
    if kind == "shopping_remove":
        if action.get("status") == "skipped" or payload.get("not_found"):
            return f"ℹ️ Не нашла «{title}» в покупках"
        return f"🗑 Убрала из покупок: {title}"
    if kind == "pantry_add":
        qty = payload.get("quantity", 1)
        unit = payload.get("unit", "шт")
        synced = " (и вычеркнула из покупок)" if payload.get("synced_shopping_item") else ""
        return f"🧊 В холодильник: {title} ({_esc(format_quantity(qty, unit))}){synced}"
    if kind == "pantry_remove":
        if action.get("status") == "skipped" or payload.get("not_found"):
            return f"ℹ️ Не нашла «{title}» в холодильнике"
        return f"🍽 Убрала из холодильника: {title}"
    if kind == "pulse_log":
        delta = int(payload.get("delta") or 0)
        score_after = payload.get("score_after")
        already_resolved = bool(payload.get("already_resolved"))
        prev_status = str(payload.get("prev_status") or "")
        late_wake_recorded = bool(payload.get("late_wake_recorded"))
        local_time = str(payload.get("local_time") or "").strip()

        if already_resolved and prev_status in {"missed", "ignored"}:
            if late_wake_recorded:
                t_part = f" в {_esc(local_time)}" if local_time else ""
                return (
                    f"💓 Режим: «{title}» уже был пропущен раньше (карма без изменений), "
                    f"но я отметила, что ты встал{t_part}, и сняла ночной режим."
                )
            return f"💓 Режим: чек-ин «{title}» уже был пропущен раньше — карма не изменилась."
        if already_resolved and prev_status == "done":
            return f"💓 Режим: «{title}» уже отмечен сегодня — повторно карму не начисляла."

        time_prefix = f"{_esc(local_time)}, " if local_time else ""
        if delta > 0 and score_after is not None:
            return f"💓 Режим: {title} — отмечено в «Сон и карма» ({time_prefix}+{delta} кармы → {score_after}/100)"
        time_note = f" ({_esc(local_time)})" if local_time else ""
        return f"💓 Режим: {title} — отмечено в «Сон и карма»{time_note}"
    if kind == "meal_log":
        kcal = int(round(float(payload.get("kcal") or 0)))
        p_g = float(payload.get("protein_g") or 0)
        f_g = float(payload.get("fat_g") or 0)
        c_g = float(payload.get("carbs_g") or 0)
        src_map = {
            "openfoodfacts": " · Open Food Facts",
            "web_search": " · из меню в сети",
            "my_label": " · твоя этикетка",
            "text_label": " · с этикетки",
            "vision_label": " · с этикетки",
        }
        src_note = src_map.get(str(payload.get("lookup_source") or payload.get("source") or ""), "")
        rem_note = f"\n   {payload['remaining_line']}" if payload.get("remaining_line") else ""
        return (
            f"🔥 <b>{title}</b> — <b>{kcal} ккал</b> "
            f"(Б {p_g:g}/Ж {f_g:g}/У {c_g:g}){src_note}{rem_note}"
        )
    if kind == "weight_log":
        w = payload.get("weight")
        bits = [f"<b>{float(w):.1f} кг</b>"] if w else [title]
        if payload.get("body_fat_pct") is not None:
            bits.append(f"жир {float(payload['body_fat_pct']):.1f}%")
        if payload.get("muscle_mass_kg") is not None:
            bits.append(f"мышцы {float(payload['muscle_mass_kg']):.1f} кг")
        trend_note = f"\n   {_esc(payload['trend_note'])}" if payload.get("trend_note") else ""
        return f"⚖ Замер тела: {' · '.join(bits)}{trend_note}"
    if kind == "workout_log":
        dur = int(payload.get("duration_min") or 15)
        kb = int(payload.get("kcal_burned") or 0)
        delta = int(payload.get("karma_delta") or 0)
        k_txt = f", +{delta} кармы" if delta > 0 else ""
        return f"🏋️ Тренировка: {title} ({dur} мин · ~{kb} ккал{k_txt})"
    if kind in {"rollover_tasks", "shift_bedtime"}:
        if action.get("status") == "awaiting_confirm":
            return f"⏳ Подтвердить: {title}"
        return f"{icon} Выполнено: {title}"
    if kind == "shift_wake":
        if action.get("status") == "skipped":
            return f"ℹ️ {title}"
        return f"⏰ {title}"
    if kind in {"task_delete", "reminder_delete"}:
        if action.get("status") == "skipped" or payload.get("not_found"):
            return f"ℹ️ Не нашла «{title}»"
        return f"🗑 {label}: {title}"
    if kind == "done" and payload.get("completed_task_id"):
        delta = int(payload.get("karma_delta") or 0)
        score_after = payload.get("score_after")
        if delta > 0 and score_after is not None:
            return f"{icon} {label}: {title} (задача закрыта, +{delta} кармы → {score_after}/100)"
        return f"{icon} {label}: {title} (задача закрыта)"
    if kind == "task":
        p_date = str(payload.get("plan_date") or "")
        c_date = str(payload.get("created_date") or "")
        if p_date and c_date and p_date != c_date:
            return f"{icon} {label} (на {format_date_display(p_date)[:5]}): {title}"
    return f"{icon} {label}: {title}"


def build_inbox_keyboard(
    inbox_id: int,
    actions: list[dict[str, Any]],
    *,
    llm_error: Optional[str] = None,
    intent: str = "capture",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    clarifying = [a for a in actions if a.get("status") == "clarifying"]
    if clarifying:
        first = clarifying[0]
        aid = first["id"]
        payload = _load_payload(first)
        if first.get("item_kind") == "pending_meal":
            pid = int(payload.get("pending_meal_id") or first.get("target_id") or 0)
            est_k = int(round(float(payload.get("kcal") or 0)))
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"✅ ~{est_k} ккал",
                        callback_data=f"kbju:pcommit:{pid}:1",
                    ),
                    InlineKeyboardButton(
                        text="🍽 Полпорции",
                        callback_data=f"kbju:pcommit:{pid}:0.5",
                    ),
                ]
            )
            rows.append([InlineKeyboardButton(text="❌ Не писать", callback_data=f"kbju:pdrop:{pid}")])
        else:
            candidates = payload.get("candidate_times") or []
            resolved_date = payload.get("resolved_date")
            if candidates:
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"🕒 {t}",
                            callback_data=f"inbox:clarify_time:{aid}:{t.replace(':', '_')}",
                        )
                        for t in candidates[:3]
                    ]
                )
            elif not resolved_date:
                rows.append(
                    [
                        InlineKeyboardButton(
                            text="Сегодня 19:00",
                            callback_data=f"inbox:clarify_preset:{aid}:today_19",
                        ),
                        InlineKeyboardButton(
                            text="Завтра 12:00",
                            callback_data=f"inbox:clarify_preset:{aid}:tomorrow_12",
                        ),
                    ]
                )
            rows.append(
                [
                    InlineKeyboardButton(text="🕒 Своё время", callback_data=f"inbox:clarify_custom:{aid}"),
                    InlineKeyboardButton(text="✅ Как задачу", callback_data=f"inbox:clarify_task:{aid}"),
                    InlineKeyboardButton(text="✖️ Убрать", callback_data=f"inbox:clarify_drop:{aid}"),
                ]
            )

    awaiting = [a for a in actions if a.get("status") == "awaiting_confirm"]
    for act in awaiting:
        aid = int(act["id"])
        payload = _load_payload(act)
        short = str(payload.get("short_label") or act.get("title") or "действие")[:24]
        rows.append(
            [
                InlineKeyboardButton(text=f"✅ Подтвердить: {short}", callback_data=f"inbox:confirm:{aid}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"inbox:reject:{aid}"),
            ]
        )

    if intent == "support":
        rows.append(
            [
                InlineKeyboardButton(text="🆘 Помощь сейчас", callback_data="help:menu"),
                InlineKeyboardButton(text="😴 Режим сна", callback_data="sleep:menu:main"),
            ]
        )

    active_or_clar = [a for a in actions if a.get("status") in {"active", "clarifying"}]
    ctrl_row: list[InlineKeyboardButton] = []
    if len(active_or_clar) == 1 and active_or_clar[0].get("item_kind") == "pulse_log" and not awaiting:
        ctrl_row.append(InlineKeyboardButton(text="🌙 Сон и карма", callback_data="pulse:hub"))
        ctrl_row.append(
            InlineKeyboardButton(
                text="📓 В дневник",
                callback_data=f"inbox:reclass:{int(active_or_clar[0]['id'])}:diary",
            )
        )
    elif len(active_or_clar) == 1 and active_or_clar[0].get("item_kind") == "meal_log" and not awaiting:
        meal_tid = int(active_or_clar[0].get("target_id") or 0)
        if meal_tid:
            ctrl_row.append(InlineKeyboardButton(text="✏️ Уточнить", callback_data=f"kbju:refine:{meal_tid}"))
        ctrl_row.append(InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"))
        ctrl_row.append(InlineKeyboardButton(text="↩️ Отменить", callback_data=f"inbox:undo:{inbox_id}"))
    elif active_or_clar and not (len(active_or_clar) == 1 and active_or_clar[0].get("item_kind") == "pending_meal"):
        ctrl_row.append(InlineKeyboardButton(text="✏️ Исправить", callback_data=f"inbox:edit:{inbox_id}"))
        ctrl_row.append(InlineKeyboardButton(text="↩️ Отменить всё", callback_data=f"inbox:undo:{inbox_id}"))
    if llm_error:
        ctrl_row.append(InlineKeyboardButton(text="🔁 Переразобрать", callback_data=f"inbox:retry_llm:{inbox_id}"))
    if ctrl_row:
        rows.append(ctrl_row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def _voice_prefix_line(inbox_row: dict[str, Any]) -> str:
    if inbox_row.get("source") == "voice" and inbox_row.get("raw_text"):
        quote = inbox_row["raw_text"].strip()
        if len(quote) > 160:
            quote = quote[:157] + "..."
        return f"🎙 Расшифровала: «{_esc(quote)}»"
    return ""


def render_inbox_summary(
    inbox_row: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    llm_error: Optional[str] = None,
    intent: str = "capture",
    support_reply: str = "",
) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    lines: list[str] = []
    voice_line = _voice_prefix_line(inbox_row)
    if voice_line:
        lines.append(voice_line)

    active = [a for a in actions if a.get("status") == "active"]
    clarifying = [a for a in actions if a.get("status") == "clarifying"]
    awaiting = [a for a in actions if a.get("status") == "awaiting_confirm"]
    skipped = [a for a in actions if a.get("status") == "skipped"]

    if intent == "support":
        warm = _esc(support_reply.strip()) if support_reply.strip() else (
            "Я рядом. Если тревожно или тяжело уснуть, не обязательно решать всё прямо сейчас — "
            "сделай спокойный выдох и дай себе немного бережности."
        )
        lines.append(warm)
        if active:
            if len(active) == 1 and active[0].get("item_kind") == "diary":
                lines.append("Бережно сохранила мысль в 📓 Дневник.")
            else:
                lines.append("Бережно сохранила:")
                for act in active:
                    lines.append(_format_action_line(act))
    elif active or skipped:
        if (
            len(active) == 1
            and active[0].get("item_kind") in {"pulse_log", "meal_log", "weight_log", "workout_log"}
            and not skipped
            and not clarifying
            and not awaiting
        ):
            lines.append(_format_action_line(active[0]))
        else:
            if active:
                lines.append("Записала:")
                for act in active:
                    lines.append(_format_action_line(act))
            for act in skipped:
                lines.append(_format_action_line(act))
    elif not clarifying and not awaiting:
        lines.append("Запись сохранена, активных пунктов сейчас нет.")

    if llm_error:
        lines.append("⚠️ Модель не ответила — разобрала по простым правилам, исходный текст сохранила.")

    if clarifying:
        first_c = clarifying[0]
        first_payload = _load_payload(first_c)
        if first_c.get("item_kind") == "pending_meal":
            title = _esc(first_payload.get("title") or first_c.get("title") or "Еда")
            est_k = int(round(float(first_payload.get("kcal") or 0)))
            est_p = float(first_payload.get("protein_g") or 0)
            est_f = float(first_payload.get("fat_g") or 0)
            est_c = float(first_payload.get("carbs_g") or 0)
            question = _esc(first_payload.get("question") or "Уточни порцию или состав?")
            lines.append(
                f"❓ <b>{title}</b> (пока ~{est_k} ккал · Б{est_p:g}/Ж{est_f:g}/У{est_c:g})\n"
                f"{question}\n"
                "<i>Ответь текстом, фото этикетки или жми кнопку — если промолчишь, через 10 мин запишу примерно сама.</i>"
            )
        else:
            title = _esc(first_payload.get("title") or first_c.get("title") or "напоминание")
            question = _esc(first_payload.get("question") or "Уточни дату и время.")
            lines.append(f"❓ Напоминание «{title}»: {question}")

    if awaiting:
        for act in awaiting:
            lines.append(_format_action_line(act))

    kb = build_inbox_keyboard(
        inbox_row["id"],
        actions,
        llm_error=llm_error,
        intent=intent,
    )
    return "\n".join(lines), (kb if kb.inline_keyboard else None)


def _compute_end_hhmm(start_hhmm: str, duration_minutes: int) -> str:
    st_t = parse_hhmm(start_hhmm) or datetime.time(9, 0)
    st_min = st_t.hour * 60 + st_t.minute
    dur = max(15, min(720, int(duration_minutes or 60)))
    en_min = min(23 * 60 + 59, st_min + dur)
    if en_min <= st_min:
        en_min = min(23 * 60 + 59, st_min + 15)
    return f"{en_min // 60:02d}:{en_min % 60:02d}"


async def commit_pending_meal(
    db,
    user: dict[str, Any],
    pending: dict[str, Any],
    *,
    mult: float = 1.0,
    status: str = "committed",
    refined_meal: Optional[dict[str, Any]] = None,
    now_utc: Optional[datetime.datetime] = None,
) -> dict[str, Any]:
    """Зафиксировать pending-черновик еды (целиком, полпорции или после уточнения) в meal_logs."""
    now = now_utc or datetime.datetime.utcnow()
    uid = int(user["id"])
    tz = user.get("timezone") or "UTC"
    local_date = str(pending.get("local_date") or local_date_str(now, tz))
    est = refined_meal or pending.get("estimated") or {}
    m_mult = max(0.25, min(4.0, float(mult or 1.0)))

    title = str(est.get("title") or pending.get("title") or "Приём пищи").strip()
    if m_mult != 1.0 and refined_meal is None and f"{m_mult:g}" not in title:
        title = f"{title} ({m_mult:g} порц.)"
    kcal = int(round(max(0.0, float(est.get("kcal") or 0) * m_mult)))
    protein_g = round(max(0.0, float(est.get("protein_g") or 0) * m_mult), 1)
    fat_g = round(max(0.0, float(est.get("fat_g") or 0) * m_mult), 1)
    carbs_g = round(max(0.0, float(est.get("carbs_g") or 0) * m_mult), 1)
    slot = str(est.get("meal_slot") or pending.get("meal_slot") or "snack")
    base_src = str(est.get("lookup_source") or pending.get("source") or "voice")
    src = f"{base_src}_approx" if status == "auto_committed" and "approx" not in base_src else base_src

    meal_id = await repo.add_meal_log(
        db,
        uid,
        local_date,
        title,
        meal_slot=slot,
        servings=m_mult,
        kcal=kcal,
        protein_g=protein_g,
        fat_g=fat_g,
        carbs_g=carbs_g,
        source=src,
    )
    await repo.resolve_pending_meal(db, int(pending["id"]), status=status, resolved_meal_id=meal_id)

    # Закрываем возможный clarifying-action от исходного сообщения
    if pending.get("inbox_message_id"):
        old_acts = await repo.list_inbox_actions(
            db, uid, int(pending["inbox_message_id"]), include_undone=False
        )
        for act in old_acts:
            if act.get("item_kind") == "pending_meal" and act.get("status") == "clarifying":
                await repo.update_inbox_action(
                    db,
                    uid,
                    int(act["id"]),
                    item_kind="meal_log",
                    target_table="meal_logs",
                    target_id=meal_id,
                    title=title,
                    status="active",
                )
        await _sync_parent_inbox_status(db, uid, int(pending["inbox_message_id"]))

    advice = await fitness_nutrition.get_daily_nutrition_advice(db, user, now_utc=now)
    return {
        "meal_id": meal_id,
        "title": title,
        "kcal": kcal,
        "protein_g": protein_g,
        "fat_g": fat_g,
        "carbs_g": carbs_g,
        "lookup_source": est.get("lookup_source") or "",
        "advice": advice,
    }


async def _persist_meal_item(
    db,
    user: dict[str, Any],
    inbox_row: dict[str, Any],
    *,
    source_quote: str,
    title_hint: str,
    local_date: str,
    now_utc: datetime.datetime,
    llm_client_instance: Optional[LLMClient] = None,
    allow_clarification: bool = True,
    is_single_item: bool = False,
) -> int:
    uid = int(user["id"])
    inbox_id = int(inbox_row["id"])
    source = str(inbox_row.get("source") or "text")
    chat_id = int(inbox_row.get("chat_id") or 0)
    sq = (source_quote or "").strip()
    th = (title_hint or "").strip()
    full_raw = str(inbox_row.get("raw_text") or "").strip()
    if is_single_item and full_raw and len(full_raw) > len(sq):
        raw_for_parse = full_raw
    elif sq and th and th.casefold() not in sq.casefold() and sq.casefold() not in th.casefold():
        raw_for_parse = f"{th}. {sq}"
    else:
        raw_for_parse = (sq or th or full_raw).strip()

    parsed_meal = await fitness_nutrition.parse_meal_input(
        raw_text=raw_for_parse,
        llm_client_instance=llm_client_instance,
        db=db,
        user_id=uid,
        allow_clarification=allow_clarification,
        allow_web_lookup=True,
        title_hint=th,
    )
    if not parsed_meal.get("ok"):
        parsed_meal = {
            "ok": True,
            "title": title_hint or raw_for_parse[:60] or "Приём пищи",
            "meal_slot": "snack",
            "kcal": 250,
            "protein_g": 10.0,
            "fat_g": 9.0,
            "carbs_g": 30.0,
            "estimated": True,
            "needs_clarification": False,
        }

    meal_title = str(parsed_meal.get("title") or title_hint or "Приём пищи").strip()
    meal_slot = str(parsed_meal.get("meal_slot") or "snack")
    if allow_clarification and parsed_meal.get("needs_clarification") and parsed_meal.get("clarification_question"):
        pending_id = await repo.create_pending_meal_clarification(
            db,
            uid,
            chat_id,
            local_date,
            raw_for_parse,
            meal_title,
            meal_slot,
            parsed_meal,
            str(parsed_meal["clarification_question"]),
            source=source,
            inbox_message_id=inbox_id,
            auto_commit_minutes=10,
            now_utc=now_utc,
        )
        return await repo.create_inbox_action(
            db,
            user_id=uid,
            inbox_message_id=inbox_id,
            item_kind="pending_meal",
            target_table="pending_meal_clarifications",
            target_id=pending_id,
            title=meal_title,
            source_quote=source_quote,
            payload={
                "pending_meal_id": pending_id,
                "title": meal_title,
                "kcal": parsed_meal.get("kcal", 0),
                "protein_g": parsed_meal.get("protein_g", 0),
                "fat_g": parsed_meal.get("fat_g", 0),
                "carbs_g": parsed_meal.get("carbs_g", 0),
                "question": parsed_meal.get("clarification_question", ""),
            },
            status="clarifying",
        )

    eff_src = str(parsed_meal.get("lookup_source") or source)
    meal_id = await repo.add_meal_log(
        db,
        uid,
        local_date,
        meal_title,
        meal_slot=meal_slot,
        kcal=float(parsed_meal.get("kcal") or 0),
        protein_g=float(parsed_meal.get("protein_g") or 0),
        fat_g=float(parsed_meal.get("fat_g") or 0),
        carbs_g=float(parsed_meal.get("carbs_g") or 0),
        source=eff_src,
    )
    advice = await fitness_nutrition.get_daily_nutrition_advice(db, user, now_utc=now_utc)
    rem_line = ""
    if not advice["targets"].get("mindful_mode"):
        rem = advice["remaining"]
        rem_line = f"Остаток: {rem['kcal']} ккал · Б +{rem['protein_g']} г"
    return await repo.create_inbox_action(
        db,
        user_id=uid,
        inbox_message_id=inbox_id,
        item_kind="meal_log",
        target_table="meal_logs",
        target_id=meal_id,
        title=meal_title,
        source_quote=source_quote,
        payload={
            "kcal": parsed_meal.get("kcal", 0),
            "protein_g": parsed_meal.get("protein_g", 0),
            "fat_g": parsed_meal.get("fat_g", 0),
            "carbs_g": parsed_meal.get("carbs_g", 0),
            "lookup_source": parsed_meal.get("lookup_source") or "",
            "source": eff_src,
            "remaining_line": rem_line,
        },
        status="active",
    )


_TASK_DATE_HINT_RE = re.compile(
    r"(?:завтра|послезавтра|сегодня|понедель|вторник|сред[ау]|четверг|пятниц|суббот|воскрес|\b\d{1,2}\.\d{1,2}(?:\.\d{4})?\b|\b\d{4}-\d{2}-\d{2}\b)",
    re.IGNORECASE,
)


def _resolve_task_plan_date(
    item: ParsedInboxItem,
    local_date: str,
    now_utc: datetime.datetime,
    tz_str: str,
) -> str:
    d_hint = str(getattr(item, "date_hint", None) or "").strip()
    quote = str(getattr(item, "source_quote", None) or "").strip()
    if not d_hint and not _TASK_DATE_HINT_RE.search(quote):
        return local_date
    sched = resolve_temporal_expression(
        d_hint or None,
        None,
        quote or d_hint,
        now_utc,
        tz_str,
    )
    if getattr(sched, "resolved_date", None) and sched.resolved_date >= local_date:
        return str(sched.resolved_date)
    return local_date


async def _persist_parsed_items(
    db,
    user: dict[str, Any],
    inbox_row: dict[str, Any],
    parsed: InboxParseResult,
    local_date: str,
    now_utc: Optional[datetime.datetime] = None,
    llm_client_instance: Optional[LLMClient] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []
    clarifications: list[dict[str, Any]] = []
    source = inbox_row.get("source") or "text"
    inbox_id = int(inbox_row["id"])
    now = now_utc or datetime.datetime.utcnow()
    is_single_item = len(parsed.items) == 1

    for item in parsed.items:
        kind = item.kind
        if is_student_mode(user) and kind in {
            "shopping",
            "shopping_remove",
            "pantry_add",
            "pantry_remove",
            "pantry_use",
            "meal_done",
            "expense",
            "meal_log",
            "weight_log",
            "workout_log",
        }:
            kind = "task" if kind == "shopping" else "diary"
        if kind == "task":
            tz_user = str(user.get("timezone") or "UTC")
            task_date = _resolve_task_plan_date(item, local_date, now, tz_user)
            item_id = await repo.add_day_plan_item(
                db,
                user["id"],
                task_date,
                item.title,
                category="inbox",
                is_important=False,
            )
            action_id = await repo.create_inbox_action(
                db,
                user_id=user["id"],
                inbox_message_id=inbox_id,
                item_kind="task",
                target_table="day_plan_items",
                target_id=item_id,
                title=item.title,
                source_quote=item.source_quote,
                payload={
                    "plan_date": task_date,
                    "created_date": local_date,
                    "model_note": item.model_note,
                    "tags": item.tags,
                },
                status="active",
            )
        elif kind == "shopping":
            shop_id = await repo.create_shopping_item(
                db,
                user["id"],
                item.title,
                qty=item.quantity,
                unit=item.unit,
                category="food",
                scope="household",
            )
            action_id = await repo.create_inbox_action(
                db,
                user_id=user["id"],
                inbox_message_id=inbox_id,
                item_kind="shopping",
                target_table="shopping_list",
                target_id=shop_id,
                title=item.title,
                source_quote=item.source_quote,
                payload={"quantity": item.quantity, "unit": item.unit, "model_note": item.model_note},
                status="active",
            )
        elif kind == "expense":
            amt = float(item.amount or 0.0)
            cat = (item.category or item.title or "другое").strip().lower()
            if amt > 0:
                exp_id = await repo.add_expense(db, user["id"], amt, cat)
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="expense",
                    target_table="expenses",
                    target_id=exp_id,
                    title=item.title or cat.capitalize(),
                    source_quote=item.source_quote,
                    payload={"amount": amt, "category": cat, "model_note": item.model_note},
                    status="active",
                )
            else:
                entry_id = await repo.create_journal_entry(
                    db,
                    user_id=user["id"],
                    entry_type="diary",
                    entry_date=local_date,
                    source=source,
                    user_text=item.source_quote,
                    title=item.title,
                    model_note=item.model_note,
                    tags=item.tags,
                    inbox_message_id=inbox_id,
                )
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="diary",
                    target_table="journal_entries",
                    target_id=entry_id,
                    title=item.title,
                    source_quote=item.source_quote,
                    payload={"model_note": item.model_note, "tags": item.tags},
                    status="active",
                )
        elif kind == "shopping_remove":
            removed = await repo.find_and_delete_shopping_item_by_name(
                db, user["id"], item.title or item.source_quote, scope="household"
            )
            if removed:
                removed_name = str(removed.get("item_name") or item.title)
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="shopping_remove",
                    target_table="shopping_list_removed",
                    target_id=int(removed["id"]) if removed.get("id") else None,
                    title=removed_name,
                    source_quote=item.source_quote,
                    payload={"removed_item": dict(removed), "model_note": item.model_note},
                    status="active",
                )
            else:
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="shopping_remove",
                    target_table="shopping_list_removed",
                    target_id=None,
                    title=item.title,
                    source_quote=item.source_quote,
                    payload={"not_found": True, "model_note": item.model_note},
                    status="skipped",
                )
        elif kind == "pantry_add":
            qty = float(item.quantity or 1.0)
            unit = str(item.unit or "шт")
            pantry_id = await repo.create_pantry_item(
                db,
                user["id"],
                item.title,
                amount=qty,
                unit=unit,
                expires_at=None,
                category="продукты",
            )
            synced_shop = await repo.find_and_delete_shopping_item_by_name(
                db, user["id"], item.title, scope="household"
            )
            action_id = await repo.create_inbox_action(
                db,
                user_id=user["id"],
                inbox_message_id=inbox_id,
                item_kind="pantry_add",
                target_table="pantry_items",
                target_id=pantry_id,
                title=item.title,
                source_quote=item.source_quote,
                payload={
                    "quantity": qty,
                    "unit": unit,
                    "synced_shopping_item": dict(synced_shop) if synced_shop else None,
                    "model_note": item.model_note,
                },
                status="active",
            )
        elif kind == "pantry_remove":
            removed_p = await repo.find_and_delete_pantry_item_by_name(
                db, user["id"], item.title or item.source_quote
            )
            if removed_p:
                removed_name = str(removed_p.get("name") or item.title)
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="pantry_remove",
                    target_table="pantry_removed",
                    target_id=int(removed_p["id"]) if removed_p.get("id") else None,
                    title=removed_name,
                    source_quote=item.source_quote,
                    payload={"removed_item": dict(removed_p), "model_note": item.model_note},
                    status="active",
                )
            elif re.match(
                r"(?iu)^(?:я\s+|мы\s+|уже\s+|только\s+что\s+)?(?:съел|доел|выпил|поел|позавтракал|пообедал|поужинал|перекусил)\b",
                (item.source_quote or "").strip(),
            ):
                action_id = await _persist_meal_item(
                    db,
                    user,
                    inbox_row,
                    source_quote=item.source_quote,
                    title_hint=item.title,
                    local_date=local_date,
                    now_utc=now,
                    llm_client_instance=llm_client_instance,
                    is_single_item=is_single_item,
                )
            else:
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="pantry_remove",
                    target_table="pantry_removed",
                    target_id=None,
                    title=item.title,
                    source_quote=item.source_quote,
                    payload={"not_found": True, "model_note": item.model_note},
                    status="skipped",
                )
        elif kind == "meal_log":
            action_id = await _persist_meal_item(
                db,
                user,
                inbox_row,
                source_quote=item.source_quote,
                title_hint=item.title,
                local_date=local_date,
                now_utc=now,
                llm_client_instance=llm_client_instance,
                is_single_item=is_single_item,
            )
        elif kind == "weight_log":
            scale_res = await fitness_nutrition.parse_smart_scale_input(
                raw_text=item.source_quote or item.title,
                llm_client_instance=llm_client_instance,
            )
            if scale_res.get("ok"):
                wid = await repo.add_weight(
                    db,
                    int(user["id"]),
                    float(scale_res["weight"]),
                    body_fat_pct=scale_res.get("body_fat_pct"),
                    muscle_mass_kg=scale_res.get("muscle_mass_kg"),
                    water_pct=scale_res.get("water_pct"),
                    visceral_fat=scale_res.get("visceral_fat"),
                    bmi=scale_res.get("bmi"),
                    source=str(scale_res.get("source") or source),
                )
                trend = await repo.body_metrics_trend(db, int(user["id"]), days=30)
                t_fb = fitness_nutrition.format_body_trend_feedback(
                    trend, str(user.get("weight_goal") or "loss")
                )
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="weight_log",
                    target_table="weights",
                    target_id=wid,
                    title=f"{float(scale_res['weight']):.1f} кг",
                    source_quote=item.source_quote,
                    payload={
                        "weight": scale_res["weight"],
                        "body_fat_pct": scale_res.get("body_fat_pct"),
                        "muscle_mass_kg": scale_res.get("muscle_mass_kg"),
                        "trend_note": t_fb,
                    },
                    status="active",
                )
            else:
                entry_id = await repo.create_journal_entry(
                    db,
                    user_id=user["id"],
                    entry_type="diary",
                    entry_date=local_date,
                    source=source,
                    user_text=item.source_quote,
                    title=item.title,
                    model_note=item.model_note,
                    tags=item.tags,
                    inbox_message_id=inbox_id,
                )
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="diary",
                    target_table="journal_entries",
                    target_id=entry_id,
                    title=item.title,
                    source_quote=item.source_quote,
                    payload={"model_note": item.model_note},
                    status="active",
                )
        elif kind == "workout_log":
            raw_w = item.source_quote or item.title
            m_dur = re.search(r"(\d+)\s*(?:мин\w*|м)\b", raw_w.lower())
            m_hr = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:час\w*|ч)\b", raw_w.lower())
            if m_dur:
                dur_min = max(2, min(180, int(m_dur.group(1))))
            elif m_hr:
                dur_min = max(5, min(180, int(round(float(m_hr.group(1).replace(",", ".")) * 60))))
            else:
                dur_min = 15
            w_res = await fitness_nutrition.complete_workout(
                db,
                user,
                {
                    "title": item.title or "Тренировка",
                    "duration_min": dur_min,
                    "focus_area": "full_body",
                    "equipment_used": ", ".join(repo.get_user_equipment(user)) or "свой вес",
                    "intensity": "moderate" if dur_min >= 12 else "easy",
                    "kcal_burned": max(20, int(round(dur_min * 6.0))),
                },
                is_lazy_start=(dur_min <= 6),
                now_utc=now,
            )
            action_id = await repo.create_inbox_action(
                db,
                user_id=user["id"],
                inbox_message_id=inbox_id,
                item_kind="workout_log",
                target_table="workout_logs",
                target_id=int(w_res["log_id"]),
                title=w_res["title"],
                source_quote=item.source_quote,
                payload={
                    "duration_min": w_res["duration_min"],
                    "kcal_burned": w_res["kcal_burned"],
                    "karma_delta": w_res["karma_delta"],
                    "points_delta": 3 if dur_min <= 6 else 5,
                    "local_date": local_date,
                },
                status="active",
            )
        elif kind == "pulse_log":
            slot = (item.category or "lunch").strip().lower()
            tz = user.get("timezone") or "UTC"
            local_hhmm = format_time_local(now, tz)
            if slot == "bedtime":
                p_res = await pulse_service.confirm_bedtime(db, user, now_utc=now)
                cid = int((p_res.get("checkin") or {}).get("id") or 0) or None
                karma = p_res.get("karma") or {}
            elif slot in {"water", "teeth"}:
                slot_host = "winddown" if slot == "teeth" and now.hour >= 15 else "wake"
                dom_host = "tasks" if slot_host == "winddown" else "sleep"
                chk = await repo.get_pulse_checkin(db, user["id"], local_date, slot_host)
                if chk is None:
                    chk = await repo.upsert_pulse_checkin(
                        db,
                        user["id"],
                        local_date,
                        slot_host,
                        dom_host,
                        now.isoformat(),
                        now.isoformat(),
                    )
                cid = int(chk["id"])
                p_res = await pulse_service.record_hygiene_tick(db, user, cid, slot, now_utc=now)
                karma = {"delta": p_res.get("delta", 0), "score_after": p_res.get("score_after")}
            else:
                slot_key = slot if slot in {"wake", "lunch", "dinner"} else "lunch"
                dom = "sleep" if slot_key == "wake" else "food"
                chk = await repo.get_pulse_checkin(db, user["id"], local_date, slot_key)
                if chk is None:
                    chk = await repo.upsert_pulse_checkin(
                        db,
                        user["id"],
                        local_date,
                        slot_key,
                        dom,
                        now.isoformat(),
                        now.isoformat(),
                    )
                cid = int(chk["id"])
                p_res = await pulse_service.complete_checkin(db, user, cid, now_utc=now)
                karma = p_res.get("karma") or {}
            already_resolved = bool(p_res.get("already_resolved") or p_res.get("already_marked"))
            prev_status = str(p_res.get("status") or ("done" if p_res.get("already_marked") else ""))
            late_wake_recorded = bool(p_res.get("late_wake_recorded"))
            action_id = await repo.create_inbox_action(
                db,
                user_id=user["id"],
                inbox_message_id=inbox_id,
                item_kind="pulse_log",
                target_table="pulse_checkins",
                target_id=cid,
                title=item.title,
                source_quote=item.source_quote,
                payload={
                    "slot": slot,
                    "delta": int(karma.get("delta") or 0),
                    "score_after": karma.get("score_after"),
                    "already_resolved": already_resolved,
                    "prev_status": prev_status,
                    "late_wake_recorded": late_wake_recorded,
                    "local_time": local_hhmm,
                    "model_note": item.model_note,
                },
                status="active",
            )
        elif kind == "rollover_tasks":
            plan_items = rows_to_dicts(await repo.list_day_plan_items(db, user["id"], local_date))
            unfinished = [p for p in plan_items if not p.get("done")]
            to_date = (datetime.date.fromisoformat(local_date) + datetime.timedelta(days=1)).isoformat()
            action_id = await repo.create_inbox_action(
                db,
                user_id=user["id"],
                inbox_message_id=inbox_id,
                item_kind="rollover_tasks",
                target_table="confirm_pending",
                target_id=None,
                title=f"Перенести хвосты на завтра ({len(unfinished)} шт.)",
                source_quote=item.source_quote,
                payload={
                    "kind": "rollover_tasks",
                    "from_date": local_date,
                    "to_date": to_date,
                    "pending_count": len(unfinished),
                    "short_label": f"перенести {len(unfinished)}",
                },
                status="awaiting_confirm",
            )
        elif kind == "shift_bedtime":
            target_hhmm = item.schedule.resolved_time if item.schedule else None
            _, _, is_weekend = pulse_service.get_effective_schedule(user, local_date)
            if target_hhmm:
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="shift_bedtime",
                    target_table="confirm_pending",
                    target_id=None,
                    title=f"Сдвинуть отбой на {target_hhmm}",
                    source_quote=item.source_quote,
                    payload={
                        "kind": "shift_bedtime",
                        "target_hhmm": target_hhmm,
                        "is_weekend": is_weekend,
                        "local_date": local_date,
                        "short_label": f"отбой {target_hhmm}",
                    },
                    status="awaiting_confirm",
                )
            else:
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="shift_bedtime",
                    target_table="confirm_pending",
                    target_id=None,
                    title="Уточнить время отбоя (нужен формат HH:MM)",
                    source_quote=item.source_quote,
                    payload={"not_found": True},
                    status="skipped",
                )
        elif kind == "shift_wake":
            target_hhmm = item.schedule.resolved_time if item.schedule else None
            if target_hhmm:
                prev_wake = str(user.get("wake_up_time") or "08:00")
                await repo.update_user_wake(db, user["id"], target_hhmm)
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="shift_wake",
                    target_table="user_wake_updated",
                    target_id=user["id"],
                    title=f"Подъём обновлён на {target_hhmm}",
                    source_quote=item.source_quote,
                    payload={"previous_wake": prev_wake, "new_wake": target_hhmm, "model_note": item.model_note},
                    status="active",
                )
            elif item.category == "no_alarm":
                target_d = (
                    (datetime.date.fromisoformat(local_date) + datetime.timedelta(days=1)).isoformat()
                    if "завтра" in (item.source_quote or "").lower()
                    else local_date
                )
                prev_day_off = str(user.get("day_off_date") or "")
                await repo.set_user_day_off(db, user["id"], target_d)
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="shift_wake",
                    target_table="user_day_off",
                    target_id=user["id"],
                    title=f"Без будильника на {format_date_display(target_d)}",
                    source_quote=item.source_quote,
                    payload={"day_off_date": target_d, "previous_day_off": prev_day_off, "model_note": item.model_note},
                    status="active",
                )
            else:
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="shift_wake",
                    target_table="confirm_pending",
                    target_id=None,
                    title="Режим без будильника на завтра (уточни время подъёма HH:MM, чтобы изменить расписание)",
                    source_quote=item.source_quote,
                    payload={"no_alarm": False, "model_note": item.model_note},
                    status="skipped",
                )
        elif kind == "task_delete":
            q_title = item.title or item.source_quote
            matched = await repo.find_open_day_plan_item_by_title(db, user["id"], q_title, local_date)
            if matched is None:
                tom_date = (datetime.date.fromisoformat(local_date) + datetime.timedelta(days=1)).isoformat()
                matched = await repo.find_open_day_plan_item_by_title(db, user["id"], q_title, tom_date)
                matched_date = tom_date if matched else local_date
            else:
                matched_date = str(matched.get("plan_date") or local_date)
            if matched:
                await repo.delete_day_plan_item(db, user["id"], int(matched["id"]))
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="task_delete",
                    target_table="day_plan_removed",
                    target_id=int(matched["id"]),
                    title=str(matched.get("title") or item.title),
                    source_quote=item.source_quote,
                    payload={"removed_item": dict(matched), "plan_date": matched_date, "model_note": item.model_note},
                    status="active",
                )
            else:
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="task_delete",
                    target_table="day_plan_removed",
                    target_id=None,
                    title=q_title,
                    source_quote=item.source_quote,
                    payload={"not_found": True, "model_note": item.model_note},
                    status="skipped",
                )
        elif kind == "reminder_delete":
            q_title = item.title or item.source_quote
            matched_rem = await repo.find_custom_reminder_by_title(db, user["id"], q_title)
            if matched_rem:
                await repo.delete_custom_reminder(db, user["id"], int(matched_rem["id"]))
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="reminder_delete",
                    target_table="custom_reminder_removed",
                    target_id=int(matched_rem["id"]),
                    title=str(matched_rem.get("title") or item.title),
                    source_quote=item.source_quote,
                    payload={"removed_item": dict(matched_rem), "model_note": item.model_note},
                    status="active",
                )
            else:
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="reminder_delete",
                    target_table="custom_reminder_removed",
                    target_id=None,
                    title=q_title,
                    source_quote=item.source_quote,
                    payload={"not_found": True, "model_note": item.model_note},
                    status="skipped",
                )
        elif kind == "reminder":
            sched = item.schedule
            if sched and not sched.is_ambiguous and sched.resolved_date and sched.resolved_time:
                rem_id = await repo.create_custom_reminder(
                    db,
                    user_id=user["id"],
                    title=item.title,
                    reminder_time=sched.resolved_time,
                    frequency_days=9999,
                    target_weekday=None,
                )
                last_sent = compute_one_time_last_sent(sched.resolved_date, local_date, 9999)
                if last_sent:
                    await repo.set_custom_reminder_sent(db, rem_id, last_sent)
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="reminder",
                    target_table="custom_reminders",
                    target_id=rem_id,
                    title=item.title,
                    source_quote=item.source_quote,
                    payload={
                        "resolved_date": sched.resolved_date,
                        "resolved_time": sched.resolved_time,
                        "model_note": item.model_note,
                    },
                    status="active",
                )
            else:
                payload = {
                    "title": item.title,
                    "resolved_date": sched.resolved_date if sched else None,
                    "ambiguity_reason": sched.ambiguity_reason if sched else "vague_when",
                    "candidate_times": sched.candidate_times if sched else [],
                    "question": sched.question if sched else "На какую дату и время поставить напоминание?",
                    "model_note": item.model_note,
                }
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="pending_clarification",
                    target_table="pending",
                    target_id=None,
                    title=item.title,
                    source_quote=item.source_quote,
                    payload=payload,
                    status="clarifying",
                )
        elif kind == "calendar_event":
            sched = item.schedule
            if sched and not sched.is_ambiguous and sched.resolved_date and sched.resolved_time:
                st_hhmm = sched.resolved_time
                en_hhmm = _compute_end_hhmm(st_hhmm, item.duration_minutes)
                cal_res = await calendar_service.create_unified_event(
                    db,
                    user,
                    title=item.title,
                    event_date=sched.resolved_date,
                    start_time=st_hhmm,
                    end_time=en_hhmm,
                    location=item.location,
                    category="inbox",
                    push_to_google=True,
                    now_utc=now,
                )
                ev_obj = cal_res.get("event") or {}
                ev_id = int(ev_obj["id"]) if ev_obj.get("id") else None
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="calendar_event",
                    target_table="calendar_event_created",
                    target_id=ev_id,
                    title=item.title,
                    source_quote=item.source_quote,
                    payload={
                        "storage": cal_res.get("storage") or "local",
                        "google_synced": bool(cal_res.get("google_synced")),
                        "google_event_id": str(cal_res.get("google_event_id") or ""),
                        "external_uid": str(ev_obj.get("external_uid") or ""),
                        "resolved_date": sched.resolved_date,
                        "start_time": st_hhmm,
                        "end_time": en_hhmm,
                        "location": item.location,
                        "model_note": item.model_note,
                    },
                    status="active",
                )
            else:
                payload = {
                    "title": item.title,
                    "is_calendar_event": True,
                    "duration_minutes": int(item.duration_minutes or 60),
                    "location": item.location,
                    "resolved_date": sched.resolved_date if sched else None,
                    "ambiguity_reason": sched.ambiguity_reason if sched else "vague_when",
                    "candidate_times": sched.candidate_times if sched else [],
                    "question": sched.question if sched else "На какую дату и время добавить событие в календарь?",
                    "model_note": item.model_note,
                }
                action_id = await repo.create_inbox_action(
                    db,
                    user_id=user["id"],
                    inbox_message_id=inbox_id,
                    item_kind="pending_clarification",
                    target_table="pending",
                    target_id=None,
                    title=item.title,
                    source_quote=item.source_quote,
                    payload=payload,
                    status="clarifying",
                )
        else:
            entry_type = kind if kind in {"diary", "idea", "done"} else "diary"
            action_payload: dict[str, Any] = {
                "model_note": item.model_note,
                "tags": item.tags,
            }
            action_title = item.title
            if entry_type == "done":
                matched_task = await repo.find_open_day_plan_item_by_title(
                    db, user["id"], item.title or item.source_quote, local_date
                )
                if matched_task is None and item.source_quote and item.source_quote != item.title:
                    matched_task = await repo.find_open_day_plan_item_by_title(
                        db, user["id"], item.source_quote, local_date
                    )
                if matched_task:
                    task_id = int(matched_task["id"])
                    task_title = str(matched_task.get("title") or item.title)
                    await repo.mark_day_plan_item_done(db, task_id, done=True)
                    karma_res = await karma_service.record_karma_event(
                        db,
                        user,
                        domain="tasks",
                        event_type="task_done",
                        delta=2,
                        reason=f"Выполнена задача «{task_title}»",
                        now_utc=now,
                    )
                    action_payload["completed_task_id"] = task_id
                    action_payload["completed_task_title"] = task_title
                    action_payload["karma_delta"] = int(karma_res.get("delta") or 0)
                    action_payload["score_after"] = karma_res.get("score_after")
                    if not repo._normalize_task_tokens(item.title)[1]:
                        action_title = task_title

            entry_id = await repo.create_journal_entry(
                db,
                user_id=user["id"],
                entry_type=entry_type,
                entry_date=local_date,
                source=source,
                user_text=item.source_quote,
                title=action_title,
                model_note=item.model_note,
                tags=item.tags,
                inbox_message_id=inbox_id,
            )
            action_id = await repo.create_inbox_action(
                db,
                user_id=user["id"],
                inbox_message_id=inbox_id,
                item_kind=entry_type,
                target_table="journal_entries",
                target_id=entry_id,
                title=action_title,
                source_quote=item.source_quote,
                payload=action_payload,
                status="active",
            )

        action_row = await repo.get_inbox_action(db, user["id"], action_id)
        if action_row:
            actions.append(action_row)
            if action_row.get("status") == "clarifying":
                clarifications.append(action_row)

    return actions, clarifications


_DIALOG_TTL_SEC = 20 * 60
_DIALOG_MAX_TURNS = 6
_DIALOG_HISTORY: dict[int, list[dict[str, Any]]] = {}


def clear_dialog_history(user_id: Optional[int] = None) -> None:
    """Очистить краткосрочную историю диалога (для тестов или сброса контекста)."""
    if user_id is None:
        _DIALOG_HISTORY.clear()
    else:
        _DIALOG_HISTORY.pop(int(user_id), None)


def _normalize_utc(dt: Optional[datetime.datetime]) -> datetime.datetime:
    cur = dt or datetime.datetime.now(datetime.timezone.utc)
    if cur.tzinfo is None:
        return cur.replace(tzinfo=datetime.timezone.utc)
    return cur.astimezone(datetime.timezone.utc)


def _get_recent_context(
    user_id: int,
    now_utc: Optional[datetime.datetime] = None,
) -> list[dict[str, Any]]:
    now = _normalize_utc(now_utc)
    turns = _DIALOG_HISTORY.get(int(user_id), [])
    fresh: list[dict[str, Any]] = []
    for t in turns:
        ts = t.get("ts")
        if isinstance(ts, datetime.datetime):
            ts_norm = _normalize_utc(ts)
            if (now - ts_norm).total_seconds() <= _DIALOG_TTL_SEC:
                fresh.append(t)
    fresh = fresh[-_DIALOG_MAX_TURNS:]
    if fresh:
        _DIALOG_HISTORY[int(user_id)] = fresh
    else:
        _DIALOG_HISTORY.pop(int(user_id), None)
    return list(fresh)


def _append_dialog_turn(
    user_id: int,
    role: str,
    text: str,
    now_utc: Optional[datetime.datetime] = None,
    meta: Optional[dict[str, Any]] = None,
) -> None:
    now = _normalize_utc(now_utc)
    history = _get_recent_context(user_id, now)
    history.append(
        {
            "role": role,
            "text": (text or "").strip(),
            "ts": now,
            "meta": dict(meta or {}),
        }
    )
    _DIALOG_HISTORY[int(user_id)] = history[-_DIALOG_MAX_TURNS:]


def _latest_food_meta(recent_context: list[dict[str, Any]]) -> dict[str, Any]:
    for turn in reversed(recent_context):
        if turn.get("role") == "assistant":
            meta = turn.get("meta") if isinstance(turn.get("meta"), dict) else {}
            if meta.get("query_type") == "food":
                return meta
    return {}


def _extract_excluded_terms(text: str) -> list[str]:
    terms: list[str] = []
    for m in re.finditer(r"(?iu)\bбез\s+([а-яёa-z-]+)", text or ""):
        raw = m.group(1).strip().lower()
        if len(raw) >= 3:
            terms.append(repo._ru_stem(raw) or raw)
    return terms


def _recipe_matches_exclusion(recipe: dict[str, Any], excluded_stems: list[str]) -> bool:
    if not excluded_stems:
        return False
    parts = [str(recipe.get("title") or ""), str(recipe.get("desc") or "")]
    for ing in recipe.get("ingredients") or []:
        if isinstance(ing, dict):
            parts.append(str(ing.get("name") or ""))
    haystack = " ".join(parts).lower()
    return any(stem in haystack for stem in excluded_stems)


async def _render_food_suggestions_with_meta(
    db,
    user: dict[str, Any],
    *,
    raw_text: str = "",
    more: bool = False,
    query_term: str = "",
    llm_client_instance: Optional[LLMClient] = None,
    now_utc: Optional[datetime.datetime] = None,
    record_history: bool = True,
) -> tuple[str, InlineKeyboardMarkup, dict[str, Any]]:
    """Подобрать 3 неповторяющихся рецепта под профиль питания (с LLM-идеями, когда каталог исчерпан)."""
    from handlers import kitchen

    now = _normalize_utc(now_utc)
    uid = int(user["id"])
    recent = _get_recent_context(uid, now)
    prev_meta = _latest_food_meta(recent)

    q_lower = f"{raw_text} {query_term}".lower()
    is_more = bool(
        more
        or query_term.strip().lower() == "more"
        or "ещ" in q_lower
        or "друг" in q_lower
        or "дальше" in q_lower
        or "без " in q_lower
        or "быстр" in q_lower
    )

    already_ids: list[str] = list(prev_meta.get("shown_recipe_ids") or []) if is_more else []
    already_titles: list[str] = list(prev_meta.get("shown_titles") or []) if is_more else []

    profile = await kitchen._get_meal_profile(db, uid)
    tag_pref = (
        "breakfast"
        if "завтрак" in q_lower
        else "lunch"
        if "обед" in q_lower
        else "dinner"
        if "ужин" in q_lower
        else (prev_meta.get("tag_pref") if is_more else None)
    )
    excluded = _extract_excluded_terms(f"{raw_text} {query_term}")
    fast_only = any(w in q_lower for w in ("побыстрее", "быстрее", "до 15", "до 20"))
    max_fast_min = 20 if ("побыстрее" in q_lower or "быстрее" in q_lower) else 30
    energy = (
        "low"
        if (fast_only or any(w in q_lower for w in ("нет сил", "лень", "устал")))
        else ("shop" if "докупить" in q_lower else "normal")
    )

    sug_res = await kitchen_ai.suggest_meals_now(
        db,
        user,
        energy=energy,
        meal_slot=tag_pref,
        exclude_ids=already_ids,
        excluded_terms=excluded,
        limit=(50 if fast_only else 3),
        now_utc=now,
    )
    candidates_info = list(sug_res["suggestions"])
    if fast_only:
        candidates_info = [
            s for s in candidates_info if int(s["recipe"].get("time_minutes") or 999) <= max_fast_min
        ][:3]
    else:
        candidates_info = candidates_info[:3]

    total_allowed = sum(
        1
        for r in kitchen.load_recipes()
        if kitchen._recipe_allowed_for_profile(r, profile)
        and (not excluded or not _recipe_matches_exclusion(r, excluded))
    )

    btn_rows: list[list[InlineKeyboardButton]] = []
    new_meta: dict[str, Any] = {
        "query_type": "food",
        "tag_pref": tag_pref,
        "shown_recipe_ids": list(already_ids),
        "shown_titles": list(already_titles),
    }

    if candidates_info:
        header = (
            "🍳 <b>Ещё подходящие варианты:</b>"
            if is_more
            else (
                f"🍳 <b>Что приготовить ({_esc(sug_res['meal_slot_label'])}, дома {int(sug_res['pantry_count'])} поз.):</b>"
                if sug_res.get("pantry_count")
                else "🍳 <b>Быстрые варианты (до 30 мин):</b>"
            )
        )
        lines = [header]
        for idx, item in enumerate(candidates_info, 1):
            r = item["recipe"]
            t = int(r.get("time_minutes") or 0)
            desc = (r.get("desc") or "").strip()
            title = str(r.get("title") or "Рецепт")
            rid = str(r.get("id") or "")
            have = item.get("have") or []
            missing = item.get("missing") or []

            line = f"{idx}. <b>{_esc(title)}</b> · {t} мин"
            if desc:
                line += f" — {_esc(desc)}"
            lines.append(line)

            if not missing:
                lines.append("   ✅ <i>Всё нужное есть дома</i>")
            elif have:
                have_str = ", ".join(_esc(h["name"]) for h in have[:2])
                miss_str = ", ".join(_esc(m["name"]) for m in missing[:3])
                if len(missing) > 3:
                    miss_str += f" и ещё {len(missing) - 3}"
                lines.append(f"   ✅ дома: {have_str} · 🛒 докупить: {miss_str}")
            elif sug_res.get("pantry_count"):
                miss_str = ", ".join(_esc(m["name"]) for m in missing[:3])
                lines.append(f"   🛒 докупить: {miss_str}")

            short_title = title[:20] + ("…" if len(title) > 20 else "")
            action_btn = (
                InlineKeyboardButton(
                    text=f"🛒 Докупить ({len(missing)})",
                    callback_data=f"kitchen:add_miss:{rid}:2",
                )
                if missing
                else InlineKeyboardButton(
                    text="✅ Приготовил",
                    callback_data=f"kitchen:cook_fast:{rid}:2",
                )
            )
            btn_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🍳 {short_title} · {t}м",
                        callback_data=f"kitchen:cook_view:{rid}:2:all:0",
                    ),
                    action_btn,
                ]
            )
            if rid and rid not in new_meta["shown_recipe_ids"]:
                new_meta["shown_recipe_ids"].append(rid)
            if title and title not in new_meta["shown_titles"]:
                new_meta["shown_titles"].append(title)
        text = "\n".join(lines)
        btn_rows.append(
            [
                InlineKeyboardButton(text="🔄 Ещё варианты", callback_data="kitchen:more_food"),
                InlineKeyboardButton(text="📅 Меню на 3 дня", callback_data="kitchen:meal_plan"),
            ]
        )
    else:
        client = llm_client_instance or default_llm_client
        if client.is_configured():
            diet_desc = (
                "строго веганские (без мяса, рыбы, яиц и молочных продуктов)"
                if profile == "vegan"
                else "вегетарианские (без мяса и рыбы)"
                if profile == "vegetarian"
                else "обычные домашние"
            )
            meal_word = {"breakfast": "завтрак", "lunch": "обед", "dinner": "ужин"}.get(
                tag_pref or "", "приём пищи"
            )
            avoid_list = ", ".join(already_titles[-12:])
            excl_note = f" Без: {', '.join(excluded)}." if excluded else ""
            sys_prompt = (
                "Ты кулинарный помощник HiDL. Предложи ровно 3 простых домашних блюда по-русски "
                "в формате короткого нумерованного списка (1., 2., 3.) на 2 порции, время готовки до 30 минут, "
                "по 1 строке на блюдо (Название · время — короткая суть). Без лишнего вступления и мед. советов."
            )
            usr_prompt = (
                f"Подбери 3 идеи на {meal_word}. Питание: {diet_desc}.{excl_note}\n"
                + (f"Не повторяй эти блюда: {avoid_list}." if avoid_list else "")
            )
            res = await client.complete_text(sys_prompt, usr_prompt, temperature=0.5, max_tokens=320)
            if res.ok and res.text.strip():
                raw_ideas = res.text.strip()
                for line in raw_ideas.splitlines():
                    cleaned = re.sub(r"^\s*\d+[\).]\s*", "", line).strip()
                    name_part = re.split(r"[·—–-]", cleaned, maxsplit=1)[0].strip(" *•_")
                    if name_part and name_part not in new_meta["shown_titles"]:
                        new_meta["shown_titles"].append(name_part)
                text = f"🍳 <b>Новые идеи:</b>\n{_esc(raw_ideas)}"
                btn_rows.append(
                    [
                        InlineKeyboardButton(text="🔄 Ещё варианты", callback_data="kitchen:more_food"),
                        InlineKeyboardButton(text="📅 Меню на 3 дня", callback_data="kitchen:meal_plan"),
                    ]
                )
            else:
                new_meta["shown_recipe_ids"] = []
                text = "🍳 Варианты из книги рецептов закончились — можешь начать круг заново или открыть всю книгу."
                btn_rows.append(
                    [
                        InlineKeyboardButton(text="🔄 Показать сначала", callback_data="kitchen:more_food"),
                        InlineKeyboardButton(text="📅 Меню на 3 дня", callback_data="kitchen:meal_plan"),
                    ]
                )
        else:
            new_meta["shown_recipe_ids"] = []
            text = (
                f"🍳 Все подходящие рецепты из книги ({total_allowed}) уже показаны — "
                "можешь открыть всю книгу или начать круг заново."
            )
            btn_rows.append(
                [
                    InlineKeyboardButton(text="🔄 Показать сначала", callback_data="kitchen:more_food"),
                    InlineKeyboardButton(text="📅 Меню на 3 дня", callback_data="kitchen:meal_plan"),
                ]
            )

    btn_rows.append(
        [
            InlineKeyboardButton(text="📖 Все рецепты", callback_data="kitchen:recipes"),
            InlineKeyboardButton(text="⬅️ Меню", callback_data="main:menu"),
        ]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=btn_rows)

    if record_history:
        _append_dialog_turn(uid, "user", raw_text or ("Ещё варианты блюд" if is_more else "Что приготовить?"), now)
        _append_dialog_turn(uid, "assistant", text, now, meta=new_meta)

    return text, kb, new_meta


async def render_food_suggestions(
    db,
    user: dict[str, Any],
    *,
    raw_text: str = "",
    more: bool = False,
    query_term: str = "",
    llm_client_instance: Optional[LLMClient] = None,
    now_utc: Optional[datetime.datetime] = None,
    record_history: bool = True,
) -> tuple[str, InlineKeyboardMarkup]:
    """Подобрать 3 неповторяющихся рецепта под профиль питания."""
    text, kb, _ = await _render_food_suggestions_with_meta(
        db,
        user,
        raw_text=raw_text,
        more=more,
        query_term=query_term,
        llm_client_instance=llm_client_instance,
        now_utc=now_utc,
        record_history=record_history,
    )
    return text, kb


def _latest_advice_ids(recent_context: Optional[list[dict[str, Any]]]) -> set[str]:
    out: set[str] = set()
    for turn in recent_context or []:
        if turn.get("role") == "assistant":
            meta = turn.get("meta") if isinstance(turn.get("meta"), dict) else {}
            for cid in meta.get("shown_advice_ids") or []:
                if cid:
                    out.add(str(cid))
    return out


_KBJU_STATUS_ONLY_RE = re.compile(
    r"^\s*(?:покажи\s+)?(?:мой\s+|мои\s+)?(?:кбжу|калории|норм[уа]\s+кбжу)(?:\s+на\s+сегодня)?\s*[?!.]*\s*$"
    r"|^\s*сколько\s+(?:у\s+меня\s+)?(?:калорий|ккал|белка|жиров|углеводов|кбжу)\s+осталось(?:\s+на\s+сегодня)?\s*[?!.]*\s*$"
    r"|^\s*(?:что|сколько)\s+(?:мне\s+)?(?:можно\s+)?добрать\s+по\s+(?:белку|кбжу|калориям)\s*[?!.]*\s*$"
)

_KBJU_COACHING_KEYWORDS = (
    "толсте",
    "похуде",
    "надо есть",
    "нужно есть",
    "нельзя",
    "можно ли",
    "если будет",
    "если перее",
    "почему",
    "зачем",
    "объясни",
    "как работа",
    "что значит",
    "дефицит",
    "профицит",
    "поддержан",
    "базовый обмен",
    "минимум",
    "максимум",
    "или это",
    "сжиг",
)


def _is_kbju_coaching_question(raw_text: str) -> bool:
    """Отличить содержательный вопрос тренеру про калории/вес от короткого запроса карточки КБЖУ."""
    clean = str(raw_text or "").strip().lower().replace("ё", "е")
    if not clean:
        return False
    if _KBJU_STATUS_ONLY_RE.match(clean):
        return False
    if any(kw in clean for kw in _KBJU_COACHING_KEYWORDS):
        return True
    if "?" in clean and len(clean.split()) >= 6 and "сколько" not in clean:
        return True
    return False


async def _build_query_response(
    db,
    user: dict[str, Any],
    parsed: InboxParseResult,
    raw_text: str,
    *,
    reply_context: str = "",
    llm_client_instance: Optional[LLMClient] = None,
    now_utc: Optional[datetime.datetime] = None,
    recent_context: Optional[list[dict[str, Any]]] = None,
) -> tuple[str, Optional[InlineKeyboardMarkup], dict[str, Any]]:
    qt = parsed.query_type
    if is_student_mode(user) and qt in {"shopping", "pantry", "food"}:
        return (
            "В режиме «Ученик» бытовые списки скрыты. Если это идея или дело для себя, напиши её обычным сообщением — я сохраню в дневник или план.",
            None,
            {"query_type": "hidden_household"},
        )
    if qt == "today":
        from utils.today import render_today

        txt, kb = await render_today(db, user)
        return txt, kb, {"query_type": "today"}

    if qt == "calendar":
        now = now_utc or datetime.datetime.utcnow()
        tz = user.get("timezone") or "UTC"
        d_today = local_date_str(now, tz)
        d_tomorrow = (datetime.date.fromisoformat(d_today) + datetime.timedelta(days=1)).isoformat()
        events = await calendar_service.get_unified_events_range(db, int(user["id"]), d_today, d_tomorrow)
        today_evs = [e for e in events if e.get("event_date") == d_today]
        tom_evs = [e for e in events if e.get("event_date") == d_tomorrow]
        lines = [f"🗓 <b>Календарь на {format_date_display(d_today)} и завтра</b>"]
        if not events:
            lines.append("\nНа ближайшие два дня событий не запланировано.")
        if today_evs:
            lines.append("\n<b>Сегодня:</b>")
            for ev in today_evs[:8]:
                when = "Весь день" if ev.get("is_all_day") else f"{ev.get('start_time')}–{ev.get('end_time')}"
                lines.append(f"• {when} — {_esc(ev.get('title'))}")
        if tom_evs:
            lines.append("\n<b>Завтра:</b>")
            for ev in tom_evs[:8]:
                when = "Весь день" if ev.get("is_all_day") else f"{ev.get('start_time')}–{ev.get('end_time')}"
                lines.append(f"• {when} — {_esc(ev.get('title'))}")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🗓 Расписание", callback_data="sched:home"),
                    InlineKeyboardButton(text="🔄 Синхр.", callback_data="sched:sync"),
                ],
                [InlineKeyboardButton(text="⬅️ Меню", callback_data="main:menu")],
            ]
        )
        return "\n".join(lines), kb, {"query_type": "calendar"}

    if qt == "karma":
        now = now_utc or datetime.datetime.utcnow()
        tz = user.get("timezone") or "UTC"
        local_date = local_date_str(now, tz)
        state = await repo.get_or_create_karma_state(db, int(user["id"]))
        events = await repo.list_karma_events(db, int(user["id"]), limit=5)
        checkins = await repo.list_pulse_checkins_for_date(db, int(user["id"]), local_date)
        eff = pulse_service.get_effective_schedule(user, local_date)
        txt = karma_service.format_karma_screen(
            user, state, events, checkins, now_utc=now, effective_schedule=eff
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🌙 Открыть «Сон и карма»", callback_data="pulse:hub"),
                    InlineKeyboardButton(text="🧠 Портрет привычек", callback_data="pulse:portrait"),
                ],
                [InlineKeyboardButton(text="💡 Совет сейчас", callback_data="advice:more")],
            ]
        )
        return txt, kb, {"query_type": "karma"}

    if qt == "pantry":
        items = rows_to_dicts(await repo.list_pantry_items(db, user["id"]))
        if not items:
            text = (
                "❄️ <b>Холодильник</b>\nПока пусто. Можешь просто написать или надиктовать: "
                "«купил тофу, гречку и огурцы»."
            )
        else:
            lines = [f"❄️ <b>В холодильнике сейчас ({len(items)}):</b>"]
            for idx, it in enumerate(items[:20], 1):
                qty_s = format_quantity(it.get("amount"), it.get("unit"))
                exp = f" · до {format_date_display(it['expires_at'])}" if it.get("expires_at") else ""
                lines.append(f"{idx}. {_esc(it.get('name'))} ({_esc(qty_s)}){exp}")
            text = "\n".join(lines)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🍳 Что приготовить?", callback_data="kitchen:quick_pick"),
                    InlineKeyboardButton(text="❄️ Открыть холодильник", callback_data="kitchen:fridge"),
                ],
                [InlineKeyboardButton(text="⬅️ Меню", callback_data="main:menu")],
            ]
        )
        return text, kb, {"query_type": "pantry"}

    if qt == "advice":
        is_more = parsed.query_term.strip().lower() == "more"
        prev_ids = _latest_advice_ids(recent_context) if is_more else set()
        reply_html, meta = await advice_service.build_situational_advice_reply(
            db,
            user,
            user_text="" if is_more else raw_text,
            reply_context="" if is_more else reply_context,
            recent_context=None if is_more else recent_context,
            llm_client_instance=llm_client_instance,
            exclude_ids=prev_ids,
            now_utc=now_utc,
        )
        card = meta.get("card") or {}
        if card.get("id"):
            prev_ids.add(str(card["id"]))
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="💡 Ещё совет", callback_data="advice:more"),
                    InlineKeyboardButton(text="📅 Сегодня", callback_data="today:menu"),
                ],
                [InlineKeyboardButton(text="🌙 Сон и карма", callback_data="pulse:hub")],
            ]
        )
        return reply_html, kb, {"query_type": "advice", "shown_advice_ids": sorted(prev_ids)}

    if qt == "shopping":
        from handlers import kitchen

        items = rows_to_dicts(await repo.list_shopping_items(db, user["id"], scope="household"))
        if not items:
            text = "🛒 <b>Список покупок</b>\nПока пусто. Можешь просто написать или надиктовать, что купить."
        else:
            bought_cnt = sum(1 for i in items if i.get("is_bought"))
            lines = [f"🛒 <b>Список покупок</b> (всего: {len(items)}, отмечено: {bought_cnt}):"]
            for idx, it in enumerate(items[:15], 1):
                mark = "✅" if it.get("is_bought") else "⭕️"
                lines.append(
                    f"{idx}. {mark} {_esc(it['item_name'])} ({_esc(format_quantity(it.get('quantity'), it.get('unit')))})"
                )
            text = "\n".join(lines)
        return text, kitchen.shopping_list_keyboard(items, scope="household"), {"query_type": "shopping"}

    if qt == "food":
        txt, kb, meta = await _render_food_suggestions_with_meta(
            db,
            user,
            raw_text=raw_text,
            more=(parsed.query_term.strip().lower() == "more"),
            query_term=parsed.query_term,
            llm_client_instance=llm_client_instance,
            now_utc=now_utc,
            record_history=False,
        )
        return txt, kb, meta

    if qt == "diary":
        term = (parsed.query_term or "").strip()
        notes = await repo.search_user_notes(db, user["id"], term, limit=8)
        header = (
            f"🔍 <b>Нашла по запросу «{_esc(term)}»:</b>"
            if term
            else "📓 <b>Последние записи дневника:</b>"
        )
        lines = [header]
        if not notes:
            lines.append("Пока ничего похожего не нашла.")
        else:
            for idx, item in enumerate(notes, 1):
                d = format_date_display(item.get("entry_date") or "")
                src_icon = "🎙" if item.get("source") == "voice" else "💬"
                txt = (item.get("user_text") or item.get("title") or "").strip()
                if len(txt) > 160:
                    txt = txt[:157] + "..."
                lines.append(f"{idx}. {d} {src_icon} {_esc(txt)}")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📓 Открыть дневник", callback_data="diary:tab:diary")],
                [InlineKeyboardButton(text="⬅️ Меню", callback_data="main:menu")],
            ]
        )
        return "\n".join(lines), kb, {"query_type": "diary", "query_term": term}

    if qt == "kbju":
        if is_student_mode(user):
            return (
                "В режиме «Ученик» подсчёт калорий отключён — ориентируйся на спокойный ритм и самочувствие.",
                None,
                {"query_type": "kbju"},
            )
        if _is_kbju_coaching_question(raw_text):
            reply_html, meta = await advice_service.build_situational_advice_reply(
                db,
                user,
                user_text=raw_text,
                reply_context=reply_context,
                recent_context=recent_context,
                llm_client_instance=llm_client_instance,
                now_utc=now_utc,
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
                        InlineKeyboardButton(text="⚖ Тело и весы", callback_data="fit:body"),
                    ]
                ]
            )
            return reply_html, kb, {"query_type": "advice", "routed_from": "kbju"}
        from handlers import nutrition

        txt, kb = await nutrition.render_kbju_card(db, user, now_utc=now_utc)
        return txt, kb, {"query_type": "kbju"}

    if qt == "meal_check":
        if is_student_mode(user):
            return (
                "В режиме «Ученик» мы не считаем калории. Ешь спокойно по голоду и настроению — всё ок!",
                None,
                {"query_type": "meal_check"},
            )
        now = now_utc or datetime.datetime.utcnow()
        tz = user.get("timezone") or "UTC"
        local_date = local_date_str(now, tz)
        cand = (parsed.query_term or raw_text).strip()
        chk = await fitness_nutrition.evaluate_meal_check(
            db,
            user,
            cand,
            now_utc=now,
            llm_client_instance=llm_client_instance,
        )
        if not chk.get("ok"):
            return (
                "Не совсем поняла блюдо для примерки — назови чуть конкретнее (например: «пицца 2 куска» или «блин в Теремке»).",
                None,
                {"query_type": "meal_check"},
            )
        dish = chk["dish"]
        pid = await repo.create_pending_meal_clarification(
            db,
            int(user["id"]),
            0,
            local_date,
            cand,
            str(dish["title"]),
            str(dish.get("meal_slot") or "snack"),
            dish,
            "",
            status="check",
            source="meal_check",
            auto_commit_minutes=60,
            now_utc=now,
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🍽 Всё-таки съел(а)", callback_data=f"kbju:pcommit:{pid}:1"),
                    InlineKeyboardButton(text="🍽 Полпорции", callback_data=f"kbju:pcommit:{pid}:0.5"),
                ],
                [
                    InlineKeyboardButton(text="❌ Не буду", callback_data=f"kbju:pdrop:{pid}"),
                    InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
                ],
                [
                    InlineKeyboardButton(text="🧊 Что из холодильника?", callback_data="kitchen:quick_pick"),
                ],
            ]
        )
        return chk["advice_html"], kb, {"query_type": "meal_check", "pending_id": pid}

    fallback_reply = _esc(parsed.reply_text) if parsed.reply_text else (
        "Не совсем поняла, где искать. Могу открыть план на сегодня или дневник."
    )
    return (
        fallback_reply,
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📓 Открыть дневник", callback_data="diary:tab:diary")],
                [InlineKeyboardButton(text="⬅️ Меню", callback_data="main:menu")],
            ]
        ),
        {"query_type": "none"},
    )


async def _analyze_and_persist(
    db,
    user: dict[str, Any],
    inbox_row: dict[str, Any],
    raw_text: str,
    now_utc: datetime.datetime,
    llm_client_instance: Optional[LLMClient] = None,
    reply_context: str = "",
) -> InboxProcessOutcome:
    tz = user.get("timezone") or "UTC"
    local_date = inbox_row.get("local_date") or local_date_str(now_utc, tz)
    uid = int(user["id"])
    inbox_id = int(inbox_row["id"])
    preface_note = ""

    # 0. Ленивое доуточнение еды: не мешаем обычной беседе и уважаем отмену!
    if not is_student_mode(user):
        pending = await repo.get_active_pending_meal(db, uid, now_utc=now_utc)
        if pending is not None:
            if fitness_nutrition.is_meal_cancel_reply(raw_text):
                await repo.resolve_pending_meal(db, int(pending["id"]), status="cancelled")
                await repo.update_inbox_message(db, inbox_id, status="processed")
                updated_row = await repo.get_inbox_message(db, uid, inbox_id) or inbox_row
                v_line = _voice_prefix_line(updated_row)
                body = "👌 Ок, не записываю этот приём."
                reply_text = f"{v_line}\n\n{body}" if v_line else body
                return InboxProcessOutcome(
                    inbox_id=inbox_id,
                    status="processed",
                    is_duplicate=False,
                    source=updated_row.get("source") or "text",
                    raw_text=raw_text,
                    actions=[],
                    clarifications=[],
                    used_llm=False,
                    reply_text=reply_text,
                    reply_markup=None,
                )

            if fitness_nutrition.is_lazy_accept_reply(raw_text):
                committed = await commit_pending_meal(
                    db, user, pending, mult=1.0, status="committed", now_utc=now_utc
                )
                await repo.update_inbox_message(db, inbox_id, status="processed")
                updated_row = await repo.get_inbox_message(db, uid, inbox_id) or inbox_row
                v_line = _voice_prefix_line(updated_row)
                rem_txt = ""
                if not committed["advice"]["targets"].get("mindful_mode"):
                    rem = committed["advice"]["remaining"]
                    rem_txt = f"\nОстаток на сегодня: <b>{rem['kcal']} ккал</b> · белок +{rem['protein_g']} г"
                reply_body = (
                    f"👌 Без заморочек — записала примерно: <b>{_esc(committed['title'])}</b>\n"
                    f"🔥 ~<b>{committed['kcal']} ккал</b> (Б {committed['protein_g']:g}/Ж {committed['fat_g']:g}/У {committed['carbs_g']:g}){rem_txt}"
                )
                reply_text = f"{v_line}\n\n{reply_body}" if v_line else reply_body
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="✏️ Уточнить", callback_data=f"kbju:refine:{committed['meal_id']}"),
                            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"kbju:del:{committed['meal_id']}"),
                        ],
                        [
                            InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
                        ],
                    ]
                )
                return InboxProcessOutcome(
                    inbox_id=inbox_id,
                    status="processed",
                    is_duplicate=False,
                    source=updated_row.get("source") or "text",
                    raw_text=raw_text,
                    actions=[],
                    clarifications=[],
                    used_llm=False,
                    reply_text=reply_text,
                    reply_markup=kb,
                )

            if fitness_nutrition.looks_like_meal_clarification(raw_text):
                combined = f"{pending['raw_text']} ({raw_text})"
                refined = await fitness_nutrition.parse_meal_input(
                    raw_text=combined,
                    llm_client_instance=llm_client_instance,
                    db=db,
                    user_id=uid,
                    allow_clarification=False,
                    allow_web_lookup=True,
                )
                committed = await commit_pending_meal(
                    db,
                    user,
                    pending,
                    mult=1.0,
                    status="refined",
                    refined_meal=(refined if refined.get("ok") else None),
                    now_utc=now_utc,
                )
                await repo.update_inbox_message(db, inbox_id, status="processed")
                updated_row = await repo.get_inbox_message(db, uid, inbox_id) or inbox_row
                v_line = _voice_prefix_line(updated_row)
                rem_txt = ""
                if not committed["advice"]["targets"].get("mindful_mode"):
                    rem = committed["advice"]["remaining"]
                    rem_txt = f"\nОстаток на сегодня: <b>{rem['kcal']} ккал</b> · белок +{rem['protein_g']} г"
                reply_body = (
                    f"✅ Уточнила и записала: <b>{_esc(committed['title'])}</b>\n"
                    f"🔥 <b>{committed['kcal']} ккал</b> (Б {committed['protein_g']:g}/Ж {committed['fat_g']:g}/У {committed['carbs_g']:g}){rem_txt}"
                )
                reply_text = f"{v_line}\n\n{reply_body}" if v_line else reply_body
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="✏️ Поправить", callback_data=f"kbju:refine:{committed['meal_id']}"),
                            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"kbju:del:{committed['meal_id']}"),
                        ],
                        [
                            InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
                        ],
                    ]
                )
                return InboxProcessOutcome(
                    inbox_id=inbox_id,
                    status="processed",
                    is_duplicate=False,
                    source=updated_row.get("source") or "text",
                    raw_text=raw_text,
                    actions=[],
                    clarifications=[],
                    used_llm=bool(refined.get("used_llm")),
                    reply_text=reply_text,
                    reply_markup=kb,
                )

            # Пользователь переключился на другую тему — тихо фиксируем прошлый приём примерно
            auto_res = await commit_pending_meal(
                db, user, pending, mult=1.0, status="auto_committed", now_utc=now_utc
            )
            preface_note = (
                f"📌 Прошлый приём записала примерно: <i>{_esc(auto_res['title'])}</i> (~{auto_res['kcal']} ккал)\n\n"
            )

    recent_ctx = _get_recent_context(uid, now_utc)
    ctx_for_parser = list(recent_ctx)
    if str(reply_context or "").strip():
        ctx_for_parser.append(
            {"role": "assistant", "text": str(reply_context).strip()[:400], "is_reply_context": True}
        )
    snapshot = await context_builder.build_user_day_snapshot(db, user, now_utc=now_utc)
    ctx_for_llm = context_builder.format_context_for_llm(snapshot)
    parsed = await analyze_inbox_text(
        raw_text,
        now_utc=now_utc,
        tz_str=tz,
        llm_client_instance=llm_client_instance,
        recent_context=ctx_for_parser,
        user_context_block=ctx_for_llm,
    )

    if parsed.intent == "query":
        await repo.update_inbox_message(
            db,
            inbox_id,
            status="answered",
            llm_provider=parsed.llm_provider,
            llm_model=parsed.llm_model,
            llm_raw_json=parsed.llm_raw_json,
            llm_error=parsed.llm_error or "",
        )
        updated_row = await repo.get_inbox_message(db, user["id"], inbox_id) or inbox_row
        body_text, reply_markup, query_meta = await _build_query_response(
            db,
            user,
            parsed,
            raw_text,
            reply_context=reply_context,
            llm_client_instance=llm_client_instance,
            now_utc=now_utc,
            recent_context=recent_ctx,
        )
        voice_line = _voice_prefix_line(updated_row)
        reply_text = f"{voice_line}\n\n{body_text}" if voice_line else body_text
        if preface_note:
            reply_text = preface_note + reply_text
        _append_dialog_turn(uid, "user", raw_text, now_utc)
        _append_dialog_turn(uid, "assistant", body_text, now_utc, meta=query_meta)
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="answered",
            is_duplicate=False,
            source=updated_row.get("source") or "text",
            raw_text=raw_text,
            actions=[],
            clarifications=[],
            llm_error=parsed.llm_error,
            used_llm=parsed.used_llm,
            reply_text=reply_text,
            reply_markup=reply_markup,
        )

    if parsed.intent == "chat" and not parsed.items:
        await repo.update_inbox_message(
            db,
            inbox_id,
            status="replied",
            llm_provider=parsed.llm_provider,
            llm_model=parsed.llm_model,
            llm_raw_json=parsed.llm_raw_json,
            llm_error=parsed.llm_error or "",
        )
        updated_row = await repo.get_inbox_message(db, user["id"], inbox_id) or inbox_row
        chat_msg = _esc(parsed.reply_text or "На связи! Напиши или надиктуй, что сделать или записать.")
        voice_line = _voice_prefix_line(updated_row)
        reply_text = f"{voice_line}\n\n{chat_msg}" if voice_line else chat_msg
        if preface_note:
            reply_text = preface_note + reply_text
        _append_dialog_turn(uid, "user", raw_text, now_utc)
        _append_dialog_turn(uid, "assistant", chat_msg, now_utc, meta={"query_type": "none"})
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="replied",
            is_duplicate=False,
            source=updated_row.get("source") or "text",
            raw_text=raw_text,
            actions=[],
            clarifications=[],
            llm_error=parsed.llm_error,
            used_llm=parsed.used_llm,
            reply_text=reply_text,
            reply_markup=None,
        )

    actions, clarifications = await _persist_parsed_items(
        db,
        user,
        inbox_row,
        parsed,
        local_date,
        now_utc=now_utc,
        llm_client_instance=llm_client_instance,
    )
    support_reply = parsed.reply_text
    if parsed.intent == "support" and (not parsed.used_llm or not (support_reply or "").strip()):
        card = advice_service.pick_situational_advice(snapshot, raw_text)
        micro = str(card.get("micro_step") or "").strip()
        tone_line = str(card.get("selected_tone_text") or "").strip()
        if tone_line and micro:
            support_reply = f"{tone_line}\nМикро-шаг сейчас: {micro}"

    new_status = "needs_clarification" if clarifications else "processed"
    await repo.update_inbox_message(
        db,
        inbox_id,
        status=new_status,
        llm_provider=parsed.llm_provider,
        llm_model=parsed.llm_model,
        llm_raw_json=parsed.llm_raw_json,
        llm_error=parsed.llm_error or "",
    )
    updated_row = await repo.get_inbox_message(db, user["id"], inbox_id) or inbox_row
    reply_text, reply_markup = render_inbox_summary(
        updated_row,
        actions,
        llm_error=parsed.llm_error,
        intent=parsed.intent,
        support_reply=support_reply,
    )
    if preface_note:
        reply_text = preface_note + reply_text
    _append_dialog_turn(uid, "user", raw_text, now_utc)
    _append_dialog_turn(uid, "assistant", reply_text, now_utc, meta={"query_type": "none"})
    return InboxProcessOutcome(
        inbox_id=inbox_id,
        status=new_status,
        is_duplicate=False,
        source=updated_row.get("source") or "text",
        raw_text=raw_text,
        actions=actions,
        clarifications=clarifications,
        llm_error=parsed.llm_error,
        used_llm=parsed.used_llm,
        reply_text=reply_text,
        reply_markup=reply_markup,
    )


async def capture_and_process_text(
    db,
    user: dict[str, Any],
    *,
    chat_id: int,
    message_id: Optional[int],
    text: str,
    reply_context: str = "",
    now_utc: Optional[datetime.datetime] = None,
    llm_client_instance: Optional[LLMClient] = None,
) -> InboxProcessOutcome:
    now = now_utc or datetime.datetime.utcnow()
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    clean_text = (text or "").strip()

    row, created = await repo.create_inbox_message_if_new(
        db,
        user_id=user["id"],
        chat_id=chat_id,
        telegram_message_id=message_id,
        source="text",
        local_date=local_date,
        raw_text=clean_text,
        status="received",
    )
    if not created:
        return InboxProcessOutcome(
            inbox_id=int(row.get("id") or 0),
            status=str(row.get("status") or "processed"),
            is_duplicate=True,
            source="text",
            raw_text=str(row.get("raw_text") or clean_text),
            reply_text="Это сообщение я уже сохранила, дубликаты не создавала.",
        )

    return await _analyze_and_persist(
        db,
        user,
        row,
        clean_text,
        now,
        llm_client_instance=llm_client_instance,
        reply_context=reply_context,
    )


async def capture_and_process_voice(
    db,
    bot,
    user: dict[str, Any],
    *,
    chat_id: int,
    message_id: Optional[int],
    voice,
    reply_context: str = "",
    now_utc: Optional[datetime.datetime] = None,
    stt_fn: Optional[Callable[..., Any]] = None,
    llm_client_instance: Optional[LLMClient] = None,
) -> InboxProcessOutcome:
    now = now_utc or datetime.datetime.utcnow()
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    file_id = getattr(voice, "file_id", None)
    file_unique_id = getattr(voice, "file_unique_id", None)
    duration = getattr(voice, "duration", None)

    row, created = await repo.create_inbox_message_if_new(
        db,
        user_id=user["id"],
        chat_id=chat_id,
        telegram_message_id=message_id,
        source="voice",
        local_date=local_date,
        raw_text=None,
        telegram_file_id=file_id,
        telegram_file_unique_id=file_unique_id,
        duration_sec=duration,
        status="received",
    )
    if not created:
        return InboxProcessOutcome(
            inbox_id=int(row.get("id") or 0),
            status=str(row.get("status") or "processed"),
            is_duplicate=True,
            source="voice",
            raw_text=str(row.get("raw_text") or ""),
            reply_text="Это голосовое я уже сохранила, дубликаты не создавала.",
        )

    inbox_id = int(row["id"])
    max_sec = stt.max_voice_duration_sec()
    if duration is not None and int(duration) > max_sec:
        await repo.update_inbox_message(
            db, inbox_id, status="failed_stt", stt_error="too_long"
        )
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="failed_stt",
            is_duplicate=False,
            source="voice",
            stt_error="too_long",
            reply_text=(
                f"Голосовое длиннее {max_sec} сек — запись сохранила, но не смогла расшифровать. "
                "Пришли покороче или текстом."
            ),
        )

    if stt_fn is None and not stt.stt_enabled():
        await repo.update_inbox_message(
            db, inbox_id, status="failed_stt", stt_error="stt_not_configured"
        )
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="failed_stt",
            is_duplicate=False,
            source="voice",
            stt_error="stt_not_configured",
            reply_text=(
                "Голосовое сохранила, но сервис расшифровки (STT) пока не подключён. "
                "Напиши это же текстом — сразу разберу и запишу."
            ),
        )

    audio_bytes, dl_err = await stt.download_telegram_voice(bot, voice)
    if not audio_bytes:
        await repo.update_inbox_message(
            db, inbox_id, status="failed_stt", stt_error=dl_err or "download_failed"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔁 Повторить расшифровку", callback_data=f"inbox:retry_stt:{inbox_id}")]
            ]
        )
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="failed_stt",
            is_duplicate=False,
            source="voice",
            stt_error=dl_err or "download_failed",
            reply_text="Запись сохранила, но не смогла скачать аудио из Telegram. Можем повторить или напиши текстом.",
            reply_markup=kb,
        )

    mime_type = getattr(voice, "mime_type", None) or "audio/ogg"
    filename = "voice.mp4" if "mp4" in mime_type else "voice.ogg"
    if stt_fn is not None:
        raw_res = await stt_fn(audio_bytes, filename=filename, mime_type=mime_type)
        if isinstance(raw_res, stt.STTResult):
            stt_res = raw_res
        elif isinstance(raw_res, str) and raw_res.strip():
            stt_res = stt.STTResult(ok=True, text=raw_res.strip(), provider="custom")
        else:
            stt_res = stt.STTResult(ok=False, error="empty_transcript")
    else:
        stt_res = await stt.transcribe_voice(audio_bytes, filename=filename, mime_type=mime_type)

    if not stt_res.ok or not stt_res.text.strip():
        await repo.update_inbox_message(
            db,
            inbox_id,
            status="failed_stt",
            stt_provider=stt_res.provider,
            stt_error=stt_res.error or "stt_failed",
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔁 Повторить расшифровку", callback_data=f"inbox:retry_stt:{inbox_id}")]
            ]
        )
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="failed_stt",
            is_duplicate=False,
            source="voice",
            stt_error=stt_res.error or "stt_failed",
            reply_text=(
                "Голосовое сохранила, но расшифровать сейчас не получилось. "
                "Можно нажать «Повторить» или прислать текстом."
            ),
            reply_markup=kb,
        )

    transcript = stt_res.text.strip()
    await repo.update_inbox_message(
        db,
        inbox_id,
        raw_text=transcript,
        stt_provider=stt_res.provider,
        stt_error="",
        status="transcribed",
    )
    updated_row = await repo.get_inbox_message(db, user["id"], inbox_id) or row
    return await _analyze_and_persist(
        db,
        user,
        updated_row,
        transcript,
        now,
        llm_client_instance=llm_client_instance,
        reply_context=reply_context,
    )


async def _delete_action_target(db, user_id: int, action: dict[str, Any]) -> None:
    table = action.get("target_table")
    payload = _load_payload(action)
    if table == "shopping_list_removed":
        if action.get("status") == "skipped":
            return
        removed = payload.get("removed_item")
        if isinstance(removed, dict) and removed.get("item_name"):
            await repo.create_shopping_item(
                db,
                user_id,
                str(removed["item_name"]),
                qty=float(removed.get("quantity") or 1.0),
                unit=str(removed.get("unit") or "шт"),
                category=str(removed.get("category") or "food"),
                scope=str(removed.get("scope") or "household"),
            )
        return
    if table == "pantry_removed":
        if action.get("status") == "skipped":
            return
        removed = payload.get("removed_item")
        if isinstance(removed, dict) and removed.get("name"):
            await repo.restore_pantry_item(db, user_id, removed)
        return
    if table == "day_plan_removed":
        if action.get("status") == "skipped":
            return
        removed = payload.get("removed_item")
        if isinstance(removed, dict) and removed.get("title"):
            plan_date = str(payload.get("plan_date") or removed.get("plan_date") or "")
            if not plan_date:
                plan_date = local_date_str(datetime.datetime.utcnow(), "UTC")
            await repo.add_day_plan_item(
                db,
                user_id,
                plan_date,
                str(removed["title"]),
                category=str(removed.get("category") or "inbox"),
                is_important=bool(removed.get("is_important")),
            )
        return
    if table == "custom_reminder_removed":
        if action.get("status") == "skipped":
            return
        removed = payload.get("removed_item")
        if isinstance(removed, dict) and removed.get("title"):
            new_rem_id = await repo.create_custom_reminder(
                db,
                user_id=user_id,
                title=str(removed["title"]),
                reminder_time=str(removed.get("reminder_time") or "12:00"),
                frequency_days=int(removed.get("frequency_days") or 1),
                target_weekday=removed.get("target_weekday"),
            )
            if removed.get("last_sent_date"):
                await repo.set_custom_reminder_sent(db, new_rem_id, str(removed["last_sent_date"]))
        return
    if table == "user_wake_updated":
        if action.get("status") == "skipped":
            return
        prev_wake = str(payload.get("previous_wake") or "").strip()
        if prev_wake:
            await repo.update_user_wake(db, user_id, prev_wake)
        return
    if table == "user_day_off":
        if action.get("status") == "skipped":
            return
        await repo.set_user_day_off(db, user_id, str(payload.get("previous_day_off") or ""))
        return
    tid = action.get("target_id")
    if table == "pantry_items":
        if tid:
            await repo.delete_pantry_item(db, user_id, int(tid))
        synced_shop = payload.get("synced_shopping_item")
        if isinstance(synced_shop, dict) and synced_shop.get("item_name"):
            await repo.create_shopping_item(
                db,
                user_id,
                str(synced_shop["item_name"]),
                qty=float(synced_shop.get("quantity") or 1.0),
                unit=str(synced_shop.get("unit") or "шт"),
                category=str(synced_shop.get("category") or "food"),
                scope=str(synced_shop.get("scope") or "household"),
            )
        return
    if table == "calendar_event_created":
        user_row = await repo.get_user(db, user_id)
        await calendar_service.delete_unified_event(
            db,
            dict(user_row) if user_row else {"id": user_id},
            storage=str(payload.get("storage") or "local"),
            event_id=int(tid) if tid else None,
            google_event_id=str(payload.get("google_event_id") or ""),
            external_uid=str(payload.get("external_uid") or ""),
            event_date=str(payload.get("resolved_date") or ""),
            start_time=str(payload.get("start_time") or ""),
        )
        return
    if not tid:
        return
    tid_int = int(tid)
    if table == "day_plan_items":
        await repo.delete_day_plan_item(db, user_id, tid_int)
    elif table == "shopping_list":
        await repo.delete_shopping_item(db, user_id, tid_int, scope="household")
    elif table == "custom_reminders":
        await repo.delete_custom_reminder(db, user_id, tid_int)
    elif table == "journal_entries":
        await repo.delete_journal_entry(db, user_id, tid_int, hard=False)
        completed_task_id = payload.get("completed_task_id")
        if completed_task_id:
            await repo.mark_day_plan_item_done(db, int(completed_task_id), done=False)
            k_delta = int(payload.get("karma_delta") or 2)
            if k_delta > 0:
                k_state = await repo.get_or_create_karma_state(db, user_id)
                old_score = int(k_state.get("score") if k_state.get("score") is not None else 70)
                await repo.update_karma_state(
                    db,
                    user_id,
                    score=max(0, old_score - k_delta),
                )
    elif table == "pulse_checkins":
        if not payload.get("already_resolved"):
            await repo.update_pulse_checkin(
                db, tid_int, status="pending", resolved_at_utc=None
            )
            k_delta = int(payload.get("delta") or 0)
            if k_delta > 0:
                k_state = await repo.get_or_create_karma_state(db, user_id)
                old_score = int(k_state.get("score") if k_state.get("score") is not None else 70)
                await repo.update_karma_state(
                    db,
                    user_id,
                    score=max(0, old_score - k_delta),
                )
    elif table == "expenses":
        await repo.delete_expense(db, user_id, tid_int)
    elif table == "meal_logs":
        await repo.delete_meal_log(db, user_id, tid_int)
    elif table == "weights":
        await repo.delete_weight_entry(db, user_id, tid_int)
    elif table == "workout_logs":
        deleted_w = await repo.delete_workout_log(db, user_id, tid_int)
        if deleted_w:
            k_delta = int(payload.get("karma_delta") or 0)
            p_delta = int(payload.get("points_delta") or 0)
            if k_delta > 0:
                k_state = await repo.get_or_create_karma_state(db, user_id)
                old_score = int(k_state.get("score") if k_state.get("score") is not None else 70)
                await repo.update_karma_state(
                    db,
                    user_id,
                    score=max(0, old_score - k_delta),
                )
            if p_delta > 0:
                await repo.add_points(
                    db,
                    user_id,
                    -p_delta,
                    local_date=str(payload.get("local_date") or "") or None,
                )
    elif table == "pending_meal_clarifications":
        await repo.resolve_pending_meal(db, tid_int, status="cancelled")


async def confirm_inbox_action(
    db,
    user: dict[str, Any],
    action_id: int,
    now_utc: Optional[datetime.datetime] = None,
) -> tuple[bool, str]:
    """Подтвердить крупное голосовое/текстовое действие (перенос хвостов или сдвиг отбоя)."""
    action = await repo.get_inbox_action(db, user["id"], action_id)
    if not action or action.get("status") != "awaiting_confirm":
        return False, "Это действие уже обработано или отменено."

    now = now_utc or datetime.datetime.utcnow()
    payload = _load_payload(action)
    kind = str(payload.get("kind") or action.get("item_kind") or "")
    inbox_id = int(action["inbox_message_id"])

    if kind == "rollover_tasks":
        from_date = str(payload.get("from_date") or local_date_str(now, user.get("timezone") or "UTC"))
        to_date = str(
            payload.get("to_date")
            or (datetime.date.fromisoformat(from_date) + datetime.timedelta(days=1)).isoformat()
        )
        moved = await repo.rollover_unfinished_day_plan_items(db, user["id"], from_date, to_date)
        if moved > 0:
            await karma_service.record_karma_event(
                db,
                user,
                domain="tasks",
                event_type="rollover",
                delta=2,
                reason=f"Перенесены хвосты на завтра ({moved} шт.)",
                now_utc=now,
            )
        payload["moved"] = moved
        await repo.update_inbox_action(
            db,
            user["id"],
            action_id,
            status="active",
            title=f"Перенесены хвосты на завтра ({moved} шт.)",
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        await _sync_parent_inbox_status(db, user["id"], inbox_id)
        if moved > 0:
            return True, f"➡️ Перенесла на завтра незавершённых дел: <b>{moved}</b> (+2 кармы)."
        return True, "✅ Незавершённых дел на сегодня не нашлось — переносить было нечего."

    if kind == "shift_bedtime":
        target_hhmm = str(payload.get("target_hhmm") or "").strip()
        if not target_hhmm:
            return False, "Не удалось определить время отбоя."
        local_date = str(payload.get("local_date") or local_date_str(now, user.get("timezone") or "UTC"))
        await repo.set_user_sleep_override(db, user["id"], local_date, target_hhmm)

        refreshed_user_row = await repo.get_user(db, user["id"])
        refreshed_user = dict(refreshed_user_row) if refreshed_user_row else dict(user)
        wellness = await repo.get_wellness(db, user["id"])
        slots = pulse_service.compute_daily_slots(
            refreshed_user, dict(wellness) if wellness else None, local_date
        )
        for s_key in ("winddown", "bedtime"):
            slot_spec = next((s for s in slots if s["slot_key"] == s_key), None)
            existing_chk = await repo.get_pulse_checkin(db, user["id"], local_date, s_key)
            if slot_spec and existing_chk and existing_chk.get("status") in {"pending", "snoozed"}:
                sched_iso = slot_spec["scheduled_at_utc"].isoformat()
                await repo.update_pulse_checkin(
                    db,
                    int(existing_chk["id"]),
                    scheduled_at_utc=sched_iso,
                    next_ping_at_utc=sched_iso,
                )

        await repo.update_inbox_action(
            db,
            user["id"],
            action_id,
            status="active",
            title=f"Отбой сдвинут на {target_hhmm}",
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        await _sync_parent_inbox_status(db, user["id"], inbox_id)
        return True, f"🌙 Отбой сдвинут на <b>{target_hhmm}</b>, Пульс на вечер обновлён."

    return False, "Неизвестный тип подтверждения."


async def reject_inbox_action(db, user: dict[str, Any], action_id: int) -> bool:
    """Отменить ожидающее подтверждения действие."""
    action = await repo.get_inbox_action(db, user["id"], action_id)
    if not action or action.get("status") != "awaiting_confirm":
        return False
    await repo.update_inbox_action(db, user["id"], action_id, status="undone")
    await _sync_parent_inbox_status(db, user["id"], int(action["inbox_message_id"]))
    return True


async def undo_inbox_message(db, user: dict[str, Any], inbox_id: int) -> int:
    actions = await repo.list_inbox_actions(db, user["id"], inbox_id, include_undone=False)
    undone_count = 0
    for act in actions:
        await _delete_action_target(db, user["id"], act)
        await repo.update_inbox_action(db, user["id"], act["id"], status="undone")
        undone_count += 1
    await repo.update_inbox_message(db, inbox_id, status="undone")
    return undone_count


async def delete_inbox_action(db, user: dict[str, Any], action_id: int) -> Optional[dict[str, Any]]:
    action = await repo.get_inbox_action(db, user["id"], action_id)
    if not action or action.get("status") == "undone":
        return None
    await _delete_action_target(db, user["id"], action)
    await repo.update_inbox_action(db, user["id"], action_id, status="undone")
    await _sync_parent_inbox_status(db, user["id"], int(action["inbox_message_id"]))
    return action


async def rename_inbox_action(
    db, user: dict[str, Any], action_id: int, new_title: str
) -> Optional[dict[str, Any]]:
    clean_title = (new_title or "").strip()
    if not clean_title:
        return None
    action = await repo.get_inbox_action(db, user["id"], action_id)
    if not action or action.get("status") == "undone":
        return None

    table = action.get("target_table")
    tid = action.get("target_id")
    if tid:
        tid_int = int(tid)
        if table == "day_plan_items":
            await repo.update_day_plan_item_title(db, user["id"], tid_int, clean_title)
        elif table == "shopping_list":
            await repo.update_shopping_item_name(db, user["id"], tid_int, clean_title, scope="household")
        elif table == "custom_reminders":
            await repo.update_custom_reminder_title(db, user["id"], tid_int, clean_title)
        elif table == "journal_entries":
            await repo.update_journal_entry(db, user["id"], tid_int, title=clean_title)
        elif table == "pantry_items":
            await repo.update_pantry_item_name(db, user["id"], tid_int, clean_title)
        elif table == "meal_logs":
            await repo.update_meal_log(db, user["id"], tid_int, title=clean_title)
        elif table == "workout_logs":
            await repo.update_workout_log_title(db, user["id"], tid_int, clean_title)
        elif table == "expenses":
            await repo.update_expense_category(db, user["id"], tid_int, clean_title.lower())

    payload = _load_payload(action)
    if "title" in payload:
        payload["title"] = clean_title
    await repo.update_inbox_action(
        db,
        user["id"],
        action_id,
        title=clean_title,
        payload_json=json.dumps(payload, ensure_ascii=False),
    )
    return await repo.get_inbox_action(db, user["id"], action_id)


async def reclassify_inbox_action(
    db,
    user: dict[str, Any],
    action_id: int,
    new_kind: str,
    *,
    now_utc: Optional[datetime.datetime] = None,
) -> Optional[dict[str, Any]]:
    if is_student_mode(user) and new_kind == "shopping":
        return None
    if new_kind not in {"task", "shopping", "diary", "idea", "done"}:
        return None
    action = await repo.get_inbox_action(db, user["id"], action_id)
    if not action or action.get("status") == "undone":
        return None

    now = now_utc or datetime.datetime.utcnow()
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    inbox_row = await repo.get_inbox_message(db, user["id"], int(action["inbox_message_id"]))
    source = (inbox_row or {}).get("source") or "text"
    payload = _load_payload(action)

    await _delete_action_target(db, user["id"], action)

    if new_kind == "task":
        new_id = await repo.add_day_plan_item(
            db, user["id"], local_date, action["title"], category="inbox", is_important=False
        )
        new_table = "day_plan_items"
    elif new_kind == "shopping":
        qty = float(payload.get("quantity") or 1.0)
        unit = str(payload.get("unit") or "шт")
        new_id = await repo.create_shopping_item(
            db, user["id"], action["title"], qty=qty, unit=unit, category="food", scope="household"
        )
        new_table = "shopping_list"
    else:
        new_id = await repo.create_journal_entry(
            db,
            user_id=user["id"],
            entry_type=new_kind,
            entry_date=local_date,
            source=source,
            user_text=action["source_quote"],
            title=action["title"],
            model_note=str(payload.get("model_note") or ""),
            tags=payload.get("tags") if isinstance(payload.get("tags"), list) else [],
            inbox_message_id=int(action["inbox_message_id"]),
        )
        new_table = "journal_entries"

    await repo.update_inbox_action(
        db,
        user["id"],
        action_id,
        item_kind=new_kind,
        target_table=new_table,
        target_id=new_id,
        status="active",
    )
    await _sync_parent_inbox_status(db, user["id"], int(action["inbox_message_id"]))
    return await repo.get_inbox_action(db, user["id"], action_id)


async def resolve_clarification_action(
    db,
    user: dict[str, Any],
    action_id: int,
    *,
    resolved_date: Optional[str] = None,
    resolved_time: Optional[str] = None,
    as_task: bool = False,
    drop: bool = False,
    now_utc: Optional[datetime.datetime] = None,
) -> Optional[dict[str, Any]]:
    action = await repo.get_inbox_action(db, user["id"], action_id)
    if not action or action.get("status") != "clarifying":
        return None

    now = now_utc or datetime.datetime.utcnow()
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    inbox_id = int(action["inbox_message_id"])

    if drop:
        await repo.update_inbox_action(db, user["id"], action_id, status="undone")
        await _sync_parent_inbox_status(db, user["id"], inbox_id)
        return await repo.get_inbox_action(db, user["id"], action_id)

    payload = _load_payload(action)
    title = payload.get("title") or action.get("title") or "Напоминание"

    if as_task:
        target_date = resolved_date or payload.get("resolved_date") or local_date
        item_id = await repo.add_day_plan_item(
            db, user["id"], target_date, title, category="inbox", is_important=False
        )
        await repo.update_inbox_action(
            db,
            user["id"],
            action_id,
            item_kind="task",
            target_table="day_plan_items",
            target_id=item_id,
            title=title,
            payload_json=json.dumps({"plan_date": target_date}, ensure_ascii=False),
            status="active",
        )
        await _sync_parent_inbox_status(db, user["id"], inbox_id)
        return await repo.get_inbox_action(db, user["id"], action_id)

    eff_date = resolved_date or payload.get("resolved_date") or local_date
    eff_time = resolved_time
    if not eff_time:
        return None

    if payload.get("is_calendar_event"):
        en_hhmm = _compute_end_hhmm(eff_time, int(payload.get("duration_minutes") or 60))
        loc = str(payload.get("location") or "")
        cal_res = await calendar_service.create_unified_event(
            db,
            user,
            title=title,
            event_date=eff_date,
            start_time=eff_time,
            end_time=en_hhmm,
            location=loc,
            category="inbox",
            push_to_google=True,
            now_utc=now,
        )
        ev_obj = cal_res.get("event") or {}
        ev_id = int(ev_obj["id"]) if ev_obj.get("id") else None
        new_payload = {
            "storage": cal_res.get("storage") or "local",
            "google_synced": bool(cal_res.get("google_synced")),
            "google_event_id": str(cal_res.get("google_event_id") or ""),
            "external_uid": str(ev_obj.get("external_uid") or ""),
            "resolved_date": eff_date,
            "start_time": eff_time,
            "end_time": en_hhmm,
            "location": loc,
            "model_note": payload.get("model_note", ""),
        }
        await repo.update_inbox_action(
            db,
            user["id"],
            action_id,
            item_kind="calendar_event",
            target_table="calendar_event_created",
            target_id=ev_id,
            title=title,
            payload_json=json.dumps(new_payload, ensure_ascii=False),
            status="active",
        )
        await _sync_parent_inbox_status(db, user["id"], inbox_id)
        return await repo.get_inbox_action(db, user["id"], action_id)

    rem_id = await repo.create_custom_reminder(
        db,
        user_id=user["id"],
        title=title,
        reminder_time=eff_time,
        frequency_days=9999,
        target_weekday=None,
    )
    last_sent = compute_one_time_last_sent(eff_date, local_date, 9999)
    if last_sent:
        await repo.set_custom_reminder_sent(db, rem_id, last_sent)

    new_payload = {
        "resolved_date": eff_date,
        "resolved_time": eff_time,
        "model_note": payload.get("model_note", ""),
    }
    await repo.update_inbox_action(
        db,
        user["id"],
        action_id,
        item_kind="reminder",
        target_table="custom_reminders",
        target_id=rem_id,
        title=title,
        payload_json=json.dumps(new_payload, ensure_ascii=False),
        status="active",
    )
    await _sync_parent_inbox_status(db, user["id"], inbox_id)
    return await repo.get_inbox_action(db, user["id"], action_id)


async def retry_inbox_stt(
    db,
    bot,
    user: dict[str, Any],
    inbox_id: int,
    *,
    now_utc: Optional[datetime.datetime] = None,
    stt_fn: Optional[Callable[..., Any]] = None,
    llm_client_instance: Optional[LLMClient] = None,
) -> InboxProcessOutcome:
    row = await repo.get_inbox_message(db, user["id"], inbox_id)
    if not row:
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="missing",
            is_duplicate=False,
            source="voice",
            reply_text="Не нашла эту запись.",
        )
    if row.get("raw_text"):
        return await retry_inbox_parse(
            db, user, inbox_id, now_utc=now_utc, llm_client_instance=llm_client_instance
        )
    file_id = row.get("telegram_file_id")
    if not file_id:
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="failed_stt",
            is_duplicate=False,
            source="voice",
            stt_error="missing_file_id",
            reply_text="У записи нет аудиофайла — пришли текстом.",
        )

    retry_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Повторить расшифровку", callback_data=f"inbox:retry_stt:{inbox_id}")]
        ]
    )
    voice_stub = SimpleNamespace(
        file_id=file_id,
        file_unique_id=row.get("telegram_file_unique_id"),
        duration=row.get("duration_sec"),
        mime_type="audio/ogg",
    )
    audio_bytes, dl_err = await stt.download_telegram_voice(bot, voice_stub)
    if not audio_bytes:
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="failed_stt",
            is_duplicate=False,
            source="voice",
            stt_error=dl_err or "download_failed",
            reply_text="Не смогла скачать аудио. Попробуй ещё раз или напиши текстом.",
            reply_markup=retry_kb,
        )

    if stt_fn is not None:
        raw_res = await stt_fn(audio_bytes, filename="voice.ogg", mime_type="audio/ogg")
        stt_res = (
            raw_res
            if isinstance(raw_res, stt.STTResult)
            else stt.STTResult(ok=bool(raw_res), text=str(raw_res or "").strip())
        )
    else:
        stt_res = await stt.transcribe_voice(audio_bytes, filename="voice.ogg", mime_type="audio/ogg")

    if not stt_res.ok or not stt_res.text.strip():
        await repo.update_inbox_message(
            db, inbox_id, status="failed_stt", stt_error=stt_res.error or "stt_failed"
        )
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="failed_stt",
            is_duplicate=False,
            source="voice",
            stt_error=stt_res.error or "stt_failed",
            reply_text="Пока всё ещё не удалось расшифровать аудио. Пришли текстом — я сразу запишу.",
            reply_markup=retry_kb,
        )

    transcript = stt_res.text.strip()
    await repo.update_inbox_message(
        db,
        inbox_id,
        raw_text=transcript,
        stt_provider=stt_res.provider,
        stt_error="",
        status="transcribed",
    )
    updated_row = await repo.get_inbox_message(db, user["id"], inbox_id) or row
    now = now_utc or datetime.datetime.utcnow()
    return await _analyze_and_persist(
        db, user, updated_row, transcript, now, llm_client_instance=llm_client_instance
    )


async def retry_inbox_parse(
    db,
    user: dict[str, Any],
    inbox_id: int,
    *,
    now_utc: Optional[datetime.datetime] = None,
    llm_client_instance: Optional[LLMClient] = None,
) -> InboxProcessOutcome:
    row = await repo.get_inbox_message(db, user["id"], inbox_id)
    if not row or not row.get("raw_text"):
        return InboxProcessOutcome(
            inbox_id=inbox_id,
            status="missing",
            is_duplicate=False,
            source="text",
            reply_text="Не нашла исходный текст для переразбора.",
        )
    await undo_inbox_message(db, user, inbox_id)
    now = now_utc or datetime.datetime.utcnow()
    return await _analyze_and_persist(
        db, user, row, str(row["raw_text"]), now, llm_client_instance=llm_client_instance
    )


async def _sync_parent_inbox_status(db, user_id: int, inbox_id: int) -> None:
    actions = await repo.list_inbox_actions(db, user_id, inbox_id, include_undone=False)
    if not actions:
        await repo.update_inbox_message(db, inbox_id, status="undone")
    elif any(a.get("status") == "clarifying" for a in actions):
        await repo.update_inbox_message(db, inbox_id, status="needs_clarification")
    else:
        await repo.update_inbox_message(db, inbox_id, status="processed")
