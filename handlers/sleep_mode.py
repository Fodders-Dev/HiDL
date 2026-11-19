from __future__ import annotations

import datetime

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from services import karma_service, pulse_service
from utils.time import parse_hhmm, local_date_str
from utils.texts import error
from utils.user import ensure_user
from utils.sender import safe_edit

router = Router()


class SleepModeState(StatesGroup):
    target_sleep = State()
    target_wake = State()


def _sleep_menu_text(user: dict) -> str:
    enabled = bool(user.get("sleep_mode_enabled"))
    status = "включен" if enabled else "выключен"
    current_sleep = user.get("sleep_time") or "23:00"
    current_wake = user.get("wake_up_time") or "08:00"
    target_sleep = user.get("sleep_target_sleep")
    target_wake = user.get("sleep_target_wake")
    if target_sleep and target_wake:
        target_line = f"Цель: {target_sleep}–{target_wake}"
    else:
        target_line = "Цель: не задана"
    step = int(user.get("sleep_shift_step") or 30)
    every = int(user.get("sleep_shift_every") or 2)
    return (
        f"😴 Режим сна: {status}\n"
        f"Сейчас: {current_sleep}–{current_wake}\n"
        f"{target_line}\n"
        f"Сдвиг: {step} мин каждые {every} дн."
    )


def _sleep_menu_keyboard(enabled: bool, origin: str) -> InlineKeyboardMarkup:
    rows = []
    if enabled:
        rows.append([InlineKeyboardButton(text="🛑 Выключить", callback_data=f"sleep:toggle:{origin}")])
        rows.append([InlineKeyboardButton(text="⏰ Изменить время", callback_data=f"sleep:setup:{origin}")])
    else:
        rows.append([InlineKeyboardButton(text="✅ Включить", callback_data=f"sleep:toggle:{origin}")])
        rows.append([InlineKeyboardButton(text="⏰ Настроить время", callback_data=f"sleep:setup:{origin}")])
    back = "settings:main" if origin == "settings" else "main:menu"
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _sleep_cancel_keyboard(origin: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"sleep:menu:{origin}")]
        ]
    )


async def _handle_sleep_cancel(message: types.Message, state: FSMContext) -> bool:
    """Сбросить FSM режима сна при текстовой отмене."""
    if (message.text or "").strip().lower() in {"отмена", "назад"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return True
    return False


async def _render_sleep_menu(
    message: types.Message,
    db,
    origin: str,
    tg_user_id: int | None = None,
    tg_full_name: str | None = None,
) -> None:
    uid = tg_user_id if tg_user_id is not None else message.from_user.id
    fname = tg_full_name if tg_full_name is not None else message.from_user.full_name
    user = await ensure_user(db, uid, fname)
    text = _sleep_menu_text(user)
    kb = _sleep_menu_keyboard(bool(user.get("sleep_mode_enabled")), origin)
    await safe_edit(message, text, reply_markup=kb)


@router.message(Command("sleep"))
async def sleep_menu_command(message: types.Message, state: FSMContext, db) -> None:
    await state.clear()
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    text = _sleep_menu_text(user)
    kb = _sleep_menu_keyboard(bool(user.get("sleep_mode_enabled")), origin="main")
    await message.answer(text, reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("sleep:menu"))
async def sleep_menu_callback(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await state.clear()
    parts = callback.data.split(":")
    origin = parts[2] if len(parts) > 2 else "main"
    await _render_sleep_menu(
        callback.message,
        db,
        origin=origin,
        tg_user_id=callback.from_user.id,
        tg_full_name=callback.from_user.full_name,
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("sleep:setup"))
async def sleep_setup_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    origin = parts[2] if len(parts) > 2 else "main"
    await state.update_data(sleep_origin=origin)
    await state.set_state(SleepModeState.target_sleep)
    await callback.message.answer(
        "Во сколько хочешь засыпать? Формат HH:MM.",
        reply_markup=_sleep_cancel_keyboard(origin),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("sleep:toggle"))
async def sleep_toggle_callback(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    parts = callback.data.split(":")
    origin = parts[2] if len(parts) > 2 else "main"
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    enabled = bool(user.get("sleep_mode_enabled"))
    if not enabled:
        if not user.get("sleep_target_sleep") or not user.get("sleep_target_wake"):
            await state.update_data(sleep_origin=origin)
            await state.set_state(SleepModeState.target_sleep)
            await callback.message.answer(
                "Чтобы включить режим, задай цель. Во сколько хочешь засыпать?",
                reply_markup=_sleep_cancel_keyboard(origin),
            )
            await callback.answer()
            return
        await repo.update_sleep_mode(db, user["id"], True)
        local_date = local_date_str(datetime.datetime.utcnow(), user.get("timezone", "UTC"))
        shift_step = int(user.get("sleep_shift_step") or 30)
        shift_every = int(user.get("sleep_shift_every") or 2)
        await repo.update_sleep_shift_settings(db, user["id"], shift_step, shift_every, local_date)
        await callback.answer("Режим сна включен.")
    else:
        await repo.update_sleep_mode(db, user["id"], False)
        await callback.answer("Режим сна выключен.")
    await _render_sleep_menu(
        callback.message,
        db,
        origin=origin,
        tg_user_id=callback.from_user.id,
        tg_full_name=callback.from_user.full_name,
    )


@router.message(SleepModeState.target_sleep, lambda m: m.text and not m.text.startswith("/"))
async def sleep_set_target_sleep(message: types.Message, state: FSMContext) -> None:
    if await _handle_sleep_cancel(message, state):
        return
    data = await state.get_data()
    origin = data.get("sleep_origin", "main")
    raw = (message.text or "").strip()
    parsed = parse_hhmm(raw)
    if not parsed:
        await message.answer(
            error("нужен формат HH:MM, например 23:30"),
            reply_markup=_sleep_cancel_keyboard(origin),
        )
        return
    await state.update_data(target_sleep=parsed.strftime("%H:%M"))
    await state.set_state(SleepModeState.target_wake)
    await message.answer(
        "Во сколько хочешь вставать? Формат HH:MM.",
        reply_markup=_sleep_cancel_keyboard(origin),
    )


@router.message(SleepModeState.target_wake, lambda m: m.text and not m.text.startswith("/"))
async def sleep_set_target_wake(message: types.Message, state: FSMContext, db) -> None:
    if await _handle_sleep_cancel(message, state):
        return
    data = await state.get_data()
    origin = data.get("sleep_origin", "main")
    raw = (message.text or "").strip()
    parsed = parse_hhmm(raw)
    if not parsed:
        await message.answer(
            error("нужен формат HH:MM, например 07:30"),
            reply_markup=_sleep_cancel_keyboard(origin),
        )
        return
    target_sleep = data.get("target_sleep")
    target_wake = parsed.strftime("%H:%M")
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.update_sleep_targets(db, user["id"], target_sleep, target_wake)
    await repo.update_sleep_mode(db, user["id"], True)
    local_date = local_date_str(datetime.datetime.utcnow(), user.get("timezone", "UTC"))
    shift_step = int(user.get("sleep_shift_step") or 30)
    shift_every = int(user.get("sleep_shift_every") or 2)
    await repo.update_sleep_shift_settings(db, user["id"], shift_step, shift_every, local_date)
    await state.clear()
    await message.answer("Приняла. Подстрою режим постепенно и без резких рывков.")
    updated_user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await message.answer(
        _sleep_menu_text(updated_user),
        reply_markup=_sleep_menu_keyboard(bool(updated_user.get("sleep_mode_enabled")), origin),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("sleep:evening"))
async def sleep_evening_ack(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    action = callback.data.split(":")[2] if len(callback.data.split(":")) > 2 else "ok"
    if action == "later":
        snooze = await pulse_service.schedule_adhoc_snooze(
            db, user, "bedtime", "sleep", 20, "Отбой"
        )
        await safe_edit(
            callback.message,
            f"⏰ Ок, напомню про отбой в {snooze['next_local_hhmm']} (+20 мин).",
            reply_markup=None,
        )
        await callback.answer(f"Напомню в {snooze['next_local_hhmm']}")
        return
    res = await pulse_service.confirm_bedtime(db, user)
    await safe_edit(
        callback.message,
        f"🌙 Отбой подтверждён. Карма: {res['karma']['score_after']}/100. Спокойной ночи!",
        reply_markup=None,
    )
    await callback.answer("Хорошо, отдыхай.")


@router.callback_query(lambda c: c.data and c.data.startswith("sleep:morning"))
async def sleep_morning_ack(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    action = callback.data.split(":")[2] if len(callback.data.split(":")) > 2 else "up"
    if action == "snooze":
        snooze = await pulse_service.schedule_adhoc_snooze(
            db, user, "wake", "sleep", 10, "Подъём"
        )
        await safe_edit(
            callback.message,
            f"⏰ Повторный подъём в {snooze['next_local_hhmm']} (+10 мин). Только не проспи!",
            reply_markup=None,
        )
        await callback.answer(f"Разбужу в {snooze['next_local_hhmm']}")
        return
    await repo.clear_user_sleep_confirmed(db, int(user["id"]))
    k_res = await karma_service.record_karma_event(
        db,
        user,
        domain="sleep",
        event_type="wake_confirmed",
        delta=4,
        reason="Подтверждён подъём утром",
    )
    await safe_edit(
        callback.message,
        f"☀️ Отлично, доброе утро! +4 кармы ({k_res['score_after']}/100).",
        reply_markup=None,
    )
    await callback.answer("Доброе утро!")
