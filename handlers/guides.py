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
)

router = Router()


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


@router.message(Command("recipes_fast"))
async def recipes_fast(message: types.Message) -> None:
    blocks = []
    for r in RECIPES_FAST:
        blocks.append(f"{r['name']} ({r['time']})\n{r['ingredients']}\n{r['notes']}")
    await message.answer("Быстрые блюда ≤20 минут:\n\n" + "\n\n".join(blocks), reply_markup=main_menu_keyboard())


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
