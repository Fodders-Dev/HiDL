import datetime

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.time import is_valid_timezone, parse_hhmm
from utils.user import ensure_user

router = Router()


class SettingsState(StatesGroup):
    timezone = State()
    wake = State()
    sleep = State()
    goals = State()
    routine_time = State()


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Часовой пояс", callback_data="settings:tz")],
            [InlineKeyboardButton(text="Подъём", callback_data="settings:wake")],
            [InlineKeyboardButton(text="Отбой", callback_data="settings:sleep")],
            [InlineKeyboardButton(text="Цель/приоритет", callback_data="settings:goals")],
            [InlineKeyboardButton(text="Профиль питания", callback_data="settings:mealprof")],
            [InlineKeyboardButton(text="ADHD-режим", callback_data="settings:adhd")],
            [
                InlineKeyboardButton(
                    text="Время: утро", callback_data="settings:rt:morning"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Время: день", callback_data="settings:rt:day"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Время: вечер", callback_data="settings:rt:evening"
                )
            ],
        ]
    )


@router.message(Command("settings"))
async def settings_entry(message: types.Message, state: FSMContext, db) -> None:
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await state.clear()
    await message.answer(
        "Твои текущие настройки:\n"
        f"• Имя: {user['name']}\n"
        f"• Часовой пояс: {user['timezone']}\n"
        f"• Подъём: {user['wake_up_time']} / Отбой: {user['sleep_time']}\n"
        f"• Цель/приоритет: {user['goals'] or 'не задано'}\n\n"
        "Что поменяем? Выбери ниже.",
        reply_markup=settings_keyboard(),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("settings:"))
async def settings_select(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    parts = callback.data.split(":")
    if len(parts) < 2:
        await callback.answer()
        return
    action = parts[1]
    await state.clear()
    if action == "tz":
        await state.set_state(SettingsState.timezone)
        await callback.message.answer(
            "Сколько у тебя сейчас времени? Напиши в формате HH:MM (я сама посчитаю смещение).\n"
            "Если хочешь задать вручную — пришли таймзону вроде Europe/Moscow или UTC+3."
        )
    elif action == "wake":
        await state.set_state(SettingsState.wake)
        await callback.message.answer("Новый подъём? Формат HH:MM, например 07:30.")
    elif action == "sleep":
        await state.set_state(SettingsState.sleep)
        await callback.message.answer("Новый отбой? Формат HH:MM, например 23:30.")
    elif action == "goals":
        await state.set_state(SettingsState.goals)
        await callback.message.answer("Коротко опиши приоритет или цель (одно сообщение).")
    elif action == "mealprof":
        if len(parts) >= 3 and parts[2] == "set":
            await callback.answer()
            return
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Обычный", callback_data="settings:mealprof:set:omnivore"),
                    InlineKeyboardButton(text="Вегетарианец", callback_data="settings:mealprof:set:vegetarian"),
                    InlineKeyboardButton(text="Веган", callback_data="settings:mealprof:set:vegan"),
                ]
            ]
        )
        await callback.message.answer("Выбери профиль питания:", reply_markup=kb)
    elif action == "mealprof" and "set" in callback.data:
        # handled by separate handler
        pass
    elif action == "adhd":
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        enabled = not bool(user.get("adhd_mode"))
        await repo.toggle_adhd(db, user["id"], enabled)
        text = "ADHD-режим включён: буду показывать только 3–5 пунктов и дробить задачи." if enabled else "ADHD-режим выключен."
        await callback.message.answer(text, reply_markup=main_menu_keyboard())
        await callback.answer("Обновлено")
        return
    elif action == "rt" and len(parts) >= 3:
        routine_key = parts[2]
        await state.update_data(routine_key=routine_key)
        await state.set_state(SettingsState.routine_time)
        await callback.message.answer(
            f"Новое время для {routine_key} (HH:MM, например 07:30)."
        )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("settings:mealprof:set:"))
async def settings_meal_profile(callback: types.CallbackQuery, db) -> None:
    _, _, _, profile = callback.data.split(":")
    from utils.user import ensure_user
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.upsert_wellness(db, user["id"], meal_profile=profile)
    label = {"omnivore": "Обычный", "vegetarian": "Вегетарианец", "vegan": "Веган"}.get(profile, profile)
    await callback.message.answer(f"Профиль питания обновлён: {label}.", reply_markup=main_menu_keyboard())
    await callback.answer("Сохранено")


@router.message(SettingsState.timezone)
async def settings_timezone(message: types.Message, state: FSMContext, db) -> None:
    tz = message.text.strip()
    computed_tz = None
    if not is_valid_timezone(tz):
        # попробуем трактовать как текущее локальное время пользователя
        hhmm = parse_hhmm(tz)
        if hhmm:
            try:
                now_utc = datetime.datetime.utcnow()
                hh, mm = map(int, hhmm.split(":"))
                today = now_utc.date()
                local_dt = datetime.datetime.combine(today, datetime.time(hour=hh, minute=mm))
                utc_dt = datetime.datetime.combine(today, datetime.time(hour=now_utc.hour, minute=now_utc.minute))
                offset_minutes = int((local_dt - utc_dt).total_seconds() // 60)
                # нормализуем в диапазон -720..+720
                if offset_minutes > 720:
                    offset_minutes -= 1440
                if offset_minutes < -720:
                    offset_minutes += 1440
                sign = "+" if offset_minutes >= 0 else "-"
                hrs = abs(offset_minutes) // 60
                mins = abs(offset_minutes) % 60
                computed_tz = f"UTC{sign}{hrs}"
                if mins:
                    computed_tz += f":{mins:02d}"
                tz = computed_tz
            except Exception:
                pass
        if not computed_tz:
            await message.answer("Не поняла. Можно прислать текущее время (HH:MM) или таймзону вида Europe/Moscow, UTC+3.")
            return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.update_user_timezone(db, user["id"], tz)
    await state.clear()
    await message.answer(
        f"Часовой пояс обновлён на {tz}.", reply_markup=main_menu_keyboard()
    )


@router.message(SettingsState.wake)
async def settings_wake(message: types.Message, state: FSMContext, db) -> None:
    time_value = parse_hhmm(message.text.strip())
    if not time_value:
        await message.answer("Не распознал время. Формат HH:MM, например 07:30.")
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.update_user_wake(db, user["id"], message.text.strip())
    await state.clear()
    await message.answer(
        f"Время подъёма обновлено: {message.text.strip()}.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(SettingsState.sleep)
async def settings_sleep(message: types.Message, state: FSMContext, db) -> None:
    time_value = parse_hhmm(message.text.strip())
    if not time_value:
        await message.answer("Не распознал время. Формат HH:MM, например 23:30.")
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.update_user_sleep(db, user["id"], message.text.strip())
    await state.clear()
    await message.answer(
        f"Время отбоя обновлено: {message.text.strip()}.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(SettingsState.goals)
async def settings_goals(message: types.Message, state: FSMContext, db) -> None:
    text = message.text.strip()
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.update_user_goals(db, user["id"], text)
    await state.clear()
    await message.answer(
        "Цель обновлена. Я буду учитывать это в подсказках.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(SettingsState.routine_time)
async def settings_routine_time(message: types.Message, state: FSMContext, db) -> None:
    hhmm = message.text.strip()
    if not parse_hhmm(hhmm):
        await message.answer("Не распознал время. Формат HH:MM, например 07:30.")
        return
    data = await state.get_data()
    routine_key = data.get("routine_key")
    if not routine_key:
        await message.answer("Не понял, какую рутину менять. Выбери снова /settings.")
        await state.clear()
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.update_user_routine_time(db, user["id"], routine_key, hhmm)
    await state.clear()
    await message.answer(
        f"Время напоминания для {routine_key} обновлено: {hhmm}.",
        reply_markup=main_menu_keyboard(),
    )
