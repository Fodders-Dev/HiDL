import datetime

from aiogram import Router, types
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command

from db import repositories as repo
from handlers import guides
from keyboards.common import (
    food_menu_keyboard,
    home_menu_keyboard,
    main_menu_keyboard,
    money_menu_keyboard,
    movement_menu_keyboard,
    settings_menu_keyboard,
)
from utils.time import format_time_local, local_date_str
from utils.tone import tone_message, tone_short_ack
from utils.user import ensure_user
from utils.today import render_today

router = Router()


@router.message(Command("menu"))
@router.message(lambda m: m.text and "меню" in m.text.lower())
async def show_menu(message: types.Message) -> None:
    await message.answer("Главное меню:", reply_markup=main_menu_keyboard())


@router.message(Command("today"))
@router.message(lambda m: m.text and "сегодня" in m.text.lower())
async def cmd_today(message: types.Message, db) -> None:
    user_row = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    user = dict(user_row)
    text, kb = await render_today(db, user)
    await message.answer(text, reply_markup=kb or main_menu_keyboard())


@router.message(Command("settings"))
async def settings_info(message: types.Message, db) -> None:
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)

    now_utc = datetime.datetime.utcnow()
    local_time = format_time_local(now_utc, user["timezone"])
    await message.answer(
        "Твои настройки:\n"
        f"Имя: {user['name']}\n"
        f"Часовой пояс: {user['timezone']} (сейчас {local_time})\n"
        f"Подъём: {user['wake_up_time']} / Отбой: {user['sleep_time']}\n"
        f"Цели/приоритет: {user['goals'] or 'не задано'}\n\n"
        "Чтобы поменять что-то — нажми ⚙ Настройки в меню.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(lambda m: m.text and "еда" in m.text.lower())
async def food_menu(message: types.Message) -> None:
    await message.answer("Еда: выбери, что нужно.", reply_markup=food_menu_keyboard())



@router.message(lambda m: m.text and "дом" in m.text.lower())
async def home_menu(message: types.Message) -> None:
    await message.answer("Дом: уборка, зоны, стирка/запах.", reply_markup=home_menu_keyboard())


@router.message(lambda m: m.text and "движ" in m.text.lower())
async def move_menu(message: types.Message) -> None:
    await message.answer("Движение: прогулки, фокус.", reply_markup=movement_menu_keyboard())


@router.message(lambda m: m.text and ("⚙" in m.text or "настрой" in m.text.lower()))
async def settings_menu(message: types.Message, db, state: FSMContext) -> None:
    from handlers import settings as settings_handler

    await settings_handler.settings_entry(message, state=state, db=db)


@router.callback_query(lambda c: c.data and c.data.startswith("food:"))
async def food_callbacks(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    action = callback.data.split(":")[1]
    if action == "plate":
        await guides.plate(callback.message, db)
    elif action == "recipes":
        await guides.recipes_fast(callback.message)
    elif action == "meal_plan":
        await guides.meal_plan(callback.message)
    elif action == "shoplist":
        await guides.shoplist(callback.message)
    elif action == "pantry":
        from handlers import ask_mom

        await ask_mom.start_cook_flow(callback.message, state)
    await callback.answer()



@router.callback_query(lambda c: c.data and c.data.startswith("home:"))
async def home_callbacks(callback: types.CallbackQuery, db) -> None:
    action = callback.data.split(":")[1]
    if action == "menu":
        await callback.message.answer("Дом: уборка, зоны, стирка/запах.", reply_markup=home_menu_keyboard())
    elif action == "clean":
        await guides.clean_schedule(callback.message)
    elif action == "zone":
        from handlers import zones

        await zones.zones(callback.message)
    elif action == "regular":
        from handlers import home_tasks

        await home_tasks.home_audit(callback.message, db)
    elif action == "quick":
        await guides.clean_schedule(callback.message)
    elif action == "laundry":
        from handlers.ask_mom import ask_menu_keyboard

        await callback.message.answer(
            "Стирка/запах: выбери, что сейчас актуально.",
            reply_markup=ask_menu_keyboard(),
        )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("move:"))
async def move_callbacks(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    action = callback.data.split(":")[1]
    if action == "short":
        await guides.walk_quests(callback.message)
    elif action == "long":
        await guides.walk_quests(callback.message)
    elif action == "focus":
        from handlers import movement
        await movement.focus_start(callback, db=db, state=state)
    elif action == "warmup":
        from handlers import movement
        await movement.move_warmup(callback)
    elif action == "home":
        from handlers import movement
        await movement.move_home(callback)
    elif action == "weight":
        from handlers import movement
        await movement.move_weight(callback, state=state, db=db)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("set:"))
async def settings_callbacks(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    parts = callback.data.split(":")
    if len(parts) < 2:
        await callback.answer()
        return
    action = parts[1]
    if action == "gentle":
        await callback.message.answer("Щадящий режим включается/выключается здесь. Если был щадящий день — завтра сам отключится.", reply_markup=main_menu_keyboard())
    elif action == "tone":
        await callback.message.answer("Тон общения: выбери стиль в настройках ниже.", reply_markup=main_menu_keyboard())
    elif action == "wellness":
        from handlers import wellness

        await wellness.wellness_settings(callback.message, db)
    elif action == "profile":
        await callback.message.answer("Профиль питания можно выбрать в «Спросить маму» → готовка.", reply_markup=main_menu_keyboard())
    elif action == "settings":
        from handlers import settings as settings_handler

        await settings_handler.settings_entry(callback.message, state=state, db=db)
    await callback.answer()
