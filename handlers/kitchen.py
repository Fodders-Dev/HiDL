import datetime
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
from utils.rows import rows_to_dicts
from utils.time import local_date_str
from utils.texts import error
from utils.user import ensure_user
from utils.pantry import format_quantity, is_low

router = Router()
logger = logging.getLogger(__name__)

RECIPES_FILE = str(Path(__file__).resolve().parents[1] / "data" / "knowledge" / "recipes_core.json")

from utils.sender import safe_edit

DIET_TAGS = {
    "omnivore": None,
    "vegetarian": {"vegetarian", "vegan"},
    "vegan": {"vegan"},
}

RECIPE_CATEGORIES = [
    ("all", "📚 Все"),
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
    val = float(qty) * float(factor)
    unit_l = (unit or "").lower()
    if unit_l in {"г", "g", "гр", "мл", "ml"}:
        return float(int(round(val)))
    if unit_l in {"кг", "kg", "л", "l"}:
        return round(val, 2)
    if unit_l in {"шт", "piece", "pieces"}:
        return float(int(math.ceil(val - 1e-9)))
    return round(val, 2)


def _unit_kind(unit: str) -> str:
    u = (unit or "").strip().lower()
    if u in {"г", "гр", "g", "kg", "кг"}:
        return "weight"
    if u in {"мл", "ml", "л", "l"}:
        return "volume"
    if u in {"шт", "piece", "pieces"}:
        return "count"
    return "other"


def _to_base(amount: float, unit: str) -> tuple[float, str]:
    kind = _unit_kind(unit)
    u = (unit or "").strip().lower()
    val = float(amount)
    if kind == "weight":
        if u in {"kg", "кг"}:
            return val * 1000.0, kind
        return val, kind  # g
    if kind == "volume":
        if u in {"л", "l"}:
            return val * 1000.0, kind
        return val, kind  # ml
    if kind == "count":
        return val, kind
    return val, kind


def _from_base(amount_base: float, unit: str) -> float:
    kind = _unit_kind(unit)
    u = (unit or "").strip().lower()
    val = float(amount_base)
    if kind == "weight":
        if u in {"kg", "кг"}:
            return val / 1000.0
        return val
    if kind == "volume":
        if u in {"л", "l"}:
            return val / 1000.0
        return val
    if kind == "count":
        return val
    return val


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
        [InlineKeyboardButton(text="❄️ Мой холодильник", callback_data="kitchen:fridge")],
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

# --- HANDLERS: MAIN ---
@router.message(Command("kitchen"))
async def kitchen_cmd(message: types.Message):
    await message.answer("🍽 <b>Умная кухня</b>\nВыбери раздел:", reply_markup=kitchen_main_keyboard(), parse_mode="HTML")

@router.callback_query(lambda c: c.data == "kitchen:main")
async def kitchen_home(callback: types.CallbackQuery):
    await callback.message.edit_text("🍽 <b>Умная кухня</b>", reply_markup=kitchen_main_keyboard(), parse_mode="HTML")
    await callback.answer()

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
@router.callback_query(lambda c: c.data == "kitchen:quick_pick")
async def quick_pick_recipes(callback: types.CallbackQuery, db):
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    profile = await _get_meal_profile(db, user["id"])
    quick = get_quick_recipes(profile, limit=3, max_minutes=30)
    rows = [
        [
            InlineKeyboardButton(
                text=_recipe_button_text(r),
                callback_data=f"kitchen:cook_view:{r['id']}:2:all:0",
            )
        ]
        for r in quick
    ]
    rows.append([InlineKeyboardButton(text="📖 Вся книга рецептов", callback_data="kitchen:recipes")])
    rows.append([InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main")])
    lines = [f"🍳 <b>Что приготовить быстро (до 30 мин)</b>\nПитание: <b>{_diet_label(profile)}</b>\n"]
    for r in quick:
        lines.append(f"• <b>{_h(r.get('title'))}</b> ({int(r.get('time_minutes') or 15)} мин) — {_h(r.get('desc', ''))}")
    if not quick:
        lines.append("Подходящих быстрых рецептов не нашлось — посмотри всю книгу.")
    await safe_edit(
        callback.message,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "kitchen:more_food")
async def kitchen_more_food(callback: types.CallbackQuery, db):
    """Показать следующие подходящие варианты блюд без повторов."""
    from services.inbox_service import render_food_suggestions

    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
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

    all_recipes = [r for r in load_recipes() if _recipe_allowed_for_profile(r, profile)]
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
async def view_recipe(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    rid = parts[2] if len(parts) > 2 else ""
    servings = _safe_int(parts[3], 2) if len(parts) > 3 else 2
    category = parts[4] if len(parts) > 4 else "all"
    page = _safe_int(parts[5], 0) if len(parts) > 5 else 0
    recipe = get_recipe(rid)
    if not recipe:
        await callback.answer("Рецепт не найден", show_alert=True)
        return
    
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
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            serv_row,
            [InlineKeyboardButton(text="✅ Проверить продукты", callback_data=f"kitchen:cook_check:{rid}:{servings}:{category}:{page}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"kitchen:recipes_cat:{category}:{page}")],
        ]
    )
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:cook_check:"))
async def cook_check_ingredients(callback: types.CallbackQuery, state: FSMContext, db):
    parts = callback.data.split(":")
    rid = parts[2] if len(parts) > 2 else ""
    servings = _safe_int(parts[3], 1) if len(parts) > 3 else 1
    category = parts[4] if len(parts) > 4 else "all"
    page = _safe_int(parts[5], 0) if len(parts) > 5 else 0
    recipe = get_recipe(rid)
    if not recipe:
        await callback.answer("Рецепт не найден", show_alert=True)
        return
    
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
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
    rid = data.get("recipe_id")
    servings = int(data.get("servings") or 1)
    category = data.get("back_category", "all")
    page = int(data.get("back_page") or 0)
    recipe = get_recipe(rid)
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    pantry_rows = await repo.list_pantry_items(db, user["id"])
    pantry = rows_to_dicts(pantry_rows) # refresh

    base = int(recipe.get("base_servings") or 1) or 1
    factor = max(1, servings) / base
    
    # Deduct Logic
    for ing in recipe.get("ingredients") or []:
        needed = _scale_qty(ing.get("qty", 0), factor, ing.get("unit", ""))
        found = next((p for p in pantry if ing["name"].lower() in p["name"].lower()), None)
        if found:
            have_unit = found.get("unit") or ing.get("unit", "")
            need_base, kind_n = _to_base(needed, ing.get("unit", ""))
            have_base, kind_h = _to_base(float(found.get("amount") or 0), have_unit)
            if kind_n == kind_h and kind_n != "other":
                need_in_have_unit = _from_base(need_base, have_unit)
                new_amount = max(0.0, float(found.get("amount") or 0) - need_in_have_unit)
                await repo.update_pantry_item(db, user["id"], found["id"], amount=new_amount)
    
    await callback.answer("Приятного аппетита! Продукты списаны.", show_alert=True)
    
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

@router.callback_query(lambda c: c.data == "kitchen:fridge_add")
async def fridge_add_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PantryAddState.name)
    await callback.message.answer("Напиши название продукта:", reply_markup=_cancel_fsm_keyboard())
    await callback.answer()

@router.message(PantryAddState.name, lambda m: bool(m.text and not m.text.startswith("/")))
async def fridge_add_name_handler(message: types.Message, state: FSMContext):
    if _is_cancel_text(message.text):
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return
    await state.update_data(name=message.text.strip())
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

