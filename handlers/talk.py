from aiogram import Router, types
from aiogram.filters import Command

from keyboards.common import main_menu_keyboard

router = Router()

# Заглушка: сюда будет подключена внешняя LLM.
LLM_PROMPT = (
    "You are HiDL, a caring female assistant who helps with daily routines, food, cleaning, finances, "
    "soft reminders and emotional support. Keep answers short, warm, without bureaucracy. Offer to set reminders "
    "or add tasks only with user confirmation."
)


@router.message(Command("talk"))
async def talk_placeholder(message: types.Message) -> None:
    await message.answer(
        "Поболтать: здесь будет живое общение с HiDL через нейросеть.\n"
        "Пока заглушка. Можешь написать, что беспокоит, я отвечу обычными подсказками.",
        reply_markup=main_menu_keyboard(),
    )

# Здесь можно будет добавить обработчик текстов в режиме чата и отправку в внешнюю LLM с промптом LLM_PROMPT.
