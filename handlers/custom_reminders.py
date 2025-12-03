import asyncio
import datetime

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.rows import row_to_dict
from utils.time import parse_hhmm
from utils.tone import tone_short_ack
from utils.time import local_date_str
from utils.nlp import match_simple_intent, parse_when
from utils.nl_parser import parse_command
from utils.texts import register_text, ack
from utils.sender import safe_edit

router = Router()


class CustomReminderState(StatesGroup):
    title = State()
    when = State()
    time = State()
    frequency = State()
    one_time = State()
    weekday = State()
    edit_choice = State()
    edit_time = State()
    edit_freq = State()
    edit_wd = State()


async def _ensure_user(message: types.Message, db, tg_user_id: int, tg_full_name: str):
    """Вернуть пользователя, при отсутствии — создать с дефолтами, чтобы не рвать диалог."""
    user = await repo.get_user_by_telegram_id(db, tg_user_id)
    if user:
        return dict(user)
    name = tg_full_name or "Друг"
    tz = "UTC"
    user_id = await repo.create_user(db, tg_user_id, name, tz, "08:00", "23:00")
    await repo.ensure_user_routines(db, user_id)
    created = await repo.get_user(db, user_id)
    return dict(created) if created else {"id": user_id, "name": name, "timezone": tz}


def list_keyboard(reminders, with_add: bool = True) -> InlineKeyboardMarkup:
    buttons = []
    if with_add:
        buttons.append([InlineKeyboardButton(text="➕ Добавить", callback_data="rem:add")])
    for r in reminders:
        row = row_to_dict(r)
        tw = row.get("target_weekday")
        wd = ""
        if tw is not None:
            try:
                wd = f", {['пн','вт','ср','чт','пт','сб','вс'][int(tw)]}"
            except Exception:
                wd = ""
        freq = row.get("frequency_days", 1)
        freq_label = "один раз" if freq and freq >= 9999 else f"каждые {freq} д"
        buttons.append(
            [
                InlineKeyboardButton(
                    text="✏️", callback_data=f"customedit:{row['id']}"
                ),
                InlineKeyboardButton(
                    text=f"🗑 {row.get('title','Напоминание')} ({row.get('reminder_time','--:--')}, {freq_label}{wd})",
                    callback_data=f"customdel:{row['id']}",
                )
            ]
        )
    if not buttons:
        buttons = [[InlineKeyboardButton(text="➕ Добавить", callback_data="rem:add")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("add_reminder"))
async def add_reminder_start(message: types.Message, state: FSMContext, db) -> None:
    user = await _ensure_user(message, db, message.from_user.id, message.from_user.full_name)
    await state.update_data(user_id=user["id"], user_tz=user["timezone"])
    await state.set_state(CustomReminderState.title)
    pause_note = ""
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    if user["pause_until"] and user["pause_until"] >= today:
        pause_note = (
            "\n\n⚠️ Сейчас включён щадящий режим/пауза, напоминания не придут. "
            "Сними паузу в настройках или через кнопку «Щадящий режим», если хочешь получить это напоминание."
        )
    await message.answer("О чём напомнить? Коротко, без лишнего." + pause_note, reply_markup=main_menu_keyboard())


@router.message(CustomReminderState.title)
async def add_reminder_title(message: types.Message, state: FSMContext) -> None:
    await state.update_data(title=message.text.strip())
    await state.set_state(CustomReminderState.when)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Через час", callback_data="rem:when:plus1"),
                InlineKeyboardButton(text="Сегодня", callback_data="rem:when:today"),
                InlineKeyboardButton(text="Завтра", callback_data="rem:when:tomorrow"),
            ],
            [
                InlineKeyboardButton(text="Раз в неделю", callback_data="rem:when:weekly"),
                InlineKeyboardButton(text="Своё время", callback_data="rem:when:custom"),
            ],
        ]
    )
    await message.answer("Когда напомнить?", reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("rem:when:"))
async def reminder_when(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    user = await repo.get_user_by_telegram_id(db, callback.from_user.id)
    if not user:
        await callback.answer(register_text(), show_alert=True)
        return
    _, _, choice = callback.data.split(":")
    if choice == "plus1":
        # set time to current +1h local
        from utils.time import format_time_local
        now_utc = datetime.datetime.utcnow()
        hhmm = format_time_local(now_utc + datetime.timedelta(hours=1), user["timezone"])
        data = await state.get_data()
        await repo.create_custom_reminder(
            db,
            user_id=user["id"],
            title=data["title"],
            reminder_time=hhmm,
            frequency_days=1,
        )
        await state.clear()
        await callback.message.answer(
            ack(f"{data['title']} в {hhmm} (ежедневно)."),
            reply_markup=main_menu_keyboard(),
        )
        await callback.answer()
        return
    if choice == "weekly":
        await state.update_data(when_choice="weekly")
        await state.set_state(CustomReminderState.time)
        await callback.message.answer("Введи время (HH:MM). Потом выберем день недели.", reply_markup=main_menu_keyboard())
        await callback.answer()
        return
    if choice in ("today", "tomorrow", "custom"):
        await state.update_data(when_choice=choice)
        await state.set_state(CustomReminderState.time)
        await callback.message.answer("Введи время в формате HH:MM (например 09:00). Напоминание будет повторяться.")
        await callback.answer()


@router.message(CustomReminderState.time)
async def add_reminder_time(message: types.Message, state: FSMContext, db) -> None:
    text = message.text.strip()
    abs_time, plus_hours, weekday = parse_when(text)
    if abs_time:
        hhmm_norm = abs_time
    elif plus_hours is not None:
        from utils.time import format_time_local
        now_utc = datetime.datetime.utcnow()
        hhmm_norm = format_time_local(now_utc + datetime.timedelta(hours=plus_hours), (await state.get_data()).get("user_tz","UTC"))
    else:
        hhmm_norm = text
        if not parse_hhmm(hhmm_norm):
            from utils import texts

            await message.answer(
                texts.error("не распознала время. Формат HH:MM или «через 2 часа»."),
            )
            return
        parts = hhmm_norm.split(":")
        hhmm_norm = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    data = await state.get_data()
    user = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if not user:
        await message.answer(register_text())
        await state.clear()
        return
    when_choice = data.get("when_choice", "today")
    reminder_time = hhmm_norm
    # Смещение старта для завтра: пометим последнюю отправку как сегодня, чтобы первый раз пришло завтра.
    last_sent = None
    if when_choice == "tomorrow":
        from utils.time import local_date_str

        local_date = local_date_str(datetime.datetime.utcnow(), user["timezone"])
        last_sent = local_date
    await state.update_data(reminder_time=reminder_time, last_sent=last_sent)
    if when_choice == "weekly":
        await state.update_data(pending_freq=7)
        await state.set_state(CustomReminderState.weekday)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Пн", callback_data="rem:wd:0"),
                    InlineKeyboardButton(text="Вт", callback_data="rem:wd:1"),
                    InlineKeyboardButton(text="Ср", callback_data="rem:wd:2"),
                    InlineKeyboardButton(text="Чт", callback_data="rem:wd:3"),
                ],
                [
                    InlineKeyboardButton(text="Пт", callback_data="rem:wd:4"),
                    InlineKeyboardButton(text="Сб", callback_data="rem:wd:5"),
                    InlineKeyboardButton(text="Вс", callback_data="rem:wd:6"),
                ],
            ]
        )
        await state.set_state(CustomReminderState.weekday)
        await message.answer("Выбери день недели:", reply_markup=kb)
        return
    # спросим про периодичность
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Каждый день", callback_data="rem:freq:1"),
                InlineKeyboardButton(text="Через день", callback_data="rem:freq:2"),
            ],
            [
                InlineKeyboardButton(text="Раз в неделю", callback_data="rem:freq:7"),
                InlineKeyboardButton(text="Только один раз", callback_data="rem:freq:once"),
                InlineKeyboardButton(text="Своя периодичность", callback_data="rem:freq:custom"),
            ],
        ]
    )
    await state.set_state(CustomReminderState.frequency)
    await message.answer("Как часто повторять?", reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("rem:freq:"))
async def reminder_freq_choice(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    choice = callback.data.split(":")[2]
    if choice == "custom":
        await callback.message.answer("Введи через сколько дней повторять (целое число, минимум 1).")
        await callback.answer()
        return
    if choice == "once":
        await state.update_data(pending_freq=9999, one_time=True)
        await state.set_state(CustomReminderState.weekday)
        await callback.message.answer("Запишу как одноразовое. Оставить без дня недели или выбрать день?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Без дня", callback_data="rem:wd:none")]]))
        await callback.answer()
        return
    if choice == "7":
        await state.update_data(pending_freq=7)
        await state.set_state(CustomReminderState.weekday)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Пн", callback_data="rem:wd:0"),
                    InlineKeyboardButton(text="Вт", callback_data="rem:wd:1"),
                    InlineKeyboardButton(text="Ср", callback_data="rem:wd:2"),
                    InlineKeyboardButton(text="Чт", callback_data="rem:wd:3"),
                ],
                [
                    InlineKeyboardButton(text="Пт", callback_data="rem:wd:4"),
                    InlineKeyboardButton(text="Сб", callback_data="rem:wd:5"),
                    InlineKeyboardButton(text="Вс", callback_data="rem:wd:6"),
                ],
            ]
        )
        await callback.message.answer("Выбери день недели:", reply_markup=kb)
        await callback.answer()
        return
    await _save_reminder_with_frequency(
        callback.message,
        state,
        db,
        int(choice),
        tg_user_id=callback.from_user.id,
        tg_full_name=callback.from_user.full_name,
    )
    await callback.answer()


@router.message(CustomReminderState.frequency)
async def add_reminder_frequency(message: types.Message, state: FSMContext, db) -> None:
    try:
        freq = int(message.text.strip())
        if freq < 1:
            raise ValueError
    except Exception:
        await message.answer(
            "Нужно целое число дней (минимум 1).\n"
            "Например: 1 — каждый день, 7 — раз в неделю, 30 — раз в месяц.",
        )
        return
    await _save_reminder_with_frequency(
        message,
        state,
        db,
        freq,
        tg_user_id=message.from_user.id,
        tg_full_name=message.from_user.full_name,
    )


@router.callback_query(lambda c: c.data and c.data.startswith("rem:wd:"))
async def reminder_weekday(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    data = await state.get_data()
    freq = data.get("pending_freq", 7)
    wd_raw = callback.data.split(":")[2]
    weekday = None if wd_raw == "none" else int(wd_raw)
    await _save_reminder_with_frequency(
        callback.message,
        state,
        db,
        freq,
        tg_user_id=callback.from_user.id,
        tg_full_name=callback.from_user.full_name,
        target_weekday=weekday,
    )
    await callback.answer()


async def _save_reminder_with_frequency(
    message: types.Message,
    state: FSMContext,
    db,
    freq: int,
    tg_user_id: int,
    tg_full_name: str,
    target_weekday: int | None = None,
):
    data = await state.get_data()
    user_id = data.get("user_id")
    user_tz = data.get("user_tz")
    user = None
    if user_id is not None:
        urow = await repo.get_user(db, user_id)
        user = row_to_dict(urow) if urow else None
    if not user:
        # попытка найти по телеграм-id (важно для callback, где message.from_user может быть бот)
        urow = await repo.get_user_by_telegram_id(db, tg_user_id)
        user = row_to_dict(urow) if urow else None
    if not user:
        # финальный fallback — создаём пользователя
        user = await _ensure_user(message, db, tg_user_id, tg_full_name)
    user_dict = dict(user)
    reminder_time = data.get("reminder_time", "09:00")
    last_sent = data.get("last_sent")
    title = data.get("title", "Напоминание")
    reminder_id = await repo.create_custom_reminder(
        db,
        user_id=user["id"],
        title=title,
        reminder_time=reminder_time,
        frequency_days=freq,
        target_weekday=target_weekday,
    )
    if last_sent:
        await repo.set_custom_reminder_sent(db, reminder_id, last_sent)
    await state.clear()
    note = ""
    today = local_date_str(datetime.datetime.utcnow(), user_dict.get("timezone", user_tz or "UTC"))
    if user_dict.get("pause_until") and user_dict["pause_until"] >= today:
        note = "\n⚠️ У тебя включён щадящий режим/пауза, поэтому уведомление придёт только после /resume или завтра."
    desc = f"{title} в {reminder_time}"
    if freq == 9999:
        desc += " (один раз)"
    elif target_weekday is not None:
        try:
            desc += f", раз в неделю ({['пн','вт','ср','чт','пт','сб','вс'][int(target_weekday)]})"
        except Exception:
            desc += ", раз в неделю"
    else:
        desc += f", каждые {freq} д."
    await message.answer(ack(f"{desc}{note}"), reply_markup=main_menu_keyboard())


@router.message(Command("reminders"))
async def list_reminders(message: types.Message, db) -> None:
    urow = await repo.get_user_by_telegram_id(db, message.from_user.id)
    user = row_to_dict(urow) if urow else None
    if not user:
        await message.answer(register_text())
        return
    reminders = await repo.list_custom_reminders(db, user["id"])
    kb = list_keyboard(reminders, with_add=True)
    text = "Твои напоминания (кликни, чтобы удалить):"
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    if user["pause_until"] and user["pause_until"] >= today:
        text = "⚠️ Внимание: включён щадящий режим/пауза, напоминания не придут до выхода из паузы.\n\n" + text
    if not reminders:
        text = "Пока нет своих напоминаний. Нажми «Добавить», чтобы создать первое."
    await message.answer(text, reply_markup=kb)


@router.message(lambda m: m.text and "напомин" in m.text.lower())
async def list_reminders_button(message: types.Message, db) -> None:
    urow = await repo.get_user_by_telegram_id(db, message.from_user.id)
    user = row_to_dict(urow) if urow else None
    if not user:
        await message.answer(register_text())
        return
    reminders = await repo.list_custom_reminders(db, user["id"])
    kb = list_keyboard(reminders, with_add=True)
    text = "Твои напоминания (кликни, чтобы удалить):"
    if not reminders:
        text = "Пока нет своих напоминаний. Нажми «Добавить», чтобы создать первое."
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    if user["pause_until"] and user["pause_until"] >= today:
        text = "⚠️ Включён щадящий режим/пауза, напоминания не придут до выхода из паузы.\n\n" + text
    await message.answer(text, reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data == "rem:list")
async def reminders_from_today(callback: types.CallbackQuery, db) -> None:
    """Показ списка напоминаний из /today кнопки."""
    urow = await repo.get_user_by_telegram_id(db, callback.from_user.id)
    user = row_to_dict(urow) if urow else None
    if not user:
        await callback.answer(register_text(), show_alert=True)
        return
    reminders = await repo.list_custom_reminders(db, user["id"])
    kb = list_keyboard(reminders, with_add=True)
    text = "Твои напоминания (кликни, чтобы удалить):"
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    if user["pause_until"] and user["pause_until"] >= today:
        text = "⚠️ Включён щадящий режим/пауза, напоминания не придут до выхода из паузы.\n\n" + text
    if not reminders:
        text = "Пока нет своих напоминаний. Нажми «Добавить», чтобы создать первое."
    await safe_edit(callback.message, text, reply_markup=kb)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data == "rem:add")
async def reminders_add_from_today(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await callback.answer()
    await add_reminder_start(callback.message, state, db)


@router.callback_query(lambda c: c.data and c.data.startswith("customedit:"))
async def reminder_edit(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    reminder_id = int(callback.data.split(":")[1])
    urow = await repo.get_user_by_telegram_id(db, callback.from_user.id)
    user = row_to_dict(urow) if urow else None
    if not user:
        await callback.answer(register_text(), show_alert=True)
        return
    await state.update_data(edit_id=reminder_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏰ Время", callback_data="rem:edit_time"),
                InlineKeyboardButton(text="↻ Частота", callback_data="rem:edit_freq"),
            ],
            [InlineKeyboardButton(text="День недели (для еженед.)", callback_data="rem:edit_wd")],
        ]
    )
    await callback.message.answer("Что меняем в напоминании?", reply_markup=kb)
    await state.set_state(CustomReminderState.edit_choice)
    await callback.answer()


@router.callback_query(lambda c: c.data in ("rem:edit_time", "rem:edit_freq", "rem:edit_wd"))
async def reminder_edit_choice(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("edit_id"):
        await callback.answer()
        return
    action = callback.data.split(":")[1]
    if action == "edit_time":
        await state.set_state(CustomReminderState.edit_time)
        await callback.message.answer("Новое время? Формат HH:MM или «через 2 часа».")
    elif action == "edit_freq":
        await state.set_state(CustomReminderState.edit_freq)
        await callback.message.answer("Новая частота (дни, число ≥1).")
    elif action == "edit_wd":
        await state.set_state(CustomReminderState.edit_wd)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Пн", callback_data="rem:wdset:0"),
                    InlineKeyboardButton(text="Вт", callback_data="rem:wdset:1"),
                    InlineKeyboardButton(text="Ср", callback_data="rem:wdset:2"),
                    InlineKeyboardButton(text="Чт", callback_data="rem:wdset:3"),
                    InlineKeyboardButton(text="Пт", callback_data="rem:wdset:4"),
                    InlineKeyboardButton(text="Сб", callback_data="rem:wdset:5"),
                    InlineKeyboardButton(text="Вс", callback_data="rem:wdset:6"),
                ]
            ]
        )
        await callback.message.answer("Выбери день недели для напоминания:", reply_markup=kb)
    await callback.answer()


@router.message(CustomReminderState.edit_time)
async def reminder_edit_time(message: types.Message, state: FSMContext, db) -> None:
    data = await state.get_data()
    reminder_id = data.get("edit_id")
    if not reminder_id:
        await message.answer("Не нашла напоминание. Попробуй снова.")
        await state.clear()
        return
    abs_time, plus_hours, weekday = parse_when(message.text)
    if abs_time:
        hhmm = abs_time
    elif plus_hours is not None:
        from utils.time import format_time_local
        user = await repo.get_user_by_telegram_id(db, message.from_user.id)
        tz = user["timezone"] if user else "UTC"
        now_utc = datetime.datetime.utcnow()
        hhmm = format_time_local(now_utc + datetime.timedelta(hours=plus_hours), tz)
    else:
        hhmm = message.text.strip()
    await repo.update_custom_reminder_time(db, message.from_user.id, reminder_id, hhmm)
    await state.clear()
    await message.answer(ack(f"Время обновлено: {hhmm}"), reply_markup=main_menu_keyboard())


@router.message(CustomReminderState.edit_freq)
async def reminder_edit_freq(message: types.Message, state: FSMContext, db) -> None:
    data = await state.get_data()
    reminder_id = data.get("edit_id")
    if not reminder_id:
        await message.answer("Не нашла напоминание. Попробуй снова.")
        await state.clear()
        return
    try:
        freq = int(message.text.strip())
        if freq < 1:
            raise ValueError
    except Exception:
        from utils import texts

        await message.answer(
            texts.error("нужно число дней, минимум 1."),
        )
        return
    await repo.update_custom_reminder_freq(db, message.from_user.id, reminder_id, freq)
    await state.clear()
    await message.answer(ack(f"Частота обновлена: каждые {freq} д."), reply_markup=main_menu_keyboard())


@router.callback_query(lambda c: c.data and c.data.startswith("rem:wdset:"))
async def reminder_edit_weekday(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    data = await state.get_data()
    reminder_id = data.get("edit_id")
    if not reminder_id:
        await callback.answer()
        return
    weekday = int(callback.data.split(":")[2])
    await repo.update_custom_reminder_freq(db, callback.from_user.id, reminder_id, frequency_days=7, target_weekday=weekday)
    await state.clear()
    await callback.message.answer("День недели обновлен.", reply_markup=main_menu_keyboard())
    await callback.answer("Сохранила")

# Свободный ввод для напоминаний (натуральные команды)
@router.message(lambda m: m.text and "напом" in m.text.lower())
async def reminder_free_parse(message: types.Message, db, state: FSMContext) -> None:
    parsed = parse_command(message.text)
    if not parsed or parsed.type != "reminder":
        return
    urow = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if not urow:
        await message.answer(register_text())
        return
    user = row_to_dict(urow)
    title = parsed.payload.get("title") or "Напоминание"
    hhmm = parsed.payload.get("time") or "09:00"
    freq = parsed.payload.get("freq_days") or 1
    target_weekday = parsed.payload.get("target_weekday")
    if parsed.payload.get("rel_hours") or parsed.payload.get("rel_minutes"):
        now_utc = datetime.datetime.utcnow()
        delta = datetime.timedelta(
            hours=parsed.payload.get("rel_hours") or 0,
            minutes=parsed.payload.get("rel_minutes") or 0,
        )
        from utils.time import format_time_local

        hhmm = format_time_local(now_utc + delta, user.get("timezone", "UTC"))
    last_sent = None
    if parsed.payload.get("day_offset"):
        local_date = local_date_str(datetime.datetime.utcnow(), user.get("timezone", "UTC"))
        last_sent = local_date
    reminder_id = await repo.create_custom_reminder(
        db,
        user_id=user["id"],
        title=title,
        reminder_time=hhmm,
        frequency_days=freq,
        target_weekday=target_weekday,
    )
    if last_sent:
        await repo.set_custom_reminder_sent(db, reminder_id, last_sent)
    await state.clear()
    await message.answer(f"Напоминание добавлено: {title} в {hhmm}.", reply_markup=main_menu_keyboard())


@router.callback_query(lambda c: c.data and c.data.startswith("customdel:"))
async def delete_reminder(callback: types.CallbackQuery, db) -> None:
    reminder_id = int(callback.data.split(":")[1])
    urow = await repo.get_user_by_telegram_id(db, callback.from_user.id)
    user = row_to_dict(urow) if urow else None
    if not user:
        await callback.answer(register_text(), show_alert=True)
        return
    await repo.delete_custom_reminder(db, user["id"], reminder_id)
    reminders = await repo.list_custom_reminders(db, user["id"])
    title = "Напоминание"
    for r in reminders:
        row = row_to_dict(r)
        if row.get("id") == reminder_id:
            title = row.get("title") or title
            break
    await callback.answer(f"Убрала напоминание «{title}».")
    if reminders:
        await callback.message.edit_reply_markup(reply_markup=list_keyboard(reminders))
    else:
        await callback.message.edit_text(
            "Список пуст. Добавь новое напоминание через раздел «⏰ Напоминания» → «Добавить»."
        )


# Actions for delivered custom reminders
async def _remind_later(bot, db, user, reminder_id: int, local_date: str, title: str):
    await asyncio.sleep(30 * 60)
    text = f"Напоминание позже: {title}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Сделал(а) ✔",
                    callback_data=f"custom:{reminder_id}:{local_date}:done",
                ),
                InlineKeyboardButton(
                    text="Пропустить",
                    callback_data=f"custom:{reminder_id}:{local_date}:skip",
                ),
            ]
        ]
    )
    await bot.send_message(chat_id=user["telegram_id"], text=text, reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("custom:"))
async def custom_action(callback: types.CallbackQuery, db) -> None:
    from utils.today import render_today
    _, reminder_id, local_date, action = callback.data.split(":")
    reminder_id = int(reminder_id)
    from utils.user import ensure_user
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    reminder_list = await repo.list_custom_reminders(db, user["id"])
    target = next((r for r in reminder_list if r["id"] == reminder_id), None)
    if not target:
        await callback.answer("Напоминание не найдено", show_alert=True)
        return

    tone = "neutral"
    wellness_row = await repo.get_wellness(db, user["id"])
    if wellness_row:
        tone = wellness_row["tone"]

    status_map = {"done": "done", "skip": "skip", "later": "later"}
    status = status_map.get(action, "pending")
    await repo.log_custom_task(
        db,
        reminder_id=reminder_id,
        user_id=user["id"],
        reminder_date=local_date,
        status=status,
    )
    if action == "done":
        await repo.add_points(db, user["id"], 3, local_date=local_date)

    if action == "done":
        await callback.answer("Отмечено ✔")
    elif action == "skip":
        await callback.answer("Пропущено.")
    elif action == "later":
        await callback.answer("Напомню через 30 минут.")
        asyncio.create_task(
            _remind_later(callback.message.bot, db, user, reminder_id, local_date, target["title"])
        )
    else:
        await callback.answer("Обновлено")

    # Вместо полной сводки — короткое подтверждение
    if action == "done":
        from utils.tone import tone_ack

        await callback.message.answer(tone_ack(tone, "напоминание"), reply_markup=main_menu_keyboard())
    elif action == "skip":
        await callback.message.answer("⏭ Пропустила напоминание.", reply_markup=main_menu_keyboard())


@router.callback_query(lambda c: c.data and c.data.startswith("rem:"))
async def reminder_menu_callbacks(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    user = await repo.get_user_by_telegram_id(db, callback.from_user.id)
    if not user:
        await callback.answer(register_text(), show_alert=True)
        return
    action = callback.data.split(":")[1]
    if action == "add":
        # сохранить пользователя для дальнейших шагов
        await state.update_data(user_id=user["id"], user_tz=user["timezone"])
        await state.set_state(CustomReminderState.title)
        await callback.message.answer("Что напомнить? Введи короткий текст.")
    elif action == "list":
        reminders = await repo.list_custom_reminders(db, user["id"])
        if not reminders:
            await callback.message.answer(
                "Пока нет своих напоминаний. Нажми «Добавить», чтобы создать первое.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить", callback_data="rem:add")]]
                ),
            )
        else:
            await callback.message.answer("Твои напоминания (кликни, чтобы удалить):", reply_markup=list_keyboard(reminders))
    await callback.answer()
