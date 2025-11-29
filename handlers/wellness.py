import asyncio
import datetime

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils import texts
from utils.texts import register_text
from utils.time import local_date_str, parse_hhmm
from utils.user import ensure_user

router = Router()
FOCUS_EDIT_REQUESTS = {}
TIME_EDIT_REQUESTS = {}


def wellness_keyboard(current) -> InlineKeyboardMarkup:
    water = "✅" if current.get("water_enabled") else "❌"
    meal = "✅" if current.get("meal_enabled") else "❌"
    focus = "✅" if current.get("focus_mode") else "❌"
    work = current.get("focus_work", 20)
    rest = current.get("focus_rest", 10)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Вода {water}", callback_data="well:water")],
            [InlineKeyboardButton(text=f"Приём пищи {meal}", callback_data="well:meal")],
            [
                InlineKeyboardButton(text=f"Фокус {work}/{rest} {focus}", callback_data="well:focus"),
                InlineKeyboardButton(text="Сменить интервалы", callback_data="well:focus_edit"),
            ],
            [
                InlineKeyboardButton(text=f"Вода часы: {current.get('water_times','')}", callback_data="well:water_times"),
            ],
            [
                InlineKeyboardButton(text=f"Еда часы: {current.get('meal_times','')}", callback_data="well:meal_times"),
            ],
            [
                InlineKeyboardButton(text="Пресет воды 1x", callback_data="well:water_preset:1"),
                InlineKeyboardButton(text="Пресет воды 2x", callback_data="well:water_preset:2"),
            ],
            [
                InlineKeyboardButton(text="Пресет еды 2x", callback_data="well:meal_preset:2"),
                InlineKeyboardButton(text="Пресет еды 3x", callback_data="well:meal_preset:3"),
            ],
        ]
    )


@router.message(Command("wellness"))
async def wellness_settings(message: types.Message, db) -> None:
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    row = await repo.get_wellness(db, user["id"])
    current = dict(row) if row else {"water_enabled": 0, "meal_enabled": 0, "focus_mode": 0}
    await message.answer(
        "Напоминания здоровья:\n"
        f"• Вода: {'вкл' if current.get('water_enabled') else 'выкл'}\n"
        f"• Приём пищи: {'вкл' if current.get('meal_enabled') else 'выкл'}\n"
        f"• Фокус-таймер {current.get('focus_work', 20)}/{current.get('focus_rest', 10)}: {'вкл' if current.get('focus_mode') else 'выкл'}\n"
        "Переключи нужное:",
        reply_markup=wellness_keyboard(current),
    )


@router.message(lambda m: m.text and m.from_user.id in FOCUS_EDIT_REQUESTS)
async def focus_edit_input(message: types.Message, db) -> None:
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Нужно два числа: работа отдых, например 20 10 или 10 5.")
        return
    try:
        work = int(parts[0])
        rest = int(parts[1])
        if work < 2 or rest < 1:
            raise ValueError
    except Exception:
        await message.answer("Нужно два целых числа (работа отдых), минимум 2 и 1.")
        return
    user = await repo.get_user_by_telegram_id(db, message.from_user.id)
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.upsert_wellness(db, user["id"], focus_work=work, focus_rest=rest)
    FOCUS_EDIT_REQUESTS.pop(message.from_user.id, None)
    await message.answer(
        f"Установила интервалы {work}/{rest}. Включи фокус в настройках здоровья (кнопка «Вода/Еда/Фокус»).",
        reply_markup=main_menu_keyboard(),
    )


@router.message(lambda m: m.text and "щад" in m.text.lower())
async def gentle_button(message: types.Message, db) -> None:
    # Redirect to gentle handler behavior
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    now_utc = datetime.datetime.utcnow()
    local_date = local_date_str(now_utc, user["timezone"])
    await repo.set_user_pause(db, user["id"], local_date)
    await message.answer(
        texts.GENTLE_ON,
        reply_markup=main_menu_keyboard(),
    )


@router.message(lambda m: m.from_user.id in TIME_EDIT_REQUESTS)
async def time_edit_input(message: types.Message, db) -> None:
    req = TIME_EDIT_REQUESTS.get(message.from_user.id)
    if not req:
        return
    kind, _ = req
    raw = message.text.strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts or any(not parse_hhmm(p) for p in parts):
        await message.answer("Формат: HH:MM,HH:MM ... Пример: 11:00,16:00")
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    if kind == "water":
        await repo.upsert_wellness(db, user["id"], water_times=",".join(parts))
    elif kind == "meal":
        await repo.upsert_wellness(db, user["id"], meal_times=",".join(parts))
    TIME_EDIT_REQUESTS.pop(message.from_user.id, None)
    await message.answer("Часы обновлены.", reply_markup=main_menu_keyboard())


@router.callback_query(lambda c: c.data and c.data.startswith("well:"))
async def wellness_toggle(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    row = await repo.get_wellness(db, user["id"])
    current = dict(row) if row else {"water_enabled": 0, "meal_enabled": 0, "focus_mode": 0}
    action = callback.data.split(":")[1]
    if action == "water":
        await repo.upsert_wellness(
            db, user["id"], water_enabled=0 if current.get("water_enabled") else 1
        )
    elif action == "meal":
        await repo.upsert_wellness(
            db, user["id"], meal_enabled=0 if current.get("meal_enabled") else 1
        )
    elif action == "focus":
        await repo.upsert_wellness(
            db, user["id"], focus_mode=0 if current.get("focus_mode") else 1
        )
    elif action == "focus_edit":
        await callback.message.answer(
            "Введи интервалы через пробел (минуты): работа отдых. Пример: 20 10 или 10 5.",
            reply_markup=None,
        )
        await callback.answer()
        FOCUS_EDIT_REQUESTS[callback.from_user.id] = callback.message.chat.id
        return
    elif action == "water_times":
        await callback.message.answer(
            "Введи часы для воды через запятую в формате HH:MM, например: 11:00,16:00",
            reply_markup=None,
        )
        TIME_EDIT_REQUESTS[callback.from_user.id] = ("water", callback.message.chat.id)
        await callback.answer()
        return
    elif action == "meal_times":
        await callback.message.answer(
            "Введи часы для еды через запятую в формате HH:MM, например: 13:00,19:00",
            reply_markup=None,
        )
        TIME_EDIT_REQUESTS[callback.from_user.id] = ("meal", callback.message.chat.id)
        await callback.answer()
        return
    elif action.startswith("water_preset"):
        preset = action.split(":")[-1]
        times = "11:00" if preset == "1" else "11:00,16:00"
        await repo.upsert_wellness(db, user["id"], water_times=times)
    elif action.startswith("meal_preset"):
        preset = action.split(":")[-1]
        times = "13:00,19:00" if preset == "2" else "10:00,14:00,19:00"
        await repo.upsert_wellness(db, user["id"], meal_times=times)
    updated = await repo.get_wellness(db, user["id"])
    try:
        await callback.message.edit_reply_markup(reply_markup=wellness_keyboard(updated))
    except Exception:
        pass
    await callback.answer("Обновлено")


# Focus mode 20/10 simple timers in chat
@router.message(lambda m: m.text and "20/10" in m.text)
@router.message(Command("focus"))
async def focus_mode(message: types.Message, db) -> None:
    user = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if not user:
        await message.answer(register_text())
        return
    row = await repo.get_wellness(db, user["id"])
    current = dict(row) if row else {"focus_mode": 0}
    if not current.get("focus_mode"):
        await message.answer("Включи режим фокуса в настройках здоровья (кнопка «Вода/Еда/Фокус» в настройках).")
        return
    work = current.get("focus_work", 20)
    rest = current.get("focus_rest", 10)
    await message.answer(f"Стартуем {work}/{rest}: {work} минут делаем, {rest} отдыхаем.")
    await asyncio.sleep(work * 60)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Ещё раунд {work}/{rest}",
                    callback_data=f"focus:again:{work}:{rest}",
                ),
                InlineKeyboardButton(text="Хватит", callback_data="focus:stop"),
            ]
        ]
    )
    await message.answer("Стоп. Отдыхай. Начать следующий раунд?", reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("focus:"))
async def focus_again(callback: types.CallbackQuery) -> None:
    parts = callback.data.split(":")
    action = parts[1]
    if action == "stop":
        await callback.answer("Ок, отдыхай.")
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    work = int(parts[2]) if len(parts) > 2 else 20
    rest = int(parts[3]) if len(parts) > 3 else 10
    await callback.answer("Стартуем ещё один раунд.")
    await callback.message.answer(f"{work} минут делаем...")
    await asyncio.sleep(work * 60)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Ещё раунд {work}/{rest}",
                    callback_data=f"focus:again:{work}:{rest}",
                ),
                InlineKeyboardButton(text="Хватит", callback_data="focus:stop"),
            ]
        ]
    )
    await callback.message.answer("Стоп. Отдыхай. Ещё раунд?", reply_markup=kb)


@router.message(Command("gentle"))
async def gentle_mode(message: types.Message, db) -> None:
    user = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if not user:
        await message.answer(register_text())
        return
    now_utc = datetime.datetime.utcnow()
    local_date = local_date_str(now_utc, user["timezone"])
    await repo.set_user_pause(db, user["id"], local_date)
    await message.answer(
        texts.GENTLE_ON,
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("resume"))
async def gentle_off(message: types.Message, db) -> None:
    user = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if not user:
        await message.answer(register_text())
        return
    await repo.clear_user_pause(db, user["id"])
    await message.answer(
        texts.GENTLE_OFF,
        reply_markup=main_menu_keyboard(),
    )
