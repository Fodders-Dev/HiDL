from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from keyboards.common import main_menu_keyboard
from utils.affirmations import random_affirmation_text
from services.knowledge import get_knowledge_service

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
    # Попробуем сначала из Базы Знаний
    ks = get_knowledge_service()
    text = ks.get_random_affirmation()
    
    # Fallback к старой функции
    if not text:
        text = random_affirmation_text()
    
    if not text:
        await message.answer(
            "У меня пока нет отдельной подборки фраз поддержки. "
            "Но ты уже многое делаешь, просто читая это и пробуя навести порядок.",
            reply_markup=main_menu_keyboard(),
        )
        return
    
    # Если текст начинается с эмодзи (из KB), не добавляем обёртку
    if text.startswith("💭"):
        await message.answer(text, reply_markup=_affirm_keyboard())
    else:
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
        # Ничего не спамим в чат: просто убираем кнопки и отвечаем "внутренним" уведомлением.
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.answer("Обняла. Если захочешь ещё — я рядом.", show_alert=False)
        return
    await callback.answer()
