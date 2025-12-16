from __future__ import annotations

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from utils.rows import rows_to_dicts
from utils.user import ensure_user
from keyboards.common import main_menu_keyboard


router = Router()


def _status_icon(status: str) -> str:
    status = (status or "full").lower()
    if status == "empty":
        return "❌"
    if status == "low":
        return "⚠️"
    return "✅"


def _status_label(status: str) -> str:
    status = (status or "full").lower()
    if status == "empty":
        return "кончилось"
    if status == "low":
        return "на исходе"
    return "есть в запасе"


def _supplies_keyboard(supplies: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in supplies:
        sid = item["id"]
        status = (item.get("status") or "full").lower()
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{_status_icon(status)} {item['name']}",
                    callback_data=f"supply:set:{sid}:full",
                ),
                InlineKeyboardButton(
                    text="⚠️",
                    callback_data=f"supply:set:{sid}:low",
                ),
                InlineKeyboardButton(
                    text="❌",
                    callback_data=f"supply:set:{sid}:empty",
                ),
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Дом", callback_data="home:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Добавить позже", callback_data="supply:none:0")],
            [InlineKeyboardButton(text="⬅️ Дом", callback_data="home:menu")],
        ]
    )


async def _render_supplies(message: types.Message, supplies: list[dict]) -> None:
    if not supplies:
        await message.answer(
            "Пока я не вижу записей по бытовой химии. "
            "Можно просто помнить, что всё это тоже расходники: пакеты, губки, средства для уборки.",
            reply_markup=main_menu_keyboard(),
        )
        return
    lines: list[str] = []
    lines.append("Бытовая химия и расходники:")
    lines.append("")
    for item in supplies:
        status = item.get("status") or "full"
        lines.append(f"{_status_icon(status)} {item['name']} — {_status_label(status)}")
    lines.append("")
    lines.append("Обнови состояние кнопками ниже: первая — «запас есть», справа — когда на исходе или закончилась.")
    kb = _supplies_keyboard(supplies)
    await message.answer("\n".join(lines), reply_markup=kb)


@router.message(Command("supplies"))
async def supplies_command(message: types.Message, db) -> None:
    """Вход через команду /supplies."""
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.ensure_supplies(db, user["id"])
    supplies_rows = await repo.list_supplies(db, user["id"])
    supplies = rows_to_dicts(supplies_rows)
    await _render_supplies(message, supplies)


async def supplies_menu(message: types.Message, db) -> None:
    """Вход из раздела Дом."""
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.ensure_supplies(db, user["id"])
    supplies_rows = await repo.list_supplies(db, user["id"])
    supplies = rows_to_dicts(supplies_rows)
    await _render_supplies(message, supplies)


@router.callback_query(lambda c: c.data and c.data.startswith("supply:set:"))
async def supply_set(callback: types.CallbackQuery, db) -> None:
    """Обновить статус расходника и перерисовать список."""
    _, _, sid_str, status = callback.data.split(":")
    try:
        supply_id = int(sid_str)
    except ValueError:
        await callback.answer()
        return
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.ensure_supplies(db, user["id"])
    await repo.update_supply_status(db, user["id"], supply_id, status)
    supplies_rows = await repo.list_supplies(db, user["id"])
    supplies = rows_to_dicts(supplies_rows)
    # редактируем сообщение, если возможно, иначе шлём новое
    text_lines = ["Бытовая химия и расходники:", ""]
    for item in supplies:
        st = item.get("status") or "full"
        text_lines.append(f"{_status_icon(st)} {item['name']} — {_status_label(st)}")
    text_lines.append("")
    text_lines.append("Обнови состояние кнопками ниже: первая — «запас есть», справа — когда на исходе или закончилась.")
    kb = _supplies_keyboard(supplies)
    text = "\n".join(text_lines)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    if status == "empty":
        hint = (
            "Если хочешь, могу напомнить: такие вещи удобно заносить в список покупок "
            "или в свои напоминания — так проще не остаться без средства в самый неудобный момент."
        )
        await callback.message.answer(hint, reply_markup=main_menu_keyboard())
    await callback.answer("Обновила запасы.")
