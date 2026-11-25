from aiogram import Router, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import datetime
import html

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from services import desk_service, household_service
from utils import texts
from utils.time import is_valid_timezone, parse_hhmm, tzinfo_from_string
from utils.profile import is_student_mode

router = Router()


class Registration(StatesGroup):
    name = State()
    gender = State()
    start_mode = State()
    timezone = State()
    wake_up = State()
    sleep = State()


def _gender_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👩 Она", callback_data="reggender:female"),
                InlineKeyboardButton(text="👨 Он", callback_data="reggender:male"),
            ],
            [InlineKeyboardButton(text="🙂 Не важно", callback_data="reggender:neutral")],
        ]
    )


def _start_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌿 Спокойный (без пингов сна и кармы)", callback_data="regmode:calm")],
            [InlineKeyboardButton(text="🔥 Полный (режим дня, сон и карма)", callback_data="regmode:full")],
        ]
    )


def _tz_preset_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Москва +3", callback_data="regtz:Europe/Moscow"),
                InlineKeyboardButton(text="Калининград +2", callback_data="regtz:Europe/Kaliningrad"),
            ],
            [
                InlineKeyboardButton(text="Самара +4", callback_data="regtz:Europe/Samara"),
                InlineKeyboardButton(text="Екб +5", callback_data="regtz:Asia/Yekaterinburg"),
            ],
            [
                InlineKeyboardButton(text="Омск +6", callback_data="regtz:Asia/Omsk"),
                InlineKeyboardButton(text="Новосиб +7", callback_data="regtz:Asia/Novosibirsk"),
            ],
            [InlineKeyboardButton(text="✍️ Введу свой / время HH:MM", callback_data="regtz:manual")],
        ]
    )


def _wake_preset_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="06:30", callback_data="regwake:06:30"),
                InlineKeyboardButton(text="07:00", callback_data="regwake:07:00"),
                InlineKeyboardButton(text="07:30", callback_data="regwake:07:30"),
            ],
            [
                InlineKeyboardButton(text="08:00", callback_data="regwake:08:00"),
                InlineKeyboardButton(text="09:00", callback_data="regwake:09:00"),
                InlineKeyboardButton(text="10:00", callback_data="regwake:10:00"),
            ],
        ]
    )


def _sleep_preset_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="22:30", callback_data="regsleep:22:30"),
                InlineKeyboardButton(text="23:00", callback_data="regsleep:23:00"),
                InlineKeyboardButton(text="23:30", callback_data="regsleep:23:30"),
            ],
            [
                InlineKeyboardButton(text="00:00", callback_data="regsleep:00:00"),
                InlineKeyboardButton(text="00:30", callback_data="regsleep:00:30"),
                InlineKeyboardButton(text="01:00", callback_data="regsleep:01:00"),
            ],
        ]
    )


async def _handle_reg_cancel(message: types.Message, state: FSMContext) -> bool:
    if (message.text or "").strip().lower() in {"отмена", "назад"}:
        await state.clear()
        await message.answer(
            "Отменила настройку. Если захочешь продолжить — набери /start.",
            reply_markup=main_menu_keyboard(),
        )
        return True
    return False


def _extract_house_invite_code(text: str | None) -> str:
    parts = (text or "").strip().split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("house_"):
        return parts[1][len("house_") :].strip()
    return ""


@router.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext, db) -> None:
    invite_code = _extract_house_invite_code(message.text)
    user = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if user:
        user_d = dict(user)
        safe_name = html.escape(str(user_d.get("name") or ""))
        if invite_code:
            house = await repo.get_household_by_code(db, invite_code)
            if house:
                await repo.set_user_household(db, user_d["id"], int(house["id"]))
                user_d["household_id"] = int(house["id"])
                await household_service.notify_household_partners(
                    getattr(message, "bot", None),
                    db,
                    user_d,
                    f"🏠 <b>{safe_name}</b> присоединился(-ась) к общему дому по приглашению.",
                )
                await message.answer(
                    f"🏠 Подключила тебя к общему дому!\n"
                    "Теперь холодильник, список покупок, общие рецепты, меню и общие счета синхронизированы.",
                    reply_markup=main_menu_keyboard(user_d),
                )
            else:
                await message.answer(
                    "Код приглашения в дом не найден или устарел. Можно ввести свежий в ⚙ Настройки → Общий дом.",
                    reply_markup=main_menu_keyboard(user_d),
                )
            return
        if is_student_mode(user):
            greeting = f"С возвращением, {safe_name}! Продолжим с планом, занятиями или напоминаниями?"
        else:
            greeting = f"С возвращением, {safe_name}! Чем помочь сегодня?"
        await message.answer(
            greeting,
            reply_markup=main_menu_keyboard(user_d),
        )
        return

    await state.clear()
    if invite_code:
        await state.update_data(pending_house_code=invite_code)
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
        await callback.message.answer(
            "Как мне к тебе обращаться? Это нужно, чтобы писать окончания нормально (сделал/сделала).",
            reply_markup=_gender_keyboard(),
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
    await message.answer(
        "Как мне к тебе обращаться? Это нужно, чтобы писать окончания нормально (сделал/сделала).",
        reply_markup=_gender_keyboard(),
    )


@router.callback_query(lambda c: c.data and c.data.startswith("reggender:"))
async def reggender_choice(callback: types.CallbackQuery, state: FSMContext) -> None:
    gender = callback.data.split(":")[1]
    if gender not in {"male", "female", "neutral"}:
        await callback.answer()
        return
    await state.update_data(gender=gender)
    await state.set_state(Registration.start_mode)
    await callback.message.answer(
        "Как начнём знакомство? Потом в любой момент легко переключить в Настройках:\n"
        "• <b>Спокойный</b> — без утренних/вечерних пингов сна и кармы, только то, о чём сам попросишь.\n"
        "• <b>Полный</b> — мягкий режим дня, чек-ины сна и карма.",
        reply_markup=_start_mode_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer("Сохранено")


@router.callback_query(lambda c: c.data and c.data.startswith("regmode:"))
async def regmode_choice(callback: types.CallbackQuery, state: FSMContext) -> None:
    mode = callback.data.split(":", 1)[1]
    if mode not in {"calm", "full"}:
        await callback.answer()
        return
    await state.update_data(start_mode=mode)
    await state.set_state(Registration.timezone)
    await callback.message.answer(
        "Выбери часовой пояс (или просто напиши текущее время <code>HH:MM</code> / пояс вроде <code>UTC+3</code>):",
        reply_markup=_tz_preset_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer("Сохранено")


@router.callback_query(lambda c: c.data and c.data.startswith("regtz:"))
async def regtz_choice(callback: types.CallbackQuery, state: FSMContext) -> None:
    tz_val = callback.data.split(":", 1)[1]
    if tz_val == "manual":
        await state.set_state(Registration.timezone)
        await callback.message.answer(
            "Пришли текущее локальное время (например <code>14:30</code>) или пояс вроде <code>Europe/Moscow</code> / <code>UTC+3</code>.",
            parse_mode="HTML",
        )
        await callback.answer()
        return
    if not is_valid_timezone(tz_val):
        await callback.answer("Не удалось распознать пояс.", show_alert=True)
        return
    await state.update_data(timezone=tz_val)
    await state.set_state(Registration.wake_up)
    await callback.message.answer(
        f"Часовой пояс: <b>{html.escape(tz_val)}</b>. Во сколько обычно просыпаешься? Выбери ниже или напиши время <code>HH:MM</code>.",
        reply_markup=_wake_preset_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer("Сохранено")


@router.message(Registration.timezone, lambda m: bool(m.text and not m.text.startswith("/")))
async def reg_timezone(message: types.Message, state: FSMContext) -> None:
    if await _handle_reg_cancel(message, state):
        return
    tz_raw = message.text.strip()
    local_t = parse_hhmm(tz_raw)
    if local_t is not None:
        now_utc = datetime.datetime.utcnow()
        utc_min = now_utc.hour * 60 + now_utc.minute
        loc_min = local_t.hour * 60 + local_t.minute
        diff_min = loc_min - utc_min
        if diff_min <= -12 * 60:
            diff_min += 24 * 60
        elif diff_min > 14 * 60:
            diff_min -= 24 * 60
        offset_h = int(round(diff_min / 60.0))
        tz_raw = "UTC" if offset_h == 0 else (f"UTC+{offset_h}" if offset_h > 0 else f"UTC{offset_h}")
    if not is_valid_timezone(tz_raw):
        await message.answer(
            texts.error("не распознала часовой пояс. Пример: Europe/Moscow, UTC+3 или текущее время HH:MM."),
        )
        return

    await state.update_data(timezone=tz_raw)
    await state.set_state(Registration.wake_up)
    await message.answer(
        f"Часовой пояс: <b>{html.escape(tz_raw)}</b>. Во сколько обычно просыпаешься? Выбери ниже или напиши <code>HH:MM</code>.",
        reply_markup=_wake_preset_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("regwake:"))
async def regwake_choice(callback: types.CallbackQuery, state: FSMContext) -> None:
    hhmm = callback.data.split(":", 1)[1].strip()
    if not parse_hhmm(hhmm):
        await callback.answer()
        return
    await state.update_data(wake_up=hhmm)
    await state.set_state(Registration.sleep)
    await callback.message.answer(
        "Во сколько обычно ложишься спать? Выбери ниже или напиши <code>HH:MM</code>.",
        reply_markup=_sleep_preset_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer("Сохранено")


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
    await message.answer(
        "Во сколько обычно ложишься спать? Выбери ниже или напиши <code>HH:MM</code>.",
        reply_markup=_sleep_preset_keyboard(),
        parse_mode="HTML",
    )


async def _finish_registration(
    target_message: types.Message,
    state: FSMContext,
    db,
    tg_user,
    sleep_hhmm: str,
    bot=None,
) -> None:
    data = await state.get_data()
    name = str(data.get("name") or getattr(tg_user, "first_name", None) or getattr(tg_user, "full_name", None) or "друг")
    timezone = str(data.get("timezone") or "Europe/Moscow")
    wake_up = str(data.get("wake_up") or "08:00")
    gender = str(data.get("gender") or "neutral")
    start_mode = str(data.get("start_mode") or "full")

    user_id = await repo.create_user(
        conn=db,
        telegram_id=tg_user.id,
        name=name,
        timezone=timezone,
        wake_up_time=wake_up,
        sleep_time=sleep_hhmm,
        gender=gender,
    )
    await repo.ensure_user_routines(db, user_id)
    if start_mode == "calm":
        await repo.update_user_pulse_flags(db, user_id, pulse_enabled=False)

    new_user = dict(await repo.get_user(db, user_id))
    pending_code = str(data.get("pending_house_code") or "").strip()
    joined_house_note = ""
    if pending_code:
        house = await repo.get_household_by_code(db, pending_code)
        if house:
            await repo.set_user_household(db, user_id, int(house["id"]))
            new_user = dict(await repo.get_user(db, user_id))
            await household_service.notify_household_partners(
                bot or getattr(target_message, "bot", None),
                db,
                new_user,
                f"🏠 <b>{html.escape(name)}</b> присоединился(-ась) к общему дому по приглашению.",
            )
            joined_house_note = "\n\n🏠 Кстати, подключила тебя к общему дому по приглашению!"
    await state.clear()

    mode_note = (
        "🌿 Включён спокойный режим (без пингов сна и кармы)."
        if start_mode == "calm"
        else "🔥 Включён полный режим (режим дня, сон и карма)."
    )
    await target_message.answer(
        f"Принято, {html.escape(name)}! {mode_note}"
        f"{joined_house_note}\n\n"
        "Кнопки меню внизу. Поменять режим, время или удалить данные можно в любой момент: <b>⚙ Ещё → ⚙ Настройки</b>.",
        reply_markup=main_menu_keyboard(new_user),
        parse_mode="HTML",
    )

    wow_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🍳 Что приготовить?", callback_data="kitchen:quick_pick"),
                InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
            ],
            [
                InlineKeyboardButton(text="📅 План на сегодня", callback_data="today:menu"),
                InlineKeyboardButton(text="⚙ Настройки", callback_data="settings:main"),
            ],
        ]
    )
    await target_message.answer(
        "✨ <b>Что можно попробовать прямо сейчас:</b>\n"
        "1) Надиктуй голосом или напиши в чат одной фразой:\n"
        "<i>«купить молоко, завтра в 15 позвонить маме, потратил 650 на кофе»</i>\n"
        "2) Скинь <b>фото тарелки</b> — посчитаю КБЖУ.\n"
        "3) Нажми «🍳 Что приготовить?» — подберу идею или меню.",
        reply_markup=wow_kb,
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("regsleep:"))
async def regsleep_choice(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    hhmm = callback.data.split(":", 1)[1].strip()
    if not parse_hhmm(hhmm):
        await callback.answer()
        return
    await _finish_registration(
        callback.message,
        state,
        db,
        callback.from_user,
        hhmm,
        bot=getattr(callback, "bot", None),
    )
    await callback.answer("Готово!")


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
    await _finish_registration(
        message,
        state,
        db,
        message.from_user,
        message.text.strip(),
        bot=getattr(message, "bot", None),
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
    has_desk = bool(user and await desk_service.get_link_for_user(db, int(user["id"])))
    desk_line = "• 📚 /desk — занятия и задания из Fodder's Desk\n" if has_desk else ""
    await message.answer(
        "В обычной жизни достаточно кнопок меню внизу — команды учить не нужно.\n\n"
        "• 📅 Сегодня — план дня, рутины, прогресс\n"
        "• 🍽 Еда и покупки — рецепты, КБЖУ, холодильник и список покупок\n"
        "• 🌙 Сон и карма — мягкий режим дня и чек-ины\n"
        "• 🗓 Расписание и 📓 Дневник — события, мысли и дела\n"
        "• ⚙ Ещё — 🏠 Наш дом, 🧹 Дом, 💰 Деньги, 🏋️ Спорт, ☕ Фокус и ⚙ Настройки\n"
        f"{desk_line}\n"
        "✨ <b>Попробуй просто написать или надиктовать голосом в чат:</b>\n"
        "• «купить молоко, завтра в 15 позвонить маме, потратил 650 на кофе»\n"
        "• скинуть фото еды — разберу по КБЖУ\n"
        "• задать вопрос по питанию, сну или плану\n\n"
        "Полезные команды:\n"
        "/today • /diary • /pantry • /house • /reminders • /sleep • /settings • /feedback\n"
        "/help_now — срочная поддержка, когда мало сил",
        reply_markup=main_menu_keyboard(user),
        parse_mode="HTML",
    )
