from aiogram import Router, types
from aiogram.types import CallbackQuery

from db import repositories as repo
from keyboards.common import knowledge_keyboard, main_menu_keyboard

router = Router()


async def _send_category(message: types.Message, category: str, db) -> None:
    articles = await repo.list_articles_by_category(db, category)
    if not articles:
        await message.answer("Материалы пока пусты, скоро пополню.")
        return
    await message.answer(
        f"{category}: выбери тему", reply_markup=knowledge_keyboard(category, articles)
    )


@router.message(lambda m: m.text and "кухн" in m.text.lower())
async def kitchen(message: types.Message, db) -> None:
    await _send_category(message, "Кухня", db)


@router.message(lambda m: m.text and "стир" in m.text.lower())
async def laundry(message: types.Message, db) -> None:
    await _send_category(message, "Стирка", db)


@router.message(lambda m: m.text and "уборк" in m.text.lower())
async def cleaning(message: types.Message, db) -> None:
    await _send_category(message, "Уборка", db)


@router.callback_query(lambda c: c.data and c.data.startswith("article:"))
async def article_view(callback: CallbackQuery, db) -> None:
    _, article_id = callback.data.split(":")
    article = await repo.get_article(db, int(article_id))
    if not article:
        await callback.answer("Не нашёл статью", show_alert=True)
        return

    text = f"{article['title']}\n\n{article['content']}\n\nШаги:\n{article['steps']}"
    await callback.message.answer(text, reply_markup=main_menu_keyboard())
    await callback.answer()
