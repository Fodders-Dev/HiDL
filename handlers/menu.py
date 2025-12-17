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

@router.callback_query(lambda c: c.data and c.data == "main:menu")
async def main_menu_callback(callback: types.CallbackQuery) -> None:
    await callback.message.answer("Главное меню:", reply_markup=main_menu_keyboard())
    await callback.answer()


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
    await message.answer(
        "Еда — это топливо и радость. Я помогу не ломать голову над вопросом «что приготовить» и следить за запасами.",
        reply_markup=food_menu_keyboard(),
    )


@router.message(lambda m: m.text and ("покуп" in m.text.lower() or "🛒" in m.text))
async def shopping_menu(message: types.Message, db) -> None:
    from handlers import kitchen

    await kitchen.send_shoplist(message, db)



@router.message(lambda m: m.text and "дом" in m.text.lower())
async def home_menu(message: types.Message, db) -> None:
    from utils.user import ensure_user
    from utils.time import local_date_str
    import datetime

    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    # гарантируем наличие базовой регулярки
    await repo.ensure_regular_tasks(db, user["id"], today)
    await message.answer(
        "Дом: помогу навести порядок по-человечески. Выбирай, что актуально сейчас:",
        reply_markup=home_menu_keyboard(),
    )


@router.message(lambda m: m.text and ("спорт" in m.text.lower() or "движ" in m.text.lower()))
async def move_menu(message: types.Message) -> None:
    await message.answer("Спорт: чуть подвигаться, чтобы стало легче в голове и теле.", reply_markup=movement_menu_keyboard())


@router.message(lambda m: m.text and ("поговор" in m.text.lower() or "болта" in m.text.lower()))
async def talk_menu(message: types.Message) -> None:
    from handlers import talk

    await talk.talk_placeholder(message)


@router.callback_query(lambda c: c.data and c.data == "today:menu")
async def today_menu(callback: types.CallbackQuery, db) -> None:
    user_row = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    user = dict(user_row)
    from utils.today import render_today
    text, kb = await render_today(db, user)
    await callback.message.edit_text(text, reply_markup=kb or main_menu_keyboard())
    await callback.answer()


@router.message(lambda m: m.text and ("⚙" in m.text or "настрой" in m.text.lower()))
async def settings_menu(message: types.Message, db, state: FSMContext) -> None:
    from handlers import settings as settings_handler

    await settings_handler.settings_entry(message, state=state, db=db)


@router.message(lambda m: m.text and "очк" in m.text.lower())
async def points_menu(message: types.Message, db) -> None:
    from handlers import stats

    await stats.stats(message, db)


@router.callback_query(lambda c: c.data and c.data.startswith("food:"))
async def food_callbacks(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    action = callback.data.split(":")[1]
    if action == "plate":
        await guides.plate(callback.message, db)
    elif action == "recipes":
        await guides.recipes_fast(callback.message, db)
    elif action == "meal_plan":
        await guides.meal_plan(callback.message)
    elif action == "shoplist":
        await guides.shoplist(callback.message)
    elif action == "pantry":
        from handlers import pantry

        await pantry.pantry_command(callback.message, db)
    elif action == "expiring_recipes":
        await guides._suggest_from_expiring(callback.message, db)  # noqa: SLF001
    await callback.answer()



@router.callback_query(lambda c: c.data and c.data.startswith("home:"))
async def home_callbacks(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    action = callback.data.split(":")[1]
    if action == "menu":
        await callback.message.answer(
            "Дом: помогу навести порядок по-человечески. Выбирай, что актуально сейчас:",
            reply_markup=home_menu_keyboard(),
        )
    elif action == "now":
        from handlers import home_tasks

        await home_tasks.start_clean_now(callback, db, state)
    elif action == "quickmenu":
        from handlers import home_tasks

        resumed = await home_tasks._resume_any_cleanup(callback.message, state)  # noqa: SLF001
        if not resumed:
            await callback.message.answer(
                "Быстрые сценарии по зонам — выбери и пройдись по шагам:",
                reply_markup=home_tasks._quick_menu_keyboard(),  # noqa: SLF001
            )
    elif action == "week":
        from handlers import home_tasks

        await home_tasks.show_week_plan(callback.message, db)
    elif action == "all":
        from handlers import home_tasks

        await home_tasks.show_all_tasks(callback.message, db)
    elif action == "supplies":
        from handlers import home_supplies

        await home_supplies.supplies_menu(callback.message, db)
    elif action == "smell":
        from handlers import home_tasks

        await home_tasks.send_smell_menu(callback.message)
    elif action == "points":
        from handlers import stats

        await stats.stats(callback.message, db)
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
        from handlers import wellness

        # используем тот же сценарий, что и по слову «щадящий» в чате:
        # одно нажатие включает щадящий день, повторный — предложит /resume.
        await wellness.gentle_button(callback.message, db)
    elif action == "tone":
        await callback.message.answer("Тон общения: выбери стиль в настройках ниже.", reply_markup=main_menu_keyboard())
    elif action == "wellness":
        from handlers import wellness

        await wellness.wellness_settings(callback.message, db)
    elif action == "profile":
        from handlers import settings as settings_handler

        await settings_handler.settings_select(
            callback.replace(data="settings:profile"),
            state=state,
            db=db,
            skip_answer=True,
        )
    elif action == "settings":
        from handlers import settings as settings_handler

        await settings_handler.settings_entry(callback.message, state=state, db=db)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data == "dmenu:plan_tomorrow")
async def dmenu_plan_tomorrow(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    from handlers import day_plan
    # Call the command handler logic directly
    await day_plan.plan_tomorrow(callback.message, state, db)
    await callback.answer()
