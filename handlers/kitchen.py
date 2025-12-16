import datetime
import json
import logging
import os
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

RECIPES_FILE = "data/knowledge/recipes_core.json"

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
        [InlineKeyboardButton(text="❄️ Мой холодильник", callback_data="kitchen:fridge")],
        [InlineKeyboardButton(text="📖 Рецепты", callback_data="kitchen:recipes")],
        [InlineKeyboardButton(text="🛒 Список покупок", callback_data="kitchen:shoplist")],
    ])

def recipes_list_keyboard(recipes: List[dict]) -> InlineKeyboardMarkup:
    rows = []
    for r in recipes:
        rows.append([InlineKeyboardButton(text=r["title"], callback_data=f"kitchen:cook_view:{r['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="kitchen:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def shopping_list_keyboard(items: List[dict]) -> InlineKeyboardMarkup:
    rows = []
    for item in items:
        status = "✅" if item["is_bought"] else "⭕️"
        txt = f"{status} {item['item_name']} ({item['quantity']:g} {item['unit']})"
        rows.append([
            InlineKeyboardButton(text=txt, callback_data=f"kitchen:shop_toggle:{item['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"kitchen:shop_del:{item['id']}")
        ])
    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="kitchen:shop_add")])
    if any(i["is_bought"] for i in items):
        rows.append([InlineKeyboardButton(text="🏠 Я всё купил (в холодильник)", callback_data="kitchen:shop_finish")])
    rows.append([InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# --- HANDLERS: MAIN ---
@router.message(Command("kitchen"))
async def kitchen_cmd(message: types.Message):
    await message.answer("🍽 <b>Умная кухня</b>\nВыбери раздел:", reply_markup=kitchen_main_keyboard(), parse_mode="HTML")

@router.callback_query(lambda c: c.data == "kitchen:main")
async def kitchen_home(callback: types.CallbackQuery):
    await callback.message.edit_text("🍽 <b>Умная кухня</b>", reply_markup=kitchen_main_keyboard(), parse_mode="HTML")
    await callback.answer()

# --- HANDLERS: SHOPPING LIST ---
@router.callback_query(lambda c: c.data == "kitchen:shoplist")
async def show_shoplist(callback: types.CallbackQuery, db):
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    rows = await repo.list_shopping_items(db, user["id"])
    items = rows_to_dicts(rows)
    text = "<b>🛒 Список покупок</b>\n"
    if not items:
        text += "Пока пусто. Можно добавить продукты вручную или из рецептов."
    else:
        bought_cnt = sum(1 for i in items if i["is_bought"])
        text += f"Всего: {len(items)}, куплено: {bought_cnt}"
    
    await callback.message.edit_text(text, reply_markup=shopping_list_keyboard(items), parse_mode="HTML")
    await callback.answer()

@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:shop_toggle:"))
async def toggle_shop_item(callback: types.CallbackQuery, db):
    iid = int(callback.data.split(":")[2])
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    rows = await repo.list_shopping_items(db, user["id"]) # Get current state to toggle
    items = rows_to_dicts(rows)
    item = next((i for i in items if i["id"] == iid), None)
    if item:
        new_status = not item["is_bought"]
        await repo.mark_shopping_bought(db, user["id"], iid, new_status)
    await show_shoplist(callback, db) # refresh

@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:shop_del:"))
async def del_shop_item(callback: types.CallbackQuery, db):
    iid = int(callback.data.split(":")[2])
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.delete_shopping_item(db, user["id"], iid)
    await show_shoplist(callback, db)

@router.callback_query(lambda c: c.data == "kitchen:shop_finish")
async def finish_shopping(callback: types.CallbackQuery, db):
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    count = await repo.complete_shopping_trip(db, user["id"])
    await callback.answer(f"Перенесено продуктов: {count}", show_alert=True)
    await show_shoplist(callback, db)

@router.callback_query(lambda c: c.data == "kitchen:shop_add")
async def add_shop_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(ShoppingAddState.name)
    await callback.message.answer("Что нужно купить? (Напиши название)", reply_markup=main_menu_keyboard())
    await callback.answer()

@router.message(ShoppingAddState.name)
async def add_shop_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(ShoppingAddState.amount)
    await message.answer("Сколько? (например '1 кг', '2 шт' или просто число)", reply_markup=main_menu_keyboard())

@router.message(ShoppingAddState.amount)
async def add_shop_amount(message: types.Message, state: FSMContext, db):
    amount, unit = _parse_amount_unit(message.text)
    data = await state.get_data()
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.create_shopping_item(db, user["id"], data["name"], amount, unit)
    await state.clear()
    await message.answer(f"✅ Добавлено: {data['name']} ({amount} {unit}) в список покупок.")
    # Show list again? Maybe just button
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 К списку", callback_data="kitchen:shoplist")]])
    await message.answer("Перейти к списку?", reply_markup=kb)

# --- HANDLERS: RECIPES ---
@router.callback_query(lambda c: c.data == "kitchen:recipes")
async def show_recipes(callback: types.CallbackQuery):
    recipes = load_recipes()
    await callback.message.edit_text("📖 <b>Книга рецептов</b>\nВыбери блюдо:", reply_markup=recipes_list_keyboard(recipes), parse_mode="HTML")
    await callback.answer()

@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:cook_view:"))
async def view_recipe(callback: types.CallbackQuery):
    rid = callback.data.split(":")[2]
    recipe = get_recipe(rid)
    if not recipe:
        await callback.answer("Рецепт не найден", show_alert=True)
        return
    
    text = f"<b>{recipe['title']}</b>\n{recipe['desc']}\n\n"
    text += f"⏱ Время: {recipe.get('time_minutes', 15)} мин\n"
    text += "📝 Ингредиенты (на 1 порцию):\n"
    for ing in recipe["ingredients"]:
        text += f"• {ing['name']}: {ing['qty']} {ing['unit']}\n"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍳 Готовить!", callback_data=f"kitchen:cook_start:{rid}")],
        [InlineKeyboardButton(text="🔙 К рецептам", callback_data="kitchen:recipes")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:cook_start:"))
async def cook_start_servings(callback: types.CallbackQuery, state: FSMContext):
    rid = callback.data.split(":")[2]
    await state.set_state(CookingState.servings)
    await state.update_data(recipe_id=rid)
    
    # Servings keyboard
    btns = []
    for i in range(1, 6):
        btns.append(InlineKeyboardButton(text=str(i), callback_data=f"kitchen:cook_serv:{i}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[btns, [InlineKeyboardButton(text="Отмена", callback_data="kitchen:recipes")]])
    
    await callback.message.edit_text("На сколько персон готовим?", reply_markup=kb)

@router.callback_query(lambda c: c.data and c.data.startswith("kitchen:cook_serv:"))
async def cook_check_ingredients(callback: types.CallbackQuery, state: FSMContext, db):
    servings = int(callback.data.split(":")[2])
    data = await state.get_data()
    rid = data["recipe_id"]
    recipe = get_recipe(rid)
    
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    pantry_rows = await repo.list_pantry_items(db, user["id"])
    pantry = rows_to_dicts(pantry_rows)
    
    text = f"👩‍🍳 <b>Готовим: {recipe['title']}</b> ({servings} чел.)\n\nПроверка продуктов:\n"
    missing = []
    
    for ing in recipe["ingredients"]:
        needed = ing["qty"] * servings
        # Find in pantry (rough matching)
        found = next((p for p in pantry if ing["name"].lower() in p["name"].lower()), None)
        have = found["amount"] if found else 0
        unit = ing["unit"]
        
        status = "✅"
        if have < needed:
            status = "⚠️ Мало" if have > 0 else "❌ Нет"
            missing.append({"name": ing["name"], "qty": needed - have, "unit": unit})
        
        text += f"{status} {ing['name']}: надо {needed:g}{unit}, (есть {have:g})\n"
        
    text += "\nНачинаем готовить?"
    
    # Store missing for shopping list logic
    await state.update_data(missing=missing, servings=servings)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Приготовил! (-продукты)", callback_data="kitchen:cook_done")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"kitchen:cook_view:{rid}")]
    ])
    
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(lambda c: c.data == "kitchen:cook_done")
async def cook_commit(callback: types.CallbackQuery, state: FSMContext, db):
    data = await state.get_data()
    rid = data["recipe_id"]
    servings = data["servings"]
    recipe = get_recipe(rid)
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    pantry_rows = await repo.list_pantry_items(db, user["id"])
    pantry = rows_to_dicts(pantry_rows) # refresh
    
    # Deduct Logic
    for ing in recipe["ingredients"]:
        needed = ing["qty"] * servings
        found = next((p for p in pantry if ing["name"].lower() in p["name"].lower()), None)
        if found:
            new_amount = max(0, found["amount"] - needed)
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
            [InlineKeyboardButton(text="Нет, спасибо", callback_data="kitchen:main")]
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
@router.callback_query(lambda c: c.data == "kitchen:fridge")
async def fridge_view(callback: types.CallbackQuery, db):
    # Reimplemented fridge view below
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    rows_all = await repo.list_pantry_items(db, user["id"])
    items = rows_to_dicts(rows_all)
    
    text = "<b>❄️ Мой холодильник</b>\n"
    if not items:
        text += "Пусто. Добавь что-нибудь."
    else:
        # Group by category
        cats = {}
        for i in items:
            c = i.get("category", "прочее")
            if c not in cats: cats[c] = []
            cats[c].append(i)
            
        for c, c_items in cats.items():
            text += f"\n<b>{c.capitalize()}</b>:\n"
            for i in c_items:
                low = " ⚠️" if is_low(i) else ""
                text += f"• {i['name']} — {format_quantity(i['amount'], i['unit'])}{low}\n"
                
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить продукт", callback_data="kitchen:fridge_add")],
        [InlineKeyboardButton(text="🗑 Удалить что-то", callback_data="kitchen:fridge_del_view")],
        [InlineKeyboardButton(text="⬅️ Меню кухни", callback_data="kitchen:main")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(lambda c: c.data == "kitchen:fridge_add")
async def fridge_add_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(PantryAddState.name)
    await callback.message.answer("Напиши название продукта:", reply_markup=main_menu_keyboard())
    await callback.answer()

@router.message(PantryAddState.name)
async def fridge_add_name_handler(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(PantryAddState.amount)
    await message.answer("Сколько? (например '1 кг')", reply_markup=main_menu_keyboard())

@router.message(PantryAddState.amount)
async def fridge_add_amount_handler(message: types.Message, state: FSMContext):
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

