import datetime

from aiogram import Router, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.time import local_date_str, format_date_display, local_date_plus_days
from utils import texts
from utils.affirmations import random_affirmation_text
from utils.user import ensure_user

router = Router()


@router.message(lambda m: (m.text or "").strip().casefold() in {"😴 сон и пауза", "сон и пауза"})
async def pause_menu(message: types.Message) -> None:
    await message.answer("Можно включить паузу на день или настроить сон.", reply_markup=_pause_menu_keyboard())


def _pause_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧘 Пауза на сегодня", callback_data="pause:one_day")],
        [InlineKeyboardButton(text="🌙 Настроить сон", callback_data="sleep:menu:pause")],
        [InlineKeyboardButton(text="▶️ Снять паузу", callback_data="pause:resume")],
    ])


@router.callback_query(lambda c: c.data == "pause:menu")
async def pause_menu_callback(callback: types.CallbackQuery) -> None:
    await callback.message.answer("Можно включить паузу на день или настроить сон.", reply_markup=_pause_menu_keyboard())
    await callback.answer()


@router.callback_query(lambda c: c.data in {"pause:one_day", "pause:resume"})
async def pause_menu_action(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    if callback.data == "pause:resume":
        await repo.clear_user_pause(db, user["id"])
        text = "Пауза снята, напоминания снова активны."
    else:
        until = local_date_str(datetime.datetime.utcnow(), user["timezone"])
        await repo.set_user_pause(db, user["id"], until)
        text = f"Пауза включена до {format_date_display(until)}."
    await callback.message.answer(text, reply_markup=main_menu_keyboard(dict(user)))
    await callback.answer()


@router.message(Command("pause"))
async def pause(message: types.Message, db) -> None:
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)

    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer(
            texts.error("указать, на сколько дней поставить на паузу, например «3»."),
        )
        return
    try:
        days = int(parts[1])
        if days < 1:
            raise ValueError
    except Exception:
        await message.answer(
            texts.error("нужно целое число дней, например «3»."),
        )
        return

    now_utc = datetime.datetime.utcnow()
    pause_until = local_date_plus_days(now_utc, user["timezone"], days)
    await repo.set_user_pause(db, user["id"], pause_until)
    await message.answer(
        f"{texts.GENTLE_ON}\nПауза до {pause_until}. Вернуть раньше — кнопка «Щадящий режим».",
        reply_markup=main_menu_keyboard(dict(user)),
    )
    # Мягкая поддержка при паузе
    aff = random_affirmation_text()
    if aff:
        await message.answer(
            f"И на паузу возьмём такую мысль:\n\n<i>{aff}</i>",
            reply_markup=main_menu_keyboard(dict(user)),
        )


@router.message(Command("resume"))
async def resume(message: types.Message, db) -> None:
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.clear_user_pause(db, user["id"])
    await message.answer(
        "Пауза снята, напоминания снова активны.", reply_markup=main_menu_keyboard(dict(user))
    )
