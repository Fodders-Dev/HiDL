from aiogram import Router, types
from aiogram.filters import Command

from keyboards.common import main_menu_keyboard
from utils.nl_parser import parse_command
from utils.tone import tone_ack, tone_error
from utils.logger import log_info
from utils.tone import tone_message
from db import repositories as repo
from utils.user import ensure_user
from llm_client import client as llm_client

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


async def _route_parsed_command(message: types.Message, parsed) -> bool:
    """Пробуем ответить на распарсенную команду."""
    if not parsed:
        return False
    if parsed.type == "expense":
        amount = parsed.payload.get("amount")
        cat = parsed.payload.get("category", "другое")
        if amount:
            await message.answer(
                f"Вижу трату: {amount:.0f} ₽, категория {cat}. Добавь её через раздел Деньги → «Записать трату».",
                reply_markup=main_menu_keyboard(),
            )
            return True
    if parsed.type == "reminder":
        title = parsed.payload.get("title") or "Напоминание"
        time = parsed.payload.get("time") or "по времени"
        await message.answer(
            f"Напоминание поняла: {title} в {time}. Создай его в «Напоминания» кнопками, я помогу.",
            reply_markup=main_menu_keyboard(),
        )
        return True
    if parsed.type == "home":
        action = parsed.payload.get("action")
        if action == "clean_now":
            await message.answer("Хочешь убраться сейчас? Жми «🧽 Уборка сейчас» в разделе Дом.", reply_markup=main_menu_keyboard())
        elif action == "home_plan":
            await message.answer("Показать план по дому? Жми «📅 План на неделю» в разделе Дом.", reply_markup=main_menu_keyboard())
        else:
            await message.answer("Дом: открой раздел «🧹 Дом», я покажу план и быстрые шаги.", reply_markup=main_menu_keyboard())
        return True
    if parsed.type == "ask":
        await message.answer(
            "Поняла вопрос, давай зайдём в «Спросить маму» — там подскажу по теме.",
            reply_markup=main_menu_keyboard(),
        )
        return True
    return False


@router.message(lambda m: m.text and "поговор" in m.text.lower())
async def talk_free(message: types.Message, db) -> None:
    """Простой чат-режим: разбираем текст, пытаемся подсказать или увести в разделы."""
    txt = message.text.strip()
    parsed = parse_command(txt)
    handled = await _route_parsed_command(message, parsed)
    if handled:
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    tone = "soft"
    wellness = await repo.get_wellness(db, user["id"])
    if wellness and wellness.get("tone"):
        tone = wellness["tone"]
    # Попробуем спросить у заглушки LLM, если текст достаточно длинный
    reply_llm = ""
    if len(txt.split()) > 5:
        reply_llm = await llm_client.ask(txt, user_context=f"user={user.get('name')}")
    # fallback: мягкий ответ
    log_info(f"Talk fallback text: {txt}")
    base = tone_message(
        tone,
        "Я здесь, чтобы помочь по быту и поддержать. Можешь спросить про уборку, еду, финансы или нажать нужный раздел ниже. "
        "Если что-то тревожит — напиши пару слов, разберёмся по‑человечески.",
    )
    text = base if not reply_llm else f"{base}\n\n{reply_llm}"
    await message.answer(text, reply_markup=main_menu_keyboard())
