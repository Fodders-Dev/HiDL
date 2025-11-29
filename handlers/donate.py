from aiogram import Router, types
from aiogram.filters import Command

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.tone import tone_message

router = Router()


BASE_DONATE_TEXT = (
    "Если бот помогает — можешь поддержать автора ☕\n\n"
    "Boosty: https://boosty.to/\n"
    "ЮMoney: 4100XXXXXXX\n"
    "СБП/карта: XXXX XXXX XXXX XXXX\n\n"
    "Спасибо! Донаты никак не ограничивают функциональность."
)


@router.message(Command("donate"))
@router.message(lambda m: m.text and "поддерж" in m.text.lower())
async def donate(message: types.Message, db) -> None:
    tone = "neutral"
    user = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if user:
        wellness = await repo.get_wellness(db, user["id"])
        if wellness:
            tone = wellness["tone"]
    await message.answer(tone_message(tone, BASE_DONATE_TEXT), reply_markup=main_menu_keyboard())
