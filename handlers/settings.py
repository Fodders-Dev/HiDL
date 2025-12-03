import datetime

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils import texts
from utils.time import is_valid_timezone, parse_hhmm
from utils.user import ensure_user

router = Router()


class SettingsState(StatesGroup):
    timezone = State()
    wake = State()
    sleep = State()
    goals = State()
    routine_time = State()
    expiry = State()
    household_join = State()


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Часовой пояс", callback_data="settings:tz")],
            [InlineKeyboardButton(text="Подъём", callback_data="settings:wake")],
            [InlineKeyboardButton(text="Отбой", callback_data="settings:sleep")],
            [InlineKeyboardButton(text="Цель/приоритет", callback_data="settings:goals")],
            [InlineKeyboardButton(text="Профиль питания", callback_data="settings:mealprof")],
            [InlineKeyboardButton(text="Аффирмации", callback_data="settings:affirm")],
            [InlineKeyboardButton(text="Срок «скоро истечёт»", callback_data="settings:expiry")],
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
            [InlineKeyboardButton(text="Общий дом", callback_data="settings:household")],
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
    elif action == "household":
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        from db import repositories as repo_mod

        await callback.message.answer(
            "Общий дом — это когда несколько человек делят одну кладовку и бытовую химию.\n\n"
            "Можно создать дом и дать код партнёру, либо присоединиться по коду.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🏠 Создать общий дом",
                            callback_data="settings:household_create",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="🔗 Присоединиться по коду",
                            callback_data="settings:household_join",
                        )
                    ],
                ]
            ),
        )
    elif action == "household_create":
        from db import repositories as repo_mod

        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        household_id = await repo_mod.get_or_create_household(db, user["id"])
        # достанем код
        cursor = await db.execute(
            "SELECT invite_code FROM households WHERE id = ?", (household_id,)
        )
        row = await cursor.fetchone()
        code = row["invite_code"] if row and row["invite_code"] else f"H{user['id']}"
        await callback.message.answer(
            "Создала общий дом. Передай партнёру этот код, чтобы он присоединился:\n"
            f"`{code}`",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(),
        )
    elif action == "household_join":
        await state.set_state(SettingsState.household_join)
        await callback.message.answer(
            "Пришли код дома, который дал тебе партнёр. Я попробую подключить тебя к тому же пространству.",
        )
    elif action == "affirm":
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        wellness = await repo.get_wellness(db, user["id"])
        current = (wellness or {}).get("affirm_mode", "off")
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text=("✅ Выкл" if current == "off" else "Выкл"), callback_data="settings:affirm:set:off"),
                    InlineKeyboardButton(text=("✅ Утром" if current == "morning" else "Утром"), callback_data="settings:affirm:set:morning"),
                ],
                [
                    InlineKeyboardButton(text=("✅ Вечером" if current == "evening" else "Вечером"), callback_data="settings:affirm:set:evening"),
                    InlineKeyboardButton(text=("✅ Утром и вечером" if current == "both" else "Утром и вечером"), callback_data="settings:affirm:set:both"),
                ],
            ]
        )
        await callback.message.answer(
            "Могу иногда подкидывать короткую фразу поддержки.\n"
            "Выбери, когда присылать аффирмации:",
            reply_markup=kb,
        )
    elif action == "expiry":
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        wellness = await repo.get_wellness(db, user["id"])
        current_days = int((wellness or {}).get("expiring_window_days", 3))
        await state.set_state(SettingsState.expiry)
        await callback.message.answer(
            "Через сколько дней до конца срока считать, что продукт «скоро испортится»?\n"
            f"Сейчас: около {current_days} дн.\n"
            "Введи целое число от 1 до 30, например 3 или 5.",
        )
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


@router.callback_query(lambda c: c.data and c.data.startswith("settings:affirm:set:"))
async def settings_affirm_mode(callback: types.CallbackQuery, db) -> None:
    _, _, _, mode = callback.data.split(":")
    if mode not in {"off", "morning", "evening", "both"}:
        await callback.answer()
        return
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.upsert_wellness(db, user["id"], affirm_mode=mode)
    labels = {
        "off": "выключены",
        "morning": "только утром",
        "evening": "только вечером",
        "both": "утром и вечером",
    }
    await callback.message.answer(
        f"Аффирмации теперь {labels[mode]}. Если станет слишком много — всегда можно вернуть режим «выкл».",
        reply_markup=main_menu_keyboard(),
    )
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
        await message.answer(
            texts.error("не распознала время. Формат HH:MM, например 07:30."),
        )
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
        await message.answer(
            texts.error("не распознала время. Формат HH:MM, например 23:30."),
        )
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


@router.message(SettingsState.expiry)
async def settings_expiry(message: types.Message, state: FSMContext, db) -> None:
    raw = message.text.strip()
    try:
        days = int(raw)
        if days < 1 or days > 30:
            raise ValueError
    except Exception:
        await message.answer(
            texts.error("нужно целое число дней от 1 до 30, например 3 или 5."),
        )
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.upsert_wellness(db, user["id"], expiring_window_days=days)
    await state.clear()
    await message.answer(
        f"Хорошо, буду считать, что продукт «скоро испортится», если до конца срока осталось ≤ {days} дн.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(SettingsState.routine_time)
async def settings_routine_time(message: types.Message, state: FSMContext, db) -> None:
    hhmm = message.text.strip()
    if not parse_hhmm(hhmm):
        await message.answer(
            texts.error("не распознала время. Формат HH:MM, например 07:30."),
        )
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


@router.message(SettingsState.household_join)
async def settings_household_join(message: types.Message, state: FSMContext, db) -> None:
    code = (message.text or "").strip()
    if not code:
        await message.answer(
            "Код пустой. Пришли, пожалуйста, код, который тебе дал партнёр (буквы и цифры)."
        )
        return
    from db import repositories as repo_mod

    household = await repo_mod.get_household_by_code(db, code)
    if not household:
        await message.answer(
            "Я не нашла дом с таким кодом. Проверь, не перепутались ли буквы/цифры, или попроси партнёра показать код ещё раз.",
            reply_markup=main_menu_keyboard(),
        )
        await state.clear()
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo_mod.set_user_household(db, user["id"], household["id"])
    await state.clear()
    await message.answer(
        "Подключила тебя к общему дому. Теперь кладовка продуктов и бытовая химия будут общими для вас.",
        reply_markup=main_menu_keyboard(),
    )
