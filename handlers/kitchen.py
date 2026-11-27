import asyncio
import datetime
import io
import json
import logging
import os
import math
import re
import secrets
import time
from html import escape as _html_escape
from pathlib import Path
from typing import Any, Dict, Tuple, List, Optional

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import food_menu_keyboard, main_menu_keyboard
from services import fitness_nutrition, household_service, kitchen_ai
from utils.profile import is_student_mode
from utils.rows import rows_to_dicts
from utils.time import format_date_display, format_time_local, local_date_str
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


_ADAPTED_RECIPES: Dict[Tuple[int, str], Tuple[float, Dict[str, Any]]] = {}
_ADAPTED_TTL_SEC = 7200.0
_ADAPTED_MAX_ITEMS = 200


def _prune_adapted_recipes(now_ts: float) -> None:
    expired = [k for k, (ts, _) in _ADAPTED_RECIPES.items() if (now_ts - ts) > _ADAPTED_TTL_SEC]
    for k in expired:
        _ADAPTED_RECIPES.pop(k, None)
    if len(_ADAPTED_RECIPES) > _ADAPTED_MAX_ITEMS:
        oldest = sorted(_ADAPTED_RECIPES.items(), key=lambda item: item[1][0])
        for k, _ in oldest[: len(_ADAPTED_RECIPES) - _ADAPTED_MAX_ITEMS]:
            _ADAPTED_RECIPES.pop(k, None)


def _store_adapted_recipe(user_id: int, aid: str, data: Dict[str, Any]) -> None:
    now_ts = time.monotonic()
    _prune_adapted_recipes(now_ts)
    _ADAPTED_RECIPES[(int(user_id), str(aid))] = (now_ts, dict(data))


def _get_adapted_recipe(user_id: int, aid: str) -> Optional[Dict[str, Any]]:
    now_ts = time.monotonic()
    _prune_adapted_recipes(now_ts)
    entry = _ADAPTED_RECIPES.get((int(user_id), str(aid)))
    return dict(entry[1]) if entry else None


_SHARED_COOKED_MEALS: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_SHARED_COOKED_TTL_SEC = 86400.0
_SHARED_COOKED_MAX = 128

_COOK_LOG_BASE: Dict[Tuple[int, int], Tuple[float, Dict[str, Any]]] = {}
_COOK_LOG_BASE_TTL_SEC = 86400.0
_COOK_LOG_BASE_MAX = 128

_CART_ALBUM_DEBOUNCE_SEC = 1.5
_CART_ALBUM_BUFFERS: Dict[str, Dict[str, Any]] = {}


def _prune_ttl_map(store: Dict[Any, Tuple[float, Dict[str, Any]]], ttl: float, max_items: int) -> None:
    now_ts = time.monotonic()
    for k in [k for k, (ts, _) in store.items() if (now_ts - ts) > ttl]:
        store.pop(k, None)
    if len(store) > max_items:
        oldest = sorted(store.items(), key=lambda it: it[1][0])
        for k, _ in oldest[: len(store) - max_items]:
            store.pop(k, None)


def _store_shared_cooked_meal(share_id: str, payload: Dict[str, Any]) -> None:
    _prune_ttl_map(_SHARED_COOKED_MEALS, _SHARED_COOKED_TTL_SEC, _SHARED_COOKED_MAX)
    _SHARED_COOKED_MEALS[share_id] = (time.monotonic(), dict(payload))


def _get_shared_cooked_meal(share_id: str) -> Optional[Dict[str, Any]]:
    _prune_ttl_map(_SHARED_COOKED_MEALS, _SHARED_COOKED_TTL_SEC, _SHARED_COOKED_MAX)
    entry = _SHARED_COOKED_MEALS.get(str(share_id))
    return entry[1] if entry else None


def _store_cook_log_base(user_id: int, log_id: int, payload: Dict[str, Any]) -> None:
    _prune_ttl_map(_COOK_LOG_BASE, _COOK_LOG_BASE_TTL_SEC, _COOK_LOG_BASE_MAX)
    _COOK_LOG_BASE[(int(user_id), int(log_id))] = (time.monotonic(), dict(payload))


def _get_cook_log_base(user_id: int, log_id: int) -> Optional[Dict[str, Any]]:
    _prune_ttl_map(_COOK_LOG_BASE, _COOK_LOG_BASE_TTL_SEC, _COOK_LOG_BASE_MAX)
    entry = _COOK_LOG_BASE.get((int(user_id), int(log_id)))
    return dict(entry[1]) if entry else None


def _short_pantry_label(name: str, max_len: int = 18) -> str:
    clean = re.sub(r"\([^)]*\)", "", str(name or "")).strip()
    clean = re.sub(r"\s+", " ", clean)
    if len(clean) <= max_len:
        return clean
    words = clean.split()
    out: List[str] = []
    cur_len = 0
    for w in words:
        nxt = cur_len + (1 if out else 0) + len(w)
        if nxt > max_len:
            break
        out.append(w)
        cur_len = nxt
    if out:
        return " ".join(out)
    return clean[: max(1, max_len - 1)].rstrip() + "…"


def _build_tail_cleanup_rows(deducted: List[Dict[str, Any]]) -> List[List[InlineKeyboardButton]]:
    rows: List[List[InlineKeyboardButton]] = []
    for d in deducted:
        pid = d.get("pantry_id")
        if pid is None:
            continue
        rem = float(d.get("remaining") or 0)
        before = float(d.get("before") or 0)
        unit = str(d.get("unit") or "шт")
        if rem <= 0.01:
            continue
        small_ratio = before > 0 and rem <= (before * 0.35 + 1e-6)
        small_weight = unit.strip().lower() in {"г", "g", "гр", "мл", "ml"} and rem <= 150.0
        if small_ratio or small_weight:
            lbl = _short_pantry_label(str(d.get("name") or "Продукт"), 16)
            qty_txt = format_quantity(rem, unit)
            rows.append([
                InlineKeyboardButton(
                    text=f"🗑 Кончилось: {lbl} ({qty_txt})",
                    callback_data=f"kitchen:tail_del:{int(pid)}",
                )
            ])
        if len(rows) >= 3:
            break
    return rows


def _build_portion_edit_row(log_id: int, current_serv: float) -> List[InlineKeyboardButton]:
    opts = [(0.5, "½ порц."), (1.0, "1 порц."), (2.0, "2 порц.")]
    row: List[InlineKeyboardButton] = []
    for val, lbl in opts:
        if abs(current_serv - val) < 0.05:
            continue
        val_str = "0.5" if val == 0.5 else str(int(val))
        row.append(
            InlineKeyboardButton(
                text=f"✏️ Я съел {lbl}",
                callback_data=f"kitchen:cook_edit_log:{int(log_id)}:{val_str}",
            )
        )
    return row


class RecipeImportState(StatesGroup):
    waiting_input = State()
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
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(.*)$", raw)
    if m:
        try:
            amount = float(m.group(1))
        except Exception:
            amount = 1.0
        tail = (m.group(2) or "").strip().lower()
    else:
        parts = raw.split()
        try:
            amount = float(parts[0])
            tail = " ".join(parts[1:]).lower()
        except Exception:
            amount = 1.0
            tail = raw.lower()
    unit = "шт"
    if re.search(r"(?:^|\b|\d)(?:кг|kg)\b", tail) or tail in {"кг", "kg"}:
        unit = "kg"
    elif re.search(r"(?:^|\b|\d)(?:мл|ml)\b", tail) or tail in {"мл", "ml"}:
        unit = "ml"
    elif re.search(r"(?:^|\b|\d)(?:г|гр|gram|grams)\b", tail) or tail in {"г", "гр", "g"}:
        unit = "g"
    elif re.search(r"(?:^|\b|\d)(?:л|литр\w*|l)\b", tail) or tail in {"л", "l"}:
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
    return food_menu_keyboard()

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
    rows.append([InlineKeyboardButton(text="📥 Импорт рецепта (фото / ссылка)", callback_data="kitchen:recipe_import")])
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

def shopping_list_keyboard(
    items: List[dict],
    scope: str = "household",
    page: int = 0,
    page_size: int = 12,
) -> InlineKeyboardMarkup:
    rows = []
    max_page = max(0, (len(items) - 1) // page_size) if items else 0
    eff_page = max(0, min(int(page or 0), max_page))
    start = eff_page * page_size
    slice_items = items[start : start + page_size]

    for item in slice_items:
        status = "✅" if item.get("is_bought") else "⭕️"
        txt = f"{status} {item['item_name']} ({format_quantity(item.get('quantity'), item.get('unit'))})"
        rows.append([
            InlineKeyboardButton(text=txt, callback_data=f"kitchen:shop_toggle:{item['id']}:{scope}:{eff_page}"),
            InlineKeyboardButton(text="🗑", callback_data=f"kitchen:shop_del:{item['id']}:{scope}:{eff_page}"),
        ])

    if max_page > 0:
        nav: list[InlineKeyboardButton] = []
        if eff_page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"kitchen:shoplist:{scope}:{eff_page - 1}"))
        nav.append(InlineKeyboardButton(text=f"📄 {eff_page + 1}/{max_page + 1}", callback_data=f"kitchen:shoplist:{scope}:{eff_page}"))
        if eff_page < max_page:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"kitchen:shoplist:{scope}:{eff_page + 1}"))
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data=f"kitchen:shop_add:{scope}")])
    if any(not i.get("is_bought") for i in items):
        rows.append([InlineKeyboardButton(text="📋 Скопировать список", callback_data=f"kitchen:shop_export:{scope}")])
    if any(i.get("is_bought") for i in items):
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
async def build_kitchen_home_payload(db, user: dict) -> Tuple[str, InlineKeyboardMarkup]:
    """Собрать главный экран кухни с кнопкой быстрого перехода к текущему приёму пищи по меню."""
    tz = str(user.get("timezone") or "UTC")
    now = datetime.datetime.utcnow()
    local_date = local_date_str(now, tz)
    try:
        local_hour = int(format_time_local(now, tz).split(":")[0])
    except Exception:
        local_hour = 12
    slot = kitchen_ai.infer_meal_slot(local_hour)

    planned_rows = rows_to_dicts(await repo.list_meal_plans_range(db, int(user["id"]), local_date, local_date))
    uncooked = [r for r in planned_rows if r.get("status") != "cooked" and r.get("recipe_id")]
    pick = next((r for r in uncooked if r.get("meal_slot") == slot), uncooked[0] if uncooked else None)

    base_kb = food_menu_keyboard()
    rows = [list(r) for r in base_kb.inline_keyboard]
    if pick:
        p_slot = str(pick.get("meal_slot") or slot)
        slot_acc = {"breakfast": "завтрак", "lunch": "обед", "dinner": "ужин"}.get(p_slot, "приём пищи")
        slot_prep = {"breakfast": "завтрак", "lunch": "обед", "dinner": "ужин"}.get(p_slot, "сегодня")
        slot_icon = {"breakfast": "🥣", "lunch": "🍲", "dinner": "🍽"}.get(p_slot, "🍳")
        title = str(pick.get("title") or "Блюдо").strip()
        t_min = int(pick.get("time_minutes") or 15)
        rows.insert(
            0,
            [
                InlineKeyboardButton(
                    text=f"{slot_icon} Мой {slot_acc}: {title[:22]}",
                    callback_data=f"kitchen:cook_view:{pick['recipe_id']}:1:plan:0",
                )
            ],
        )
        text = (
            "🍽 <b>Умная кухня</b>\n"
            f"📅 <b>По твоему меню на {slot_prep} сегодня:</b> {_h(title)} · {t_min} мин\n"
            "<i>Нажми на кнопку с блюдом ниже, чтобы открыть рецепт, или выбери 1 из вариантов в «Что приготовить» / «ИИ-шефе».</i>"
        )
    else:
        text = (
            "🍽 <b>Умная кухня</b>\n"
            "• <b>📅 Меню на 3 дня</b> — твой готовый план завтрака, обеда и ужина\n"
            "• <b>🍳 Что приготовить сейчас? / ✨ ИИ-шеф</b> — подберут 3 варианта на выбор (выбираешь один)"
        )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("kitchen"))
async def kitchen_cmd(message: types.Message, db):
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    text, kb = await build_kitchen_home_payload(db, user)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data == "kitchen:main")
async def kitchen_home(callback: types.CallbackQuery, db):
    await _safe_answer_callback(callback)
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    text, kb = await build_kitchen_home_payload(db, user)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")

# --- HANDLERS: SHOPPING LIST ---
async def _render_shoplist(callback: types.CallbackQuery, db, scope: str, page: int = 0) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    rows = await repo.list_shopping_items(db, user["id"], scope=scope)
    items = rows_to_dicts(rows)
    text = "<b>🛒 Список покупок</b>\n"
    scope_label = "👥 общий" if scope != "personal" else "👤 личный"
    if not items:
        text += (
            "Пока пусто.\n"
            "Напиши в чат <code>купить молоко и хлеб</code> или добавь продукты кнопками ниже."
        )
    else:
        bought_cnt = sum(1 for i in items if i["is_bought"])
        text += f"{scope_label}\nВсего: {len(items)}, отмечено: {bought_cnt}"

    kb = shopping_list_keyboard(items, scope=scope, page=page)
    kb.inline_keyboard.insert(0, _scope_switch_row(scope))
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:shoplist"))
async def show_shoplist(callback: types.CallbackQuery, db):
    parts = (callback.data or "").split(":")
    scope = parts[2] if len(parts) > 2 else "household"
    page = _safe_int(parts[3], 0) if len(parts) > 3 else 0
    await _render_shoplist(callback, db, scope=scope, page=page)
    await callback.answer()


async def send_shoplist(message: types.Message, db) -> None:
    """Открыть список покупок из обычного сообщения (reply-кнопки)."""
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    rows = await repo.list_shopping_items(db, user["id"], scope="household")
    items = rows_to_dicts(rows)
    text = "<b>🛒 Список покупок</b>\n"
    if not items:
        text += (
            "Пока пусто.\n"
            "Напиши в чат <code>купить молоко и хлеб</code> или добавь продукты кнопками ниже."
        )
    else:
        bought_cnt = sum(1 for i in items if i["is_bought"])
        text += f"👥 общий\nВсего: {len(items)}, отмечено: {bought_cnt}"
    kb = shopping_list_keyboard(items, scope="household", page=0)
    kb.inline_keyboard.insert(0, _scope_switch_row("household"))
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:shop_toggle:"))
async def toggle_shop_item(callback: types.CallbackQuery, db):
    parts = callback.data.split(":")
    iid = int(parts[2])
    scope = parts[3] if len(parts) > 3 else "household"
    page = _safe_int(parts[4], 0) if len(parts) > 4 else 0
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    rows = await repo.list_shopping_items(db, user["id"], scope=scope) # includes both bought/unbought
    items = rows_to_dicts(rows)
    item = next((i for i in items if i["id"] == iid), None)
    if item:
        new_status = not item["is_bought"]
        await repo.mark_shopping_bought(db, user["id"], iid, new_status, scope=scope)
    await _render_shoplist(callback, db, scope=scope, page=page)
    await callback.answer()

@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:shop_del:"))
async def del_shop_item(callback: types.CallbackQuery, db):
    parts = callback.data.split(":")
    iid = int(parts[2])
    scope = parts[3] if len(parts) > 3 else "household"
    page = _safe_int(parts[4], 0) if len(parts) > 4 else 0
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.delete_shopping_item(db, user["id"], iid, scope=scope)
    await _render_shoplist(callback, db, scope=scope, page=page)
    await callback.answer()

@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:shop_finish"))
async def finish_shopping(callback: types.CallbackQuery, db):
    parts = (callback.data or "").split(":")
    scope = parts[2] if len(parts) > 2 else "household"
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    count = await repo.complete_shopping_trip(db, user["id"], scope=scope)
    await callback.answer(f"Перенесено продуктов: {count}", show_alert=True)
    await _render_shoplist(callback, db, scope=scope)


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:shop_export:"))
async def export_shoplist(callback: types.CallbackQuery, db):
    parts = (callback.data or "").split(":")
    scope = parts[2] if len(parts) > 2 else "household"
    await _safe_answer_callback(callback)
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    items = [
        r for r in rows_to_dicts(await repo.list_shopping_items(db, user["id"], scope=scope))
        if not r.get("is_bought")
    ]
    if not items:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К списку", callback_data=f"kitchen:shoplist:{scope}")],
        ])
        await safe_edit(callback.message, "В списке покупок сейчас нет активных позиций.", reply_markup=kb)
        return

    mapped = [
        {
            "name": str(i.get("item_name") or ""),
            "qty": float(i.get("quantity") or 1.0),
            "unit": str(i.get("unit") or "шт"),
        }
        for i in items
    ]
    scope_title = "Список покупок (личный)" if scope == "personal" else "Общий список покупок дома"
    exp = kitchen_ai.build_shopping_export(mapped, header=scope_title, preserve_explicit=True)
    plain = exp["plain_text"][:3600]
    text = (
        "📋 <b>Список для копирования</b>\n"
        "<i>Нажми на блок ниже, чтобы скопировать и вставить в доставку или ИИ:</i>\n\n"
        f"<pre>{_h(plain)}</pre>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬅️ К списку", callback_data=f"kitchen:shoplist:{scope}"),
            InlineKeyboardButton(text="📅 Меню на 3 дня", callback_data="kitchen:meal_plan"),
        ],
        [InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main")],
    ])
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")

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
    nutr = item.get("nutrition") or {}
    nutr_str = (
        f" · ~{int(nutr['kcal'])} ккал (Б{float(nutr['protein_g']):g}/Ж{float(nutr['fat_g']):g}/У{float(nutr['carbs_g']):g})"
        if nutr.get("kcal")
        else ""
    )
    have = item.get("have") or []
    missing = item.get("missing") or []
    lines = [f"{idx}. <b>{title}</b> · {t_min} мин{nutr_str}"]
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


def _suggestion_action_row(
    item: dict,
    *,
    back_category: str = "all",
    back_page: int = 0,
) -> list[InlineKeyboardButton]:
    recipe = item["recipe"]
    rid = str(recipe.get("id") or "")
    title = str(recipe.get("title") or "Рецепт")
    short_title = title[:20] + ("…" if len(title) > 20 else "")
    t_min = int(recipe.get("time_minutes") or 15)
    missing = item.get("missing") or []
    left = InlineKeyboardButton(
        text=f"🍳 {short_title} · {t_min}м",
        callback_data=f"kitchen:cook_view:{rid}:2:{back_category}:{back_page}",
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
    advice = all_res.get("daily_advice") or {}

    lines = [
        f"🍳 <b>Что приготовить на {_h(slot_label)}</b> ({_h(energy_label)})",
        f"Питание: <b>{_diet_label(profile)}</b> • В холодильнике: {pantry_count} поз.",
    ]
    if advice.get("summary_line"):
        lines.append(f"🔥 {_h(advice['summary_line'])}")
    if advice.get("eat_more"):
        lines.append(f"💡 Лучше добрать: {_h(advice['eat_more'][0])}")
    if advice.get("go_easy_on"):
        lines.append(f"⚠️ Поаккуратнее с: {_h(advice['go_easy_on'][0])}")
    lines.append("")
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
        lines.append(f"<i>Выбери 1 из {len(chunk)} вариантов — нажми на название, чтобы открыть рецепт:</i>")
        for idx, item in enumerate(chunk, 1):
            lines.append(_format_suggestion_line(idx, item))
            rows.append(_suggestion_action_row(item))
    else:
        lines.append("Подходящих рецептов под фильтр питания не нашлось — посмотри всю книгу.")

    rows.append([
        InlineKeyboardButton(text="🔄 Ещё варианты", callback_data=f"kitchen:quick_pick:{energy}:{page + 1}"),
        InlineKeyboardButton(text="🛒 Не хватает 1–2", callback_data="kitchen:almost_ready:0"),
    ])
    rows.append([
        InlineKeyboardButton(text="📅 Меню на 3 дня", callback_data="kitchen:meal_plan"),
        InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
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


@router.callback_query(
    lambda c: c.data and (c.data == "kitchen:almost_ready" or c.data.startswith("kitchen:almost_ready:"))
)
async def almost_ready_recipes(callback: types.CallbackQuery, db):
    await _safe_answer_callback(callback)
    parts = (callback.data or "").split(":")
    page = max(0, _safe_int(parts[2], 0)) if len(parts) > 2 else 0

    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    res = await kitchen_ai.suggest_almost_ready_recipes(db, user, max_missing=2, limit=40)
    all_items = list(res.get("suggestions") or [])
    page_size = 4
    max_page = max(0, (len(all_items) - 1) // page_size)
    page = min(page, max_page)
    chunk = all_items[page * page_size : (page + 1) * page_size]

    profile = str(res.get("meal_profile") or "omnivore")
    pantry_count = int(res.get("pantry_count") or 0)
    total_matches = int(res.get("total_matches") or 0)
    advice = res.get("daily_advice") or {}

    lines = [
        "🛒 <b>Не хватает 1–2 продуктов</b>",
        f"Питание: <b>{_diet_label(profile)}</b> • Дома: {pantry_count} поз. • Найдено: {total_matches}",
    ]
    if advice.get("summary_line"):
        lines.append(f"🔥 {_h(advice['summary_line'])}")
    lines.append("")

    rows: list[list[InlineKeyboardButton]] = []
    if chunk:
        for idx, item in enumerate(chunk, page * page_size + 1):
            lines.append(_format_suggestion_line(idx, item))
            exp_used = item.get("uses_expiring") or []
            if exp_used:
                lines.append(f"   ⏰ пустит в дело: {_h(', '.join(exp_used[:3]))}")
            rows.append(_suggestion_action_row(item, back_category="almost", back_page=page))
    else:
        lines.append("Рецептов с нехваткой ровно 1–2 продуктов под твой холодильник пока не нашлось.")

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"kitchen:almost_ready:{page - 1}"))
    if (page + 1) * page_size < len(all_items):
        nav.append(InlineKeyboardButton(text="Ещё ➡️", callback_data=f"kitchen:almost_ready:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton(text="🍳 Готовить из дома", callback_data="kitchen:quick_pick"),
        InlineKeyboardButton(text="🛒 Покупки", callback_data="kitchen:shoplist"),
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
    await safe_edit(
        callback.message,
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
        await safe_edit(
            callback.message,
            f"{title}\n\nПока нет рецептов под твой фильтр питания. Можно сменить профиль в настройках.",
            reply_markup=recipes_categories_keyboard(profile),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    page_size = 6
    max_page = max(0, (len(items) - 1) // page_size)
    page = max(0, min(page, max_page))

    await safe_edit(
        callback.message,
        f"{title}\n"
        f"Питание: <b>{_diet_label(profile)}</b>\n"
        f"Страница {page+1}/{max_page+1}",
        reply_markup=recipes_paged_keyboard(items, category, page, page_size),
        parse_mode="HTML",
    )
    await callback.answer()

def _render_recipe_card_payload(
    recipe: Dict[str, Any],
    pantry: List[Dict[str, Any]],
    *,
    servings: int,
    category: str,
    page: int,
    adapted_id: Optional[str] = None,
    substitutions: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, InlineKeyboardMarkup]:
    """Сформировать текст и клавиатуру карточки рецепта (обычного или адаптированного под холодильник)."""
    rid = str(recipe.get("adapted_from_id") or recipe.get("id") or "")
    match = kitchen_ai.match_recipe_to_pantry(recipe, pantry, servings=servings)
    base = int(recipe.get("base_servings") or 1) or 1
    servings = max(1, servings)
    factor = servings / base

    nutr = fitness_nutrition.estimate_recipe_nutrition(recipe)
    title = _h(recipe.get("title", "Рецепт"))
    desc = _h(recipe.get("desc", ""))
    prep_warn = kitchen_ai.detect_recipe_prep_warning(recipe)
    text = f"<b>{title}</b>\n{desc}\n\n"
    text += f"⏱ {int(recipe.get('time_minutes', 15))} мин • ❄️ Спишется с холодильника: <b>на {servings} порц.</b>\n"
    text += (
        f"🔥 ~<b>{nutr['kcal']} ккал</b> на 1 порцию "
        f"(Б {nutr['protein_g']:g}г / Ж {nutr['fat_g']:g}г / У {nutr['carbs_g']:g}г)\n"
    )
    if prep_warn:
        text += f"⏳ <b>Подготовка заранее:</b> {_h(prep_warn)}\n"
    text += "\n"
    if adapted_id and substitutions:
        swap_parts = []
        for s in substitutions:
            if s.get("action") == "replace" and s.get("replacement"):
                swap_parts.append(f"{_h(s['missing_name'])} → <b>{_h(s['replacement'])}</b>")
            elif s.get("action") == "skip":
                swap_parts.append(f"{_h(s['missing_name'])} → <i>пропускаем</i>")
        if swap_parts:
            text += "🔄 <b>Замены под холодильник:</b>\n• " + "\n• ".join(swap_parts) + "\n\n"

    have = match["have"]
    missing = match["missing"]
    have_by_ing: Dict[str, str] = {}
    for h_item in have:
        h_name = str(h_item.get("name") or "").strip().casefold().replace("ё", "е")
        p_name = str(h_item.get("pantry_name") or "").strip()
        if h_name and p_name:
            have_by_ing[h_name] = p_name

    text += f"🧾 <b>Ингредиенты</b> (на {servings} порц.):\n"
    for ing in recipe.get("ingredients") or []:
        qty = _scale_qty(ing.get("qty", 0), factor, ing.get("unit", ""))
        ing_name = str(ing.get("name") or "ингредиент")
        line = _format_ing_line(ing_name, qty, ing.get("unit", ""))
        matched_p = have_by_ing.get(ing_name.strip().casefold().replace("ё", "е"), "")
        if matched_p and matched_p.casefold().replace("ё", "е") != ing_name.strip().casefold().replace("ё", "е"):
            line += f" <i>(дома: {_h(matched_p)})</i>"
        text += line + "\n"

    total_ns = len(have) + len(missing)
    if pantry or missing:
        if not missing:
            text += f"\n✅ <b>Есть дома ({len(have)}/{total_ns}):</b> все основные продукты в наличии\n"
        else:
            miss_list = ", ".join(_h(m["name"]) for m in missing)
            if have:
                have_list = ", ".join(_h(h["name"]) for h in have)
                text += f"\n✅ <b>Есть дома ({len(have)}/{total_ns}):</b> {have_list}\n⚠️ <b>Не хватает ({len(missing)}):</b> {miss_list}\n"
            else:
                text += f"\n⚠️ <b>Не хватает ({len(missing)}):</b> {miss_list}\n"

    steps = recipe.get("steps") or []
    if steps:
        safe_steps = [_h(s) for s in steps[:12]]
        text += "\n👩‍🍳 <b>Шаги</b>:\n" + "\n".join([f"{i+1}. {s}" for i, s in enumerate(safe_steps)])
        if len(steps) > 12:
            text += "\n…"

    back_cb = (
        "kitchen:meal_plan:view:3"
        if category == "plan"
        else (f"kitchen:almost_ready:{page}" if category == "almost" else f"kitchen:recipes_cat:{category}:{page}")
    )

    rows: List[List[InlineKeyboardButton]] = []
    if adapted_id:
        rows.append([
            InlineKeyboardButton(
                text=f"🍳 Приготовил ({servings} порц.) + мне 1 порц. в КБЖУ",
                callback_data=f"kitchen:cook_and_log_ad:{adapted_id}:{servings}:1",
            )
        ])
        rows.append([
            InlineKeyboardButton(text=f"❄️ Только списать {servings} порц.", callback_data=f"kitchen:cook_deduct_ad:{adapted_id}:{servings}"),
            InlineKeyboardButton(text="⬅️ Вернуть оригинал", callback_data=f"kitchen:cook_view:{rid}:{servings}:{category}:{page}"),
        ])
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)])
    else:
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
        rows.append(serv_row)
        if missing and len(missing) <= 3 and pantry and (match["coverage"] >= 0.55 or len(have) >= 2):
            rows.append([
                InlineKeyboardButton(
                    text="✨ Заменить по холодильнику",
                    callback_data=f"kitchen:adapt:{rid}:{servings}:{category}:{page}",
                )
            ])
        rows.append([
            InlineKeyboardButton(
                text=f"🍳 Приготовил ({servings} порц.) + мне 1 порц. в КБЖУ",
                callback_data=f"kitchen:cook_and_log:{rid}:{servings}:1",
            )
        ])
        action_row = [
            InlineKeyboardButton(text=f"❄️ Только списать {servings} порц.", callback_data=f"kitchen:cook_deduct:{rid}:{servings}")
        ]
        if missing:
            action_row.append(
                InlineKeyboardButton(text=f"🛒 Докупить ({len(missing)})", callback_data=f"kitchen:add_miss:{rid}:{servings}")
            )
        rows.append(action_row)
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)])

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


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
    from services import inbox_service

    inbox_service.record_active_recipe_context(
        int(user["id"]), recipe, kitchen_ai.match_recipe_to_pantry(recipe, pantry, servings=servings)
    )
    text, kb = _render_recipe_card_payload(
        recipe, pantry, servings=servings, category=category, page=page
    )
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:adapt:"))
async def adapt_recipe_cb(callback: types.CallbackQuery, db):
    parts = (callback.data or "").split(":")
    rid = parts[2] if len(parts) > 2 else ""
    servings = _safe_int(parts[3], 2) if len(parts) > 3 else 2
    category = parts[4] if len(parts) > 4 else "all"
    page = _safe_int(parts[5], 0) if len(parts) > 5 else 0
    await _safe_answer_callback(callback, "Подбираю замены по холодильнику…")

    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    res = await kitchen_ai.adapt_recipe_to_pantry(db, user, rid, servings=servings)
    if not res.get("ok"):
        err = res.get("error")
        if err == "too_many_missing":
            miss_names = ", ".join(_h(m["name"]) for m in (res.get("missing") or [])[:6])
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"🛒 Докупить ({len(res.get('missing') or [])})", callback_data=f"kitchen:add_miss:{rid}:{servings}")],
                [InlineKeyboardButton(text="⬅️ Назад к рецепту", callback_data=f"kitchen:cook_view:{rid}:{servings}:{category}:{page}")],
            ])
            await safe_edit(
                callback.message,
                f"😕 Для этого рецепта не хватает слишком много базы ({miss_names}).\n"
                "Лучше добавить недостающее в покупки или выбрать другой рецепт.",
                reply_markup=kb,
                parse_mode="HTML",
            )
            return
        await safe_edit(
            callback.message,
            str(res.get("message") or "Не удалось подобрать замены."),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"kitchen:cook_view:{rid}:{servings}:{category}:{page}")]
            ]),
        )
        return

    if res.get("already_complete"):
        pantry = rows_to_dicts(await repo.list_pantry_items(db, user["id"]))
        text, kb = _render_recipe_card_payload(
            res["orig_recipe"], pantry, servings=servings, category=category, page=page
        )
        await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
        return

    aid = secrets.token_hex(4)
    _store_adapted_recipe(int(user["id"]), aid, {
        "adapted_recipe": res["adapted_recipe"],
        "substitutions": res["substitutions"],
        "orig_rid": rid,
        "servings": servings,
        "category": category,
        "page": page,
    })

    orig_n = res["orig_nutrition"]
    new_n = res["adapted_nutrition"]
    lines = [
        f"✨ <b>Адаптация под твой холодильник:</b> {_h(res['orig_recipe'].get('title'))}",
        f"🔥 КБЖУ: ~{orig_n['kcal']} → <b>~{new_n['kcal']} ккал</b> "
        f"(Б {new_n['protein_g']:g}г / Ж {new_n['fat_g']:g}г / У {new_n['carbs_g']:g}г)\n",
        "<b>План по недостающим продуктам:</b>",
    ]
    for s in res["substitutions"]:
        act = s.get("action")
        reason = f" <i>({_h(s['reason'])})</i>" if s.get("reason") else ""
        if act == "replace" and s.get("replacement"):
            lines.append(f"• {_h(s['missing_name'])} → <b>{_h(s['replacement'])}</b>{reason}")
        elif act == "skip":
            lines.append(f"• {_h(s['missing_name'])} → <b>пропускаем</b>{reason}")
        else:
            lines.append(f"• {_h(s['missing_name'])} → ⚠️ нечем заменить дома")
    if res.get("note"):
        lines.append(f"\n💡 <i>{_h(res['note'])}</i>")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Норм, готовлю так", callback_data=f"kitchen:adapt_apply:{aid}:{servings}:{category}:{page}")],
        [
            InlineKeyboardButton(text="🛒 Лучше докупить", callback_data=f"kitchen:add_miss:{rid}:{servings}"),
            InlineKeyboardButton(text="⬅️ Вернуть оригинал", callback_data=f"kitchen:cook_view:{rid}:{servings}:{category}:{page}"),
        ],
    ])
    await safe_edit(callback.message, "\n".join(lines), reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:adapt_apply:"))
async def adapt_apply_cb(callback: types.CallbackQuery, db):
    parts = (callback.data or "").split(":")
    aid = parts[2] if len(parts) > 2 else ""
    servings = _safe_int(parts[3], 2) if len(parts) > 3 else 2
    category = parts[4] if len(parts) > 4 else "all"
    page = _safe_int(parts[5], 0) if len(parts) > 5 else 0

    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    stored = _get_adapted_recipe(int(user["id"]), aid)
    if not stored:
        await callback.answer("Адаптация устарела, подбери ещё раз в рецепте.", show_alert=True)
        return

    pantry = rows_to_dicts(await repo.list_pantry_items(db, user["id"]))
    from services import inbox_service

    inbox_service.record_active_recipe_context(
        int(user["id"]),
        stored["adapted_recipe"],
        kitchen_ai.match_recipe_to_pantry(stored["adapted_recipe"], pantry, servings=servings),
    )
    text, kb = _render_recipe_card_payload(
        stored["adapted_recipe"],
        pantry,
        servings=servings,
        category=category,
        page=page,
        adapted_id=aid,
        substitutions=stored.get("substitutions") or [],
    )
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Рецепт адаптирован под холодильник ✅")


async def _notify_partners_after_cook(
    callback: types.CallbackQuery,
    db,
    user: dict,
    recipe: Dict[str, Any],
    per_serv: Dict[str, Any],
    servings: int,
) -> None:
    actor_name = _h(user.get("name") or "Партнёр")
    title = str(recipe.get("title") or "Блюдо")
    bot = getattr(callback, "bot", None)
    if servings >= 2:
        share_id = secrets.token_hex(4)
        kcal = float(per_serv.get("kcal") or 0)
        p_g = float(per_serv.get("protein_g") or 0)
        f_g = float(per_serv.get("fat_g") or 0)
        c_g = float(per_serv.get("carbs_g") or 0)
        _store_shared_cooked_meal(
            share_id,
            {
                "actor_id": int(user["id"]),
                "actor_name": str(user.get("name") or "Партнёр"),
                "title": title,
                "recipe_id": str(recipe.get("adapted_from_id") or recipe.get("id") or ""),
                "kcal": kcal,
                "protein_g": p_g,
                "fat_g": f_g,
                "carbs_g": c_g,
                "claimed_uids": set(),
            },
        )
        nutr_note = (
            f"\n🔥 ~<b>{round(kcal)} ккал</b> за 1 порцию "
            f"(Б {p_g:g}г / Ж {f_g:g}г / У {c_g:g}г)"
            if kcal > 0
            else ""
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🍽 Я тоже поел(а) — 1 порц.", callback_data=f"kitchen:partner_eat:{share_id}:1")],
            [
                InlineKeyboardButton(text="🍽 ½ порц.", callback_data=f"kitchen:partner_eat:{share_id}:0.5"),
                InlineKeyboardButton(text="Не ела", callback_data=f"kitchen:partner_eat:{share_id}:0"),
            ],
        ])
        await household_service.notify_household_partners(
            bot,
            db,
            user,
            f"🍳 <b>{actor_name}</b> приготовил(а) на {servings} порц.: <b>{_h(title)}</b>.{nutr_note}\nЗаписать тебе в КБЖУ?",
            reply_markup=kb,
        )
        return
    await household_service.notify_household_partners(
        bot,
        db,
        user,
        f"🍳 <b>{actor_name}</b> приготовил(а): <b>{_h(title)}</b>.",
    )


async def _handle_cook_and_log_result(
    callback: types.CallbackQuery,
    db,
    user: dict,
    *,
    rid: str,
    servings: int,
    log_servings: float,
    recipe_override: Optional[Dict[str, Any]] = None,
) -> None:
    now_utc = datetime.datetime.utcnow()
    res = await kitchen_ai.commit_cooked_recipe(
        db, user, rid, servings=servings, recipe_override=recipe_override, now_utc=now_utc
    )
    if not res.get("ok"):
        await callback.answer(str(res.get("error") or "Рецепт не найден"), show_alert=True)
        return

    recipe = res["recipe"]
    per_serv = res.get("nutrition_per_serving") or fitness_nutrition.estimate_recipe_nutrition(recipe)
    local_date = local_date_str(now_utc, user.get("timezone") or "UTC")
    kcal = round(float(per_serv.get("kcal") or 0) * log_servings)
    p_g = round(float(per_serv.get("protein_g") or 0) * log_servings, 1)
    f_g = round(float(per_serv.get("fat_g") or 0) * log_servings, 1)
    c_g = round(float(per_serv.get("carbs_g") or 0) * log_servings, 1)

    meal_log_id = await repo.add_meal_log(
        db,
        int(user["id"]),
        local_date,
        str(recipe.get("title") or "Блюдо"),
        recipe_id=str(recipe.get("adapted_from_id") or recipe.get("id") or rid),
        servings=log_servings,
        kcal=kcal,
        protein_g=p_g,
        fat_g=f_g,
        carbs_g=c_g,
        source="recipe",
    )
    _store_cook_log_base(
        int(user["id"]),
        int(meal_log_id),
        {
            "title": str(recipe.get("title") or "Блюдо"),
            "per_serv": dict(per_serv),
            "deducted": list(res.get("deducted") or []),
            "marked_slot": res.get("marked_slot"),
        },
    )
    from services import pulse_service

    pulse_extra = await pulse_service.close_open_food_pulse_slot(db, user, "", now_utc=now_utc)
    await _notify_partners_after_cook(callback, db, user, recipe, per_serv, servings)

    lines = [
        f"🍳 <b>Приготовлено ({servings} порц.) и учтено: {_h(recipe.get('title'))}!</b>",
        f"🔥 Записано в твой КБЖУ ({log_servings:g} порц.): <b>{kcal} ккал</b> (Б {p_g:g}г / Ж {f_g:g}г / У {c_g:g}г)",
    ]
    deducted = res.get("deducted") or []
    if deducted:
        ded_str = ", ".join(f"{_h(d['name'])} (ост. {format_quantity(d['remaining'], d['unit'])})" for d in deducted[:5])
        lines.append(f"❄️ Списано из холодильника: {ded_str}")
    if res.get("marked_slot"):
        slot_ru = kitchen_ai.MEAL_SLOT_LABELS.get(str(res["marked_slot"]), str(res["marked_slot"]))
        lines.append(f"📅 В меню на сегодня слот «{slot_ru}» отмечен выполненным.")
    closed_slot = res.get("pulse_closed") or (pulse_extra.get("slot") if pulse_extra else None)
    if closed_slot:
        slot_ru = {"breakfast": "Завтрак", "lunch": "Обед", "dinner": "Ужин"}.get(str(closed_slot), "Еда")
        lines.append(f"💓 Чек-ин Пульса «{slot_ru}» закрыт.")

    kb_rows: List[List[InlineKeyboardButton]] = [
        _build_portion_edit_row(int(meal_log_id), log_servings),
        *_build_tail_cleanup_rows(deducted),
        [
            InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
            InlineKeyboardButton(text="📅 Меню на 3 дня", callback_data="kitchen:meal_plan"),
        ],
        [
            InlineKeyboardButton(text="❄️ Холодильник", callback_data="kitchen:fridge"),
            InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main"),
        ],
    ]
    await safe_edit(callback.message, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    await callback.answer("Списала продукты и записала КБЖУ! 🍽")


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:cook_and_log:"))
async def cook_and_log_commit(callback: types.CallbackQuery, db):
    """Одним нажатием списать продукты, закрыть слот меню/Пульса и записать порцию в КБЖУ."""
    parts = (callback.data or "").split(":")
    rid = parts[2] if len(parts) > 2 else ""
    servings = _safe_int(parts[3], 2) if len(parts) > 3 else 2
    try:
        log_servings = max(0.25, min(4.0, float(parts[4]))) if len(parts) > 4 else 1.0
    except (IndexError, ValueError):
        log_servings = 1.0

    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    await _handle_cook_and_log_result(
        callback, db, user, rid=rid, servings=servings, log_servings=log_servings
    )


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:cook_and_log_ad:"))
async def cook_and_log_adapted_commit(callback: types.CallbackQuery, db):
    parts = (callback.data or "").split(":")
    aid = parts[2] if len(parts) > 2 else ""
    servings = _safe_int(parts[3], 2) if len(parts) > 3 else 2
    try:
        log_servings = max(0.25, min(4.0, float(parts[4]))) if len(parts) > 4 else 1.0
    except (IndexError, ValueError):
        log_servings = 1.0

    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    stored = _get_adapted_recipe(int(user["id"]), aid)
    if not stored:
        await callback.answer("Адаптация устарела, открой рецепт заново.", show_alert=True)
        return
    await _handle_cook_and_log_result(
        callback,
        db,
        user,
        rid=str(stored.get("orig_rid") or ""),
        servings=servings,
        log_servings=log_servings,
        recipe_override=stored["adapted_recipe"],
    )


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


async def _handle_cook_fast_result(
    callback: types.CallbackQuery,
    db,
    user: dict,
    *,
    rid: str,
    servings: int,
    recipe_override: Optional[Dict[str, Any]] = None,
) -> None:
    res = await kitchen_ai.commit_cooked_recipe(
        db, user, rid, servings=servings, recipe_override=recipe_override
    )
    if not res.get("ok"):
        await callback.answer(str(res.get("error") or "Рецепт не найден"), show_alert=True)
        return

    recipe = res["recipe"]
    log_rid = str(recipe.get("adapted_from_id") or recipe.get("id") or rid)
    nutr = res.get("nutrition_per_serving") or {}
    await _notify_partners_after_cook(callback, db, user, recipe, nutr, servings)
    lines = [f"🍽 <b>Приготовлено ({servings} порц.): {_h(recipe.get('title'))}!</b>"]
    if nutr.get("kcal"):
        lines.append(
            f"🔥 Продукты списаны без записи в твой КБЖУ · 1 порция ~<b>{nutr['kcal']} ккал</b> "
            f"(Б {float(nutr['protein_g']):g}г / Ж {float(nutr['fat_g']):g}г / У {float(nutr['carbs_g']):g}г)"
        )
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
        slot_ru = {"breakfast": "Завтрак", "lunch": "Обед", "dinner": "Ужин"}.get(
            str(res["pulse_closed"]), "Еда"
        )
        if delta > 0 and score_after is not None:
            lines.append(f"💓 Чек-ин Пульса «{slot_ru}» закрыт (+{delta} кармы → {score_after}/100).")
        else:
            lines.append(f"💓 Чек-ин Пульса «{slot_ru}» закрыт.")
    elif delta > 0 and score_after is not None:
        lines.append(f"💓 Карма еды: +{delta} → <b>{score_after}/100</b>")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🍽 ½ порц. себе", callback_data=f"kbju:log_recipe:{log_rid}:0.5"),
            InlineKeyboardButton(text="🍽 1 порц. себе", callback_data=f"kbju:log_recipe:{log_rid}:1"),
        ],
        *_build_tail_cleanup_rows(deducted),
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
    await callback.answer("Продукты списаны! 🍽")


@router.callback_query(lambda c: c.data and (c.data.startswith("kitchen:cook_fast:") or c.data.startswith("kitchen:cook_deduct:")))
async def cook_fast_commit(callback: types.CallbackQuery, db):
    parts = callback.data.split(":")
    rid = parts[2] if len(parts) > 2 else ""
    servings = _safe_int(parts[3], 2) if len(parts) > 3 else 2
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    await _handle_cook_fast_result(callback, db, user, rid=rid, servings=servings)


@router.callback_query(lambda c: c.data and (c.data.startswith("kitchen:cook_fast_ad:") or c.data.startswith("kitchen:cook_deduct_ad:")))
async def cook_fast_adapted_commit(callback: types.CallbackQuery, db):
    parts = callback.data.split(":")
    aid = parts[2] if len(parts) > 2 else ""
    servings = _safe_int(parts[3], 2) if len(parts) > 3 else 2
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    stored = _get_adapted_recipe(int(user["id"]), aid)
    if not stored:
        await callback.answer("Адаптация устарела, открой рецепт заново.", show_alert=True)
        return
    await _handle_cook_fast_result(
        callback,
        db,
        user,
        rid=str(stored.get("orig_rid") or ""),
        servings=servings,
        recipe_override=stored["adapted_recipe"],
    )


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:cook_edit_log:"))
async def cook_edit_logged_portion_cb(callback: types.CallbackQuery, db):
    parts = (callback.data or "").split(":")
    try:
        log_id = int(parts[2])
        new_serv = max(0.25, min(4.0, float(parts[3])))
    except (IndexError, ValueError):
        await callback.answer()
        return

    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    uid = int(user["id"])
    existing = await repo.get_meal_log(db, uid, log_id)
    if not existing:
        await callback.answer("Запись КБЖУ не найдена.", show_alert=True)
        return

    base_info = _get_cook_log_base(uid, log_id)
    if base_info and base_info.get("per_serv"):
        per_serv = base_info["per_serv"]
    else:
        old_s = max(0.25, float(existing.get("servings") or 1.0))
        per_serv = {
            "kcal": float(existing.get("kcal") or 0) / old_s,
            "protein_g": float(existing.get("protein_g") or 0) / old_s,
            "fat_g": float(existing.get("fat_g") or 0) / old_s,
            "carbs_g": float(existing.get("carbs_g") or 0) / old_s,
        }

    kcal = round(float(per_serv.get("kcal") or 0) * new_serv)
    p_g = round(float(per_serv.get("protein_g") or 0) * new_serv, 1)
    f_g = round(float(per_serv.get("fat_g") or 0) * new_serv, 1)
    c_g = round(float(per_serv.get("carbs_g") or 0) * new_serv, 1)
    await repo.update_meal_log(
        db,
        uid,
        log_id,
        servings=new_serv,
        kcal=kcal,
        protein_g=p_g,
        fat_g=f_g,
        carbs_g=c_g,
    )

    title = str((base_info or {}).get("title") or existing.get("title") or "Блюдо")
    deducted = list((base_info or {}).get("deducted") or [])
    lines = [
        f"🍳 <b>Обновила твою порцию: {_h(title)}!</b>",
        f"🔥 Записано в твой КБЖУ ({new_serv:g} порц.): <b>{kcal} ккал</b> (Б {p_g:g}г / Ж {f_g:g}г / У {c_g:g}г)",
    ]
    if deducted:
        ded_str = ", ".join(f"{_h(d['name'])} (ост. {format_quantity(d['remaining'], d['unit'])})" for d in deducted[:5])
        lines.append(f"❄️ Списано из холодильника: {ded_str}")

    kb_rows: List[List[InlineKeyboardButton]] = [
        _build_portion_edit_row(log_id, new_serv),
        *_build_tail_cleanup_rows(deducted),
        [
            InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
            InlineKeyboardButton(text="📅 Меню на 3 дня", callback_data="kitchen:meal_plan"),
        ],
        [
            InlineKeyboardButton(text="❄️ Холодильник", callback_data="kitchen:fridge"),
            InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main"),
        ],
    ]
    await safe_edit(callback.message, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    await callback.answer(f"Порция обновлена: {new_serv:g} порц. ✅")


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:tail_del:"))
async def tail_delete_pantry_cb(callback: types.CallbackQuery, db):
    parts = (callback.data or "").split(":")
    try:
        pid = int(parts[2])
    except (IndexError, ValueError):
        await callback.answer()
        return

    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    deleted = await repo.delete_pantry_item(db, int(user["id"]), pid)

    cur_markup = getattr(callback.message, "reply_markup", None)
    if cur_markup and getattr(cur_markup, "inline_keyboard", None):
        target_cb = f"kitchen:tail_del:{pid}"
        new_rows = []
        for row in cur_markup.inline_keyboard:
            kept = [btn for btn in row if getattr(btn, "callback_data", None) != target_cb]
            if kept:
                new_rows.append(kept)
        try:
            await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=new_rows))
        except Exception:
            pass
    await callback.answer("Убрала остаток из холодильника 🗑" if deleted else "Уже убрано 👍")


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:partner_eat:"))
async def partner_eat_shared_cb(callback: types.CallbackQuery, db):
    parts = (callback.data or "").split(":")
    share_id = parts[2] if len(parts) > 2 else ""
    try:
        eat_serv = float(parts[3]) if len(parts) > 3 else 1.0
    except ValueError:
        eat_serv = 1.0

    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    uid = int(user["id"])
    if is_student_mode(user):
        await callback.answer("Раздел КБЖУ отключён в учебном режиме.", show_alert=True)
        return

    shared = _get_shared_cooked_meal(share_id)
    if not shared:
        await callback.answer("Кнопка устарела — можно записать приём пищи в чате.", show_alert=True)
        return

    if eat_serv <= 0:
        shared.setdefault("claimed_uids", set()).add(uid)
        await safe_edit(
            callback.message,
            f"👌 Ок, не записывала <b>{_h(shared['title'])}</b> в твой КБЖУ.",
            parse_mode="HTML",
        )
        await callback.answer("Не записывала 👍")
        return

    claimed = shared.setdefault("claimed_uids", set())
    if uid in claimed:
        await callback.answer("Уже записано в твой КБЖУ 👍")
        return

    eat_serv = max(0.25, min(3.0, eat_serv))
    now_utc = datetime.datetime.utcnow()
    local_date = local_date_str(now_utc, user.get("timezone") or "UTC")
    kcal = round(float(shared.get("kcal") or 0) * eat_serv)
    p_g = round(float(shared.get("protein_g") or 0) * eat_serv, 1)
    f_g = round(float(shared.get("fat_g") or 0) * eat_serv, 1)
    c_g = round(float(shared.get("carbs_g") or 0) * eat_serv, 1)

    await repo.add_meal_log(
        db,
        uid,
        local_date,
        str(shared.get("title") or "Блюдо"),
        recipe_id=str(shared.get("recipe_id") or ""),
        servings=eat_serv,
        kcal=kcal,
        protein_g=p_g,
        fat_g=f_g,
        carbs_g=c_g,
        source="recipe_shared",
    )
    claimed.add(uid)
    from services import pulse_service

    pulse_extra = await pulse_service.close_open_food_pulse_slot(db, user, "", now_utc=now_utc)
    pulse_note = ""
    if pulse_extra and pulse_extra.get("slot"):
        slot_ru = {"breakfast": "Завтрак", "lunch": "Обед", "dinner": "Ужин"}.get(str(pulse_extra["slot"]), "Еда")
        pulse_note = f"\n💓 Чек-ин Пульса «{slot_ru}» закрыт."

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today")]
    ])
    await safe_edit(
        callback.message,
        f"🍽 Записала в твой КБЖУ ({eat_serv:g} порц.): <b>{_h(shared['title'])}</b>\n"
        f"🔥 <b>{kcal} ккал</b> (Б {p_g:g}г / Ж {f_g:g}г / У {c_g:g}г){pulse_note}",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await callback.answer("Записала в твой КБЖУ! 🍽")

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
    
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")

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

    recipe = res.get("recipe") or {}
    actor_name = _h(user.get("name") or "Партнёр")
    await household_service.notify_household_partners(
        getattr(callback, "bot", None),
        db,
        user,
        f"🍳 <b>{actor_name}</b> приготовил(а): <b>{_h(recipe.get('title') or 'Блюдо')}</b>.",
    )
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
            [
                InlineKeyboardButton(text="🍽 ½ порц.", callback_data=f"kbju:log_recipe:{rid}:0.5"),
                InlineKeyboardButton(text="🍽 1 порц.", callback_data=f"kbju:log_recipe:{rid}:1"),
                InlineKeyboardButton(text="🍽 2 порц.", callback_data=f"kbju:log_recipe:{rid}:2"),
            ],
            [InlineKeyboardButton(text="Нет, спасибо", callback_data=f"kitchen:cook_view:{rid}:{servings}:{category}:{page}")],
        ])
        await safe_edit(callback.message, text, reply_markup=kb)
    else:
        nutr = res.get("nutrition_per_serving") or {}
        nutr_note = (
            f"\n🔥 1 порция: ~<b>{nutr['kcal']} ккал</b> (Б {float(nutr['protein_g']):g}г / Ж {float(nutr['fat_g']):g}г / У {float(nutr['carbs_g']):g}г)"
            if nutr.get("kcal")
            else ""
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
            InlineKeyboardButton(text="🍽 ½ порц.", callback_data=f"kbju:log_recipe:{rid}:0.5"),
            InlineKeyboardButton(text="🍽 1 порц.", callback_data=f"kbju:log_recipe:{rid}:1"),
            InlineKeyboardButton(text="🍽 2 порц.", callback_data=f"kbju:log_recipe:{rid}:2"),
        ],
            [
                InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
                InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main"),
            ],
        ])
        await safe_edit(
            callback.message,
            f"Всё готово! Приятного аппетита 😋{nutr_note}\nЧто дальше?",
            reply_markup=kb,
            parse_mode="HTML",
        )


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
    targets = plan_data.get("nutrition_targets") or None
    daily_summary = plan_data.get("daily_summary") or {}
    show_kbju = bool(targets and not targets.get("mindful_mode"))

    header_goal = (
        f" · цель ~<b>{int(targets['kcal'])} ккал · Б {int(targets['protein_g'])}г</b>"
        if show_kbju
        else ""
    )
    lines = [
        f"📅 <b>Меню на {days} дн.</b> · {_diet_label(profile)}{header_goal}",
    ]
    if plan_data.get("is_stale"):
        lines.append("⚠️ <i>Меню собрано раньше или неполное — жми «🔄 Пересобрать меню» ниже.</i>")
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
            est_k = int(slot.get("est_kcal") or 0)
            est_p = int(round(float(slot.get("est_protein_g") or 0)))
            kbju_note = f" · ~{est_k} ккал, Б {est_p}г" if (show_kbju and est_k > 0) else ""
            miss_n = int(slot.get("missing_count") or 0)
            miss_note = f" · ⚠️ нет {miss_n} прод." if (miss_n > 0 and d_str == start_date and slot.get("status") != "cooked") else ""
            prep_note = " · ⏳ заранее" if (slot.get("prep_warning") and slot.get("status") != "cooked") else ""
            lines.append(f"{icon} {s_lbl}: <b>{_h(slot.get('title'))}</b> · {t_min} мин{kbju_note}{miss_note}{prep_note}")
        if show_kbju and d_str in daily_summary:
            ds = daily_summary[d_str]
            p_gap = int(ds.get("target_protein_g") or 0) - int(ds.get("protein_g") or 0)
            gap_note = f", +перекус/протеин ~{p_gap} г Б" if p_gap >= 25 else ""
            lines.append(
                f"   ↳ <i>Итого за день: ~{int(ds['kcal'])}/{int(ds['target_kcal'])} ккал · "
                f"Б ~{int(ds['protein_g'])}/{int(ds['target_protein_g'])} г{gap_note}</i>"
            )

    if missing:
        preview = ", ".join(_h(m["name"]) for m in missing[:7])
        extra = f" и ещё {len(missing) - 7}" if len(missing) > 7 else ""
        lines.append(f"\n🛒 <b>Недостающие продукты ({len(missing)}):</b> {preview}{extra}")
    else:
        lines.append("\n✅ <b>Всё нужное для этого меню уже есть дома или в покупках!</b>")

    kb_rows: list[list[InlineKeyboardButton]] = []
    today_slots = [
        s for s in by_date.get(start_date, [])
        if s.get("status") != "cooked" and s.get("recipe_id")
    ]
    if today_slots:
        cook_row = [
            InlineKeyboardButton(
                text=f"{slot_icons.get(str(s.get('meal_slot')), '🍳')} {slot_names.get(str(s.get('meal_slot')), 'Готовить')}",
                callback_data=f"kitchen:cook_view:{s['recipe_id']}:1:plan:0",
            )
            for s in today_slots[:3]
        ]
        kb_rows.append(cook_row)

    if missing:
        kb_rows.append([
            InlineKeyboardButton(
                text=f"🛒 Докупить ({len(missing)})",
                callback_data=f"kitchen:meal_plan:shop:{days}",
            ),
            InlineKeyboardButton(
                text="📋 Список для доставки",
                callback_data=f"kitchen:meal_plan:export:{days}",
            ),
        ])
    else:
        kb_rows.append([
            InlineKeyboardButton(
                text="📋 Экспорт списка покупок",
                callback_data="kitchen:shop_export:household",
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
    elif action in {"export", "export_all"}:
        plan_rows = await repo.list_meal_plans_range(db, user["id"], start_date, end_date)
        if not plan_rows:
            plan_data = await kitchen_ai.get_meal_plan_view(db, user, days=days, auto_generate=True)
            plan_rows = list(plan_data.get("items") or [])
        include_active = action == "export_all"
        exp = await kitchen_ai.build_meal_plan_shopping_export(
            db, user, plan_rows, include_active_shopping=include_active
        )
        if exp["total_items"] == 0:
            kb_empty = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="⬅️ К меню", callback_data=f"kitchen:meal_plan:view:{days}"),
                    InlineKeyboardButton(text="🛒 Покупки", callback_data="kitchen:shoplist:household"),
                ]
            ])
            await safe_edit(
                callback.message,
                "✅ Всё нужное для этого меню уже есть дома или в списке покупок!",
                reply_markup=kb_empty,
            )
            return

        plain = exp["plain_text"][:3600]
        exp_text = (
            "📋 <b>Список продуктов для доставки / ИИ</b>\n"
            "<i>Нажми на блок ниже, чтобы скопировать и вставить в Перекрёсток, Ленту или чат-помощник:</i>\n\n"
            f"<pre>{_h(plain)}</pre>"
        )
        exp_rows: list[list[InlineKeyboardButton]] = []
        if exp["missing_count"] > 0:
            exp_rows.append([
                InlineKeyboardButton(
                    text=f"🛒 Добавить недостающее в список ({exp['missing_count']})",
                    callback_data=f"kitchen:meal_plan:shop:{days}",
                )
            ])
        toggle_btn = (
            InlineKeyboardButton(text="📅 Только под меню", callback_data=f"kitchen:meal_plan:export:{days}")
            if include_active
            else InlineKeyboardButton(text="📦 + Текущий список покупок", callback_data=f"kitchen:meal_plan:export_all:{days}")
        )
        exp_rows.append([toggle_btn])
        exp_rows.append([
            InlineKeyboardButton(text="⬅️ К меню", callback_data=f"kitchen:meal_plan:view:{days}"),
            InlineKeyboardButton(text="🛒 Покупки", callback_data="kitchen:shoplist:household"),
        ])
        await safe_edit(callback.message, exp_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=exp_rows), parse_mode="HTML")
        return
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
    await safe_edit(callback.message, "Продукты добавлены в список покупок 🛒", reply_markup=kitchen_main_keyboard())
    await state.clear()

# --- HANDLERS: FRIDGE (Legacy Pantry Logic Wrapper) ---
async def _build_fridge_payload(db, tg_user_id: int, tg_full_name: str) -> Tuple[str, InlineKeyboardMarkup]:
    user = await ensure_user(db, tg_user_id, tg_full_name)
    rows_all = await repo.list_pantry_items(db, user["id"])
    items = rows_to_dicts(rows_all)

    text = "<b>❄️ Мой холодильник</b>\n"
    if not items:
        text += (
            "Пока пусто.\n"
            "Просто напиши или надиктуй в чат: <code>купил яйца, молоко, овсянку</code> — я сама разложу по полкам.\n"
            "Или собери базовый минимум в покупки кнопкой ниже."
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
        [
            InlineKeyboardButton(text="➕ Добавить продукт", callback_data="kitchen:fridge_add"),
            InlineKeyboardButton(text="⚡ Добавить списком", callback_data="kitchen:fridge_bulk"),
        ],
        [
            InlineKeyboardButton(text="⏰ Скоро истекает", callback_data="kitchen:fridge_expiring"),
            InlineKeyboardButton(text="🗑 Удалить что-то", callback_data="kitchen:fridge_del_view"),
        ],
        [InlineKeyboardButton(text="🧺 Базовый минимум в покупки", callback_data="kitchen:shop_min:add")],
        [InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main")],
    ])
    return text, kb


async def send_fridge(message: types.Message, db) -> None:
    """Открыть холодильник из обычного сообщения (/pantry)."""
    text, kb = await _build_fridge_payload(db, message.from_user.id, message.from_user.full_name)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data == "kitchen:fridge")
async def fridge_view(callback: types.CallbackQuery, db):
    text, kb = await _build_fridge_payload(db, callback.from_user.id, callback.from_user.full_name)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(lambda c: c.data in {"kitchen:fridge_expiring", "pantry:expiring"})
async def fridge_expiring_view(callback: types.CallbackQuery, db):
    """Показать скоропортящиеся и просроченные продукты с подбором блюд."""
    await _safe_answer_callback(callback)
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    tz = user.get("timezone") or "UTC"
    local_today = local_date_str(datetime.datetime.utcnow(), tz)
    wellness = await repo.get_wellness(db, int(user["id"]))
    window_days = int((dict(wellness) if wellness else {}).get("expiring_window_days", 5))
    soon, expired = await repo.pantry_expiring(db, int(user["id"]), local_today, window_days=window_days)
    soon_d = rows_to_dicts(soon)
    exp_d = rows_to_dicts(expired)

    lines = [f"⏰ <b>Сроки годности в холодильнике (окно {window_days} дн.)</b>"]
    if not soon_d and not exp_d:
        lines.append("\nСейчас нет скоропортящихся или просроченных продуктов с указанной датой 👌")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🍳 Что приготовить?", callback_data="kitchen:quick_pick")],
            [InlineKeyboardButton(text="⬅️ Холодильник", callback_data="kitchen:fridge")],
        ])
        await safe_edit(callback.message, "\n".join(lines), reply_markup=kb, parse_mode="HTML")
        return

    if exp_d:
        lines.append("\n❗️ <b>Уже истёк срок:</b>")
        for it in exp_d[:10]:
            lines.append(f"• {_h(it.get('name'))} — {format_quantity(it.get('amount'), it.get('unit'))} (до {it.get('expires_at')})")
    if soon_d:
        lines.append("\n⏳ <b>Скоро испортится:</b>")
        for it in soon_d[:10]:
            lines.append(f"• {_h(it.get('name'))} — {format_quantity(it.get('amount'), it.get('unit'))} (до {it.get('expires_at')})")

    sug = await kitchen_ai.suggest_meals_now(db, user, limit=3)
    rows: list[list[InlineKeyboardButton]] = []
    if sug.get("items"):
        lines.append("\n🍳 <b>Что приготовить в первую очередь:</b>")
        for idx, item in enumerate(sug["items"][:3], 1):
            rec = item["recipe"]
            lines.append(f"{idx}. <b>{_h(rec.get('title'))}</b> · {int(rec.get('time_minutes') or 15)} мин")
            rows.append([
                InlineKeyboardButton(
                    text=f"🍳 {idx}. {str(rec.get('title') or 'Рецепт')[:34]}",
                    callback_data=f"kitchen:cook_view:{rec['id']}:2:all:0",
                )
            ])
    rows.append([InlineKeyboardButton(text="⬅️ Холодильник", callback_data="kitchen:fridge")])
    await safe_edit(callback.message, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


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
    await safe_edit(
        callback.message,
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
    
    await repo.upsert_pantry_item_merged(
        db, user["id"], data["name"], data["amount"], data["unit"], None, cat
    )
    await state.clear()
    await callback.answer("Сохранено!")
    await fridge_view(callback, db) # easy return


# --- HANDLERS: CART / RECEIPT IMPORT ---
def _merge_cart_previews(existing: Optional[Dict[str, Any]], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Объединить несколько распознанных скриншотов корзины/чека без дублей и повторного суммирования итога."""
    base = dict(existing or {})
    merged_items: List[Dict[str, Any]] = []
    seen_keys: set[tuple[str, float, str]] = set()
    for src in (base.get("items") or [], incoming.get("items") or []):
        for raw_it in src:
            if not isinstance(raw_it, dict):
                continue
            name = str(raw_it.get("name") or "").strip()
            if not name:
                continue
            try:
                amt = round(float(raw_it.get("amount") or 1.0), 2)
            except (TypeError, ValueError):
                amt = 1.0
            unit = str(raw_it.get("unit") or "шт").strip().lower()
            key = (re.sub(r"\s+", " ", name.casefold().replace("ё", "е")), amt, unit)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged_items.append(dict(raw_it))

    prev_total = base.get("total_amount")
    new_total = incoming.get("total_amount")
    total_vals = [float(v) for v in (prev_total, new_total) if v is not None and float(v) > 0]
    merged_total = max(total_vals) if total_vals else None

    store = str(base.get("store") or incoming.get("store") or "").strip()
    prev_photos = int(base.get("photos_count") or (1 if base.get("items") else 0))
    inc_photos = int(incoming.get("photos_count") or (1 if (incoming.get("items") or incoming.get("ok") is not None) else 0))
    prev_failed = int(base.get("failed_photos") or 0)
    inc_failed = int(incoming.get("failed_photos") or (0 if incoming.get("ok") else 1))
    last_err = incoming.get("error") or base.get("error")

    return {
        "ok": bool(merged_items),
        "items": merged_items,
        "total_amount": merged_total,
        "store": store,
        "photos_count": max(1, prev_photos + inc_photos) if (prev_photos or inc_photos) else 0,
        "failed_photos": prev_failed + inc_failed,
        "used_llm": bool(base.get("used_llm") or incoming.get("used_llm")),
        "error": None if merged_items else last_err,
    }


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
    photos_cnt = int(parsed.get("photos_count") or 1)
    failed_cnt = int(parsed.get("failed_photos") or 0)
    ctx_parts = []
    if store:
        ctx_parts.append(_h(store))
    if photos_cnt > 1:
        ctx_parts.append(f"{photos_cnt} скрин.")
    ctx_note = f" ({', '.join(ctx_parts)})" if ctx_parts else ""
    header = f"🛒 <b>Разобрала корзину{ctx_note}: {len(items)} поз.</b>"
    lines = [header]
    for it in items[:25]:
        lines.append(f"• {_h(it['name'])} ({format_quantity(it['amount'], it['unit'])})")
    if len(items) > 25:
        lines.append(f"… и ещё {len(items) - 25} поз.")
    if failed_cnt > 0:
        lines.append(f"\n⚠️ Не удалось разобрать фото: {failed_cnt} (остальные сохранила).")

    total_amt = parsed.get("total_amount")
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=f"🧊 Всё в холодильник ({len(items)} поз.)", callback_data="kitchen:cart_confirm:pantry")]
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
    lines.append("\n<i>Если скринов было больше — просто докинь ещё фото сюда же.</i>")
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="kitchen:cancel_fsm")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@router.callback_query(lambda c: c.data == "kitchen:cart_import")
async def cart_import_start(callback: types.CallbackQuery, state: FSMContext):
    await _safe_answer_callback(callback)
    await state.set_state(CartImportState.waiting_input)
    await callback.message.answer(
        "📥 <b>Импорт корзины / чека</b>\n\n"
        "Пришли <b>1 или сразу 3–4 скриншота</b> корзины/чека (например, ВкусВилл или Перекрёсток) — либо вставь <b>список покупок текстом</b>.\n"
        "Я сама склею все позиции без дублей, вычеркну их из покупок и положу в общий холодильник.",
        reply_markup=_cancel_fsm_keyboard(),
        parse_mode="HTML",
    )


async def _download_photo_bytes(message: types.Message) -> bytes:
    photo = message.photo[-1]
    buf = io.BytesIO()
    dl = await message.bot.download(photo, destination=buf)
    img_bytes = buf.getvalue()
    if not img_bytes and dl is not None:
        if hasattr(dl, "getvalue"):
            img_bytes = dl.getvalue()
        elif isinstance(dl, (bytes, bytearray)):
            img_bytes = bytes(dl)
    return img_bytes


async def _parse_cart_messages_batch(
    messages: List[types.Message],
    state: FSMContext,
    existing_preview: Optional[Dict[str, Any]] = None,
) -> None:
    caption = next((str(getattr(m, "caption", None) or "").strip() for m in messages if getattr(m, "caption", None)), "")
    blobs: List[bytes] = []
    for msg in messages:
        blobs.append(await _download_photo_bytes(msg))
    parsed_list = await asyncio.gather(
        *[
            kitchen_ai.parse_cart_input(
                raw_text=caption if idx == 0 else "",
                image_bytes=blob,
                image_mime="image/jpeg",
            )
            for idx, blob in enumerate(blobs)
        ]
    )
    acc = dict(existing_preview) if existing_preview else None
    for parsed in parsed_list:
        acc = _merge_cart_previews(acc, parsed)
    await _show_cart_preview(messages[-1], state, acc or {"ok": False, "items": [], "error": "no_items_found"})


async def handle_cart_photo_batch_from_chat(messages: List[types.Message], state: FSMContext) -> None:
    """Принять пачку скриншотов корзины напрямую из свободного чата и показать единое превью."""
    if not messages:
        return
    data = await state.get_data()
    cur_state = await state.get_state()
    existing = data.get("cart_preview") if cur_state == CartImportState.confirm.state else None
    await _parse_cart_messages_batch(messages, state, existing_preview=existing)


async def handle_cart_photo_from_chat(message: types.Message, state: FSMContext) -> None:
    """Принять одиночный или альбомный скриншот корзины из экрана импорта или свободного чата."""
    mgid = getattr(message, "media_group_id", None)
    if mgid:
        buf_key = f"{int(message.chat.id)}:{mgid}"
        buf = _CART_ALBUM_BUFFERS.get(buf_key)
        if buf is None:
            buf = {"messages": [message], "task": None}
            _CART_ALBUM_BUFFERS[buf_key] = buf

            async def _flush_album() -> None:
                try:
                    await asyncio.sleep(_CART_ALBUM_DEBOUNCE_SEC)
                finally:
                    entry = _CART_ALBUM_BUFFERS.pop(buf_key, None)
                if entry and entry.get("messages"):
                    await handle_cart_photo_batch_from_chat(list(entry["messages"]), state)

            buf["task"] = asyncio.create_task(_flush_album())
        else:
            buf["messages"].append(message)
        return

    data = await state.get_data()
    cur_state = await state.get_state()
    existing = data.get("cart_preview") if cur_state == CartImportState.confirm.state else None
    await _parse_cart_messages_batch([message], state, existing_preview=existing)


@router.message(CartImportState.waiting_input, F.photo)
@router.message(CartImportState.confirm, F.photo)
async def cart_import_photo(message: types.Message, state: FSMContext):
    await handle_cart_photo_from_chat(message, state)


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
    hid = await repo.get_or_create_household(db, int(user["id"]))
    members = await repo.list_household_members(db, hid)
    exp_scope = "household" if len(members) > 1 else "personal"
    total_amt = preview.get("total_amount") if mode == "both" else None
    res = await kitchen_ai.import_cart_items(
        db,
        user,
        items,
        total_amount=total_amt,
        expense_scope=exp_scope,
        sync_shopping=True,
    )
    await state.clear()

    added = res.get("added") or []
    synced = res.get("synced_shopping") or []
    actor_name = _h(user.get("name") or "Партнёр")
    if added:
        preview_names = ", ".join(_h(it["name"]) for it in added[:6])
        more = f" и ещё {len(added) - 6}" if len(added) > 6 else ""
        await household_service.notify_household_partners(
            getattr(callback, "bot", None),
            db,
            user,
            f"🛒 <b>{actor_name}</b> пополнил(а) общий холодильник (+{len(added)} поз.): {preview_names}{more}",
        )
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
    advice_summary: str = "",
) -> Tuple[str, InlineKeyboardMarkup]:
    lines = [
        f"✨ <b>ИИ-шеф: идеи на {_h(slot_label)}</b>",
        f"Питание: <b>{_diet_label(profile)}</b> • В холодильнике: {pantry_count} поз.",
    ]
    if advice_summary:
        lines.append(f"🔥 {_h(advice_summary)}")
    if expiring_names:
        lines.append(f"⏰ В первую очередь пускаем в дело: {_h(', '.join(expiring_names[:5]))}")
    lines.append("")

    rows: list[list[InlineKeyboardButton]] = []
    if recipes:
        lines.append(f"<i>Выбери 1 из {len(recipes)} вариантов — нажми на блюдо, чтобы открыть рецепт:</i>")
    for idx, rec in enumerate(recipes, 1):
        title = str(rec.get("title") or f"Блюдо {idx}")
        t_min = int(rec.get("time_minutes") or 20)
        desc = str(rec.get("desc") or "").strip()
        nutr = rec.get("nutrition") or {}
        nutr_str = (
            f" · ~{int(nutr['kcal'])} ккал (Б{float(nutr['protein_g']):g}/Ж{float(nutr['fat_g']):g}/У{float(nutr['carbs_g']):g})"
            if nutr.get("kcal")
            else ""
        )
        lines.append(f"<b>{idx}. {_h(title)}</b> · {t_min} мин{nutr_str}")
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
        if res.get("error") == "daily_quota_exceeded":
            msg = (
                "На сегодня дневной лимит ИИ-запросов исчерпан — попробуй завтра или воспользуйся мгновенным подбором из базы рецептов."
            )
        else:
            msg = (
                "Не получилось связаться с ИИ-шефом прямо сейчас — можно воспользоваться мгновенным подбором из базы рецептов."
            )
        await safe_edit(callback.message, msg, reply_markup=kb)
        return

    advice_summary = str((res.get("daily_advice") or {}).get("summary_line") or "")
    await state.update_data(
        ai_recipes=res["recipes"],
        ai_chef_meta={
            "profile": res["meal_profile"],
            "slot_label": res["meal_slot_label"],
            "pantry_count": res["pantry_count"],
            "expiring_names": res["expiring_names"],
            "advice_summary": advice_summary,
        },
    )
    text, kb = _render_ai_recipes_list(
        res["recipes"],
        profile=res["meal_profile"],
        slot_label=res["meal_slot_label"],
        pantry_count=int(res["pantry_count"] or 0),
        expiring_names=list(res["expiring_names"] or []),
        advice_summary=advice_summary,
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
    nutr = rec.get("nutrition") or fitness_nutrition.estimate_recipe_nutrition(rec)
    lines = [f"✨ <b>{_h(rec.get('title'))}</b>"]
    if rec.get("desc"):
        lines.append(_h(rec["desc"]))
    lines.append(f"\n⏱ {int(rec.get('time_minutes') or 20)} мин • 🍽 {int(rec.get('base_servings') or 2)} порц.")
    if nutr.get("kcal"):
        lines.append(
            f"🔥 ~<b>{nutr['kcal']} ккал</b> на 1 порцию "
            f"(Б {float(nutr['protein_g']):g}г / Ж {float(nutr['fat_g']):g}г / У {float(nutr['carbs_g']):g}г)"
        )
    lines.append("\n🧾 <b>Ингредиенты:</b>")
    for ing in rec.get("ingredients") or []:
        lines.append(_format_ing_line(ing.get("name", ""), float(ing.get("qty") or 1), str(ing.get("unit") or "")))
    lines.append("\n👩‍🍳 <b>Шаги:</b>")
    for s_idx, step in enumerate(rec.get("steps") or [], 1):
        lines.append(f"{s_idx}. {_h(step)}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍳 Приготовить и в КБЖУ", callback_data=f"kitchen:ai_cook:{idx}")],
        [InlineKeyboardButton(text="⭐ В мои рецепты", callback_data=f"kitchen:ai_save:{idx}")],
        [
            InlineKeyboardButton(text="⬅️ К вариантам", callback_data="kitchen:ai_back"),
            InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main"),
        ],
    ])
    await safe_edit(callback.message, "\n".join(lines), reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:ai_cook:"))
async def ai_chef_cook_and_log(callback: types.CallbackQuery, state: FSMContext, db):
    """Сохранить рецепт ИИ-шефа, списать продукты и сразу записать 1 порцию в КБЖУ."""
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
        nutrition=rec.get("nutrition") if isinstance(rec.get("nutrition"), dict) else None,
        source="ai_chef",
        source_label="ИИ-шеф",
    )
    rid = str(saved.get("id") or "")
    rec["saved_key"] = rid
    recipes[idx] = rec
    await state.update_data(ai_recipes=recipes)

    now_utc = datetime.datetime.utcnow()
    cook_res = await kitchen_ai.commit_cooked_recipe(
        db, user, rid, servings=int(saved.get("base_servings") or 2), now_utc=now_utc
    )
    per_serv = (
        cook_res.get("nutrition_per_serving")
        or saved.get("nutrition")
        or fitness_nutrition.estimate_recipe_nutrition(saved)
    )
    local_date = local_date_str(now_utc, user.get("timezone") or "UTC")
    kcal = round(float(per_serv.get("kcal") or 0))
    p_g = round(float(per_serv.get("protein_g") or 0), 1)
    f_g = round(float(per_serv.get("fat_g") or 0), 1)
    c_g = round(float(per_serv.get("carbs_g") or 0), 1)
    await repo.add_meal_log(
        db,
        int(user["id"]),
        local_date,
        str(saved.get("title") or "Блюдо"),
        recipe_id=rid,
        servings=1.0,
        kcal=kcal,
        protein_g=p_g,
        fat_g=f_g,
        carbs_g=c_g,
        source="recipe",
    )
    from services import pulse_service

    pulse_extra = await pulse_service.close_open_food_pulse_slot(db, user, "", now_utc=now_utc)
    lines = [
        f"✨🍳 <b>{_h(saved.get('title'))}</b> — сохранено, продукты списаны!",
        f"🔥 Записано в КБЖУ (1 порц.): <b>{kcal} ккал</b> (Б {p_g:g}г / Ж {f_g:g}г / У {c_g:g}г)",
    ]
    deducted = cook_res.get("deducted") or []
    if deducted:
        ded_str = ", ".join(f"{_h(d['name'])} (ост. {format_quantity(d['remaining'], d['unit'])})" for d in deducted[:4])
        lines.append(f"❄️ Списано: {ded_str}")
    closed_slot = cook_res.get("pulse_closed") or (pulse_extra.get("slot") if pulse_extra else None)
    if closed_slot:
        slot_ru = {"breakfast": "Завтрак", "lunch": "Обед", "dinner": "Ужин"}.get(str(closed_slot), "Еда")
        lines.append(f"💓 Чек-ин Пульса «{slot_ru}» закрыт.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
            InlineKeyboardButton(text="❄️ Холодильник", callback_data="kitchen:fridge"),
        ],
        [
            InlineKeyboardButton(text="⬅️ К вариантам", callback_data="kitchen:ai_back"),
            InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main"),
        ],
    ])
    await safe_edit(callback.message, "\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await _safe_answer_callback(callback, "Приготовлено и записано в КБЖУ! 🍽")


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
        nutrition=rec.get("nutrition") if isinstance(rec.get("nutrition"), dict) else None,
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
        advice_summary=str(meta.get("advice_summary") or ""),
    )
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")


# --- HANDLERS: RECIPE IMPORT (PHOTO / URL / TEXT) ---
_RECIPE_IMPORT_PROMPT = (
    "📥 <b>Импорт рецепта в общую книгу</b>\n\n"
    "Пришли <b>любой вариант</b>:\n"
    "• <b>Фото</b> страницы кулинарной книги или скриншот рецепта;\n"
    "• <b>Ссылку</b> (<code>https://...</code>) на рецепт;\n"
    "• <b>Текст</b> рецепта (название, ингредиенты, шаги).\n\n"
    "Я разберу граммовки, посчитаю КБЖУ и сохраню в «Мои рецепты»."
)


@router.message(Command("addrecipe"))
async def addrecipe_cmd(message: types.Message, state: FSMContext, db):
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    if is_student_mode(user):
        await message.answer("Импорт рецептов доступен во взрослом режиме HiDL.")
        return
    await state.set_state(RecipeImportState.waiting_input)
    await message.answer(_RECIPE_IMPORT_PROMPT, reply_markup=_cancel_fsm_keyboard(), parse_mode="HTML")


@router.callback_query(lambda c: c.data == "kitchen:recipe_import")
async def recipe_import_start(callback: types.CallbackQuery, state: FSMContext, db):
    await _safe_answer_callback(callback)
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if is_student_mode(user):
        await callback.message.answer("Импорт рецептов доступен во взрослом режиме HiDL.")
        return
    await state.set_state(RecipeImportState.waiting_input)
    await callback.message.answer(_RECIPE_IMPORT_PROMPT, reply_markup=_cancel_fsm_keyboard(), parse_mode="HTML")


async def _show_recipe_import_preview(message: types.Message, state: FSMContext, parsed: dict) -> None:
    rec = parsed.get("recipe") if parsed.get("ok") else None
    if not rec:
        err = str(parsed.get("error") or "")
        if err == "llm_not_configured":
            hint = "ИИ-распознавание фото сейчас недоступно — пришли рецепт текстом или ссылкой на сайт с рецептом."
        elif err == "unsafe_url":
            hint = "Эта ссылка не подходит (принимаются только внешние публичные http/https страницы)."
        elif err == "fetch_failed":
            hint = "Не удалось открыть страницу по ссылке — скопируй текст рецепта сюда, и я разберу его."
        else:
            hint = (
                "Не получилось разобрать рецепт. Пришли название, ингредиенты и шаги с новой строки, "
                "ссылку на сайт или чёткое фото страницы."
            )
        await message.answer(hint, reply_markup=_cancel_fsm_keyboard())
        return

    src = str(parsed.get("source") or "text_import")
    src_lbl = str(parsed.get("source_label") or "Импорт")
    await state.set_state(RecipeImportState.confirm)
    await state.update_data(imported_recipe=rec, import_source=src, import_source_label=src_lbl)

    nutr = rec.get("nutrition") or fitness_nutrition.estimate_recipe_nutrition(rec)
    lines = [
        f"📥 <b>Проверь рецепт</b> ({_h(src_lbl)})",
        f"\n<b>{_h(rec.get('title'))}</b>",
    ]
    if rec.get("desc"):
        lines.append(f"<i>{_h(rec['desc'])}</i>")
    lines.append(
        f"⏱ {int(rec.get('time_minutes') or 20)} мин • 🍽 {int(rec.get('base_servings') or 2)} порц."
    )
    if nutr.get("kcal"):
        lines.append(
            f"🔥 ~<b>{int(nutr['kcal'])} ккал</b> на 1 порцию "
            f"(Б {float(nutr['protein_g']):g}г / Ж {float(nutr['fat_g']):g}г / У {float(nutr['carbs_g']):g}г)"
        )
    lines.append("\n🧾 <b>Ингредиенты:</b>")
    ings = list(rec.get("ingredients") or [])
    for ing in ings[:20]:
        lines.append(_format_ing_line(ing.get("name", ""), float(ing.get("qty") or 1), str(ing.get("unit") or "")))
    if len(ings) > 20:
        lines.append(f"… и ещё {len(ings) - 20}")

    lines.append("\n👩‍🍳 <b>Шаги:</b>")
    steps = list(rec.get("steps") or [])
    for idx, step in enumerate(steps[:10], 1):
        lines.append(f"{idx}. {_h(step)}")
    if len(steps) > 10:
        lines.append("…")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сохранить в «Мои рецепты»", callback_data="kitchen:recipe_import_save")],
        [
            InlineKeyboardButton(text="🔄 Прислать другой", callback_data="kitchen:recipe_import"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="kitchen:cancel_fsm"),
        ],
    ])
    await message.answer("\n".join(lines)[:4000], reply_markup=kb, parse_mode="HTML")


@router.message(RecipeImportState.waiting_input, F.photo)
async def recipe_import_photo(message: types.Message, state: FSMContext, db):
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    profile = await _get_meal_profile(db, user["id"])
    img_bytes = await _download_photo_bytes(message)
    parsed = await kitchen_ai.parse_recipe_from_input(
        raw_text=message.caption or "",
        image_bytes=img_bytes,
        image_mime="image/jpeg",
        profile=profile,
    )
    await _show_recipe_import_preview(message, state, parsed)


@router.message(RecipeImportState.waiting_input, lambda m: bool(m.text and not m.text.startswith("/")))
async def recipe_import_text(message: types.Message, state: FSMContext, db):
    if _is_cancel_text(message.text):
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    profile = await _get_meal_profile(db, user["id"])
    parsed = await kitchen_ai.parse_recipe_from_input(
        raw_text=message.text or "",
        profile=profile,
    )
    await _show_recipe_import_preview(message, state, parsed)


@router.callback_query(lambda c: c.data == "kitchen:recipe_import_save")
async def recipe_import_save(callback: types.CallbackQuery, state: FSMContext, db):
    data = await state.get_data()
    rec = data.get("imported_recipe")
    if not isinstance(rec, dict):
        await state.clear()
        await _safe_answer_callback(callback, "Рецепт не найден или уже сохранён.", show_alert=True)
        await safe_edit(callback.message, "Пришли рецепт заново, чтобы сохранить его.", reply_markup=kitchen_main_keyboard())
        return

    src = str(data.get("import_source") or rec.get("source") or "text_import")
    src_lbl = str(data.get("import_source_label") or "Импорт")
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    saved = await repo.upsert_user_recipe(
        db,
        int(user["id"]),
        title=str(rec.get("title") or "Рецепт"),
        desc=str(rec.get("desc") or ""),
        tags=list(rec.get("tags") or []),
        base_servings=int(rec.get("base_servings") or 2),
        time_minutes=int(rec.get("time_minutes") or 20),
        ingredients=list(rec.get("ingredients") or []),
        steps=list(rec.get("steps") or []),
        nutrition=rec.get("nutrition") if isinstance(rec.get("nutrition"), dict) else None,
        source=src,
        source_label=src_lbl,
    )
    await state.clear()
    await _safe_answer_callback(callback, "Сохранила в «Мои рецепты» ⭐")

    actor_name = _h(user.get("name") or "Партнёр")
    await household_service.notify_household_partners(
        getattr(callback, "bot", None),
        db,
        user,
        f"📖 <b>{actor_name}</b> добавил(а) рецепт в общую книгу: <b>{_h(saved.get('title'))}</b>",
    )

    rid = str(saved.get("id") or "")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍳 Открыть / приготовить", callback_data=f"kitchen:cook_view:{rid}:2:mine:0")],
        [
            InlineKeyboardButton(text="⭐ Мои рецепты", callback_data="kitchen:recipes_cat:mine:0"),
            InlineKeyboardButton(text="📥 Ещё рецепт", callback_data="kitchen:recipe_import"),
        ],
        [InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main")],
    ])
    await safe_edit(
        callback.message,
        f"⭐ Рецепт <b>«{_h(saved.get('title'))}»</b> сохранён в общую книгу <b>«Мои рецепты»</b>!\n"
        "Теперь он доступен вам обоим, участвует в подборе и меню на 3–4 дня.",
        reply_markup=kb,
        parse_mode="HTML",
    )


