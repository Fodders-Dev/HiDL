from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from keyboards.common import main_menu_keyboard
from utils.affirmations import random_affirmation_text

router = Router()


def _affirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Ещё одну", callback_data="affirm:more"),
                InlineKeyboardButton(text="Спасибо, хватит", callback_data="affirm:stop"),
            ]
        ]
    )


async def _send_affirmation(message: types.Message) -> None:
    text = random_affirmation_text()
    if not text:
        await message.answer(
            "У меня пока нет отдельной подборки фраз поддержки. "
            "Но ты уже многое делаешь, просто читая это и пробуя навести порядок.",
            reply_markup=main_menu_keyboard(),
        )
        return
    await message.answer(
        "Давай я напомню тебе одну важную вещь на сегодня:\n\n"
        f"<i>{text}</i>",
        reply_markup=_affirm_keyboard(),
    )


@router.message(Command("affirm"))
async def affirm_cmd(message: types.Message) -> None:
    await _send_affirmation(message)


@router.callback_query(lambda c: c.data and c.data.startswith("affirm:"))
async def affirm_callbacks(callback: types.CallbackQuery) -> None:
    action = callback.data.split(":")[1]
    if action == "more":
        await _send_affirmation(callback.message)
    elif action == "stop":
        await callback.message.answer("Хорошо, я рядом. Если захочется ещё поддержки — просто напиши /affirm.", reply_markup=main_menu_keyboard())
    await callback.answer()

