import datetime
from typing import Tuple

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.rows import rows_to_dicts, row_to_dict
from utils.time import local_date_str, format_date_display
from utils.texts import error
from utils.user import ensure_user


router = Router()


class PantryAddState(StatesGroup):
    name = State()
    amount = State()
    expiry = State()
    category = State()


class PantryEditState(StatesGroup):
    amount = State()
    expiry = State()


class ReceiptState(StatesGroup):
    wait_photo = State()


CATEGORY_LABELS = {
    "крупы": "Крупы и макароны",
    "мясо/рыба": "Мясо/рыба",
    "овощи": "Овощи",
    "фрукты": "Фрукты",
    "молочка": "Молочка",
    "прочее": "Прочее",
}


def _parse_amount_unit(text: str) -> Tuple[float, str]:
    raw = (text or "").strip().replace(",", ".")
    if not raw:
        return 1.0, "шт"
    parts = raw.split()
    try:
        amount = float(parts[0])
        tail = " ".join(parts[1:]).lower()
    except Exception:
        amount = 1.0
        tail = raw.lower()
    unit = "шт"
    if any(u in tail for u in ["кг", "kg"]):
        unit = "kg"
    elif any(u in tail for u in ["г ", "гр", "gram"]):
        unit = "g"
    elif "мл" in tail:
        unit = "ml"
    elif any(u in tail for u in ["л", "литр"]):
        unit = "l"
    return amount, unit


def _parse_expires(text: str) -> Tuple[str | None, str | None]:
    raw = (text or "").strip()
    if not raw or raw.lower() in {"нет", "не знаю", "no"}:
        return None, None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            dt = datetime.datetime.strptime(raw, fmt).date()
            return dt.isoformat(), None
        except Exception:
            continue
    return None, "нужна дата в формате 2025-12-31 или 31.12.2025"


def _category_keyboard() -> types.InlineKeyboardMarkup:
    rows = [
        [
            types.InlineKeyboardButton(text="Крупы", callback_data="pantry:cat:крупы"),
            types.InlineKeyboardButton(text="Молочка", callback_data="pantry:cat:молочка"),
        ],
        [
            types.InlineKeyboardButton(text="Овощи", callback_data="pantry:cat:овощи"),
            types.InlineKeyboardButton(text="Фрукты", callback_data="pantry:cat:фрукты"),
        ],
        [
            types.InlineKeyboardButton(text="Мясо/рыба", callback_data="pantry:cat:мясо/рыба"),
            types.InlineKeyboardButton(text="Прочее", callback_data="pantry:cat:прочее"),
        ],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def _pantry_keyboard(items: list[dict]) -> types.InlineKeyboardMarkup:
    rows: list[list[types.InlineKeyboardButton]] = []
    for item in items:
        label = item.get("name", "")
        amt = item.get("amount") or 0
        unit = item.get("unit") or "шт"
        btn_text = f"{label} — {amt:g} {unit}"
        rows.append(
            [
                types.InlineKeyboardButton(text=btn_text[:40], callback_data=f"pantry:edit:{item['id']}"),
                types.InlineKeyboardButton(text="🗑", callback_data=f"pantry:del:{item['id']}"),
            ]
        )
    rows.append([types.InlineKeyboardButton(text="➕ Добавить продукт", callback_data="pantry:add")])
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_pantry(message: types.Message, db) -> None:
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    rows = await repo.list_pantry_items(db, user["id"])
    items = rows_to_dicts(rows)
    if not items:
        kb = types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text="➕ Добавить продукт", callback_data="pantry:add")]]
        )
        await message.answer(
            "Я пока не знаю, что у тебя лежит на кухне.\n"
            "Давай добавим хотя бы один продукт — потом смогу подсказывать, что готовить из того, что есть.",
            reply_markup=kb,
        )
        return
    lines = ["Что у тебя есть дома (по ощущениям):"]
    current_cat = None
    for item in items:
        cat = item.get("category") or "прочее"
        if cat != current_cat:
            current_cat = cat
            label = CATEGORY_LABELS.get(cat, cat)
            lines.append(f"\n<b>{label}</b>")
        amt = item.get("amount") or 0
        unit = item.get("unit") or "шт"
        if item.get("expires_at"):
            lines.append(
                f"• {item['name']} — {amt:g} {unit}, годен до {format_date_display(item['expires_at'])}"
            )
        else:
            lines.append(f"• {item['name']} — {amt:g} {unit}")
    kb = _pantry_keyboard(items)
    await message.answer("\n".join(lines), reply_markup=kb)


@router.message(Command("pantry"))
async def pantry_command(message: types.Message, db) -> None:
    await _render_pantry(message, db)


@router.callback_query(lambda c: c.data and c.data == "pantry:expiring")
async def pantry_expiring_view(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    now_utc = datetime.datetime.utcnow()
    local_date = local_date_str(now_utc, user["timezone"])
    soon, expired = await repo.pantry_expiring(db, user["id"], local_date, window_days=5)
    soon_d = rows_to_dicts(soon)
    expired_d = rows_to_dicts(expired)
    if not soon_d and not expired_d:
        await callback.answer("Сейчас нет продуктов с истекающим сроком.", show_alert=True)
        return
    lines = ["Продукты, за которыми стоит приглядывать:"]
    if soon_d:
        lines.append("\n<b>Скоро истечёт срок:</b>")
        for row in soon_d:
            lines.append(
                f"• {row.get('name')} — до {format_date_display(row.get('expires_at'))}"
            )
    if expired_d:
        lines.append("\n<b>Похоже, срок уже прошёл:</b>")
        for row in expired_d:
            lines.append(
                f"• {row.get('name')} — дата {format_date_display(row.get('expires_at'))}"
            )
    await callback.message.answer("\n".join(lines), reply_markup=main_menu_keyboard())
    await callback.answer()


@router.message(Command("receipt_photo"))
async def receipt_photo_start(message: types.Message, state: FSMContext) -> None:
    """Запросить у пользователя фото чека для будущего OCR."""
    await state.set_state(ReceiptState.wait_photo)
    await message.answer(
        "Пришли фото чека. Я пока не умею его распознавать, "
        "но сохраню, чтобы в будущем вытаскивать продукты и добавлять их в список «что есть дома».",
        reply_markup=main_menu_keyboard(),
    )


@router.message(ReceiptState.wait_photo, F.photo)
async def receipt_photo_save(message: types.Message, state: FSMContext, db) -> None:
    """Сохранить file_id чека и показать мягкое подтверждение."""
    if not message.photo:
        await message.answer(
            error("нужно именно фото чека, а не текст или файл"),
            reply_markup=main_menu_keyboard(),
        )
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    file_id = message.photo[-1].file_id
    await repo.insert_receipt_photo(db, user["id"], file_id)
    await state.clear()
    await message.answer(
        "Сохранила фото чека.\n"
        "Чуть позже научусь вытаскивать из него продукты и обновлять список запасов автоматически.",
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("pantry:add"))
async def pantry_add_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PantryAddState.name)
    await callback.message.answer(
        "Напиши продукт, который есть у тебя дома. Например: гречка, курица, сыр.",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer()


@router.message(PantryAddState.name)
async def pantry_add_name(message: types.Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer(error("нужно хотя бы название продукта"))
        return
    await state.update_data(name=name)
    await state.set_state(PantryAddState.amount)
    await message.answer(
        "Сколько примерно этого продукта? Напиши количество и единицу, например:\n"
        "• 1 кг\n"
        "• 500 г\n"
        "• 2 шт\n"
        "Если напишешь только число — поставлю единицу «шт».",
        reply_markup=main_menu_keyboard(),
    )


@router.message(PantryAddState.amount)
async def pantry_add_amount(message: types.Message, state: FSMContext) -> None:
    amount, unit = _parse_amount_unit(message.text or "")
    await state.update_data(amount=amount, unit=unit)
    await state.set_state(PantryAddState.expiry)
    await message.answer(
        "Если знаешь срок годности — напиши дату в формате 2025-12-31 или 31.12.2025.\n"
        "Если не хочешь заморачиваться, напиши «нет».",
        reply_markup=main_menu_keyboard(),
    )


@router.message(PantryAddState.expiry)
async def pantry_add_expiry(message: types.Message, state: FSMContext) -> None:
    expires_at, err = _parse_expires(message.text or "")
    if err:
        await message.answer(error(err))
        return
    await state.update_data(expires_at=expires_at)
    await state.set_state(PantryAddState.category)
    await message.answer(
        "К какому разделу это отнесём?", reply_markup=_category_keyboard()
    )


@router.callback_query(lambda c: c.data and c.data.startswith("pantry:cat:"))
async def pantry_add_finish(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    category = callback.data.split(":")[2]
    data = await state.get_data()
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.create_pantry_item(
        db,
        user["id"],
        data.get("name", ""),
        data.get("amount", 1.0),
        data.get("unit", "шт"),
        data.get("expires_at"),
        category,
    )
    await state.clear()
    await callback.message.answer(
        f"Добавила в список: {data.get('name','')} — {data.get('amount',1):g} {data.get('unit','шт')}.",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer("Сохранено")


@router.callback_query(lambda c: c.data and c.data.startswith("pantry:del:"))
async def pantry_delete(callback: types.CallbackQuery, db) -> None:
    item_id = int(callback.data.split(":")[2])
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.delete_pantry_item(db, user["id"], item_id)
    await callback.answer("Убрала продукт.")
    await _render_pantry(callback.message, db)


@router.callback_query(lambda c: c.data and c.data.startswith("pantry:edit:"))
async def pantry_edit_start(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    item_id = int(callback.data.split(":")[2])
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    row = await repo.list_pantry_items(db, user["id"])
    items = rows_to_dicts(row)
    item = next((i for i in items if i.get("id") == item_id), None)
    if not item:
        await callback.answer("Не нашла этот продукт.", show_alert=True)
        return
    await state.update_data(edit_item_id=item_id)
    await state.set_state(PantryEditState.amount)
    amt = item.get("amount") or 0
    unit = item.get("unit") or "шт"
    await callback.message.answer(
        f"Сколько теперь {item.get('name')}? Сейчас записано ~{amt:g} {unit}.\n"
        "Если не хочешь менять количество — напиши «нет».",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer()


@router.message(PantryEditState.amount)
async def pantry_edit_amount(message: types.Message, state: FSMContext) -> None:
    txt = (message.text or "").strip().lower()
    if txt in {"нет", "ничего", "no"}:
        await state.update_data(edit_amount=None)
    else:
        amount, unit = _parse_amount_unit(message.text or "")
        await state.update_data(edit_amount=amount, edit_unit=unit)
    await state.set_state(PantryEditState.expiry)
    await message.answer(
        "Если хочешь, обнови срок годности (2025-12-31 или 31.12.2025).\n"
        "Если оставить как есть — напиши «нет».",
        reply_markup=main_menu_keyboard(),
    )


@router.message(PantryEditState.expiry)
async def pantry_edit_expiry(message: types.Message, state: FSMContext, db) -> None:
    data = await state.get_data()
    item_id = data.get("edit_item_id")
    if not item_id:
        await state.clear()
        await message.answer("Не получилось обновить продукт, попробуем ещё раз позже.")
        return
    txt = (message.text or "").strip().lower()
    expires_at: str | None
    if txt in {"нет", "ничего", "no"}:
        expires_at = None
    else:
        expires_at, err = _parse_expires(message.text or "")
        if err:
            await message.answer(error(err))
            return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    amount = data.get("edit_amount")
    if amount is not None:
        await repo.update_pantry_item(
            db, user["id"], int(item_id), amount=amount, expires_at=expires_at
        )
    else:
        await repo.update_pantry_item(
            db, user["id"], int(item_id), amount=None, expires_at=expires_at
        )
    await state.clear()
    await message.answer("Обновила продукт.", reply_markup=main_menu_keyboard())
    await _render_pantry(message, db)
