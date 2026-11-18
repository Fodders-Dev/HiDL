from aiogram import Router, types
from aiogram.filters import Command

from keyboards.common import main_menu_keyboard

router = Router()


@router.message(Command("talk"))
async def talk_placeholder(message: types.Message) -> None:
    await message.answer(
        "Просто напиши или надиктуй голосовое обычными словами.\n"
        "Я сохраню текст, разложу дела, покупки, напоминания, мысли и идеи — и дам кнопки, чтобы поправить или отменить.\n"
        "Записи дневника и экспорт: /diary.",
        reply_markup=main_menu_keyboard(),
    )
