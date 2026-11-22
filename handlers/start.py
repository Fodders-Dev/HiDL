from aiogram import Router, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import html

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils import texts
from utils.time import is_valid_timezone, parse_hhmm, tzinfo_from_string
from utils.profile import is_student_mode

router = Router()


class Registration(StatesGroup):
    name = State()
    gender = State()
    timezone = State()
    wake_up = State()
    sleep = State()


async def _handle_reg_cancel(message: types.Message, state: FSMContext) -> bool:
    if (message.text or "").strip().lower() in {"отмена", "назад"}:
        await state.clear()
        await message.answer(
            "Отменила настройку. Если захочешь продолжить — набери /start.",
            reply_markup=main_menu_keyboard(),
        )
        return True
    return False


@router.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext, db) -> None:
    user = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if user:
        safe_name = html.escape(str(user["name"] or ""))
        if is_student_mode(user):
            greeting = f"С возвращением, {safe_name}! Продолжим с планом, занятиями или напоминаниями?"
        else:
            greeting = f"С возвращением, {safe_name}! Чем помочь сегодня?"
        await message.answer(
            greeting,
            reply_markup=main_menu_keyboard(dict(user)),
        )
        return

    await state.clear()
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
        await state.set_state(Registration.name)
        await callback.message.answer("Как к тебе обращаться?")
    await callback.answer()


@router.message(Registration.name, lambda m: bool(m.text and not m.text.startswith("/")))
async def reg_name(message: types.Message, state: FSMContext) -> None:
    if await _handle_reg_cancel(message, state):
        return
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


@router.message(Registration.timezone, lambda m: bool(m.text and not m.text.startswith("/")))
async def reg_timezone(message: types.Message, state: FSMContext) -> None:
    if await _handle_reg_cancel(message, state):
        return
    tz_raw = message.text.strip()
    if not is_valid_timezone(tz_raw):
        await message.answer(
            texts.error("не распознала часовой пояс. Пример: Europe/Moscow или UTC+3."),
        )
        return

    await state.update_data(timezone=tz_raw)
    await state.set_state(Registration.wake_up)
    await message.answer("Во сколько просыпаешься обычно? Формат HH:MM, например 07:30. После этого настроим план по дому (5 вопросов).")


@router.message(Registration.wake_up, lambda m: bool(m.text and not m.text.startswith("/")))
async def reg_wake_up(message: types.Message, state: FSMContext) -> None:
    if await _handle_reg_cancel(message, state):
        return
    time_value = parse_hhmm(message.text.strip())
    if not time_value:
        await message.answer(
            texts.error("не распознала время. Формат HH:MM, например 07:30."),
        )
        return
    await state.update_data(wake_up=message.text.strip())
    await state.set_state(Registration.sleep)
    await message.answer("Во сколько обычно ложишься спать? Формат HH:MM, например 23:30.")


@router.message(Registration.sleep, lambda m: bool(m.text and not m.text.startswith("/")))
async def reg_sleep(message: types.Message, state: FSMContext, db) -> None:
    if await _handle_reg_cancel(message, state):
        return
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
        "Расписание, Дневник, Кафе фокуса, Настройки.\n\n"
        "Если захочешь, мы настроим план по дому из раздела 🧹 Дом — там я задам пару вопросов и соберу список регулярных дел.\n\n"
        "Если что-то захочешь поменять — заходи в ⚙ Настройки. "
        "Если запутаешься — всегда можно набрать /help.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(message: types.Message, db) -> None:
    user_row = await repo.get_user_by_telegram_id(db, message.from_user.id)
    user = dict(user_row) if user_row else None
    if is_student_mode(user):
        await message.answer(
            "📅 Сегодня — план дня и напоминания.\n📚 Учёба / Desk — занятия и задания.\n"
            "☕ Кафе фокуса — короткий старт работы.\n😴 Сон и пауза — бережный режим.\n"
            "Любой текст или голос можно отправить сюда — я помогу разложить его по плану.",
            reply_markup=main_menu_keyboard(user),
        )
        return
    await message.answer(
        "В обычной жизни достаточно кнопок меню внизу — команды учить не нужно.\n\n"
        "• 📅 Сегодня — план дня, рутины, прогресс\n"
        "• 🍽 Еда и 🛒 Покупки — рецепты, холодильник и список покупок\n"
        "• 🧹 Дом и 💰 Деньги — уборка по шагам, траты и счета\n"
        "• 🗓 Расписание и ☕ Кафе фокуса — слоты дня и таймер фокуса\n"
        "• 📓 Дневник (или просто текст/голос в чат) — разложу дела, мысли и напоминания\n\n"
        "• 📚 /desk — занятия и задания из Fodder's Desk\n\n"
        "Полезные команды:\n"
        "/today — сегодня • /diary — дневник • /pantry — холодильник\n"
        "/reminders — напоминания • /sleep — сон • /settings — настройки\n"
        "/help_now — срочная поддержка, когда мало сил",
        reply_markup=main_menu_keyboard(user),
    )
