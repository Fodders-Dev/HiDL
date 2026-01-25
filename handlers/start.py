from aiogram import Router, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils import texts
from utils.time import is_valid_timezone, parse_hhmm, tzinfo_from_string

router = Router()


class Registration(StatesGroup):
    name = State()
    gender = State()
    timezone = State()
    wake_up = State()
    sleep = State()


@router.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext, db) -> None:
    user = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if user:
        await message.answer(
            f"С возвращением, {user['name']}! Чем помочь сегодня?",
            reply_markup=main_menu_keyboard(),
        )
        return

    await state.clear()
    await state.set_state(Registration.name)
    first_name = message.from_user.first_name or "друг"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"Да, {first_name}", callback_data=f"regname:{first_name}"),
                InlineKeyboardButton(text="Другое имя", callback_data="regname:other"),
            ]
        ]
    )
    await message.answer(
        "Привет 👋\nЯ HiDL — домашняя помощница. Помогу с бытом, едой, деньгами и мягкими напоминаниями.\n\n"
        "Мы будем больше работать через кнопки, команды помнить не нужно. "
        "Все настройки — имя, часовой пояс, подъём/отбой — всегда можно поменять позже в ⚙ Настройки.\n\n"
        f"Можно буду звать тебя {first_name}?",
        reply_markup=kb,
    )


@router.callback_query(lambda c: c.data and c.data.startswith("regname:"))
async def regname_choice(callback: types.CallbackQuery, state: FSMContext) -> None:
    choice = callback.data.split(":")[1]
    if choice != "other":
        await state.update_data(name=choice)
        await state.set_state(Registration.gender)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="👩 Она", callback_data="reggender:female"),
                    InlineKeyboardButton(text="👨 Он", callback_data="reggender:male"),
                ],
                [InlineKeyboardButton(text="🙂 Не важно", callback_data="reggender:neutral")],
            ]
        )
        await callback.message.answer(
            "Как мне к тебе обращаться? Это нужно, чтобы писать окончания нормально (сделал/сделала).",
            reply_markup=kb,
        )
    else:
        await callback.message.answer("Как к тебе обращаться?")
    await callback.answer()


@router.message(Registration.name)
async def reg_name(message: types.Message, state: FSMContext) -> None:
    await state.update_data(name=(message.text or "").strip() or message.from_user.first_name or "друг")
    await state.set_state(Registration.gender)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👩 Она", callback_data="reggender:female"),
                InlineKeyboardButton(text="👨 Он", callback_data="reggender:male"),
            ],
            [InlineKeyboardButton(text="🙂 Не важно", callback_data="reggender:neutral")],
        ]
    )
    await message.answer(
        "Как мне к тебе обращаться? Это нужно, чтобы писать окончания нормально (сделал/сделала).",
        reply_markup=kb,
    )


@router.callback_query(lambda c: c.data and c.data.startswith("reggender:"))
async def reggender_choice(callback: types.CallbackQuery, state: FSMContext) -> None:
    gender = callback.data.split(":")[1]
    if gender not in {"male", "female", "neutral"}:
        await callback.answer()
        return
    await state.update_data(gender=gender)
    await state.set_state(Registration.timezone)
    await callback.message.answer(
        "В каком ты часовом поясе? Можно прислать текущее время (HH:MM) - я сама посчитаю смещение. "
        "Или введи явно: Europe/Moscow, UTC+3. Потом можно поменять в ⚙ Настройки."
    )
    await callback.answer("Сохранено")


@router.message(Registration.timezone)
async def reg_timezone(message: types.Message, state: FSMContext) -> None:
    tz_raw = message.text.strip()
    if not is_valid_timezone(tz_raw):
        await message.answer(
            texts.error("не распознала часовой пояс. Пример: Europe/Moscow или UTC+3."),
        )
        return

    await state.update_data(timezone=tz_raw)
    await state.set_state(Registration.wake_up)
    await message.answer("Во сколько просыпаешься обычно? Формат HH:MM, например 07:30. После этого настроим план по дому (5 вопросов).")


@router.message(Registration.wake_up)
async def reg_wake_up(message: types.Message, state: FSMContext) -> None:
    time_value = parse_hhmm(message.text.strip())
    if not time_value:
        await message.answer(
            texts.error("не распознала время. Формат HH:MM, например 07:30."),
        )
        return
    await state.update_data(wake_up=message.text.strip())
    await state.set_state(Registration.sleep)
    await message.answer("Во сколько обычно ложишься спать? Формат HH:MM, например 23:30.")


@router.message(Registration.sleep)
async def reg_sleep(message: types.Message, state: FSMContext, db) -> None:
    time_value = parse_hhmm(message.text.strip())
    if not time_value:
        await message.answer(
            texts.error("не распознала время. Формат HH:MM, например 23:30."),
        )
        return

    data = await state.get_data()
    user_id = await repo.create_user(
        conn=db,
        telegram_id=message.from_user.id,
        name=data["name"],
        timezone=data["timezone"],
        wake_up_time=data["wake_up"],
        sleep_time=message.text.strip(),
        gender=data.get("gender", "neutral"),
    )
    await repo.ensure_user_routines(db, user_id)
    # Аудит запускаем по желанию через меню Дом -> План
    await state.clear()
    await message.answer(
        f"Принято, {data['name']}! Добавила базовые настройки.\n\n"
        "Главное меню внизу: Сегодня, Еда, Дом, Деньги, Спорт, Покупки,\n"
        "Расписание, Поговорить, Кафе фокуса, Настройки.\n\n"
        "Если захочешь, мы настроим план по дому из раздела 🧹 Дом — там я задам пару вопросов и соберу список регулярных дел.\n\n"
        "Если что-то захочешь поменять — заходи в ⚙ Настройки. "
        "Если запутаешься — всегда можно набрать /help.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(message: types.Message) -> None:
    await message.answer(
        "Команды ниже — для тех случаев, когда хочется «по‑старому».\n"
        "В обычной жизни достаточно нажимать большие кнопки внизу.\n\n"
        "Главное:\n"
        "📅 Сегодня — твои дела и кнопки отметок\n"
        "🍽 Еда — тарелка дня, рецепты, меню, список покупок\n"
        "💰 Деньги — траты, отчёт, лимиты (всё через кнопки)\n"
        "🧹 Дом — уборка, зоны, стирка/запах, план по дому\n"
        "🏋️ Спорт — прогулки/фокус, позже добавим тренировки/вес\n"
        "☕ Кафе фокуса — короткие фокус‑сессии с чек‑инами\n"
        "⚙ Настройки — тон, вода/еда, фокус, щадящий режим, профиль, время рутин\n"
        "🗓 Расписание — день по блокам + подсказки.\n\n"
        "Продвинутые команды:\n"
        "/today — показать план\n"
        "/schedule — расписание\n"
        "/reminders — свои напоминания\n"
        "/stats — статистика\n"
        "/budget_set /budget_cat /spent /spent_week — финансы\n"
        "/bills — счета\n"
        "/cafe — кафе фокуса\n"
        "/gentle /resume — щадящий режим\n"
        "/tone — тон\n"
        "/wellness — вода/еда/фокус\n"
        "/donate — поддержать\n"
        "/home_plan /home_audit — дом\n"
        "/meal_plan /plate /shoplist — еда\n"
        "/talk — поболтать",
        reply_markup=main_menu_keyboard(),
    )
