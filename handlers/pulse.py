"""Хендлеры экрана «🌙 Сон и карма», чек-инов Пульса дня и заморозки ЧП."""

from __future__ import annotations

import datetime
import html
from typing import Any, Dict, Tuple

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from services import context_builder, karma_service, pulse_service
from utils.menu_match import match_menu_label
from utils.profile import is_student_mode
from utils.sender import safe_delete_or_clear_markup, safe_edit
from utils.time import local_date_str, parse_hhmm
from utils.user import ensure_user

router = Router()


def _message_pair(chat_id, message_id) -> tuple[int, int] | None:
    if chat_id is None or message_id is None:
        return None
    try:
        return int(chat_id), int(message_id)
    except (TypeError, ValueError):
        return None


def _callback_pair(callback: types.CallbackQuery) -> tuple[int, int] | None:
    message = getattr(callback, "message", None)
    chat = getattr(message, "chat", None)
    return _message_pair(getattr(chat, "id", None), getattr(message, "message_id", None))


async def _clear_callback_markup(callback: types.CallbackQuery) -> None:
    message = getattr(callback, "message", None)
    edit_markup = getattr(message, "edit_reply_markup", None)
    if not callable(edit_markup):
        return
    try:
        await edit_markup(reply_markup=None)
    except Exception:
        pass


def _has_wake_checklist(message) -> bool:
    markup = getattr(message, "reply_markup", None)
    rows = getattr(markup, "inline_keyboard", None) or []
    return any(
        str(getattr(button, "callback_data", "") or "").startswith("pulse:hygiene:")
        for row in rows
        for button in row
    )


async def _handle_already_resolved(
    callback: types.CallbackQuery,
    result: dict[str, Any],
    *,
    preserve_hub: bool = False,
) -> bool:
    if not result.get("already_resolved"):
        return False
    checkin = result.get("checkin") or {}
    current_pair = _callback_pair(callback)
    stored_pair = _message_pair(checkin.get("card_chat_id"), checkin.get("card_message_id"))
    preserve_wake = (
        result.get("status") == "done"
        and checkin.get("slot_key") == "wake"
        and (current_pair == stored_pair or _has_wake_checklist(callback.message))
    )
    ans_text = (
        "Поздний подъём отмечен, ночной режим снят"
        if result.get("late_wake_recorded")
        else "Карточка уже закрыта ✅"
    )
    await callback.answer(ans_text)
    if not preserve_hub and not preserve_wake:
        await _clear_callback_markup(callback)
    return True


async def _cleanup_previous_card(callback: types.CallbackQuery, result: dict[str, Any]) -> None:
    previous = result.get("previous_card")
    if not isinstance(previous, (tuple, list)) or len(previous) != 2:
        return
    previous_pair = _message_pair(previous[0], previous[1])
    if previous_pair is None or previous_pair == _callback_pair(callback):
        return
    bot = getattr(getattr(callback, "message", None), "bot", None)
    if bot is not None:
        await safe_delete_or_clear_markup(bot, *previous_pair)


async def _rebind_closed_card_reference(
    db,
    callback: types.CallbackQuery,
    result: dict[str, Any],
    *,
    keep_current: bool,
) -> None:
    previous = result.get("previous_card")
    if not isinstance(previous, (tuple, list)) or len(previous) != 2:
        return
    old_pair = _message_pair(previous[0], previous[1])
    current_pair = _callback_pair(callback)
    if old_pair is None or old_pair == current_pair:
        return
    checkin = result.get("checkin") or {}
    checkin_id = checkin.get("id")
    checkin_user_id = checkin.get("user_id")
    if checkin_id is None or checkin_user_id is None:
        return
    await repo.transition_pulse_checkin(
        db,
        int(checkin_id),
        allowed_statuses=(str(checkin.get("status") or ""),),
        user_id=int(checkin_user_id),
        expected_fields={"card_chat_id": old_pair[0], "card_message_id": old_pair[1]},
        card_chat_id=current_pair[0] if keep_current and current_pair else None,
        card_message_id=current_pair[1] if keep_current and current_pair else None,
    )


class PulseScheduleState(StatesGroup):
    wd_wake = State()
    wd_sleep = State()
    we_wake = State()
    we_sleep = State()


def _hub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🌙 Ложусь спать", callback_data="pulse:manual_bedtime"),
                InlineKeyboardButton(text="☀️ Я встал", callback_data="pulse:manual_wake"),
            ],
            [
                InlineKeyboardButton(text="⏰ Будни / Выходные", callback_data="pulse:schedule_menu"),
                InlineKeyboardButton(text="🤒 Болею 24ч", callback_data="pulse:sick:prompt"),
            ],
            [
                InlineKeyboardButton(text="🔔 Настройки пульса", callback_data="pulse:toggles_menu"),
                InlineKeyboardButton(text="📜 История кармы", callback_data="pulse:history"),
            ],
            [
                InlineKeyboardButton(text="🧠 Портрет привычек", callback_data="pulse:portrait"),
                InlineKeyboardButton(text="💡 Совет сейчас", callback_data="advice:more"),
            ],
            [
                InlineKeyboardButton(text="🧪 Пробный пинг", callback_data="pulse:test_ping"),
                InlineKeyboardButton(text="📅 Сегодня", callback_data="today:menu"),
            ],
        ]
    )


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="pulse:cancel_fsm")]]
    )


async def _build_hub_payload(
    db, tg_user_id: int, tg_full_name: str
) -> Tuple[str, InlineKeyboardMarkup]:
    user = await ensure_user(db, tg_user_id, tg_full_name)
    now = datetime.datetime.utcnow()
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    state = await repo.get_or_create_karma_state(db, int(user["id"]))
    events = await repo.list_karma_events(db, int(user["id"]), limit=5)
    checkins = await repo.list_pulse_checkins_for_date(db, int(user["id"]), local_date)
    eff = pulse_service.get_effective_schedule(user, local_date)
    text = karma_service.format_karma_screen(
        user,
        state,
        events,
        checkins,
        now_utc=now,
        effective_schedule=eff,
    )
    return text, _hub_keyboard()


async def send_pulse_hub(
    message: types.Message,
    db,
    *,
    tg_user_id: int | None = None,
    tg_full_name: str | None = None,
) -> None:
    """Открыть экран «🌙 Сон и карма» из reply-меню или команды."""
    uid = tg_user_id if tg_user_id is not None else message.from_user.id
    fname = tg_full_name if tg_full_name is not None else message.from_user.full_name
    text, kb = await _build_hub_payload(db, uid, fname)
    await message.answer(text, reply_markup=kb)


@router.message(Command("karma"))
@router.message(Command("pulse"))
@router.message(lambda m: match_menu_label(m.text, {"сон и карма", "карма", "пульс"}))
async def cmd_pulse_hub(message: types.Message, state: FSMContext, db) -> None:
    await state.clear()
    await send_pulse_hub(message, db)


@router.message(Command("sick"))
async def cmd_sick(message: types.Message, state: FSMContext, db) -> None:
    await state.clear()
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    text, kb = _build_sick_prompt(user)
    await message.answer(text, reply_markup=kb)


def _build_sick_prompt(
    user: Dict[str, Any], origin: str = "hub"
) -> Tuple[str, InlineKeyboardMarkup]:
    back_cb = "more:menu" if origin == "more" else "pulse:hub"
    confirm_cb = f"pulse:sick:confirm:{origin}" if origin == "more" else "pulse:sick:confirm"
    now = datetime.datetime.utcnow()
    if karma_service.is_sick_freeze_active(user, now):
        text = (
            "🤒 <b>Заморозка «Болею / форс-мажор» уже активна.</b>\n"
            "Штрафы кармы и жёсткие дожимы временно отключены на 24 часа."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)]]
        )
        return text, kb

    used, limit = karma_service.sick_freeze_remaining_uses(user, now)
    rem = max(0, limit - used)
    if rem <= 0:
        text = (
            f"🚫 Лимит заморозки на этот месяц исчерпан ({used}/{limit}).\n"
            "Используй честные переносы или 🔕 Тихий режим в настройках."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)]]
        )
        return text, kb

    text = (
        "🤒 <b>Включить заморозку «Болею / форс-мажор» на 24 часа?</b>\n\n"
        "• На 24 часа отключаются все штрафы кармы и эскалация 2/3 уровня.\n"
        "• Сохраняются только мягкие первые касания и личные лекарства.\n"
        f"• Доступно в этом месяце: <b>{rem} из {limit}</b>."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Включить на 24 часа", callback_data=confirm_cb)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)],
        ]
    )
    return text, kb


@router.callback_query(lambda c: c.data == "pulse:hub")
async def cb_pulse_hub(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await state.clear()
    text, kb = await _build_hub_payload(db, callback.from_user.id, callback.from_user.full_name)
    await safe_edit(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(lambda c: c.data == "pulse:cancel_fsm")
async def cb_pulse_cancel_fsm(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await state.clear()
    text, kb = await _build_hub_payload(db, callback.from_user.id, callback.from_user.full_name)
    await safe_edit(callback.message, text, reply_markup=kb)
    await callback.answer("Отменено.")


@router.callback_query(lambda c: c.data and c.data.startswith("pulse:done:"))
async def cb_pulse_done(callback: types.CallbackQuery, db) -> None:
    cid = int(callback.data.split(":")[2])
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    res = await pulse_service.complete_checkin(db, user, cid)
    if not res.get("ok"):
        await callback.answer(res.get("error", "Не найден чек-ин"), show_alert=True)
        return
    if await _handle_already_resolved(callback, res):
        return
    await _cleanup_previous_card(callback, res)

    karma = res["karma"]
    score = int(karma.get("score_after") or 70)
    delta = int(karma.get("delta") or 0)
    mode = karma.get("mode") or karma_service.get_karma_mode(score, user)
    sign = f"+{delta}" if delta > 0 else str(delta)

    if res.get("is_wake"):
        meta = pulse_service._load_meta(res["checkin"])  # noqa: SLF001
        kb = pulse_service.build_wake_checklist_keyboard(cid, meta, is_student=is_student_mode(user))
        bf_note = (
            f"\n🥣 Про завтрак мягко напомню в <b>{res['breakfast_scheduled_hhmm']}</b> (или запиши сразу кнопкой ниже)."
            if res.get("breakfast_scheduled_hhmm")
            else ""
        )
        p_tasks = list(res.get("pending_tasks") or [])
        tasks_note = (
            "\n\n📋 <b>Задачи на сегодня:</b>\n" + "\n".join(f"• {html.escape(t)}" for t in p_tasks[:4])
            if p_tasks
            else ""
        )
        text = (
            f"☀️ <b>Добро пожаловать в день!</b>\n"
            f"Карма: <b>{sign}</b> → {mode['emoji']} <b>{score}/100</b> ({html.escape(mode['label'])})\n\n"
            f"Добей быстрый утренний минимум в 1 тап:{bf_note}{tasks_note}"
        )
        await safe_edit(callback.message, text, reply_markup=kb)
        await _rebind_closed_card_reference(db, callback, res, keep_current=True)
        await callback.answer("Подъём отмечен!")
        return

    slot_key = str((res.get("checkin") or {}).get("slot_key") or "")
    title = pulse_service.SLOT_TITLES.get(slot_key, "Чек-ин")
    text = (
        f"✅ <b>Закрыто: {html.escape(title)}</b>\n"
        f"Карма: <b>{sign}</b> → {mode['emoji']} <b>{score}/100</b> ({html.escape(mode['label'])})"
    )
    kb_rows = []
    if slot_key in {"breakfast", "lunch", "dinner"}:
        kb_rows.append([InlineKeyboardButton(text="➕ Записать в КБЖУ (фото / текст)", callback_data="kbju:add")])
    kb_rows.append([InlineKeyboardButton(text="🌙 Сон и карма", callback_data="pulse:hub")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await safe_edit(callback.message, text, reply_markup=kb)
    await _rebind_closed_card_reference(db, callback, res, keep_current=False)
    await callback.answer("Отмечено!")


@router.callback_query(lambda c: c.data and (c.data.startswith("pulse:bedtime_ok:") or c.data == "pulse:manual_bedtime"))
async def cb_pulse_bedtime(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    cid = int(callback.data.split(":")[2]) if callback.data.startswith("pulse:bedtime_ok:") else None
    res = await pulse_service.confirm_bedtime(db, user, checkin_id=cid)
    if not res.get("ok"):
        await callback.answer(res.get("error", "Не удалось закрыть чек-ин"), show_alert=True)
        return
    if await _handle_already_resolved(callback, res, preserve_hub=callback.data == "pulse:manual_bedtime"):
        return
    await _cleanup_previous_card(callback, res)
    karma = res["karma"]
    score = int(karma.get("score_after") or 70)
    delta = int(karma.get("delta") or 0)
    delta_txt = f"+{delta}" if delta > 0 else str(delta)
    text = (
        "🌙 <b>Приняла: телефон убран, отбой подтверждён.</b>\n"
        f"Карма: <b>{delta_txt}</b> → <b>{score}/100</b>.\n\n"
        "До самого утра пингами тревожить не буду."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🌙 Сон и карма", callback_data="pulse:hub")]]
    )
    await safe_edit(callback.message, text, reply_markup=kb)
    await _rebind_closed_card_reference(db, callback, res, keep_current=False)
    await callback.answer("Спокойной ночи!")


@router.callback_query(lambda c: c.data == "pulse:manual_wake")
async def cb_pulse_manual_wake(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    now = datetime.datetime.utcnow()
    tz = user.get("timezone") or "UTC"
    local_date = local_date_str(now, tz)
    checkin = await repo.get_pulse_checkin(db, int(user["id"]), local_date, "wake")
    if checkin is None:
        checkin = await repo.upsert_pulse_checkin(
            db,
            int(user["id"]),
            local_date,
            "wake",
            "sleep",
            now.isoformat(),
            now.isoformat(),
        )
    res = await pulse_service.complete_checkin(db, user, int(checkin["id"]), now_utc=now)
    if not res.get("ok"):
        await callback.answer(res.get("error", "Не удалось закрыть чек-ин"), show_alert=True)
        return
    if await _handle_already_resolved(callback, res, preserve_hub=True):
        return
    await _cleanup_previous_card(callback, res)
    karma = res["karma"]
    score = int(karma.get("score_after") or 70)
    delta = int(karma.get("delta") or 0)
    sign = f"+{delta}" if delta > 0 else str(delta)
    meta = pulse_service._load_meta(res["checkin"])  # noqa: SLF001
    kb = pulse_service.build_wake_checklist_keyboard(int(checkin["id"]), meta, is_student=is_student_mode(user))
    bf_note = (
        f"\n🥣 Про завтрак мягко напомню в <b>{res['breakfast_scheduled_hhmm']}</b> (или запиши сразу кнопкой ниже)."
        if res.get("breakfast_scheduled_hhmm")
        else ""
    )
    p_tasks = list(res.get("pending_tasks") or [])
    tasks_note = (
        "\n\n📋 <b>Задачи на сегодня:</b>\n" + "\n".join(f"• {html.escape(t)}" for t in p_tasks[:4])
        if p_tasks
        else ""
    )
    text = (
        f"☀️ <b>Доброе утро! Подъём отмечен.</b>\n"
        f"Карма: <b>{sign}</b> → <b>{score}/100</b>.\n\n"
        f"Отметь воду и зубы:{bf_note}{tasks_note}"
    )
    await safe_edit(callback.message, text, reply_markup=kb)
    await _rebind_closed_card_reference(db, callback, res, keep_current=True)
    await callback.answer("Подъём отмечен!")


@router.callback_query(lambda c: c.data and c.data.startswith("pulse:snooze:"))
async def cb_pulse_snooze(callback: types.CallbackQuery, db) -> None:
    parts = callback.data.split(":")
    cid = int(parts[2])
    mins = int(parts[3]) if len(parts) > 3 else 15
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    current_pair = _callback_pair(callback)
    res = await pulse_service.snooze_checkin(
        db,
        user,
        cid,
        mins,
        card_chat_id=current_pair[0] if current_pair else None,
        card_message_id=current_pair[1] if current_pair else None,
    )
    if not res.get("ok"):
        await callback.answer(res.get("error", "Не найден чек-ин"), show_alert=True)
        return
    if await _handle_already_resolved(callback, res):
        return
    await _cleanup_previous_card(callback, res)
    karma = res["karma"]
    delta = int(karma.get("delta") or 0)
    penalty_note = (
        f"\n⚠️ Повторный перенос: <b>{delta} кармы</b> → {karma['score_after']}/100."
        if delta < 0
        else "\nПервый перенос без штрафа — но я реально вернусь в это время."
    )
    text = (
        f"⏰ <b>Ок, поставила честный таймер на +{res['minutes']} мин</b> (напомню в <b>{res['next_local_hhmm']}</b>)."
        f"{penalty_note}"
    )
    await safe_edit(callback.message, text, reply_markup=None)
    await callback.answer(f"Напомню в {res['next_local_hhmm']}")


@router.callback_query(lambda c: c.data and c.data.startswith("pulse:skip:"))
async def cb_pulse_skip(callback: types.CallbackQuery, db) -> None:
    cid = int(callback.data.split(":")[2])
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    res = await pulse_service.skip_checkin(db, user, cid)
    if not res.get("ok"):
        await callback.answer(res.get("error", "Не найден чек-ин"), show_alert=True)
        return
    if await _handle_already_resolved(callback, res):
        return
    await _cleanup_previous_card(callback, res)
    karma = res["karma"]
    text = (
        f"⏭ <b>Пропуск засчитан честно.</b>\n"
        f"Карма: <b>{karma['delta']}</b> → <b>{karma['score_after']}/100</b> (меньше штрафа, чем за полный игнор)."
    )
    await safe_edit(callback.message, text, reply_markup=None)
    await _rebind_closed_card_reference(db, callback, res, keep_current=False)
    await callback.answer("Пропущено.")


@router.callback_query(lambda c: c.data and c.data.startswith("pulse:hygiene:"))
async def cb_pulse_hygiene(callback: types.CallbackQuery, db) -> None:
    parts = callback.data.split(":")
    cid = int(parts[2])
    kind = parts[3] if len(parts) > 3 else "water"
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    res = await pulse_service.record_hygiene_tick(db, user, cid, kind)
    if not res.get("ok"):
        await callback.answer(res.get("error", "Ошибка"), show_alert=True)
        return

    checkin = await repo.get_pulse_checkin_by_id(db, cid)
    if checkin and checkin.get("slot_key") == "wake":
        kb = pulse_service.build_wake_checklist_keyboard(cid, res["meta"])
        try:
            await callback.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
    if res.get("already_marked"):
        await callback.answer("Уже отмечено ✅")
    else:
        await callback.answer(f"+{res['delta']} кармы ({res['score_after']}/100)!")


@router.callback_query(lambda c: c.data and c.data.startswith("pulse:rollover:"))
async def cb_pulse_rollover(callback: types.CallbackQuery, db) -> None:
    cid = int(callback.data.split(":")[2])
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    now = datetime.datetime.utcnow()
    tz = user.get("timezone") or "UTC"
    checkin = await repo.get_pulse_checkin_by_id(db, cid)
    from_date = str((checkin or {}).get("local_date") or local_date_str(now, tz))
    to_date = (datetime.date.fromisoformat(from_date) + datetime.timedelta(days=1)).isoformat()

    moved = await repo.rollover_unfinished_day_plan_items(db, int(user["id"]), from_date, to_date)
    if moved > 0:
        await karma_service.record_karma_event(
            db,
            user,
            domain="tasks",
            event_type="rollover",
            delta=2,
            reason=f"Аккуратно перенесены хвосты на завтра ({moved} шт.)",
            now_utc=now,
        )
        text = f"➡️ <b>Перенесла на завтра незавершённых дел: {moved}.</b>\nГолова чище — можешь спокойно сворачивать день."
        toast = f"Перенесено: {moved}"
    else:
        text = "✅ Незавершённых дел на сегодня уже не осталось."
        toast = "Хвостов нет"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🌙 Сон и карма", callback_data="pulse:hub")]]
    )
    await safe_edit(callback.message, text, reply_markup=kb)
    await callback.answer(toast)


@router.callback_query(lambda c: c.data and (c.data == "pulse:sick:prompt" or c.data.startswith("pulse:sick:prompt:")))
async def cb_pulse_sick_prompt(callback: types.CallbackQuery, db) -> None:
    parts = callback.data.split(":")
    origin = parts[3] if len(parts) > 3 else "hub"
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    text, kb = _build_sick_prompt(user, origin=origin)
    await safe_edit(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(lambda c: c.data and (c.data == "pulse:sick:confirm" or c.data.startswith("pulse:sick:confirm:")))
async def cb_pulse_sick_confirm(callback: types.CallbackQuery, db) -> None:
    parts = callback.data.split(":")
    origin = parts[3] if len(parts) > 3 else "hub"
    back_cb = "more:menu" if origin == "more" else "pulse:hub"
    back_label = "⬅️ Назад" if origin == "more" else "⬅️ В «Сон и карма»"
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    res = await karma_service.activate_sick_freeze(db, user)
    if not res.get("ok"):
        if res.get("limit_reached"):
            text = f"🚫 Лимит заморозки на этот месяц исчерпан ({res['used']}/{res['limit']})."
        else:
            text = f"🤒 Заморозка уже активна до {res.get('until_local') or 'завтра'}."
    else:
        text = (
            f"🤒 <b>Заморозка ЧП включена на 24 часа (до {res['until_local']}).</b>\n"
            f"Штрафы кармы и эскалация приостановлены. Осталось использований в этом месяце: {res['remaining']}/{res['limit']}."
        )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=back_label, callback_data=back_cb)]]
    )
    await safe_edit(callback.message, text, reply_markup=kb)
    await callback.answer("Обновлено.")


def _schedule_menu_payload(user: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    wd_w = pulse_service._normalize_hhmm(user.get("wake_up_time"), "08:00")  # noqa: SLF001
    wd_s = pulse_service._normalize_hhmm(user.get("sleep_time"), "23:00")  # noqa: SLF001
    we_w_raw = str(user.get("weekend_wake_up_time") or "").strip()
    we_s_raw = str(user.get("weekend_sleep_time") or "").strip()
    we_line = (
        f"• Выходные: подъём <b>{we_w_raw}</b> · отбой <b>{we_s_raw}</b>"
        if we_w_raw and we_s_raw
        else "• Выходные: совпадают с буднями"
    )
    text = (
        "<b>⏰ Расписание сна и подъёма</b>\n\n"
        f"• Будни: подъём <b>{wd_w}</b> · отбой <b>{wd_s}</b>\n"
        f"{we_line}\n\n"
        "Пульс дня автоматически ставит подъём и протокол отбоя (-60м и -15м) по этому графику."
    )
    rows = [
        [InlineKeyboardButton(text="✏️ Изменить будни и выходные", callback_data="pulse:schedule_edit")],
    ]
    if we_w_raw and we_s_raw:
        rows.append([InlineKeyboardButton(text="🔄 Сбросить выходные (= будни)", callback_data="pulse:schedule_clear_we")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="pulse:hub")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(lambda c: c.data == "pulse:schedule_menu")
async def cb_pulse_schedule_menu(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    text, kb = _schedule_menu_payload(user)
    await safe_edit(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(lambda c: c.data == "pulse:schedule_clear_we")
async def cb_pulse_schedule_clear_we(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.update_user_weekend_schedule(db, int(user["id"]), "", "")
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    text, kb = _schedule_menu_payload(user)
    await safe_edit(callback.message, text, reply_markup=kb)
    await callback.answer("Выходные сброшены.")


@router.callback_query(lambda c: c.data == "pulse:schedule_edit")
async def cb_pulse_schedule_edit(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PulseScheduleState.wd_wake)
    await callback.message.answer(
        "Во сколько подъём в <b>будни</b>? (формат HH:MM, например 07:00)",
        reply_markup=_cancel_kb(),
    )
    await callback.answer()


async def _check_schedule_cancel(message: types.Message, state: FSMContext) -> bool:
    if (message.text or "").strip().lower() in {"отмена", "назад"}:
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu_keyboard())
        return True
    return False


@router.message(PulseScheduleState.wd_wake, lambda m: bool(m.text and not m.text.startswith("/")))
async def fsm_wd_wake(message: types.Message, state: FSMContext) -> None:
    if await _check_schedule_cancel(message, state):
        return
    parsed = parse_hhmm((message.text or "").strip())
    if not parsed:
        await message.answer("Нужно время в формате HH:MM (например 07:30).", reply_markup=_cancel_kb())
        return
    await state.update_data(wd_wake=parsed.strftime("%H:%M"))
    await state.set_state(PulseScheduleState.wd_sleep)
    await message.answer("Во сколько отбой в <b>будни</b>? (формат HH:MM, например 23:30)", reply_markup=_cancel_kb())


@router.message(PulseScheduleState.wd_sleep, lambda m: bool(m.text and not m.text.startswith("/")))
async def fsm_wd_sleep(message: types.Message, state: FSMContext) -> None:
    if await _check_schedule_cancel(message, state):
        return
    parsed = parse_hhmm((message.text or "").strip())
    if not parsed:
        await message.answer("Нужно время в формате HH:MM (например 23:30).", reply_markup=_cancel_kb())
        return
    await state.update_data(wd_sleep=parsed.strftime("%H:%M"))
    await state.set_state(PulseScheduleState.we_wake)
    await message.answer(
        "Во сколько подъём в <b>выходные</b>? (HH:MM или напиши «как в будни»)",
        reply_markup=_cancel_kb(),
    )


@router.message(PulseScheduleState.we_wake, lambda m: bool(m.text and not m.text.startswith("/")))
async def fsm_we_wake(message: types.Message, state: FSMContext, db) -> None:
    if await _check_schedule_cancel(message, state):
        return
    raw = (message.text or "").strip()
    if raw.lower() in {"как в будни", "нет", "совпадает"}:
        data = await state.get_data()
        user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
        await repo.update_user_schedule(db, int(user["id"]), data["wd_wake"], data["wd_sleep"])
        await repo.update_user_weekend_schedule(db, int(user["id"]), "", "")
        await state.clear()
        await message.answer("Сохранила график (выходные как будни).", reply_markup=main_menu_keyboard())
        await send_pulse_hub(message, db)
        return
    parsed = parse_hhmm(raw)
    if not parsed:
        await message.answer("Нужно время в формате HH:MM или «как в будни».", reply_markup=_cancel_kb())
        return
    await state.update_data(we_wake=parsed.strftime("%H:%M"))
    await state.set_state(PulseScheduleState.we_sleep)
    await message.answer("Во сколько отбой в <b>выходные</b>? (формат HH:MM, например 00:30)", reply_markup=_cancel_kb())


@router.message(PulseScheduleState.we_sleep, lambda m: bool(m.text and not m.text.startswith("/")))
async def fsm_we_sleep(message: types.Message, state: FSMContext, db) -> None:
    if await _check_schedule_cancel(message, state):
        return
    parsed = parse_hhmm((message.text or "").strip())
    if not parsed:
        await message.answer("Нужно время в формате HH:MM (например 00:30).", reply_markup=_cancel_kb())
        return
    data = await state.get_data()
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.update_user_schedule(db, int(user["id"]), data["wd_wake"], data["wd_sleep"])
    await repo.update_user_weekend_schedule(db, int(user["id"]), data["we_wake"], parsed.strftime("%H:%M"))
    await state.clear()
    await message.answer("График будней и выходных сохранён!", reply_markup=main_menu_keyboard())
    await send_pulse_hub(message, db)


def _toggles_payload(user: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    pulse_on = bool(user.get("pulse_enabled", 1))
    legacy_on = bool(user.get("legacy_pings_enabled", 0))
    text = (
        "<b>🔔 Настройки Пульса дня</b>\n\n"
        f"• Единый Пульс дня (5 чекпоинтов + эскалация + карма): {'✅ Включён' if pulse_on else '⏸ Выключен'}\n"
        f"• Старые авто-пинги (аффирмации, щётка, отдельные вода/еда): {'✅ Включены' if legacy_on else '🔕 Выключены'}\n\n"
        "<i>Личные /custom напоминания, лекарства, счета и фокус-сессии работают всегда независимо от этих флагов.</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{'⏸ Выключить' if pulse_on else '✅ Включить'} Пульс дня",
                    callback_data="pulse:toggle:pulse",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{'🔕 Выключить' if legacy_on else '🔔 Включить'} старые пинги",
                    callback_data="pulse:toggle:legacy",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="pulse:hub")],
        ]
    )
    return text, kb


@router.callback_query(lambda c: c.data == "pulse:toggles_menu")
async def cb_pulse_toggles_menu(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    text, kb = _toggles_payload(user)
    await safe_edit(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("pulse:toggle:"))
async def cb_pulse_toggle(callback: types.CallbackQuery, db) -> None:
    which = callback.data.split(":")[2]
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    if which == "pulse":
        new_val = not bool(user.get("pulse_enabled", 1))
        await repo.update_user_pulse_flags(db, int(user["id"]), pulse_enabled=new_val)
    elif which == "legacy":
        new_val = not bool(user.get("legacy_pings_enabled", 0))
        await repo.update_user_pulse_flags(db, int(user["id"]), legacy_pings_enabled=new_val)
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    text, kb = _toggles_payload(user)
    await safe_edit(callback.message, text, reply_markup=kb)
    await callback.answer("Обновлено.")


@router.callback_query(lambda c: c.data == "pulse:history")
async def cb_pulse_history(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    events = await repo.list_karma_events(db, int(user["id"]), limit=10)
    if not events:
        text = "<b>📜 История кармы</b>\n\nПока событий ещё нет — закрой первый чек-ин!"
    else:
        lines = ["<b>📜 Последние события кармы</b>\n"]
        for ev in events:
            delta = int(ev.get("delta") or 0)
            sign = f"{delta:+d}" if delta != 0 else "0"
            lines.append(
                f"• {html.escape(str(ev.get('local_date') or ''))} · <code>{sign}</code> "
                f"{html.escape(str(ev.get('reason') or ''))} → <b>{int(ev.get('score_after') or 0)}/100</b>"
            )
        text = "\n".join(lines)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="pulse:hub")]]
    )
    await safe_edit(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(lambda c: c.data == "pulse:test_ping")
async def cb_pulse_test_ping(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    result = await pulse_service.send_test_pulse_ping(db, callback.message.bot, user)
    if isinstance(result, dict) and result.get("sent") is False:
        await callback.answer("Не удалось отправить пробный пинг.", show_alert=True)
        return
    await callback.answer("Пробный пинг отправлен!")


@router.callback_query(lambda c: c.data in {"pulse:portrait", "pulse:portrait_refresh"})
async def cb_pulse_portrait(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    force = callback.data == "pulse:portrait_refresh"
    memory = await context_builder.refresh_user_portrait_if_needed(db, user, force=force)
    karma_state = await repo.get_or_create_karma_state(db, int(user["id"]))
    text = context_builder.format_portrait_card(user, memory, karma_state)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить портрет", callback_data="pulse:portrait_refresh"),
                InlineKeyboardButton(text="💡 Совет сейчас", callback_data="advice:more"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="pulse:hub")],
        ]
    )
    await safe_edit(callback.message, text, reply_markup=kb)
    await callback.answer("Портрет обновлён." if force else "")
