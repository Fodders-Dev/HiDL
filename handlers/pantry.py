from aiogram import types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from utils.pantry import format_quantity, is_low
from utils.rows import rows_to_dicts
from utils.user import ensure_user


async def pantry_command(message: types.Message, db) -> None:
    """Backward-compat entrypoint for old `food:pantry` callbacks."""
    tg_id = message.from_user.id if message.from_user else message.chat.id
    full_name = message.from_user.full_name if message.from_user else "Друг"
    user = await ensure_user(db, tg_id, full_name)

    rows_all = await repo.list_pantry_items(db, user["id"])
    items = rows_to_dicts(rows_all)

    text = "<b>❄️ Мой холодильник</b>\n"
    if not items:
        text += "Пока пусто. Добавь что-нибудь."
    else:
        cats: dict[str, list[dict]] = {}
        for item in items:
            cat = item.get("category") or "прочее"
            cats.setdefault(cat, []).append(item)
        for cat, cat_items in cats.items():
            text += f"\n<b>{cat.capitalize()}</b>:\n"
            for item in cat_items:
                low = " ⚠️" if is_low(item) else ""
                qty = format_quantity(item.get("amount") or 1, item.get("unit") or "шт")
                text += f"• {item['name']} — {qty}{low}\n"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить продукт", callback_data="kitchen:fridge_add")],
            [InlineKeyboardButton(text="🗑 Удалить что-то", callback_data="kitchen:fridge_del_view")],
            [InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main")],
        ]
    )

    try:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
