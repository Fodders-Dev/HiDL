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
    more_menu_keyboard,
    movement_menu_keyboard,
    settings_menu_keyboard,
)
from utils.menu_match import match_menu_label
from utils.time import format_time_local, local_date_str
from utils.tone import tone_message, tone_short_ack
from utils.user import ensure_user
from utils.today import render_today

router = Router()

@router.callback_query(lambda c: c.data and c.data == "main:menu")
async def main_menu_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Главное меню внизу 👇")


@router.message(Command("menu"))
@router.message(lambda m: match_menu_label(m.text, {"меню", "главное меню"}))
async def show_menu(message: types.Message, state: FSMContext, db) -> None:
    await state.clear()
    row = await repo.get_user_by_telegram_id(db, message.from_user.id)
    user = dict(row) if row else None
    await message.answer("Главное меню:", reply_markup=main_menu_keyboard(user))


@router.message(Command("today"))
@router.message(lambda m: match_menu_label(m.text, {"сегодня"}))
async def cmd_today(message: types.Message, db, state: FSMContext) -> None:
    await state.clear()
    user_row = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    user = dict(user_row)
    text, kb = await render_today(db, user)
    await message.answer(text, reply_markup=kb or main_menu_keyboard(user))


@router.message(Command("settings"))
async def settings_info(message: types.Message, state: FSMContext, db) -> None:
    from handlers import settings as settings_handler

    await settings_handler.settings_entry(message, state=state, db=db)


@router.message(lambda m: match_menu_label(m.text, {"еда", "еда и покупки"}))
async def food_menu(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Еда — это топливо и радость. Я помогу не ломать голову над вопросом «что приготовить» и следить за запасами.",
        reply_markup=food_menu_keyboard(),
    )


@router.message(lambda m: match_menu_label(m.text, {"ещё", "еще"}))
async def more_menu(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Дополнительные разделы (дом, деньги, спорт, фокус, настройки и заморозка ЧП):",
        reply_markup=more_menu_keyboard(),
    )


@router.message(lambda m: match_menu_label(m.text, {"покупки", "список покупок"}))
async def shopping_menu(message: types.Message, db, state: FSMContext) -> None:
    from handlers import kitchen

    await state.clear()
    await kitchen.send_shoplist(message, db)



@router.message(Command("pantry"))
async def pantry_command(message: types.Message, db, state: FSMContext) -> None:
    from handlers import kitchen

    await state.clear()
    await kitchen.send_fridge(message, db)


@router.message(Command("home"))
@router.message(lambda m: match_menu_label(m.text, {"дом"}))
async def home_menu(
    message: types.Message,
    db,
    state: FSMContext,
    tg_user_id: int | None = None,
    tg_full_name: str | None = None,
) -> None:
    from utils.user import ensure_user
    from utils.time import local_date_str
    import datetime

    await state.clear()
    uid = tg_user_id if tg_user_id is not None else message.from_user.id
    uname = tg_full_name if tg_full_name is not None else message.from_user.full_name
    user = await ensure_user(db, uid, uname)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    # гарантируем наличие базовой регулярки
    await repo.ensure_regular_tasks(db, user["id"], today)
    await message.answer(
        "Дом: помогу навести порядок по-человечески. Выбирай, что актуально сейчас:",
        reply_markup=home_menu_keyboard(),
    )


@router.message(Command("movement"))
@router.message(lambda m: match_menu_label(m.text, {"спорт", "движение"}))
async def move_menu(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Спорт: чуть подвигаться, чтобы стало легче в голове и теле.", reply_markup=movement_menu_keyboard())


@router.message(lambda m: match_menu_label(m.text, {"дневник"}))
async def diary_menu(message: types.Message, state: FSMContext, db) -> None:
    from handlers import inbox

    await inbox.cmd_diary(message.model_copy(update={"text": "/diary"}), state, db)


@router.message(lambda m: match_menu_label(m.text, {"поговорить", "поболтать"}))
async def talk_menu(message: types.Message, state: FSMContext) -> None:
    from handlers import talk

    await state.clear()
    await talk.talk_placeholder(message)


@router.callback_query(lambda c: c.data and c.data == "today:menu")
async def today_menu(callback: types.CallbackQuery, db) -> None:
    user_row = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    user = dict(user_row)
    from utils.today import render_today
    text, kb = await render_today(db, user)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.message(lambda m: match_menu_label(m.text, {"настройки"}))
async def settings_menu(message: types.Message, db, state: FSMContext) -> None:
    from handlers import settings as settings_handler

    await state.clear()
    await settings_handler.settings_entry(message, state=state, db=db)


@router.message(lambda m: match_menu_label(m.text, {"очки", "мои очки"}))
async def points_menu(message: types.Message, db, state: FSMContext) -> None:
    from handlers import stats

    await state.clear()
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
        await state.clear()
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

        await home_tasks.show_week_plan(
            callback.message,
            db,
            tg_user_id=callback.from_user.id,
            tg_full_name=callback.from_user.full_name,
        )
    elif action == "all":
        from handlers import home_tasks

        await home_tasks.show_all_tasks(
            callback.message,
            db,
            tg_user_id=callback.from_user.id,
            tg_full_name=callback.from_user.full_name,
        )
    elif action == "supplies":
        from handlers import home_supplies

        await home_supplies.supplies_menu(
            callback.message,
            db,
            tg_user_id=callback.from_user.id,
            tg_full_name=callback.from_user.full_name,
        )
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
    elif action == "home10":
        from handlers import movement
        await movement.move_home10(callback)
    elif action == "home20":
        from handlers import movement
        await movement.move_home20(callback)
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
        user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
        await callback.message.answer("Тон общения: выбери стиль в настройках ниже.", reply_markup=main_menu_keyboard(user))
    elif action == "wellness":
        from handlers import wellness

        await wellness.wellness_settings(callback.message, db)
    elif action == "profile":
        from handlers import settings as settings_handler

        await settings_handler.settings_select(
            callback.model_copy(update={"data": "settings:profile"}),
            state=state,
            db=db,
            skip_answer=True,
        )
    elif action == "settings":
        from handlers import settings as settings_handler

        await settings_handler.settings_select(
            callback.model_copy(update={"data": "settings:main"}),
            state=state,
            db=db,
            skip_answer=True,
        )
        return
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("more:"))
async def more_callbacks(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    action = callback.data.split(":")[1]
    if action == "home":
        await home_menu(
            callback.message,
            db,
            state,
            tg_user_id=callback.from_user.id,
            tg_full_name=callback.from_user.full_name,
        )
    elif action == "money":
        await state.clear()
        await callback.message.answer(
            "Деньги: давай без боли и осуждения. Выбирай:",
            reply_markup=money_menu_keyboard(),
        )
    elif action == "sport":
        await state.clear()
        await callback.message.answer(
            "Спорт: чуть подвигаться, чтобы стало легче в голове и теле.",
            reply_markup=movement_menu_keyboard(),
        )
    elif action == "focus":
        from handlers import focus_cafe

        await focus_cafe.cafe_start(
            callback.message,
            state=state,
            db=db,
            tg_user_id=callback.from_user.id,
            tg_full_name=callback.from_user.full_name,
        )
    elif action == "menu":
        await state.clear()
        await callback.message.answer(
            "Дополнительные разделы:",
            reply_markup=more_menu_keyboard(),
        )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data == "dmenu:plan_tomorrow")
async def dmenu_plan_tomorrow(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    from handlers import day_plan
    # Call the command handler logic directly
    await day_plan.plan_tomorrow(callback.message, state, db)
    await callback.answer()
