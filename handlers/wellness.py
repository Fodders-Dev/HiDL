import asyncio
import datetime

from aiogram import Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from services import karma_service, pulse_service
from utils import texts
from utils.menu_match import match_menu_label
from utils.sender import safe_edit
from utils.texts import register_text
from utils.time import local_date_str, parse_hhmm
from utils.user import ensure_user

router = Router()
FOCUS_EDIT_REQUESTS = {}
TIME_EDIT_REQUESTS = {}


class WellnessState(StatesGroup):
    focus_edit = State()
    time_edit = State()


def _wellness_text(current: dict) -> str:
    work = current.get("focus_work") or 20
    rest = current.get("focus_rest") or 10
    return (
        "Напоминания здоровья:\n"
        f"• Вода: {'вкл' if current.get('water_enabled') else 'выкл'}\n"
        f"• Приём пищи: {'вкл' if current.get('meal_enabled') else 'выкл'}\n"
        f"• Фокус-таймер {work}/{rest}: {'вкл' if current.get('focus_mode') else 'выкл'}\n"
        "Переключи нужное:"
    )


def wellness_keyboard(current) -> InlineKeyboardMarkup:
    water = "✅" if current.get("water_enabled") else "❌"
    meal = "✅" if current.get("meal_enabled") else "❌"
    focus = "✅" if current.get("focus_mode") else "❌"
    work = current.get("focus_work") or 20
    rest = current.get("focus_rest") or 10
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"Вода {water}", callback_data="well:water")],
            [InlineKeyboardButton(text=f"Приём пищи {meal}", callback_data="well:meal")],
            [
                InlineKeyboardButton(text=f"Фокус {work}/{rest} {focus}", callback_data="well:focus"),
                InlineKeyboardButton(text="Сменить интервалы", callback_data="well:focus_edit"),
            ],
            [
                InlineKeyboardButton(
                    text=f"Вода часы: {current.get('water_times') or 'не задано'}",
                    callback_data="well:water_times",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"Еда часы: {current.get('meal_times') or 'не задано'}",
                    callback_data="well:meal_times",
                ),
            ],
            [
                InlineKeyboardButton(text="Пресет воды 1x", callback_data="well:water_preset:1"),
                InlineKeyboardButton(text="Пресет воды 2x", callback_data="well:water_preset:2"),
            ],
            [
                InlineKeyboardButton(text="Пресет еды 2x", callback_data="well:meal_preset:2"),
                InlineKeyboardButton(text="Пресет еды 3x", callback_data="well:meal_preset:3"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="main:menu")],
        ]
    )


@router.message(Command("wellness"))
async def wellness_settings(message: types.Message, db, state: FSMContext | None = None) -> None:
    if state:
        await state.clear()
    FOCUS_EDIT_REQUESTS.pop(message.from_user.id, None)
    TIME_EDIT_REQUESTS.pop(message.from_user.id, None)
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    row = await repo.get_wellness(db, user["id"])
    current = dict(row) if row else {"water_enabled": 0, "meal_enabled": 0, "focus_mode": 0}
    await message.answer(_wellness_text(current), reply_markup=wellness_keyboard(current))


@router.message(lambda m: m.text and not m.text.startswith("/") and m.from_user.id in FOCUS_EDIT_REQUESTS)
async def focus_edit_input(message: types.Message, db, state: FSMContext | None = None) -> None:
    raw = message.text.strip()
    if raw.lower() in {"отмена", "назад"}:
        FOCUS_EDIT_REQUESTS.pop(message.from_user.id, None)
        if state:
            await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return
    parts = raw.split()
    if len(parts) < 2:
        await message.answer(
            texts.error("нужно два числа: работа отдых, например 20 10."),
        )
        return
    try:
        work = int(parts[0])
        rest = int(parts[1])
        if work < 2 or rest < 1:
            raise ValueError
    except Exception:
        await message.answer(
            texts.error("нужны целые числа (работа отдых)."),
        )
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.upsert_wellness(db, user["id"], focus_work=work, focus_rest=rest)
    FOCUS_EDIT_REQUESTS.pop(message.from_user.id, None)
    if state:
        await state.clear()
    await message.answer(
        f"Установила интервалы {work}/{rest}. Включи фокус в настройках здоровья (кнопка «Вода/Еда/Фокус»).",
        reply_markup=main_menu_keyboard(),
    )


@router.message(StateFilter(None), lambda m: match_menu_label(m.text, {"щадящий", "щадящий режим"}))
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


@router.message(lambda m: m.text and not m.text.startswith("/") and m.from_user.id in TIME_EDIT_REQUESTS)
async def time_edit_input(message: types.Message, db, state: FSMContext | None = None) -> None:
    req = TIME_EDIT_REQUESTS.get(message.from_user.id)
    if not req:
        return
    raw = message.text.strip()
    if raw.lower() in {"отмена", "назад"}:
        TIME_EDIT_REQUESTS.pop(message.from_user.id, None)
        if state:
            await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return
    kind, _ = req
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    parsed = [parse_hhmm(p) for p in parts]
    if not parts or any(t is None for t in parsed):
        await message.answer(
            texts.error("время в формате HH:MM, например 11:00,16:00."),
        )
        return
    normalized = ",".join(t.strftime("%H:%M") for t in parsed if t is not None)
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    if kind == "water":
        await repo.upsert_wellness(db, user["id"], water_times=normalized)
    elif kind == "meal":
        await repo.upsert_wellness(db, user["id"], meal_times=normalized)
    TIME_EDIT_REQUESTS.pop(message.from_user.id, None)
    if state:
        await state.clear()
    await message.answer("Часы обновлены.", reply_markup=main_menu_keyboard())


@router.callback_query(lambda c: c.data and c.data.startswith("well:"))
async def wellness_toggle(callback: types.CallbackQuery, db, state: FSMContext | None = None) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    row = await repo.get_wellness(db, user["id"])
    current = dict(row) if row else {"water_enabled": 0, "meal_enabled": 0, "focus_mode": 0}
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
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
        if state:
            await state.set_state(WellnessState.focus_edit)
        await callback.message.answer(
            "Введи интервалы через пробел (минуты): работа отдых. Пример: 20 10 или 10 5.",
            reply_markup=None,
        )
        await callback.answer()
        FOCUS_EDIT_REQUESTS[callback.from_user.id] = callback.message.chat.id
        return
    elif action == "water_times":
        if state:
            await state.set_state(WellnessState.time_edit)
        await callback.message.answer(
            "Введи часы для воды через запятую в формате HH:MM, например: 11:00,16:00",
            reply_markup=None,
        )
        TIME_EDIT_REQUESTS[callback.from_user.id] = ("water", callback.message.chat.id)
        await callback.answer()
        return
    elif action == "meal_times":
        if state:
            await state.set_state(WellnessState.time_edit)
        await callback.message.answer(
            "Введи часы для еды через запятую в формате HH:MM, например: 13:00,19:00",
            reply_markup=None,
        )
        TIME_EDIT_REQUESTS[callback.from_user.id] = ("meal", callback.message.chat.id)
        await callback.answer()
        return
    elif action == "water_preset":
        preset = parts[2] if len(parts) > 2 else ""
        times = "11:00" if preset == "1" else "11:00,16:00"
        await repo.upsert_wellness(db, user["id"], water_times=times)
    elif action == "meal_preset":
        preset = parts[2] if len(parts) > 2 else ""
        times = "13:00,19:00" if preset == "2" else "10:00,14:00,19:00"
        await repo.upsert_wellness(db, user["id"], meal_times=times)
    FOCUS_EDIT_REQUESTS.pop(callback.from_user.id, None)
    TIME_EDIT_REQUESTS.pop(callback.from_user.id, None)
    if state:
        await state.clear()
    updated = await repo.get_wellness(db, user["id"])
    updated_dict = dict(updated) if updated else {"water_enabled": 0, "meal_enabled": 0, "focus_mode": 0}
    await safe_edit(
        callback.message,
        _wellness_text(updated_dict),
        reply_markup=wellness_keyboard(updated_dict),
    )
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
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Включить/выключить фокус-таймер",
                        callback_data="well:focus",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⏱ Сменить интервалы",
                        callback_data="well:focus_edit",
                    ),
                    InlineKeyboardButton(
                        text="☕ Кафе фокуса",
                        callback_data="help:cafe",
                    ),
                ],
            ]
        )
        await message.answer(
            "Режим фокуса сейчас выключен.\n"
            "Включи фокус-таймер прямо здесь или открой «Кафе фокуса»:",
            reply_markup=kb,
        )
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


@router.callback_query(lambda c: c.data and c.data.startswith("mealconfirm:"))
async def meal_confirm(callback: types.CallbackQuery, db) -> None:
    """Handle meal confirmation: Да! (+1 point) or Напомни попозже."""
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer()
        return
    
    local_date = parts[1]
    action = parts[2]
    
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    
    if action == "yes":
        await repo.add_points(db, user["id"], 1, local_date)
        k_res = await karma_service.record_karma_event(
            db,
            user,
            domain="food",
            event_type="meal_confirm",
            delta=3,
            reason="Подтверждён приём пищи",
        )
        await callback.message.edit_text(
            f"🎉 Отлично! Молодец что позаботился о себе. +1 очко, +3 кармы ({k_res['score_after']}/100)!",
            reply_markup=None,
        )
        await callback.answer("Записано! +1 🌟")
    elif action == "later":
        snooze = await pulse_service.schedule_adhoc_snooze(
            db, user, "lunch_snooze", "food", 30, "Поесть"
        )
        await callback.message.edit_text(
            f"⏰ Ок, поставила напоминание на {snooze['next_local_hhmm']} (+30 мин). Не забудь поесть!",
            reply_markup=None,
        )
        await callback.answer(f"Напомню в {snooze['next_local_hhmm']}")
    else:
        await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("waterconfirm:"))
async def water_confirm(callback: types.CallbackQuery, db) -> None:
    """Handle water confirmation: Выпил! (+1 point) or Позже."""
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer()
        return

    local_date = parts[1]
    action = parts[2]

    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)

    if action == "yes":
        await repo.add_points(db, user["id"], 1, local_date)
        k_res = await karma_service.record_karma_event(
            db,
            user,
            domain="hygiene",
            event_type="water_confirm",
            delta=2,
            reason="Выпит стакан воды",
        )
        await callback.message.edit_text(
            f"💧 Умничка! Водный баланс — это важно. +1 очко, +2 кармы ({k_res['score_after']}/100)!",
            reply_markup=None,
        )
        await callback.answer("Записано! +1 🌟")
    elif action == "later":
        snooze = await pulse_service.schedule_adhoc_snooze(
            db, user, "water_snooze", "hygiene", 30, "Выпить воды"
        )
        await callback.message.edit_text(
            f"⏰ Ок, напомню о воде в {snooze['next_local_hhmm']} (+30 мин)!",
            reply_markup=None,
        )
        await callback.answer(f"Напомню в {snooze['next_local_hhmm']}")
    else:
        await callback.answer()

