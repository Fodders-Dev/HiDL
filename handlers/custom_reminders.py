import asyncio
import datetime
import re

from aiogram import Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.menu_match import is_single_clause_reminder
from utils.rows import row_to_dict
from utils.time import parse_hhmm, local_date_str, format_time_local
from utils.tone import tone_short_ack
from utils.nlp import match_simple_intent, parse_when
from utils.nl_parser import parse_command
from utils.texts import register_text, ack
from utils import texts
from utils.gender import done_button_label
from utils.sender import safe_edit
from utils.logger import log_debug

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
    parsed_confirm = State()
    parsed_edit_title = State()
    parsed_edit_time = State()
    parsed_edit_freq = State()
    parsed_edit_weekday = State()


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
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="main:menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _weekday_label(wd: int | None) -> str:
    if wd is None:
        return ""
    labels = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    try:
        return labels[int(wd)]
    except Exception:
        return ""


def _freq_label(freq: int | None) -> str:
    if not freq or freq == 1:
        return "каждый день"
    if freq == 2:
        return "через день"
    if freq == 7:
        return "раз в неделю"
    if freq >= 9999:
        return "только один раз"
    return f"каждые {freq} д."


def _compute_last_sent(freq: int, day_offset: int, tz: str) -> str | None:
    """Подвинуть last_sent назад, чтобы напоминание стартовало через offset дней."""
    if not day_offset or not freq:
        return None
    try:
        today = datetime.date.fromisoformat(local_date_str(datetime.datetime.utcnow(), tz))
    except Exception:
        return None
    back_days = max(freq - day_offset, 0)
    return (today - datetime.timedelta(days=back_days)).isoformat()


def _render_pending_summary(pending: dict, tz: str) -> str:
    lines = ["Так сохраню напоминание:"]
    lines.append(f"• Текст: {pending.get('title', 'Напоминание')}")
    rt = pending.get("reminder_time")
    lines.append(f"• Время: {rt if rt else 'не указано'}")
    if pending.get("day_offset"):
        try:
            base = datetime.date.fromisoformat(local_date_str(datetime.datetime.utcnow(), tz))
            target_date = base + datetime.timedelta(days=int(pending["day_offset"]))
            lines.append(f"• День: через {pending['day_offset']} д. ({target_date.isoformat()})")
        except Exception:
            lines.append(f"• День: через {pending.get('day_offset')} д.")
    if pending.get("target_weekday") is not None:
        wd_label = _weekday_label(pending.get("target_weekday"))
        lines.append(f"• День недели: {wd_label or 'выбран'}")
    lines.append(f"• Повтор: {_freq_label(pending.get('frequency_days'))}")
    return "\n".join(lines)


def _parsed_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Сохранить", callback_data="remparsed:save"),
                InlineKeyboardButton(text="✖️ Отмена", callback_data="remparsed:cancel"),
            ],
            [
                InlineKeyboardButton(text="✏️ Текст", callback_data="remparsed:edit_title"),
                InlineKeyboardButton(text="🕒 Время/дата", callback_data="remparsed:edit_time"),
            ],
            [
                InlineKeyboardButton(text="📅 День недели", callback_data="remparsed:edit_wd"),
                InlineKeyboardButton(text="🔁 Повтор", callback_data="remparsed:edit_freq"),
            ],
        ]
    )


def _parsed_freq_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Каждый день", callback_data="remparsed:freqset:1"),
                InlineKeyboardButton(text="Через день", callback_data="remparsed:freqset:2"),
            ],
            [
                InlineKeyboardButton(text="Раз в неделю", callback_data="remparsed:freqset:7"),
                InlineKeyboardButton(text="Только один раз", callback_data="remparsed:freqset:once"),
            ],
            [
                InlineKeyboardButton(text="Своя периодичность", callback_data="remparsed:freqset:custom"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="remparsed:back"),
            ],
        ]
    )


def _parsed_weekday_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="Пн", callback_data="remparsed:wdset:0"),
            InlineKeyboardButton(text="Вт", callback_data="remparsed:wdset:1"),
            InlineKeyboardButton(text="Ср", callback_data="remparsed:wdset:2"),
            InlineKeyboardButton(text="Чт", callback_data="remparsed:wdset:3"),
        ],
        [
            InlineKeyboardButton(text="Пт", callback_data="remparsed:wdset:4"),
            InlineKeyboardButton(text="Сб", callback_data="remparsed:wdset:5"),
            InlineKeyboardButton(text="Вс", callback_data="remparsed:wdset:6"),
        ],
        [InlineKeyboardButton(text="Убрать день недели", callback_data="remparsed:wdset:none")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="remparsed:back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _refresh_pending_preview(bot, chat_id: int, state: FSMContext, text: str, kb: InlineKeyboardMarkup):
    """Обновить/создать сообщение с превью напоминания."""
    data = await state.get_data()
    msg_id = data.get("pending_summary_id")
    if msg_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=kb)
            return
        except Exception:
            pass
    sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
    await state.update_data(pending_summary_id=sent.message_id)


def _build_pending_from_payload(payload: dict, user: dict, base: dict | None = None) -> dict:
    """Собрать черновик напоминания из распарсенного текста."""
    tz = user.get("timezone", "UTC")
    title = payload.get("title") or (base.get("title") if base else "Напоминание")
    day_offset = payload.get("day_offset")
    if day_offset is None and base is not None:
        day_offset = base.get("day_offset", 0)
    rel_hours = payload.get("rel_hours")
    rel_minutes = payload.get("rel_minutes")
    hhmm = payload.get("time") or (base.get("reminder_time") if base else None)
    if rel_hours or rel_minutes:
        delta = datetime.timedelta(
            hours=rel_hours or 0,
            minutes=rel_minutes or 0,
            days=day_offset if day_offset and not payload.get("time") else 0,
        )
        hhmm = format_time_local(datetime.datetime.utcnow() + delta, tz)
        if payload.get("time") is None and day_offset:
            day_offset = 0
    if hhmm and parse_hhmm(hhmm):
        parts = hhmm.split(":")
        hhmm = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    else:
        hhmm = None

    target_weekday = payload.get("target_weekday")
    if target_weekday is None and base is not None:
        target_weekday = base.get("target_weekday")

    freq = payload.get("freq_days")
    if freq is None and base is not None:
        freq = base.get("frequency_days")
    once_flag = payload.get("one_time", False)
    if freq is None:
        if once_flag or rel_hours or rel_minutes or day_offset:
            freq = 9999
        elif target_weekday is not None:
            freq = 7
        else:
            # safer default: one-time unless user explicitly asked to repeat
            freq = 9999
    if once_flag and freq < 9999:
        freq = 9999

    last_sent = _compute_last_sent(freq, day_offset or 0, tz)
    base_last_sent = base.get("last_sent") if base else None
    if base_last_sent and (day_offset or 0) == (base.get("day_offset") if base else 0):
        last_sent = base_last_sent

    return {
        "title": title,
        "reminder_time": hhmm,
        "frequency_days": freq,
        "target_weekday": target_weekday,
        "day_offset": day_offset or 0,
        "last_sent": last_sent,
    }


async def _persist_parsed_reminder(message: types.Message, state: FSMContext, db, user: dict, pending: dict):
    reminder_time = pending.get("reminder_time")
    if not reminder_time or not parse_hhmm(reminder_time):
        await message.answer("Нужно указать время. Например: <в 10>, <в 09:30> или <завтра в 14:00>.", reply_markup=main_menu_keyboard())
        return
    freq = pending.get("frequency_days") or 9999
    reminder_id = await repo.create_custom_reminder(
        db,
        user_id=user["id"],
        title=pending.get("title", "Напоминание"),
        reminder_time=reminder_time,
        frequency_days=freq,
        target_weekday=pending.get("target_weekday"),
    )
    if pending.get("last_sent"):
        await repo.set_custom_reminder_sent(db, reminder_id, pending["last_sent"])
    await state.clear()
    await message.answer(
        ack(
            f"{pending.get('title','Напоминание')} в {reminder_time} "
            f"({_freq_label(freq)})"
        ),
        reply_markup=main_menu_keyboard(),
    )


async def _update_preview_from_pending(message: types.Message, state: FSMContext, user: dict, pending: dict):
    await state.update_data(pending_parsed=pending)
    await _refresh_pending_preview(
        message.bot,
        message.chat.id,
        state,
        _render_pending_summary(pending, user.get("timezone", "UTC")),
        _parsed_confirm_keyboard(),
    )


async def _start_add_reminder(
    message: types.Message,
    state: FSMContext,
    db,
    tg_user_id: int,
    tg_full_name: str,
) -> None:
    user = await _ensure_user(db, tg_user_id, tg_full_name)
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


@router.message(Command("add_reminder"))
async def add_reminder_start(message: types.Message, state: FSMContext, db) -> None:
    await _start_add_reminder(
        message,
        state,
        db,
        message.from_user.id,
        message.from_user.full_name,
    )


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
        target_date = local_date_str(now_utc + datetime.timedelta(hours=1), user["timezone"])
        reminder_id = await repo.create_custom_reminder(
            db,
            user_id=user["id"],
            title=data["title"],
            reminder_time=hhmm,
            frequency_days=9999,
        )
        try:
            due = datetime.date.fromisoformat(target_date)
            baseline = (due - datetime.timedelta(days=9999)).isoformat()
            await repo.set_custom_reminder_sent(db, reminder_id, baseline)
        except Exception:
            pass
        await state.clear()
        await callback.message.answer(
            ack(f"{data['title']} в {hhmm} (один раз).\n\nПосмотреть и удалить старые можно в разделе ⏰ Напоминания."),
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
        await callback.message.answer("Введи время в формате HH:MM (например 09:00). Потом выберем повтор.")
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
    local_date = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    target_date = local_date
    if when_choice == "tomorrow":
        target_date = (datetime.date.fromisoformat(local_date) + datetime.timedelta(days=1)).isoformat()
        last_sent = local_date
    await state.update_data(reminder_time=reminder_time, last_sent=last_sent, target_date=target_date)
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
        await callback.message.answer("Раз в сколько дней напоминать? Напиши число.\nНапример, 2 — через день, 7 — раз в неделю, 30 — раз в месяц.")
        await callback.answer()
        return
    if choice == "once":
        # Одноразовое напоминание: не привязываем к дню недели, просто
        # помечаем большой период между отправками, чтобы сработало один раз.
        await state.update_data(pending_freq=9999, one_time=True)
        await _save_reminder_with_frequency(
            callback.message,
            state,
            db,
            9999,
            tg_user_id=callback.from_user.id,
            tg_full_name=callback.from_user.full_name,
            target_weekday=None,
        )
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
        from utils import texts
        await message.answer(
             texts.error("не поняла число. Напиши цифрами, например 7 (раз в неделю) или 1 (каждый день).")
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
    # Всегда берём пользователя по telegram_id, чтобы не промахнуться по state.
    urow = await repo.get_user_by_telegram_id(db, tg_user_id)
    user = row_to_dict(urow) if urow else None
    if not user:
        user = await _ensure_user(message, db, tg_user_id, tg_full_name)
    user_dict = dict(user)
    reminder_time = data.get("reminder_time", "09:00")
    last_sent = data.get("last_sent")
    effective_last_sent = last_sent
    if freq == 9999:
        tz = user_dict.get("timezone", "UTC")
        tdate = data.get("target_date") or local_date_str(datetime.datetime.utcnow(), tz)
        try:
            due = datetime.date.fromisoformat(tdate)
            effective_last_sent = (due - datetime.timedelta(days=9999)).isoformat()
        except Exception:
            effective_last_sent = last_sent
    title = data.get("title", "Напоминание")
    reminder_id = await repo.create_custom_reminder(
        db,
        user_id=user["id"],
        title=title,
        reminder_time=reminder_time,
        frequency_days=freq,
        target_weekday=target_weekday,
    )
    log_debug(
        f"custom_reminder.create user_id={user['id']} tg_user_id={tg_user_id} title={title!r} time={reminder_time} "
        f"freq={freq} target_weekday={target_weekday} last_sent={last_sent}"
    )
    if effective_last_sent:
        await repo.set_custom_reminder_sent(db, reminder_id, effective_last_sent)
    await state.clear()
    note = ""
    today = local_date_str(datetime.datetime.utcnow(), user_dict.get("timezone", "UTC"))
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
    await message.answer(ack(f"{desc}{note}\n\nПосмотреть и удалить старые можно в разделе ⏰ Напоминания."), reply_markup=main_menu_keyboard())


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


@router.message(
    lambda m: m.text
    and m.text.lower().strip() in {"напоминания", "напоминание", "⏰ напоминания", "напоминания!"}
)
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
async def reminders_from_today(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    """Показ списка напоминаний из /today кнопки."""
    await state.clear()
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
    await _start_add_reminder(
        callback.message,
        state,
        db,
        callback.from_user.id,
        callback.from_user.full_name,
    )


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
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="rem:list")],
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
                ],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="rem:list")],
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
    user = await _ensure_user(message, db, message.from_user.id, message.from_user.full_name)
    abs_time, plus_hours, weekday = parse_when(message.text)
    if abs_time:
        hhmm = abs_time
    elif plus_hours is not None:
        from utils.time import format_time_local
        tz = user["timezone"] if user else "UTC"
        now_utc = datetime.datetime.utcnow()
        hhmm = format_time_local(now_utc + datetime.timedelta(hours=plus_hours), tz)
    else:
        hhmm = message.text.strip()
    await repo.update_custom_reminder_time(db, user["id"], reminder_id, hhmm)
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
    user = await _ensure_user(message, db, message.from_user.id, message.from_user.full_name)
    await repo.update_custom_reminder_freq(db, user["id"], reminder_id, freq)
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
    user = await _ensure_user(callback.message, db, callback.from_user.id, callback.from_user.full_name)
    await repo.update_custom_reminder_freq(db, user["id"], reminder_id, frequency_days=7, target_weekday=weekday)
    await state.clear()
    await callback.message.answer("День недели обновлен.", reply_markup=main_menu_keyboard())
    await callback.answer("Сохранила")

# Свободный ввод для напоминаний (натуральные команды)
@router.message(
    StateFilter(None, CustomReminderState.parsed_confirm),
    lambda m: m.text and "напом" in m.text.lower() and is_single_clause_reminder(m.text),
)
async def reminder_free_parse(message: types.Message, db, state: FSMContext) -> None:
    if message.text and message.text.lower().strip() in {"напоминания", "напоминание", "⏰ напоминания", "напоминания!"}:
        # Пусть короткое слово ведёт в список, а не создаёт новое.
        return
    parsed = parse_command(message.text or "")
    if not parsed or parsed.type != "reminder":
        return
    urow = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if not urow:
        await message.answer(register_text())
        return
    user = row_to_dict(urow)
    pending = _build_pending_from_payload(parsed.payload, user)
    await state.update_data(
        pending_parsed=pending,
        pending_summary_id=None,
        user_id=user["id"],
        user_tz=user.get("timezone", "UTC"),
    )
    preview = _render_pending_summary(pending, user.get("timezone", "UTC"))
    sent = await message.answer(preview, reply_markup=_parsed_confirm_keyboard())
    await state.update_data(pending_summary_id=sent.message_id)
    await state.set_state(CustomReminderState.parsed_confirm)


@router.callback_query(lambda c: c.data and c.data.startswith("remparsed:"))
async def reminder_parsed_callbacks(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    data = await state.get_data()
    pending = data.get("pending_parsed")
    if not pending:
        await callback.answer("Не вижу черновик.", show_alert=True)
        return
    urow = await repo.get_user_by_telegram_id(db, callback.from_user.id)
    user = row_to_dict(urow) if urow else None
    if not user:
        await callback.answer(register_text(), show_alert=True)
        return
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "save":
        if not pending.get("reminder_time"):
            await callback.answer("Нужно время", show_alert=True)
            await state.set_state(CustomReminderState.parsed_edit_time)
            await callback.message.answer("На когда? Например: <в 10>, <завтра в 14:00> или <через 2 часа>.")
            return
        await _persist_parsed_reminder(callback.message, state, db, user, pending)
        await callback.answer("Сохранила.")
        return
    if action == "cancel":
        await state.clear()
        await callback.message.answer("Отменила создание напоминания.", reply_markup=main_menu_keyboard())
        await callback.answer()
        return
    if action == "edit_title":
        await state.set_state(CustomReminderState.parsed_edit_title)
        await callback.message.answer("Как сформулировать напоминание? Напиши текст.")
        await callback.answer()
        return
    if action == "edit_time":
        await state.set_state(CustomReminderState.parsed_edit_time)
        await callback.message.answer("На когда? Например: «завтра в 14:00», «в 09:30» или «через 2 часа».")
        await callback.answer()
        return
    if action == "edit_freq":
        await callback.message.answer("Как часто повторять?", reply_markup=_parsed_freq_keyboard())
        await callback.answer()
        return
    if action == "edit_wd":
        await callback.message.answer("Выбери день недели (для еженедельных напоминаний):", reply_markup=_parsed_weekday_keyboard())
        await callback.answer()
        return
    if action == "back":
        await _update_preview_from_pending(callback.message, state, user, pending)
        await callback.answer()
        return
    if action == "freqset":
        value = parts[2] if len(parts) > 2 else ""
        if value == "custom":
            await state.set_state(CustomReminderState.parsed_edit_freq)
            await callback.message.answer("Введи через сколько дней повторять (целое число, минимум 1).")
            await callback.answer()
            return
        if value == "once":
            freq = 9999
        else:
            try:
                freq = int(value)
            except Exception:
                await callback.answer("Не поняла частоту.")
                return
        pending["frequency_days"] = freq
        pending["last_sent"] = _compute_last_sent(freq, pending.get("day_offset", 0), user.get("timezone", "UTC"))
        await _update_preview_from_pending(callback.message, state, user, pending)
        await callback.answer("Обновила частоту.")
        return
    if action == "wdset":
        raw = parts[2] if len(parts) > 2 else "none"
        weekday = None if raw == "none" else int(raw)
        pending["target_weekday"] = weekday
        if weekday is not None and pending.get("frequency_days") == 1:
            pending["frequency_days"] = 7
        pending["last_sent"] = _compute_last_sent(
            pending.get("frequency_days", 1), pending.get("day_offset", 0), user.get("timezone", "UTC")
        )
        await _update_preview_from_pending(callback.message, state, user, pending)
        await callback.answer("День недели обновила.")
        return


@router.message(CustomReminderState.parsed_edit_title)
async def reminder_parsed_edit_title(message: types.Message, state: FSMContext, db) -> None:
    data = await state.get_data()
    pending = data.get("pending_parsed") or {}
    pending["title"] = message.text.strip()
    urow = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if not urow:
        await message.answer(register_text())
        await state.clear()
        return
    user = row_to_dict(urow)
    await state.set_state(CustomReminderState.parsed_confirm)
    await _update_preview_from_pending(message, state, user, pending)


@router.message(CustomReminderState.parsed_edit_time)
async def reminder_parsed_edit_time(message: types.Message, state: FSMContext, db) -> None:
    data = await state.get_data()
    pending = data.get("pending_parsed") or {}
    urow = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if not urow:
        await message.answer(register_text())
        await state.clear()
        return
    user = row_to_dict(urow)
    raw = (message.text or "").strip()
    # Часто отвечают просто "10" вместо "в 10".
    m = re.match(r"^\s*(\d{1,2})\s*$", raw)
    if m:
        raw = f"{int(m.group(1)):02d}:00"
    parsed = parse_command(f"напомни {raw}")
    payload = parsed.payload if parsed and parsed.type == "reminder" else {"time": raw}
    # В этом шаге редактируем "когда", поэтому не перетираем title.
    payload["title"] = pending.get("title")
    new_pending = _build_pending_from_payload(payload, user, base=pending)
    await state.set_state(CustomReminderState.parsed_confirm)
    await _update_preview_from_pending(message, state, user, new_pending)


@router.message(CustomReminderState.parsed_edit_freq)
async def reminder_parsed_edit_freq(message: types.Message, state: FSMContext, db) -> None:
    data = await state.get_data()
    pending = data.get("pending_parsed") or {}
    try:
        freq = int(message.text.strip())
        if freq < 1:
            raise ValueError
    except Exception:
        await message.answer("Нужно целое число дней, минимум 1.")
        return
    urow = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if not urow:
        await message.answer(register_text())
        await state.clear()
        return
    user = row_to_dict(urow)
    pending["frequency_days"] = freq
    pending["last_sent"] = _compute_last_sent(freq, pending.get("day_offset", 0), user.get("timezone", "UTC"))
    await state.set_state(CustomReminderState.parsed_confirm)
    await _update_preview_from_pending(message, state, user, pending)


@router.message(CustomReminderState.parsed_edit_weekday)
async def reminder_parsed_edit_weekday(message: types.Message, state: FSMContext, db) -> None:
    # запасной текстовый режим — если что, просто возвращаем в предпросмотр
    data = await state.get_data()
    pending = data.get("pending_parsed") or {}
    urow = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if not urow:
        await message.answer(register_text())
        await state.clear()
        return
    user = row_to_dict(urow)
    await state.set_state(CustomReminderState.parsed_confirm)
    await _update_preview_from_pending(message, state, user, pending)


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
                    text=done_button_label(user),
                    callback_data=f"custom:{reminder_id}:{local_date}:done",
                ),
                InlineKeyboardButton(
                    text="Позже",
                    callback_data=f"custom:{reminder_id}:{local_date}:later",
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
    target = await repo.get_custom_reminder(db, user["id"], reminder_id)
    if not target:
        await callback.answer("Напоминание не найдено", show_alert=True)
        return
    target = dict(target)

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

    # Без лишних сообщений — подтверждаем тостом.


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
                    inline_keyboard=[
                        [InlineKeyboardButton(text="➕ Добавить", callback_data="rem:add")],
                        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main:menu")],
                    ]
                ),
            )
        else:
            await callback.message.answer("Твои напоминания (кликни, чтобы удалить):", reply_markup=list_keyboard(reminders))
    await callback.answer()
