import datetime
import io
import json
import logging
import os
import math
from html import escape as _html_escape
from pathlib import Path
from typing import Tuple, List, Optional

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from services import kitchen_ai
from utils.rows import rows_to_dicts
from utils.time import format_date_display, local_date_str
from utils.texts import error
from utils.user import ensure_user
from utils.pantry import format_quantity, is_low

router = Router()
logger = logging.getLogger(__name__)

RECIPES_FILE = str(kitchen_ai.RECIPES_FILE)

from utils.sender import safe_edit

DIET_TAGS = kitchen_ai.DIET_TAGS

RECIPE_CATEGORIES = [
    ("all", "📚 Все"),
    ("mine", "⭐ Мои рецепты"),
    ("breakfast", "🥣 Завтрак"),
    ("lunch", "🍲 Обед"),
    ("dinner", "🍽 Ужин"),
    ("snack", "🥪 Перекус"),
    ("salad", "🥗 Салаты"),
    ("fast", "⚡ Быстро до 15 мин"),
    ("budget", "💸 Бюджетно"),
    ("comfort_food", "🧡 Комфорт"),
    ("healthy", "🫶 Полезно"),
]

# --- STATES ---
class PantryAddState(StatesGroup):
    name = State()
    amount = State()
    expiry = State()
    category = State()

class PantryBulkState(StatesGroup):
    text = State()

class CartImportState(StatesGroup):
    waiting_input = State()
    confirm = State()

class PantryEditState(StatesGroup):
    amount = State()
    expiry = State()

class ShoppingAddState(StatesGroup):
    name = State()
    amount = State()

class CookingState(StatesGroup):
    recipe_id = State()
    servings = State()
    confirm = State()

# --- HELPERS ---
def _h(value: object) -> str:
    return _html_escape(str(value or ""))


def load_recipes() -> List[dict]:
    if not os.path.exists(RECIPES_FILE):
        return []
    try:
        with open(RECIPES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading recipes: {e}")
        return []

def get_recipe(rid: str) -> Optional[dict]:
    for r in load_recipes():
        if r["id"] == rid:
            return r
    return None


async def _get_meal_profile(db, user_id: int) -> str:
    wellness = await repo.get_wellness(db, user_id)
    profile = (dict(wellness) if wellness else {}).get("meal_profile", "omnivore")
    return profile if profile in {"omnivore", "vegetarian", "vegan"} else "omnivore"


def _diet_label(profile: str) -> str:
    return {"omnivore": "🥩 обычный", "vegetarian": "🥗 вегетарианец", "vegan": "🌱 веган"}.get(profile, "🥩 обычный")


def _recipe_allowed_for_profile(recipe: dict, profile: str) -> bool:
    tags = set(recipe.get("tags") or [])
    allowed = DIET_TAGS.get(profile)
    if allowed is None:
        return True
    return bool(tags.intersection(allowed))


def get_quick_recipes(profile: str, limit: int = 3, max_minutes: int = 30) -> List[dict]:
    """Return up to `limit` quick recipes matching the user's meal profile."""
    allowed = [r for r in load_recipes() if _recipe_allowed_for_profile(r, profile)]
    quick = [r for r in allowed if int(r.get("time_minutes") or 999) <= max_minutes]
    quick.sort(key=lambda r: int(r.get("time_minutes") or 999))
    return (quick or allowed)[:limit]


def _cancel_fsm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="kitchen:cancel_fsm")]]
    )


def _is_cancel_text(text: Optional[str]) -> bool:
    return (text or "").strip().lower() in {"отмена", "назад"}


def _recipe_in_category(recipe: dict, category: str) -> bool:
    if category == "all":
        return True
    if category == "mine":
        return bool(recipe.get("is_user_recipe"))
    tags = set(recipe.get("tags") or [])
    if category in {"breakfast", "lunch", "dinner", "salad"}:
        return category in tags
    if category == "snack":
        return "snack" in tags or "breakfast" in tags
    if category == "fast":
        return "fast" in tags or int(recipe.get("time_minutes") or 0) <= 15
    if category in {"budget", "comfort_food", "healthy"}:
        return category in tags
    return False


def _safe_int(value: str, default: int = 1) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _scale_qty(qty: float, factor: float, unit: str) -> float:
    return kitchen_ai.scale_qty(qty, factor, unit)


def _unit_kind(unit: str) -> str:
    return kitchen_ai.unit_kind(unit)


def _to_base(amount: float, unit: str) -> tuple[float, str]:
    return kitchen_ai.to_base(amount, unit)


def _from_base(amount_base: float, unit: str) -> float:
    return kitchen_ai.from_base(amount_base, unit)


def _format_ing_line(name: str, qty: float, unit: str) -> str:
    name = _h(name)
    q = f"{qty:g}"
    u = _h(unit).strip()
    if not u:
        return f"• {name}: {q}"
    return f"• {name}: {q} {u}"


def _recipe_button_text(recipe: dict) -> str:
    title = recipe.get("title", "Рецепт")
    t = int(recipe.get("time_minutes") or 0)
    return f"{title} · {t}м" if t else title

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

# --- KEYBOARDS ---
def kitchen_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍳 Что приготовить сейчас?", callback_data="kitchen:quick_pick")],
        [InlineKeyboardButton(text="✨ Придумать из холодильника", callback_data="kitchen:ai_chef")],
        [InlineKeyboardButton(text="📅 Меню на 3 дня", callback_data="kitchen:meal_plan")],
        [InlineKeyboardButton(text="❄️ Мой холодильник", callback_data="kitchen:fridge")],
        [InlineKeyboardButton(text="📥 Импорт корзины / чека", callback_data="kitchen:cart_import")],
        [InlineKeyboardButton(text="📖 Рецепты", callback_data="kitchen:recipes")],
        [InlineKeyboardButton(text="🛒 Список покупок", callback_data="kitchen:shoplist")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="main:menu")],
    ])

def recipes_list_keyboard(recipes: List[dict]) -> InlineKeyboardMarkup:
    rows = []
    for r in recipes:
        rows.append([InlineKeyboardButton(text=_recipe_button_text(r), callback_data=f"kitchen:cook_view:{r['id']}:2:all:0")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="kitchen:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def recipes_categories_keyboard(profile: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(RECIPE_CATEGORIES), 2):
        pair = RECIPE_CATEGORIES[i : i + 2]
        rows.append([InlineKeyboardButton(text=label, callback_data=f"kitchen:recipes_cat:{key}:0") for key, label in pair])
    rows.append([InlineKeyboardButton(text=f"Питание: {_diet_label(profile)}", callback_data="settings:mealprof")])
    rows.append([InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def recipes_paged_keyboard(items: List[dict], category: str, page: int, page_size: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    start = page * page_size
    chunk = items[start : start + page_size]
    for r in chunk:
        rows.append([InlineKeyboardButton(text=_recipe_button_text(r), callback_data=f"kitchen:cook_view:{r['id']}:2:{category}:{page}")])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"kitchen:recipes_cat:{category}:{page-1}"))
    if start + page_size < len(items):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"kitchen:recipes_cat:{category}:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="⬅️ Категории", callback_data="kitchen:recipes")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def shopping_list_keyboard(items: List[dict], scope: str = "household") -> InlineKeyboardMarkup:
    rows = []
    for item in items:
        status = "✅" if item.get("is_bought") else "⭕️"
        txt = f"{status} {item['item_name']} ({format_quantity(item.get('quantity'), item.get('unit'))})"
        rows.append([
            InlineKeyboardButton(text=txt, callback_data=f"kitchen:shop_toggle:{item['id']}:{scope}"),
            InlineKeyboardButton(text="🗑", callback_data=f"kitchen:shop_del:{item['id']}:{scope}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data=f"kitchen:shop_add:{scope}")])
    if any(i["is_bought"] for i in items):
        rows.append([InlineKeyboardButton(text="🏠 Перенести отмеченное в холодильник", callback_data=f"kitchen:shop_finish:{scope}")])
    rows.append([InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _scope_switch_row(current: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text=("✅ 👥 Общий" if current != "personal" else "👥 Общий"), callback_data="kitchen:shoplist:household"),
        InlineKeyboardButton(text=("✅ 👤 Личный" if current == "personal" else "👤 Личный"), callback_data="kitchen:shoplist:personal"),
    ]

async def _safe_answer_callback(
    callback: types.CallbackQuery,
    text: Optional[str] = None,
    *,
    show_alert: bool = False,
) -> None:
    try:
        if text is None and not show_alert:
            await callback.answer()
        else:
            await callback.answer(text, show_alert=show_alert)
    except Exception:
        pass


# --- HANDLERS: MAIN ---
@router.message(Command("kitchen"))
async def kitchen_cmd(message: types.Message):
    await message.answer("🍽 <b>Умная кухня</b>\nВыбери раздел:", reply_markup=kitchen_main_keyboard(), parse_mode="HTML")

@router.callback_query(lambda c: c.data == "kitchen:main")
async def kitchen_home(callback: types.CallbackQuery):
    await _safe_answer_callback(callback)
    await safe_edit(callback.message, "🍽 <b>Умная кухня</b>", reply_markup=kitchen_main_keyboard(), parse_mode="HTML")

# --- HANDLERS: SHOPPING LIST ---
async def _render_shoplist(callback: types.CallbackQuery, db, scope: str) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    rows = await repo.list_shopping_items(db, user["id"], scope=scope)
    items = rows_to_dicts(rows)
    text = "<b>🛒 Список покупок</b>\n"
    scope_label = "👥 общий" if scope != "personal" else "👤 личный"
    if not items:
        text += "Пока пусто. Можно добавить продукты вручную или из рецептов."
    else:
        bought_cnt = sum(1 for i in items if i["is_bought"])
        text += f"{scope_label}\nВсего: {len(items)}, отмечено: {bought_cnt}"
    
    kb = shopping_list_keyboard(items, scope=scope)
    kb.inline_keyboard.insert(0, _scope_switch_row(scope))
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:shoplist"))
async def show_shoplist(callback: types.CallbackQuery, db):
    parts = (callback.data or "").split(":")
    scope = parts[2] if len(parts) > 2 else "household"
    await _render_shoplist(callback, db, scope=scope)
    await callback.answer()


async def send_shoplist(message: types.Message, db) -> None:
    """Открыть список покупок из обычного сообщения (reply-кнопки)."""
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    rows = await repo.list_shopping_items(db, user["id"], scope="household")
    items = rows_to_dicts(rows)
    text = "<b>🛒 Список покупок</b>\n"
    if not items:
        text += "Пока пусто. Можно добавить продукты вручную или из рецептов."
    else:
        bought_cnt = sum(1 for i in items if i["is_bought"])
        text += f"👥 общий\nВсего: {len(items)}, отмечено: {bought_cnt}"
    kb = shopping_list_keyboard(items, scope="household")
    kb.inline_keyboard.insert(0, _scope_switch_row("household"))
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:shop_toggle:"))
async def toggle_shop_item(callback: types.CallbackQuery, db):
    parts = callback.data.split(":")
    iid = int(parts[2])
    scope = parts[3] if len(parts) > 3 else "household"
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    rows = await repo.list_shopping_items(db, user["id"], scope=scope) # includes both bought/unbought
    items = rows_to_dicts(rows)
    item = next((i for i in items if i["id"] == iid), None)
    if item:
        new_status = not item["is_bought"]
        await repo.mark_shopping_bought(db, user["id"], iid, new_status, scope=scope)
    await _render_shoplist(callback, db, scope=scope)
    await callback.answer()

@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:shop_del:"))
async def del_shop_item(callback: types.CallbackQuery, db):
    parts = callback.data.split(":")
    iid = int(parts[2])
    scope = parts[3] if len(parts) > 3 else "household"
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.delete_shopping_item(db, user["id"], iid, scope=scope)
    await _render_shoplist(callback, db, scope=scope)
    await callback.answer()

@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:shop_finish"))
async def finish_shopping(callback: types.CallbackQuery, db):
    parts = (callback.data or "").split(":")
    scope = parts[2] if len(parts) > 2 else "household"
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    count = await repo.complete_shopping_trip(db, user["id"], scope=scope)
    await callback.answer(f"Перенесено продуктов: {count}", show_alert=True)
    await _render_shoplist(callback, db, scope=scope)

@router.callback_query(lambda c: c.data == "kitchen:cancel_fsm")
async def kitchen_cancel_fsm(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(callback.message, "Отменила.", reply_markup=None)
    await callback.answer("Отменила.")


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:shop_add"))
async def add_shop_start(callback: types.CallbackQuery, state: FSMContext):
    parts = (callback.data or "").split(":")
    scope = parts[2] if len(parts) > 2 else "household"
    await state.set_state(ShoppingAddState.name)
    await state.update_data(shop_scope=scope)
    await callback.message.answer("Что нужно купить? (Напиши название)", reply_markup=_cancel_fsm_keyboard())
    await callback.answer()

@router.message(ShoppingAddState.name, lambda m: bool(m.text and not m.text.startswith("/")))
async def add_shop_name(message: types.Message, state: FSMContext):
    if _is_cancel_text(message.text):
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(ShoppingAddState.amount)
    await message.answer("Сколько? (например '1 кг', '2 шт' или просто число)", reply_markup=_cancel_fsm_keyboard())

@router.message(ShoppingAddState.amount, lambda m: bool(m.text and not m.text.startswith("/")))
async def add_shop_amount(message: types.Message, state: FSMContext, db):
    if _is_cancel_text(message.text):
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return
    amount, unit = _parse_amount_unit(message.text)
    data = await state.get_data()
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    scope = data.get("shop_scope") or "household"
    await repo.create_shopping_item(db, user["id"], data["name"], amount, unit, scope=scope)
    await state.clear()
    await message.answer(f"✅ Добавлено: {data['name']} ({amount} {unit}) в список покупок.")
    # Show list again? Maybe just button
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 К списку", callback_data=f"kitchen:shoplist:{scope}")]])
    await message.answer("Перейти к списку?", reply_markup=kb)

# --- HANDLERS: RECIPES ---
def _format_suggestion_line(idx: int, item: dict) -> str:
    recipe = item["recipe"]
    title = _h(recipe.get("title") or "Рецепт")
    t_min = int(recipe.get("time_minutes") or 15)
    have = item.get("have") or []
    missing = item.get("missing") or []
    lines = [f"{idx}. <b>{title}</b> · {t_min} мин"]
    if not missing:
        lines.append("   ✅ <i>Всё нужное есть дома</i>")
    else:
        miss_str = ", ".join(_h(m["name"]) for m in missing[:3])
        if len(missing) > 3:
            miss_str += f" и ещё {len(missing) - 3}"
        if have:
            have_str = ", ".join(_h(h["name"]) for h in have[:2])
            lines.append(f"   ✅ дома: {have_str} · 🛒 докупить: {miss_str}")
        else:
            lines.append(f"   🛒 докупить: {miss_str}")
    return "\n".join(lines)


def _suggestion_action_row(item: dict) -> list[InlineKeyboardButton]:
    recipe = item["recipe"]
    rid = str(recipe.get("id") or "")
    title = str(recipe.get("title") or "Рецепт")
    short_title = title[:20] + ("…" if len(title) > 20 else "")
    t_min = int(recipe.get("time_minutes") or 15)
    missing = item.get("missing") or []
    left = InlineKeyboardButton(
        text=f"🍳 {short_title} · {t_min}м",
        callback_data=f"kitchen:cook_view:{rid}:2:all:0",
    )
    if missing:
        right = InlineKeyboardButton(
            text=f"🛒 Докупить ({len(missing)})",
            callback_data=f"kitchen:add_miss:{rid}:2",
        )
    else:
        right = InlineKeyboardButton(
            text="✅ Приготовил",
            callback_data=f"kitchen:cook_fast:{rid}:2",
        )
    return [left, right]


@router.callback_query(
    lambda c: c.data and (c.data == "kitchen:quick_pick" or c.data.startswith("kitchen:quick_pick:"))
)
async def quick_pick_recipes(callback: types.CallbackQuery, db):
    await _safe_answer_callback(callback)
    parts = (callback.data or "").split(":")
    energy = parts[2] if len(parts) > 2 and parts[2] in kitchen_ai.ENERGY_MODES else "normal"
    page = _safe_int(parts[3], 0) if len(parts) > 3 else 0

    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    all_res = await kitchen_ai.suggest_meals_now(
        db, user, energy=energy, limit=(page + 1) * 3
    )
    all_items = all_res["suggestions"]
    chunk = all_items[page * 3 : (page + 1) * 3]
    if not chunk and page > 0:
        page = 0
        chunk = all_items[:3]

    profile = all_res["meal_profile"]
    slot_label = all_res["meal_slot_label"]
    pantry_count = int(all_res["pantry_count"] or 0)
    energy_label = kitchen_ai.ENERGY_MODES.get(energy, "🍳 20–30м")

    lines = [
        f"🍳 <b>Что приготовить на {_h(slot_label)}</b> ({_h(energy_label)})",
        f"Питание: <b>{_diet_label(profile)}</b> • В холодильнике: {pantry_count} поз.\n",
    ]
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=(f"• {lbl}" if code == energy else lbl),
                callback_data=f"kitchen:quick_pick:{code}:0",
            )
            for code, lbl in kitchen_ai.ENERGY_MODES.items()
        ]
    ]

    if chunk:
        for idx, item in enumerate(chunk, 1):
            lines.append(_format_suggestion_line(idx, item))
            rows.append(_suggestion_action_row(item))
    else:
        lines.append("Подходящих рецептов под фильтр питания не нашлось — посмотри всю книгу.")

    rows.append([
        InlineKeyboardButton(text="🔄 Ещё варианты", callback_data=f"kitchen:quick_pick:{energy}:{page + 1}"),
        InlineKeyboardButton(text="📅 Меню на 3 дня", callback_data="kitchen:meal_plan"),
    ])
    rows.append([
        InlineKeyboardButton(text="❄️ Холодильник", callback_data="kitchen:fridge"),
        InlineKeyboardButton(text="📖 Рецепты", callback_data="kitchen:recipes"),
    ])
    rows.append([InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main")])

    await safe_edit(
        callback.message,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "kitchen:more_food")
async def kitchen_more_food(callback: types.CallbackQuery, db):
    """Показать следующие подходящие варианты блюд без повторов."""
    from services.inbox_service import render_food_suggestions

    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    text, kb = await render_food_suggestions(db, user, raw_text="ещё", more=True)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(lambda c: c.data == "kitchen:recipes")
async def show_recipes(callback: types.CallbackQuery, db):
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    profile = await _get_meal_profile(db, user["id"])
    await callback.message.edit_text(
        "📖 <b>Книга рецептов</b>\n"
        f"Фильтр питания: <b>{_diet_label(profile)}</b>\n\n"
        "Выбери категорию:",
        reply_markup=recipes_categories_keyboard(profile),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:recipes_cat:"))
async def show_recipes_category(callback: types.CallbackQuery, db):
    _, _, category, page_s = callback.data.split(":")
    page = _safe_int(page_s, 0)

    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    profile = await _get_meal_profile(db, user["id"])

    all_recipes = [r for r in await kitchen_ai.load_recipes_for_user(db, user["id"]) if _recipe_allowed_for_profile(r, profile)]
    items = [r for r in all_recipes if _recipe_in_category(r, category)]

    title = dict(RECIPE_CATEGORIES).get(category, "Рецепты")
    if not items:
        await callback.message.edit_text(
            f"{title}\n\nПока нет рецептов под твой фильтр питания. Можно сменить профиль в настройках.",
            reply_markup=recipes_categories_keyboard(profile),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    page_size = 6
    max_page = max(0, (len(items) - 1) // page_size)
    page = max(0, min(page, max_page))

    await callback.message.edit_text(
        f"{title}\n"
        f"Питание: <b>{_diet_label(profile)}</b>\n"
        f"Страница {page+1}/{max_page+1}",
        reply_markup=recipes_paged_keyboard(items, category, page, page_size),
        parse_mode="HTML",
    )
    await callback.answer()

@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:cook_view:"))
async def view_recipe(callback: types.CallbackQuery, db):
    parts = callback.data.split(":")
    rid = parts[2] if len(parts) > 2 else ""
    servings = _safe_int(parts[3], 2) if len(parts) > 3 else 2
    category = parts[4] if len(parts) > 4 else "all"
    page = _safe_int(parts[5], 0) if len(parts) > 5 else 0
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    recipe = await kitchen_ai.get_recipe_for_user(db, user["id"], rid)
    if not recipe:
        await callback.answer("Рецепт не найден", show_alert=True)
        return

    pantry = rows_to_dicts(await repo.list_pantry_items(db, user["id"]))
    match = kitchen_ai.match_recipe_to_pantry(recipe, pantry, servings=servings)

    base = int(recipe.get("base_servings") or 1) or 1
    servings = max(1, servings)
    factor = servings / base

    title = _h(recipe.get("title", "Рецепт"))
    desc = _h(recipe.get("desc", ""))
    text = f"<b>{title}</b>\n{desc}\n\n"
    text += f"⏱ {int(recipe.get('time_minutes', 15))} мин • 🍽 {servings} порц.\n\n"
    text += f"🧾 <b>Ингредиенты</b> (на {servings} порц.):\n"
    for ing in recipe.get("ingredients") or []:
        qty = _scale_qty(ing.get("qty", 0), factor, ing.get("unit", ""))
        text += _format_ing_line(ing.get("name", "ингредиент"), qty, ing.get("unit", "")) + "\n"

    have = match["have"]
    missing = match["missing"]
    if pantry or missing:
        text += "\n❄️ <b>По холодильнику:</b> "
        if not missing:
            text += "✅ все основные продукты есть дома"
        else:
            miss_list = ", ".join(_h(m["name"]) for m in missing)
            if have:
                have_list = ", ".join(_h(h["name"]) for h in have)
                text += f"есть {have_list}; 🛒 докупить: {miss_list}"
            else:
                text += f"🛒 докупить: {miss_list}"
        text += "\n"

    steps = recipe.get("steps") or []
    if steps:
        safe_steps = [_h(s) for s in steps[:12]]
        text += "\n👩‍🍳 <b>Шаги</b>:\n" + "\n".join([f"{i+1}. {s}" for i, s in enumerate(safe_steps)])
        if len(steps) > 12:
            text += "\n…"

    serv_row = [
        InlineKeyboardButton(
            text=(
                f"✅ {i}"
                if i == servings
                else (f"🍽 {i}" if (i == 2 and servings != 2) or (i == 1 and servings == 2) else str(i))
            ),
            callback_data=f"kitchen:cook_view:{rid}:{i}:{category}:{page}",
        )
        for i in range(1, 6)
    ]
    action_row = [
        InlineKeyboardButton(
            text="🔥 Приготовил (+3 кармы)",
            callback_data=f"kitchen:cook_fast:{rid}:{servings}",
        )
    ]
    if missing:
        action_row.append(
            InlineKeyboardButton(
                text=f"🛒 Докупить ({len(missing)})",
                callback_data=f"kitchen:add_miss:{rid}:{servings}",
            )
        )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            serv_row,
            action_row,
            [InlineKeyboardButton(text="✅ Проверить продукты", callback_data=f"kitchen:cook_check:{rid}:{servings}:{category}:{page}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"kitchen:recipes_cat:{category}:{page}")],
        ]
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:add_miss:"))
async def add_recipe_missing_to_shop(callback: types.CallbackQuery, db):
    parts = callback.data.split(":")
    rid = parts[2] if len(parts) > 2 else ""
    servings = _safe_int(parts[3], 2) if len(parts) > 3 else 2
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    recipe = await kitchen_ai.get_recipe_for_user(db, user["id"], rid)
    if not recipe:
        await callback.answer("Рецепт не найден", show_alert=True)
        return

    pantry = rows_to_dicts(await repo.list_pantry_items(db, user["id"]))
    match = kitchen_ai.match_recipe_to_pantry(recipe, pantry, servings=servings)
    res = await kitchen_ai.add_missing_ingredients_to_shopping(db, user["id"], match["missing"], scope="household")
    added = res["added"]
    if added:
        names = ", ".join(x["name"] for x in added)
        await callback.answer(f"🛒 В покупки: {names[:150]}", show_alert=True)
    else:
        await callback.answer("Всё недостающее уже есть в покупках или холодильнике 👍", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:cook_fast:"))
async def cook_fast_commit(callback: types.CallbackQuery, db):
    parts = callback.data.split(":")
    rid = parts[2] if len(parts) > 2 else ""
    servings = _safe_int(parts[3], 2) if len(parts) > 3 else 2
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    res = await kitchen_ai.commit_cooked_recipe(db, user, rid, servings=servings)
    if not res.get("ok"):
        await callback.answer(str(res.get("error") or "Рецепт не найден"), show_alert=True)
        return

    recipe = res["recipe"]
    lines = [f"🍽 <b>Приготовлено: {_h(recipe.get('title'))}!</b>"]
    deducted = res.get("deducted") or []
    if deducted:
        ded_str = ", ".join(
            f"{_h(d['name'])} (ост. {format_quantity(d['remaining'], d['unit'])})"
            for d in deducted[:5]
        )
        lines.append(f"❄️ Списано из холодильника: {ded_str}")
    else:
        lines.append("❄️ В холодильнике эти продукты не были заведены — списание пропустила.")

    if res.get("marked_slot"):
        slot_ru = kitchen_ai.MEAL_SLOT_LABELS.get(str(res["marked_slot"]), str(res["marked_slot"]))
        lines.append(f"📅 В меню на сегодня слот «{slot_ru}» отмечен выполненным.")

    delta = int(res.get("karma_delta") or 0)
    score_after = res.get("score_after")
    if res.get("pulse_closed"):
        slot_ru = "Обед" if res["pulse_closed"] == "lunch" else "Ужин"
        if delta > 0 and score_after is not None:
            lines.append(f"💓 Чек-ин Пульса «{slot_ru}» закрыт (+{delta} кармы → {score_after}/100).")
        else:
            lines.append(f"💓 Чек-ин Пульса «{slot_ru}» закрыт.")
    elif delta > 0 and score_after is not None:
        lines.append(f"💓 Карма еды: +{delta} → <b>{score_after}/100</b>")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🍳 Что приготовить ещё?", callback_data="kitchen:quick_pick"),
            InlineKeyboardButton(text="📅 Меню на 3 дня", callback_data="kitchen:meal_plan"),
        ],
        [
            InlineKeyboardButton(text="❄️ Холодильник", callback_data="kitchen:fridge"),
            InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main"),
        ],
    ])
    await safe_edit(callback.message, "\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await callback.answer("Приятного аппетита! 🍽")

@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:cook_check:"))
async def cook_check_ingredients(callback: types.CallbackQuery, state: FSMContext, db):
    parts = callback.data.split(":")
    rid = parts[2] if len(parts) > 2 else ""
    servings = _safe_int(parts[3], 1) if len(parts) > 3 else 1
    category = parts[4] if len(parts) > 4 else "all"
    page = _safe_int(parts[5], 0) if len(parts) > 5 else 0
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    recipe = await kitchen_ai.get_recipe_for_user(db, user["id"], rid)
    if not recipe:
        await callback.answer("Рецепт не найден", show_alert=True)
        return
    
    pantry_rows = await repo.list_pantry_items(db, user["id"])
    pantry = rows_to_dicts(pantry_rows)
    
    base = int(recipe.get("base_servings") or 1) or 1
    servings = max(1, servings)
    factor = servings / base

    text = f"🧑‍🍳 <b>Готовим: {_h(recipe.get('title', 'Рецепт'))}</b> ({servings} порц.)\n\nПроверка продуктов:\n"
    missing = []
    
    for ing in recipe.get("ingredients") or []:
        ing_name = str(ing.get("name", ""))
        needed = _scale_qty(ing.get("qty", 0), factor, ing.get("unit", ""))
        # Find in pantry (rough matching)
        found = next((p for p in pantry if ing_name.lower() in (p.get("name", "") or "").lower()), None)
        have = float(found["amount"]) if found and found.get("amount") is not None else 0.0
        unit = ing.get("unit", "")
        have_unit = (found.get("unit") if found else "") or unit
        
        status = "✅"
        if not found:
            status = "❌ Нет"
            missing.append({"name": ing_name, "qty": needed, "unit": unit})
        else:
            need_base, kind_n = _to_base(needed, unit)
            have_base, kind_h = _to_base(have, have_unit)
            if kind_n == kind_h and kind_n != "other":
                if have_base < need_base:
                    status = "⚠️ Мало" if have_base > 0 else "❌ Нет"
                    miss_base = max(0.0, need_base - have_base)
                    miss_qty = _from_base(miss_base, unit)
                    missing.append({"name": ing_name, "qty": miss_qty, "unit": unit})
            else:
                status = "❔"
        
        text += f"{status} {_h(ing_name)}: надо {needed:g} {_h(unit)}, (есть {have:g} {_h(have_unit)})\n"
        
    text += "\nНачинаем готовить?"
    
    # Store missing for shopping list logic
    await state.set_state(CookingState.confirm)
    await state.update_data(missing=missing, servings=servings, recipe_id=rid, back_category=category, back_page=page)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Приготовил! (-продукты)", callback_data="kitchen:cook_done")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"kitchen:cook_view:{rid}:{servings}:{category}:{page}")],
    ])
    
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(lambda c: c.data == "kitchen:cook_done")
async def cook_commit(callback: types.CallbackQuery, state: FSMContext, db):
    data = await state.get_data()
    rid = str(data.get("recipe_id") or "")
    servings = int(data.get("servings") or 1)
    category = data.get("back_category", "all")
    page = int(data.get("back_page") or 0)
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    res = await kitchen_ai.commit_cooked_recipe(db, user, rid, servings=servings)
    if not res.get("ok"):
        await callback.answer("Рецепт не найден", show_alert=True)
        return

    delta = int(res.get("karma_delta") or 0)
    await callback.answer(
        f"Приятного аппетита! Продукты списаны (+{delta} кармы)." if delta else "Приятного аппетита! Продукты списаны.",
        show_alert=True,
    )

    # Check missing to add to shopping list
    missing = data.get("missing", [])
    if missing:
        text = "Некоторых продуктов не хватило. Добавить их в список покупок?\n"
        for m in missing:
            text += f"• {m['name']} ({m['qty']:g} {m['unit']})\n"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Да, добавить", callback_data="kitchen:shop_auto_add")],
            [InlineKeyboardButton(text="Нет, спасибо", callback_data=f"kitchen:cook_view:{rid}:{servings}:{category}:{page}")],
        ])
        await callback.message.edit_text(text, reply_markup=kb)
    else:
        await callback.message.edit_text("Всё готово! Приятного аппетита 😋\nЧто дальше?", reply_markup=kitchen_main_keyboard())


# --- HANDLERS: 3-4 DAY MEAL PLAN ---
_WEEKDAY_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def _format_plan_day_header(plan_date: str, start_date: str) -> str:
    try:
        dt = datetime.date.fromisoformat(plan_date)
        base = datetime.date.fromisoformat(start_date)
        diff = (dt - base).days
        wd = _WEEKDAY_SHORT[dt.weekday()]
        dd = format_date_display(plan_date)
        if diff == 0:
            return f"Сегодня ({wd}, {dd})"
        if diff == 1:
            return f"Завтра ({wd}, {dd})"
        return f"{wd}, {dd}"
    except Exception:
        return plan_date


def _render_meal_plan_payload(plan_data: dict) -> Tuple[str, InlineKeyboardMarkup]:
    days = int(plan_data.get("days") or 3)
    profile = str(plan_data.get("meal_profile") or "omnivore")
    start_date = str(plan_data.get("start_date") or "")
    items = list(plan_data.get("items") or [])
    missing = list(plan_data.get("missing_shopping") or [])

    lines = [
        f"📅 <b>Меню на {days} дн.</b> · {_diet_label(profile)}",
    ]
    by_date: dict[str, list[dict]] = {}
    for it in items:
        by_date.setdefault(str(it.get("plan_date") or ""), []).append(it)

    slot_icons = {"breakfast": "🥣", "lunch": "🍲", "dinner": "🍽"}
    slot_names = {"breakfast": "Завтрак", "lunch": "Обед", "dinner": "Ужин"}

    for d_str in sorted(by_date.keys()):
        lines.append(f"\n<b>{_h(_format_plan_day_header(d_str, start_date))}</b>")
        for slot in by_date[d_str]:
            ms = str(slot.get("meal_slot") or "lunch")
            icon = "✅" if slot.get("status") == "cooked" else slot_icons.get(ms, "🍽")
            s_lbl = slot_names.get(ms, ms)
            t_min = int(slot.get("time_minutes") or 20)
            lines.append(f"{icon} {s_lbl}: <b>{_h(slot.get('title'))}</b> · {t_min} мин")

    if missing:
        preview = ", ".join(_h(m["name"]) for m in missing[:7])
        extra = f" и ещё {len(missing) - 7}" if len(missing) > 7 else ""
        lines.append(f"\n🛒 <b>Недостающие продукты ({len(missing)}):</b> {preview}{extra}")
    else:
        lines.append("\n✅ <b>Всё нужное для этого меню уже есть дома или в покупках!</b>")

    kb_rows: list[list[InlineKeyboardButton]] = []
    if missing:
        kb_rows.append([
            InlineKeyboardButton(
                text=f"🛒 Докупить недостающее ({len(missing)})",
                callback_data=f"kitchen:meal_plan:shop:{days}",
            )
        ])
    other_days = 4 if days == 3 else 3
    kb_rows.append([
        InlineKeyboardButton(text="🔄 Пересобрать меню", callback_data=f"kitchen:meal_plan:gen:{days}"),
        InlineKeyboardButton(text=f"📅 На {other_days} дня", callback_data=f"kitchen:meal_plan:days:{other_days}"),
    ])
    kb_rows.append([
        InlineKeyboardButton(text="🍳 Приготовить сейчас", callback_data="kitchen:quick_pick"),
        InlineKeyboardButton(text="❄️ Холодильник", callback_data="kitchen:fridge"),
    ])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.message(Command("mealplan"))
async def mealplan_cmd(message: types.Message, db):
    parts = (message.text or "").split()
    days = 4 if len(parts) > 1 and parts[1].strip() == "4" else 3
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    plan_data = await kitchen_ai.get_meal_plan_view(db, user, days=days, auto_generate=True)
    text, kb = _render_meal_plan_payload(plan_data)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(
    lambda c: c.data and (c.data == "kitchen:meal_plan" or c.data.startswith("kitchen:meal_plan:"))
)
async def mealplan_callbacks(callback: types.CallbackQuery, db):
    parts = (callback.data or "").split(":")
    action = parts[2] if len(parts) > 2 else "view"
    days = _safe_int(parts[3], 3) if len(parts) > 3 else 3
    days = 4 if days == 4 else 3

    if action != "shop":
        early_toast = {
            "gen": "Меню обновлено ✨",
            "days": f"Собрала меню на {days} дн. 📅",
        }.get(action)
        await _safe_answer_callback(callback, early_toast)

    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    tz = user.get("timezone") or "UTC"
    start_date = local_date_str(datetime.datetime.utcnow(), tz)
    end_date = (datetime.date.fromisoformat(start_date) + datetime.timedelta(days=days - 1)).isoformat()

    if action == "shop":
        plan_rows = await repo.list_meal_plans_range(db, user["id"], start_date, end_date)
        missing = await kitchen_ai.compute_meal_plan_missing_shopping(db, user, plan_rows)
        res = await kitchen_ai.add_missing_ingredients_to_shopping(db, user["id"], missing, scope="household")
        added_cnt = len(res["added"])
        await _safe_answer_callback(
            callback,
            f"🛒 Добавила в список покупок: {added_cnt} поз." if added_cnt else "Всё уже в покупках 👍",
            show_alert=True,
        )
        plan_data = await kitchen_ai.get_meal_plan_view(db, user, days=days, auto_generate=True)
    elif action == "gen":
        cur_rows = await repo.list_meal_plans_range(db, user["id"], start_date, end_date)
        excl = [str(r.get("recipe_id") or "") for r in cur_rows if r.get("recipe_id")]
        plan_data = await kitchen_ai.generate_meal_plan(db, user, days=days, exclude_ids=excl)
    elif action == "days":
        plan_data = await kitchen_ai.generate_meal_plan(db, user, days=days)
    else:
        plan_data = await kitchen_ai.get_meal_plan_view(db, user, days=days, auto_generate=True)

    text, kb = _render_meal_plan_payload(plan_data)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(lambda c: c.data == "kitchen:shop_auto_add")
async def cook_auto_add_shop(callback: types.CallbackQuery, state: FSMContext, db):
    data = await state.get_data()
    missing = data.get("missing", [])
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    
    for m in missing:
        await repo.create_shopping_item(db, user["id"], m["name"], m["qty"], m["unit"])
        
    await callback.answer("Добавлено в список покупок!")
    await callback.message.edit_text("Продукты добавлены в список покупок 🛒", reply_markup=kitchen_main_keyboard())
    await state.clear()

# --- HANDLERS: FRIDGE (Legacy Pantry Logic Wrapper) ---
async def _build_fridge_payload(db, tg_user_id: int, tg_full_name: str) -> Tuple[str, InlineKeyboardMarkup]:
    user = await ensure_user(db, tg_user_id, tg_full_name)
    rows_all = await repo.list_pantry_items(db, user["id"])
    items = rows_to_dicts(rows_all)

    text = "<b>❄️ Мой холодильник</b>\n"
    if not items:
        text += (
            "Пока пусто.\n\n"
            "Если не хочется думать — я могу собрать базовый минимум в список покупок."
        )
    else:
        cats = {}
        for i in items:
            c = i.get("category", "прочее")
            cats.setdefault(c, []).append(i)

        for c, c_items in cats.items():
            text += f"\n<b>{_h((c or '').capitalize())}</b>:\n"
            for i in c_items:
                low = " ⚠️" if is_low(i) else ""
                text += f"• {_h(i.get('name',''))} — {format_quantity(i['amount'], i['unit'])}{low}\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить продукт", callback_data="kitchen:fridge_add")],
        [InlineKeyboardButton(text="⚡ Добавить списком", callback_data="kitchen:fridge_bulk")],
        [InlineKeyboardButton(text="🗑 Удалить что-то", callback_data="kitchen:fridge_del_view")],
        [InlineKeyboardButton(text="🧺 Базовый минимум в покупки", callback_data="kitchen:shop_min:add")],
        [InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main")]
    ])
    return text, kb


async def send_fridge(message: types.Message, db) -> None:
    """Открыть холодильник из обычного сообщения (/pantry)."""
    text, kb = await _build_fridge_payload(db, message.from_user.id, message.from_user.full_name)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data == "kitchen:fridge")
async def fridge_view(callback: types.CallbackQuery, db):
    text, kb = await _build_fridge_payload(db, callback.from_user.id, callback.from_user.full_name)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(lambda c: c.data == "kitchen:fridge_del_view")
async def fridge_delete_menu(callback: types.CallbackQuery, db):
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    rows_all = await repo.list_pantry_items(db, user["id"])
    items = rows_to_dicts(rows_all)
    if not items:
        await callback.answer("Тут пусто.", show_alert=True)
        await fridge_view(callback, db)
        return

    rows: list[list[InlineKeyboardButton]] = []
    for item in items[:40]:
        label = f"🗑 {item['name']} ({format_quantity(item.get('amount'), item.get('unit'))})"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"kitchen:fridge_del:{item['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="kitchen:fridge")])
    await callback.message.edit_text(
        "<b>Удалить продукт</b>\nВыбери, что убрать из холодильника:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:fridge_del:"))
async def fridge_delete_item(callback: types.CallbackQuery, db):
    item_id = int(callback.data.split(":")[2])
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.delete_pantry_item(db, user["id"], item_id)
    await callback.answer("Убрала")
    await fridge_view(callback, db)


@router.callback_query(lambda c: c.data == "kitchen:shop_min:add")
async def add_minimum_shoplist(callback: types.CallbackQuery, db):
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    profile = await _get_meal_profile(db, user["id"])

    if profile == "vegan":
        items = [
            ("овсянка", 500, "г"),
            ("рис", 1, "кг"),
            ("чечевица", 500, "г"),
            ("нут", 500, "г"),
            ("овощи (на салат)", 1, "набор"),
            ("фрукты", 1, "кг"),
            ("растительное масло", 1, "шт"),
            ("соевый соус", 1, "шт"),
        ]
    elif profile == "vegetarian":
        items = [
            ("овсянка", 500, "г"),
            ("рис", 1, "кг"),
            ("яйца", 10, "шт"),
            ("сыр", 200, "г"),
            ("йогурт", 2, "шт"),
            ("овощи (на салат)", 1, "набор"),
            ("фрукты", 1, "кг"),
            ("оливковое масло", 1, "шт"),
        ]
    else:
        items = [
            ("овсянка", 500, "г"),
            ("рис", 1, "кг"),
            ("яйца", 10, "шт"),
            ("курица/индейка", 700, "г"),
            ("овощи (на салат)", 1, "набор"),
            ("фрукты", 1, "кг"),
            ("масло", 1, "шт"),
            ("хлеб", 1, "шт"),
        ]

    for name, qty, unit in items:
        await repo.create_shopping_item(db, user["id"], name, qty, unit, category="минимум", scope="household")

    await callback.answer("Добавила базовый минимум 🛒", show_alert=True)
    await _render_shoplist(callback, db, scope="household")

async def _finish_bulk_pantry_message(message: types.Message, state: FSMContext, db, raw_text: str) -> None:
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    res = await kitchen_ai.bulk_add_pantry_items(db, user["id"], raw_text)
    await state.clear()
    added = res["added"]
    synced = res["synced_shopping"]
    if not added:
        await message.answer("Не смогла разобрать продукты — напиши через запятую, например: тофу 300 г, гречка, огурцы.")
        return
    lines = [f"🧊 <b>Добавила в холодильник ({len(added)}):</b>"]
    for item in added:
        lines.append(f"• {_h(item['name'])} ({format_quantity(item['amount'], item['unit'])})")
    if synced:
        lines.append(f"\n🗑 Вычеркнула из покупок: {_h(', '.join(synced))}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="❄️ Холодильник", callback_data="kitchen:fridge"),
            InlineKeyboardButton(text="🍳 Что приготовить?", callback_data="kitchen:quick_pick"),
        ]
    ])
    await message.answer("\n".join(lines), reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data == "kitchen:fridge_bulk")
async def fridge_bulk_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PantryBulkState.text)
    await callback.message.answer(
        "Пришли продукты одним списком через запятую или с новой строки.\n"
        "Например: <code>Тофу 300 г, гречка 1 кг, огурцы</code>",
        reply_markup=_cancel_fsm_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(PantryBulkState.text, lambda m: bool(m.text and not m.text.startswith("/")))
async def fridge_bulk_handler(message: types.Message, state: FSMContext, db):
    if _is_cancel_text(message.text):
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return
    await _finish_bulk_pantry_message(message, state, db, message.text or "")


@router.callback_query(lambda c: c.data == "kitchen:fridge_add")
async def fridge_add_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PantryAddState.name)
    await callback.message.answer(
        "Напиши название продукта (или сразу список через запятую, например «Тофу 300 г, гречка»):",
        reply_markup=_cancel_fsm_keyboard(),
    )
    await callback.answer()

@router.message(PantryAddState.name, lambda m: bool(m.text and not m.text.startswith("/")))
async def fridge_add_name_handler(message: types.Message, state: FSMContext, db):
    if _is_cancel_text(message.text):
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return
    txt = (message.text or "").strip()
    if any(sep in txt for sep in (",", ";", "\n")):
        await _finish_bulk_pantry_message(message, state, db, txt)
        return
    await state.update_data(name=txt)
    await state.set_state(PantryAddState.amount)
    await message.answer("Сколько? (например '1 кг')", reply_markup=_cancel_fsm_keyboard())

@router.message(PantryAddState.amount, lambda m: bool(m.text and not m.text.startswith("/")))
async def fridge_add_amount_handler(message: types.Message, state: FSMContext):
    if _is_cancel_text(message.text):
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return
    amt, unit = _parse_amount_unit(message.text)
    await state.update_data(amount=amt, unit=unit)
    await state.set_state(PantryAddState.category)
    
    # Simple category buttons
    cats = ["крупы", "молочка", "овощи", "фрукты", "мясо/рыба", "прочее"]
    rows = [[InlineKeyboardButton(text=c.capitalize(), callback_data=f"kitchen:cat:{c}") for c in cats[i:i+2]] for i in range(0, len(cats), 2)]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer("Выбери категорию:", reply_markup=kb)

@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:cat:"))
async def fridge_save(callback: types.CallbackQuery, state: FSMContext, db):
    cat = callback.data.split(":")[2]
    data = await state.get_data()
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    
    await repo.create_pantry_item(
        db, user["id"], data["name"], data["amount"], data["unit"], None, cat
    )
    await state.clear()
    await callback.answer("Сохранено!")
    await fridge_view(callback, db) # easy return


# --- HANDLERS: CART / RECEIPT IMPORT ---
async def _show_cart_preview(message: types.Message, state: FSMContext, parsed: dict) -> None:
    items = list(parsed.get("items") or [])
    if not parsed.get("ok") or not items:
        err = parsed.get("error") or ""
        hint = (
            "ИИ-распознавание фото сейчас недоступно — пришли позиции корзины текстом (каждый продукт с новой строки)."
            if err in {"llm_not_configured", "vision_parse_failed"}
            else "Не удалось найти продукты. Пришли скриншот корзины или список строками, например:\n<code>Тофу 300 г\nМолоко овсяное 1 л</code>"
        )
        await message.answer(hint, reply_markup=_cancel_fsm_keyboard(), parse_mode="HTML")
        return

    await state.set_state(CartImportState.confirm)
    await state.update_data(cart_preview=parsed)

    store = str(parsed.get("store") or "").strip()
    header = f"🛒 <b>Разобрала корзину{f' ({_h(store)})' if store else ''}: {len(items)} поз.</b>"
    lines = [header]
    for it in items[:20]:
        lines.append(f"• {_h(it['name'])} ({format_quantity(it['amount'], it['unit'])})")
    if len(items) > 20:
        lines.append(f"… и ещё {len(items) - 20} поз.")

    total_amt = parsed.get("total_amount")
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🧊 Всё в холодильник", callback_data="kitchen:cart_confirm:pantry")]
    ]
    if total_amt is not None and float(total_amt) > 0:
        amt_val = float(total_amt)
        lines.append(f"\n💸 Итого по чеку/корзине: <b>{amt_val:g} ₽</b>")
        rows.append([
            InlineKeyboardButton(
                text=f"🧊 В холодильник + 💸 Трата {amt_val:g} ₽",
                callback_data="kitchen:cart_confirm:both",
            )
        ])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="kitchen:cancel_fsm")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@router.callback_query(lambda c: c.data == "kitchen:cart_import")
async def cart_import_start(callback: types.CallbackQuery, state: FSMContext):
    await _safe_answer_callback(callback)
    await state.set_state(CartImportState.waiting_input)
    await callback.message.answer(
        "📥 <b>Импорт корзины / чека</b>\n\n"
        "Пришли <b>скриншот (фото)</b> корзины или чека (например, из Перекрёстка) — либо вставь <b>список покупок текстом</b>.\n"
        "Я сама разберу продукты, вычеркну их из списка покупок и положу в холодильник.",
        reply_markup=_cancel_fsm_keyboard(),
        parse_mode="HTML",
    )


@router.message(CartImportState.waiting_input, F.photo)
async def cart_import_photo(message: types.Message, state: FSMContext):
    photo = message.photo[-1]
    buf = io.BytesIO()
    dl = await message.bot.download(photo, destination=buf)
    img_bytes = buf.getvalue()
    if not img_bytes and dl is not None:
        if hasattr(dl, "getvalue"):
            img_bytes = dl.getvalue()
        elif isinstance(dl, (bytes, bytearray)):
            img_bytes = bytes(dl)
    parsed = await kitchen_ai.parse_cart_input(
        raw_text=message.caption or "",
        image_bytes=img_bytes,
        image_mime="image/jpeg",
    )
    await _show_cart_preview(message, state, parsed)


@router.message(CartImportState.waiting_input, lambda m: bool(m.text and not m.text.startswith("/")))
async def cart_import_text(message: types.Message, state: FSMContext):
    if _is_cancel_text(message.text):
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return
    parsed = await kitchen_ai.parse_cart_input(raw_text=message.text or "")
    await _show_cart_preview(message, state, parsed)


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:cart_confirm:"))
async def cart_import_confirm(callback: types.CallbackQuery, state: FSMContext, db):
    await _safe_answer_callback(callback)
    parts = (callback.data or "").split(":")
    mode = parts[2] if len(parts) > 2 else "pantry"
    data = await state.get_data()
    preview = data.get("cart_preview") or {}
    items = list(preview.get("items") or [])
    if not items:
        await state.clear()
        await safe_edit(callback.message, "Корзина пуста или уже была добавлена.", reply_markup=kitchen_main_keyboard())
        return

    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    total_amt = preview.get("total_amount") if mode == "both" else None
    res = await kitchen_ai.import_cart_items(
        db,
        user,
        items,
        total_amount=total_amt,
        sync_shopping=True,
    )
    await state.clear()

    added = res.get("added") or []
    synced = res.get("synced_shopping") or []
    lines = [f"✅ <b>Перенесла в холодильник: {len(added)} поз.</b>"]
    for it in added[:12]:
        lines.append(f"• {_h(it['name'])} ({format_quantity(it['amount'], it['unit'])})")
    if len(added) > 12:
        lines.append(f"… и ещё {len(added) - 12} поз.")
    if synced:
        lines.append(f"\n🗑 Вычеркнула из списка покупок: {_h(', '.join(synced[:10]))}")
    if res.get("total_amount"):
        lines.append(f"💸 Записала трату: <b>{float(res['total_amount']):g} ₽</b> (продукты)")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Придумать блюдо из холодильника", callback_data="kitchen:ai_chef")],
        [
            InlineKeyboardButton(text="❄️ Мой холодильник", callback_data="kitchen:fridge"),
            InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main"),
        ],
    ])
    await safe_edit(callback.message, "\n".join(lines), reply_markup=kb, parse_mode="HTML")


# --- HANDLERS: AI CHEF ---
def _render_ai_recipes_list(
    recipes: list[dict],
    *,
    profile: str,
    slot_label: str,
    pantry_count: int,
    expiring_names: list[str],
) -> Tuple[str, InlineKeyboardMarkup]:
    lines = [
        f"✨ <b>ИИ-шеф: идеи на {_h(slot_label)}</b>",
        f"Питание: <b>{_diet_label(profile)}</b> • В холодильнике: {pantry_count} поз.",
    ]
    if expiring_names:
        lines.append(f"⏰ В первую очередь пускаем в дело: {_h(', '.join(expiring_names[:5]))}")
    lines.append("")

    rows: list[list[InlineKeyboardButton]] = []
    for idx, rec in enumerate(recipes, 1):
        title = str(rec.get("title") or f"Блюдо {idx}")
        t_min = int(rec.get("time_minutes") or 20)
        desc = str(rec.get("desc") or "").strip()
        lines.append(f"<b>{idx}. {_h(title)}</b> · {t_min} мин")
        if desc:
            lines.append(f"<i>{_h(desc)}</i>")
        rows.append([
            InlineKeyboardButton(
                text=f"👁 {idx}. {title[:32]}",
                callback_data=f"kitchen:ai_view:{idx - 1}",
            )
        ])

    rows.append([InlineKeyboardButton(text="🔄 Ещё варианты", callback_data="kitchen:ai_chef")])
    rows.append([
        InlineKeyboardButton(text="❄️ Холодильник", callback_data="kitchen:fridge"),
        InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(lambda c: c.data == "kitchen:ai_chef")
async def ai_chef_suggest(callback: types.CallbackQuery, state: FSMContext, db):
    await _safe_answer_callback(callback, "Подбираю идеи из холодильника...")
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    res = await kitchen_ai.generate_ai_recipes_from_pantry(db, user)
    if not res.get("ok") or not res.get("recipes"):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🍳 Подбор из базы", callback_data="kitchen:quick_pick")],
            [InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main")],
        ])
        msg = (
            "Не получилось связаться с ИИ-шефом прямо сейчас — можно воспользоваться мгновенным подбором из базы рецептов."
        )
        await safe_edit(callback.message, msg, reply_markup=kb)
        return

    await state.update_data(
        ai_recipes=res["recipes"],
        ai_chef_meta={
            "profile": res["meal_profile"],
            "slot_label": res["meal_slot_label"],
            "pantry_count": res["pantry_count"],
            "expiring_names": res["expiring_names"],
        },
    )
    text, kb = _render_ai_recipes_list(
        res["recipes"],
        profile=res["meal_profile"],
        slot_label=res["meal_slot_label"],
        pantry_count=int(res["pantry_count"] or 0),
        expiring_names=list(res["expiring_names"] or []),
    )
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:ai_view:"))
async def ai_chef_view_recipe(callback: types.CallbackQuery, state: FSMContext):
    await _safe_answer_callback(callback)
    parts = (callback.data or "").split(":")
    idx = _safe_int(parts[2], -1) if len(parts) > 2 else -1
    data = await state.get_data()
    recipes = list(data.get("ai_recipes") or [])
    if idx < 0 or idx >= len(recipes):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✨ Подобрать заново", callback_data="kitchen:ai_chef")],
            [InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main")],
        ])
        await safe_edit(callback.message, "Эти варианты уже устарели — давай подберём свежие.", reply_markup=kb)
        return

    rec = recipes[idx]
    lines = [f"✨ <b>{_h(rec.get('title'))}</b>"]
    if rec.get("desc"):
        lines.append(_h(rec["desc"]))
    lines.append(f"\n⏱ {int(rec.get('time_minutes') or 20)} мин • 🍽 {int(rec.get('base_servings') or 2)} порц.")
    lines.append("\n🧾 <b>Ингредиенты:</b>")
    for ing in rec.get("ingredients") or []:
        lines.append(_format_ing_line(ing.get("name", ""), float(ing.get("qty") or 1), str(ing.get("unit") or "")))
    lines.append("\n👩‍🍳 <b>Шаги:</b>")
    for s_idx, step in enumerate(rec.get("steps") or [], 1):
        lines.append(f"{s_idx}. {_h(step)}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ В мои рецепты", callback_data=f"kitchen:ai_save:{idx}")],
        [
            InlineKeyboardButton(text="⬅️ К вариантам", callback_data="kitchen:ai_back"),
            InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main"),
        ],
    ])
    await safe_edit(callback.message, "\n".join(lines), reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:ai_save:"))
async def ai_chef_save_recipe(callback: types.CallbackQuery, state: FSMContext, db):
    parts = (callback.data or "").split(":")
    idx = _safe_int(parts[2], -1) if len(parts) > 2 else -1
    data = await state.get_data()
    recipes = list(data.get("ai_recipes") or [])
    if idx < 0 or idx >= len(recipes):
        await _safe_answer_callback(callback, "Вариант не найден, сгенерируй заново.", show_alert=True)
        return

    rec = dict(recipes[idx])
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    saved = await repo.upsert_user_recipe(
        db,
        int(user["id"]),
        recipe_key=str(rec.get("saved_key") or "").strip() or None,
        title=str(rec.get("title") or "Рецепт"),
        desc=str(rec.get("desc") or ""),
        tags=list(rec.get("tags") or []),
        base_servings=int(rec.get("base_servings") or 2),
        time_minutes=int(rec.get("time_minutes") or 20),
        ingredients=list(rec.get("ingredients") or []),
        steps=list(rec.get("steps") or []),
        source="ai_chef",
        source_label="ИИ-шеф",
    )
    rec["saved_key"] = saved.get("id")
    recipes[idx] = rec
    await state.update_data(ai_recipes=recipes)

    await _safe_answer_callback(callback, "Сохранила в «Мои рецепты» ⭐")
    rid = str(saved.get("id") or "")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍳 Приготовить / списать продукты", callback_data=f"kitchen:cook_view:{rid}:2:mine:0")],
        [
            InlineKeyboardButton(text="⭐ Мои рецепты", callback_data="kitchen:recipes_cat:mine:0"),
            InlineKeyboardButton(text="⬅️ К вариантам", callback_data="kitchen:ai_back"),
        ],
    ])
    await safe_edit(
        callback.message,
        f"⭐ Рецепт <b>«{_h(saved.get('title'))}»</b> сохранён в <b>«Мои рецепты»</b>.\n"
        "Теперь он участвует и в подборе блюд, и его можно сразу приготовить со списанием продуктов.",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "kitchen:ai_back")
async def ai_chef_back(callback: types.CallbackQuery, state: FSMContext, db):
    await _safe_answer_callback(callback)
    data = await state.get_data()
    recipes = list(data.get("ai_recipes") or [])
    if not recipes:
        await ai_chef_suggest(callback, state, db)
        return
    meta = data.get("ai_chef_meta") or {}
    text, kb = _render_ai_recipes_list(
        recipes,
        profile=str(meta.get("profile") or "omnivore"),
        slot_label=str(meta.get("slot_label") or "блюдо"),
        pantry_count=int(meta.get("pantry_count") or 0),
        expiring_names=list(meta.get("expiring_names") or []),
    )
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")


