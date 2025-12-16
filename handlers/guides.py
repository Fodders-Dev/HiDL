import logging

from aiogram import Router, types
from aiogram.filters import Command

from keyboards.common import main_menu_keyboard
from utils.pager import start_paged, get_page
from research_data import (
    CLEAN_SCHEDULE,
    FINANCE_TIPS,
    PLATE_PRESETS,
    RECIPES_FAST,
    MEAL_PLANS,
    SHOPLISTS,
    WALK_QUESTS,
    COOK_RECIPES,
)

from db import repositories as repo
from utils.rows import rows_to_dicts
from utils.user import ensure_user

router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(lambda c: c.data and c.data.startswith("page:"))
async def paginate(callback: types.CallbackQuery) -> None:
    _, key, index = callback.data.split(":")
    idx = int(index)
    chunk = get_page(callback.message.chat.id, key, idx)
    if not chunk:
        await callback.answer("Больше нет страниц.")
        return
    buttons = []
    buttons.append([types.InlineKeyboardButton(text="Дальше", callback_data=f"page:{key}:{idx+1}")])
    kb = types.InlineKeyboardMarkup(inline_keyboard=buttons) if chunk else None
    await callback.message.answer(chunk, reply_markup=kb or main_menu_keyboard())
    await callback.answer()


@router.message(Command("meal_plan"))
async def meal_plan(message: types.Message) -> None:
    econ = MEAL_PLANS["минимум"]
    mid = MEAL_PLANS["средний"]
    relaxed = MEAL_PLANS.get("расслабленный", mid)
    shop_econ = "\n".join(SHOPLISTS["эконом"]["позиции"])
    shop_mid = "\n".join(SHOPLISTS["средний"]["позиции"])
    text = (
        "Меню-конструктор на неделю:\n\n"
        f"Минимум ({econ['цена']}):\n" + "\n".join(econ["дни"]) +
        f"\nСписок покупок ({SHOPLISTS['эконом']['цена']}):\n{shop_econ}"
        f"\n\nСредний ({mid['цена']}):\n" + "\n".join(mid["дни"]) +
        f"\nСписок покупок ({SHOPLISTS['средний']['цена']}):\n{shop_mid}"
        f"\n\nРасслабленный ({relaxed['цена']}):\n" + "\n".join(relaxed.get("дни", [])) +
        "\n\nПринцип: 2–3 основы (крупы/макароны), белок (курица/яйца/бобовые/тофу/сыр/рыба), овощи (свежие/заморозка). Меняй местами обеды/ужины."
    )
    key, chunks = start_paged(text, message.chat.id)
    if len(chunks) == 1:
        await message.answer(chunks[0], reply_markup=main_menu_keyboard())
    else:
        kb = types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text="Дальше", callback_data=f"page:{key}:1")]]
        )
        await message.answer(chunks[0], reply_markup=kb)


@router.message(Command("plate"))
async def plate(message: types.Message, db) -> None:
    from utils.user import ensure_user
    from db import repositories as repo

    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    wellness = await repo.get_wellness(db, user["id"])
    profile = wellness.get("meal_profile", "omnivore") if wellness else "omnivore"
    prof_label = {"omnivore": "обычный", "vegetarian": "вегетарианец", "vegan": "веган"}.get(profile, "обычный")
    preset = (
        PLATE_PRESETS["обычная_бюджет"] if profile == "omnivore" else
        PLATE_PRESETS["вегетариан"] if profile == "vegetarian" else
        PLATE_PRESETS["веган"]
    )
    text = (
        f"Тарелка дня (профиль: {prof_label}):\n"
        f"• овощи: {preset['овощи']}\n"
        f"• крупа: {preset['углеводы']}\n"
        f"• белок: {preset['белок']}\n"
        f"{PLATE_PRESETS['ккал_ориентир']}"
    )
    await message.answer(text, reply_markup=main_menu_keyboard())


@router.message(Command("finance_tips"))
async def finance_tips(message: types.Message) -> None:
    text = (
        "Финансовые микро-уроки:\n"
        f"- Подписки: {FINANCE_TIPS['подписки']}\n"
        f"- Рассрочки: {FINANCE_TIPS['рассрочки']}\n"
        f"- Подушка: {FINANCE_TIPS['подушка']}\n"
        f"- Схемы: {FINANCE_TIPS['схемы']}\n"
    )
    await message.answer(text, reply_markup=main_menu_keyboard())


@router.message(Command("paperwork"))
async def paperwork(message: types.Message) -> None:
    await message.answer(
        "Бумажки/врачи/переезд:\n"
        "- Храни копии паспорта/полиса/договоров в облаке и бумажной папке.\n"
        "- Аренда: спроси про счётчики, аварийные контакты, правила оплаты.\n"
        "- Врачи: зубной и зрение раз в год, аптечка — проверяй сроки раз в 6 мес.\n"
        "- Если переехал: базовый набор — посуда, тряпки/химия, постельное, аптечка, мусорные пакеты.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("adhd_mode"))
async def adhd_mode(message: types.Message) -> None:
    await message.answer(
        "ADHD-friendly подход:\n"
        "- Не больше 3–5 задач в день.\n"
        "- Дроби: мусор → посуда → стол (не всё сразу).\n"
        "- Используй фокус 10/5 или 5/2: короткий раунд, короткий отдых.\n"
        "- Щадящий день? Жми кнопку “Щадящий режим” в настройках.",
        reply_markup=main_menu_keyboard(),
    )


def _cook_recipes_map():
    return {r["key"]: r for r in COOK_RECIPES}


def _scale_ingredients(recipe: dict, servings: int) -> list[dict]:
    base = recipe.get("base_servings", 1) or 1
    factor = max(1, servings) / base
    scaled = []
    for ing in recipe.get("ingredients", []):
        scaled.append(
            {
                "tag": ing.get("tag"),
                "label": ing.get("label"),
                "amount": round(ing.get("amount", 0) * factor),
                "unit": ing.get("unit", "g"),
            }
        )
    return scaled


def _pantry_index(pantry_rows: list[dict]) -> dict:
    index: dict[str, dict] = {}
    for row in pantry_rows:
        name = (row.get("name") or "").lower()
        if not name:
            continue
        index[name] = row
    return index


def _to_base(amount: float, unit: str) -> tuple[float, str]:
    u = (unit or "").lower()
    if u in {"kg", "кг"}:
        return amount * 1000, "g"
    if u in {"g", "гр"}:
        return amount, "g"
    if u in {"l", "л"}:
        return amount * 1000, "ml"
    if u in {"ml", "мл"}:
        return amount, "ml"
    if u in {"шт"}:
        return amount, "шт"
    return amount, u or ""


def _check_pantry_for_recipe(
    scaled_ingredients: list[dict], pantry_items: list[dict]
) -> tuple[list[str], list[str], list[tuple[str, float]]]:
    """Вернуть списки (хватает, мало, не хватает)."""
    index = _pantry_index(pantry_items)
    ok: list[str] = []
    low: list[tuple[str, float]] = []
    missing: list[str] = []
    for ing in scaled_ingredients:
        tag = (ing.get("tag") or "").lower()
        label = ing.get("label") or tag or "ингредиент"
        needed_amt, needed_unit = _to_base(float(ing.get("amount", 0)), ing.get("unit", "g"))
        pantry_row = None
        for name, row in index.items():
            if tag and tag in name:
                pantry_row = row
                break
        if not pantry_row or needed_amt <= 0:
            missing.append(label)
            continue
        have_amt, have_unit = _to_base(float(pantry_row.get("amount", 0)), pantry_row.get("unit", "шт"))
        if have_unit != needed_unit or needed_amt == 0:
            ok.append(label)
            continue
        leftover = have_amt - needed_amt
        if leftover < 0:
            missing.append(label)
        elif leftover <= (100 if needed_unit in {"g", "ml"} else 1):
            low.append((label, max(0.0, leftover)))
        else:
            ok.append(label)
    return ok, missing, low


async def _send_recipe_list(message: types.Message, db) -> None:
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    pantry_rows = rows_to_dicts(await repo.list_pantry_items(db, user["id"]))
    lines = ["Быстрые блюда ≤30 минут. Выбери, что хочется приготовить:"]
    kb_rows = []
    for r in COOK_RECIPES:
        lines.append(f"• {r['title']} ({r['time']}) — {r['short_desc']}")
        kb_rows.append(
            [
                types.InlineKeyboardButton(
                    text=r["title"], callback_data=f"recipe:card:{r['key']}:1"
                )
            ]
        )
    kb = types.InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await message.answer("\n".join(lines), reply_markup=kb)


@router.message(Command("recipes_fast"))
async def recipes_fast(message: types.Message, db) -> None:
    await _send_recipe_list(message, db)


@router.callback_query(lambda c: c.data and c.data.startswith("recipe:card:"))
async def recipe_card(callback: types.CallbackQuery, db) -> None:
    _, _, key, servings_str = callback.data.split(":")
    servings = int(servings_str)
    recipe = _cook_recipes_map().get(key)
    if not recipe:
        await callback.answer("Рецепт не нашла.", show_alert=True)
        return
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    pantry_rows = rows_to_dicts(await repo.list_pantry_items(db, user["id"]))
    scaled = _scale_ingredients(recipe, servings)
    ok, missing, low = _check_pantry_for_recipe(scaled, pantry_rows)
    lines = [
        f"{recipe['title']} ({recipe['time']})",
        recipe["short_desc"],
        "",
        f"На {servings} порцию(и) нужно:",
    ]
    for ing in scaled:
        lines.append(f"• {ing['label']} — {ing['amount']} {ing['unit']}")
    if ok or missing or low:
        lines.append("")
        lines.append("По запасам дома:")
        if ok:
            lines.append("✔ Уже есть: " + ", ".join(ok))
        if low:
            show = ", ".join(f"{name} (останется ~{leftover:.0f})" for name, leftover in low)
            lines.append("⚠ Почти кончатся: " + show)
        if missing:
            lines.append("✖ Не хватает: " + ", ".join(missing))
    kb_rows = [
        [
            types.InlineKeyboardButton(
                text="Готовим", callback_data=f"recipe:steps:{key}:{servings}"
            )
        ],
        [
            types.InlineKeyboardButton(
                text="Изменить порции", callback_data=f"recipe:serve:{key}:{servings}"
            )
        ],
    ]
    kb = types.InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await callback.message.answer("\n".join(lines), reply_markup=kb)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("recipe:serve:"))
async def recipe_change_servings(callback: types.CallbackQuery) -> None:
    _, _, key, current_str = callback.data.split(":")
    recipe = _cook_recipes_map().get(key)
    if not recipe:
        await callback.answer()
        return
    kb_rows = []
    for s in (1, 2, 3, 4):
        kb_rows.append(
            [
                types.InlineKeyboardButton(
                    text=f"{s} порц.",
                    callback_data=f"recipe:card:{key}:{s}",
                )
            ]
        )
    kb = types.InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await callback.message.answer(
        "На сколько порций готовим? Тебе не обязательно всё съедать сразу — можно убрать часть в контейнер.",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("recipe:steps:"))
async def recipe_steps(callback: types.CallbackQuery) -> None:
    _, _, key, servings_str = callback.data.split(":")
    servings = int(servings_str)
    recipe = _cook_recipes_map().get(key)
    if not recipe:
        await callback.answer("Рецепт не нашла.", show_alert=True)
        return
    steps = recipe.get("steps", [])
    lines = [f"Готовим: {recipe['title']} (на {servings} порц.)"]
    for idx, step in enumerate(steps, start=1):
        lines.append(f"{idx}. {step}")
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="Я приготовил(а)", callback_data=f"recipe:cooked:{key}:{servings}"
                )
            ]
        ]
    )
    await callback.message.answer("\n".join(lines), reply_markup=kb)
    await callback.answer()


def _apply_recipe_to_pantry(recipe: dict, servings: int, pantry_items: list[dict]) -> tuple[list[str], list[str]]:
    """Вычесть продукты из pantry по приготовленному рецепту."""
    base = recipe.get("base_servings", 1) or 1
    factor = max(1, servings) / base
    index = _pantry_index(pantry_items)
    finished: list[str] = []
    low_left: list[str] = []
    for ing in recipe.get("ingredients", []):
        tag = (ing.get("tag") or "").lower()
        label = ing.get("label") or tag or "ингредиент"
        needed_amt, needed_unit = _to_base(ing.get("amount", 0) * factor, ing.get("unit", "g"))
        pantry_row = None
        for name, row in index.items():
            if tag and tag in name:
                pantry_row = row
                break
        if not pantry_row:
            continue
        have_amt, have_unit = _to_base(pantry_row.get("amount", 0), pantry_row.get("unit", "шт"))
        if have_unit != needed_unit or needed_amt <= 0:
            continue
        leftover = have_amt - needed_amt
        if leftover <= 0:
            finished.append(label)
            new_amount_base = 0
        else:
            new_amount_base = leftover
            if leftover <= (100 if needed_unit in {"g", "ml"} else 1):
                low_left.append(label)
        # конвертируем обратно в единицы pantry
        unit = pantry_row.get("unit", "шт")
        if unit in {"kg", "кг"} and needed_unit == "g":
            new_amount = new_amount_base / 1000
        elif unit in {"g", "гр"} and needed_unit == "g":
            new_amount = new_amount_base
        elif unit in {"l", "л"} and needed_unit == "ml":
            new_amount = new_amount_base / 1000
        elif unit in {"ml", "мл"} and needed_unit == "ml":
            new_amount = new_amount_base
        elif unit in {"шт"} and needed_unit == "шт":
            new_amount = new_amount_base
        else:
            new_amount = pantry_row.get("amount", 0)
        pantry_row["amount"] = max(0, float(new_amount))
    return finished, low_left


@router.callback_query(lambda c: c.data and c.data.startswith("recipe:cooked:"))
async def recipe_cooked(callback: types.CallbackQuery, db) -> None:
    _, _, key, servings_str = callback.data.split(":")
    servings = int(servings_str)
    recipe = _cook_recipes_map().get(key)
    if not recipe:
        await callback.answer("Рецепт не нашла.", show_alert=True)
        return
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    pantry_rows_raw = await repo.list_pantry_items(db, user["id"])
    pantry_items = rows_to_dicts(pantry_rows_raw)
    finished, low_left = _apply_recipe_to_pantry(recipe, servings, pantry_items)
    # обновить БД
    for item in pantry_items:
        await repo.update_pantry_item(db, user["id"], item["id"], amount=item.get("amount"), expires_at=None)
    lines = ["Отлично! Надеюсь, было вкусно. Списала продукты (если они были в запасах).", ""]
    lines.append("Что изменилось в запасах после готовки:")
    if not finished and not low_left:
        lines.append("• Я не увидела изменений по продуктам, но блюдо всё равно засчитываем.")
    if finished:
        lines.append("• Закончились: " + ", ".join(finished))
    if low_left:
        lines.append("• Почти закончились: " + ", ".join(low_left))
        lines.append("Если хочешь, можешь добавить их в список покупок через финансы или свои напоминания.")
    await callback.message.answer("\n".join(lines), reply_markup=main_menu_keyboard())
    await callback.answer("Приятного аппетита")
    logger.info(
        "recipe.cooked",
        extra={
            "user_id": user["id"],
            "recipe_key": key,
            "servings": servings,
            "finished": finished,
            "low_left": low_left,
        },
    )


@router.message(Command("shoplist"))
async def shoplist(message: types.Message) -> None:
    econ = SHOPLISTS["эконом"]
    mid = SHOPLISTS["средний"]
    text = (
        f"Эконом (~{econ['цена']}):\n- " + "\n- ".join(econ["позиции"]) +
        "\n\nСредний (~{mid}):\n- ".format(mid=mid["цена"]) + "\n- ".join(mid["позиции"])
    )
    await message.answer("Списки покупок на неделю:\n\n" + text, reply_markup=main_menu_keyboard())


@router.message(Command("clean_schedule"))
async def clean_schedule(message: types.Message) -> None:
    parts = []
    for freq, items in CLEAN_SCHEDULE.items():
        parts.append(f"{freq}:")
        parts.extend([f"- {i}" for i in items])
    await message.answer("Регулярка по дому:\n" + "\n".join(parts), reply_markup=main_menu_keyboard())


@router.message(Command("walk_quests"))
async def walk_quests(message: types.Message) -> None:
    lines = []
    for w in WALK_QUESTS:
        line = f"{w['name']}: {w['time']}, {w.get('steps','')} {w.get('freq','')}\n{w['note']}"
        lines.append(line)
    await message.answer("Мини-квесты ходьбы:\n" + "\n\n".join(lines), reply_markup=main_menu_keyboard())


async def _suggest_from_expiring(message: types.Message, db) -> None:
    """
    Подсказка: что можно приготовить из продуктов, у которых скоро истечёт срок.

    Берём 1–3 ближайших по сроку продукта и пытаемся найти рецепты,
    где они фигурируют в ингредиентах. Если не нашли — даём общие идеи.
    """
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    now = datetime.datetime.utcnow()
    from utils.time import local_date_str

    local_date = local_date_str(now, user["timezone"])
    wellness = await repo.get_wellness(db, user["id"])
    window_days = int((wellness or {}).get("expiring_window_days", 3))
    soon, expired = await repo.pantry_expiring(db, user["id"], local_date, window_days=window_days)
    soon_d = rows_to_dicts(soon)
    expired_d = rows_to_dicts(expired)
    candidates = (soon_d + expired_d)[:3]
    if not candidates:
        await message.answer(
            "Пока я не вижу продуктов с горящим сроком. Можно просто выбрать рецепт из «Быстрых блюд».",
            reply_markup=main_menu_keyboard(),
        )
        return

    product_names = [c.get("name") for c in candidates if c.get("name")]
    recipes_map = _cook_recipes_map()
    matched: list[dict] = []
    for r in recipes_map.values():
        tags = [ing.get("tag", "").lower() for ing in r.get("ingredients", [])]
        labels = [ing.get("label", "").lower() for ing in r.get("ingredients", [])]
        for name in product_names:
            name_l = (name or "").lower()
            if any(name_l in t for t in tags + labels):
                matched.append(r)
                break
        if len(matched) >= 2:
            break

    lines: list[str] = []
    lines.append("Смотрю на продукты, у которых горят сроки.")
    lines.append("")
    lines.append("Скоро истечёт или уже истёк срок у:")
    for c in candidates:
        lines.append(f"• {c.get('name')} — до {c.get('expires_at')}")

    if matched:
        lines.append("")
        lines.append("Можно приготовить, например:")
        for r in matched:
            lines.append(f"• {r['title']} ({r['time']}) — {r['short_desc']}")
        lines.append("")
        lines.append("Если хочешь, открой «Быстрые рецепты» и выбери один из них.")
    else:
        lines.append("")
        lines.append(
            "Прямо подходящих рецептов я не нашла, но можно использовать эти продукты "
            "в простых блюдах: яйца — в омлете/яичнице, овощи — в гарнире или супе, "
            "молочку — в завтраке."
        )

    await message.answer("\n".join(lines), reply_markup=main_menu_keyboard())


@router.message(Command("cook_from_expiring"))
async def cook_from_expiring_command(message: types.Message, db) -> None:
    """Команда: подсказать, что можно приготовить из продуктов с горящим сроком."""
    await _suggest_from_expiring(message, db)
