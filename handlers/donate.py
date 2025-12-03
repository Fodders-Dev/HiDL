from aiogram import Router, types
from aiogram.filters import Command

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.tone import tone_message

router = Router()


BASE_DONATE_TEXT = (
    "Если бот помогает — можешь поддержать автора ☕\n\n"
    "Ни одна функция не спрятана за донатами — всё, что у меня есть, доступно бесплатно.\n"
    "Пожертвования — это просто способ сказать «спасибо» и помочь развивать проект.\n\n"
    "Самые простые варианты:\n"
    "• Boosty: https://boosty.to/\n"
    "• ЮMoney: 4100XXXXXXX\n"
    "Другие варианты могу подсказать отдельно.\n\n"
    "Спасибо за любую поддержку — даже если это просто тёплая мысль в мою сторону."
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
