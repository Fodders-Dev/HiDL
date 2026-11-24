from __future__ import annotations

import html

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from services import household_service
from utils.rows import rows_to_dicts
from utils.sender import safe_edit
from utils.user import ensure_user


router = Router()


class SupplyAddState(StatesGroup):
    waiting_name = State()


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
        row_btns = [
            InlineKeyboardButton(
                text=f"{_status_icon(status)} {item['name']}",
                callback_data=f"supply:set:{sid}:full",
            ),
            InlineKeyboardButton(text="⚠️", callback_data=f"supply:set:{sid}:low"),
            InlineKeyboardButton(text="❌", callback_data=f"supply:set:{sid}:empty"),
        ]
        if status in {"low", "empty"}:
            row_btns.append(
                InlineKeyboardButton(text="🛒", callback_data=f"supply:toshop:{sid}")
            )
        rows.append(row_btns)
    rows.append([InlineKeyboardButton(text="➕ Добавить расходник", callback_data="supply:add")])
    rows.append(
        [
            InlineKeyboardButton(text="🏠 Наш дом", callback_data="hh:hub"),
            InlineKeyboardButton(text="⬅️ Дом", callback_data="home:menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _render_supplies_payload(supplies: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    lines: list[str] = ["Бытовая химия и расходники:", ""]
    for item in supplies:
        status = item.get("status") or "full"
        by_name = str(item.get("updated_by_name") or "").strip()
        by_note = f" · {by_name}" if by_name else ""
        lines.append(f"{_status_icon(status)} {item['name']} — {_status_label(status)}{by_note}")
    lines.append("")
    lines.append(
        "Кнопки справа: ⚠️ на исходе, ❌ кончилось, 🛒 в общий список покупок."
    )
    return "\n".join(lines), _supplies_keyboard(supplies)


async def _render_supplies(message: types.Message, supplies: list[dict]) -> None:
    if not supplies:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить расходник", callback_data="supply:add")],
                [InlineKeyboardButton(text="🏠 Наш дом", callback_data="hh:hub")],
            ]
        )
        await message.answer(
            "Пока я не вижу записей по бытовой химии. "
            "Можно добавить сюда пакеты, губки или средства для уборки.",
            reply_markup=kb,
        )
        return
    text, kb = _render_supplies_payload(supplies)
    await message.answer(text, reply_markup=kb)


@router.message(Command("supplies"))
async def supplies_command(message: types.Message, db) -> None:
    """Вход через команду /supplies."""
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.ensure_supplies(db, user["id"])
    supplies_rows = await repo.list_supplies(db, user["id"])
    supplies = rows_to_dicts(supplies_rows)
    await _render_supplies(message, supplies)


async def supplies_menu(
    message: types.Message,
    db,
    *,
    tg_user_id: int | None = None,
    tg_full_name: str | None = None,
) -> None:
    """Вход из раздела Дом."""
    uid = tg_user_id if tg_user_id is not None else message.from_user.id
    uname = tg_full_name if tg_full_name is not None else message.from_user.full_name
    user = await ensure_user(db, uid, uname)
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
    u_dict = dict(user)
    actor_name = str(u_dict.get("name") or "")
    await repo.ensure_supplies(db, u_dict["id"])
    updated_item = await repo.update_supply_status(
        db, u_dict["id"], supply_id, status, actor_name=actor_name
    )
    supplies_rows = await repo.list_supplies(db, u_dict["id"])
    supplies = rows_to_dicts(supplies_rows)
    text, kb = _render_supplies_payload(supplies)
    await safe_edit(callback.message, text, reply_markup=kb)
    if status == "empty" and updated_item:
        shop_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"🛒 Добавить «{updated_item['name']}» в покупки",
                        callback_data=f"supply:toshop:{supply_id}",
                    )
                ]
            ]
        )
        await callback.message.answer(
            f"❌ <b>{html.escape(updated_item['name'])}</b> закончилось — закинуть в общий список покупок?",
            reply_markup=shop_kb,
            parse_mode="HTML",
        )
        await household_service.notify_household_partners(
            getattr(callback, "bot", None),
            db,
            u_dict,
            f"🧴 <b>{html.escape(actor_name or 'Партнёр')}</b> отметил(а), что закончилось: {html.escape(updated_item['name'])} ❌",
        )
    await callback.answer("Обновила запасы.")


@router.callback_query(lambda c: c.data and c.data.startswith("supply:toshop:"))
async def supply_to_shopping(callback: types.CallbackQuery, db) -> None:
    parts = (callback.data or "").split(":")
    try:
        supply_id = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer()
        return
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    item = await repo.get_supply_by_id(db, int(user["id"]), supply_id)
    if not item:
        await callback.answer("Расходник не найден.", show_alert=True)
        return
    existing = rows_to_dicts(await repo.list_shopping_items(db, int(user["id"]), scope="household"))
    name_key = str(item["name"] or "").strip().casefold()
    if any(str(s.get("item_name") or "").strip().casefold() == name_key and not s.get("is_bought") for s in existing):
        await callback.answer("Уже есть в общем списке покупок 🛒")
        return
    await repo.create_shopping_item(
        db,
        int(user["id"]),
        str(item["name"]),
        qty=1.0,
        unit="шт",
        category="бытовое",
        scope="household",
    )
    await callback.answer(f"Добавила «{item['name']}» в общий список 🛒")


@router.callback_query(lambda c: c.data == "supply:add")
async def supply_add_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SupplyAddState.waiting_name)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="supply:add_cancel")]]
    )
    await callback.message.answer(
        "Напиши название расходника для дома (например: <code>Таблетки для посудомойки</code>).",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "supply:add_cancel")
async def supply_add_cancel(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(callback.message, "Отменено 👌", reply_markup=None)
    await callback.answer()


@router.message(SupplyAddState.waiting_name, lambda m: bool(m.text and not m.text.startswith("/")))
async def supply_add_name(message: types.Message, state: FSMContext, db) -> None:
    raw = (message.text or "").strip()
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    if raw.lower() in {"отмена", "назад"}:
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu_keyboard(dict(user)))
        return
    await repo.add_supply_item(db, int(user["id"]), raw)
    await state.clear()
    supplies = rows_to_dicts(await repo.list_supplies(db, int(user["id"])))
    text, kb = _render_supplies_payload(supplies)
    await message.answer(
        f"✅ Добавила <b>{html.escape(raw)}</b> в список дома.\n\n{html.escape(text)}",
        reply_markup=kb,
        parse_mode="HTML",
    )

