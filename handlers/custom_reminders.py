import asyncio
import datetime
import re

from aiogram import Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from services.inbox_parser import compute_one_time_last_sent, resolve_temporal_expression
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


async def _existing_menu_user(db, tg_user_id: int) -> dict | None:
    """Read an existing profile for menu rendering without creating an account."""
    row = await repo.get_user_by_telegram_id(db, tg_user_id)
    return row_to_dict(row) if row else None


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
            await bot(EditMessageText(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=kb))
            return
        except Exception:
            pass
    sent = await bot(SendMessage(chat_id=chat_id, text=text, reply_markup=kb))
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
        await message.answer("Нужно указать время. Например: <в 10>, <в 09:30> или <завтра в 14:00>.", reply_markup=main_menu_keyboard(user))
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
        reply_markup=main_menu_keyboard(user),
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
    user = await _ensure_user(message, db, tg_user_id, tg_full_name)
    await state.update_data(user_id=user["id"], user_tz=user["timezone"])
    await state.set_state(CustomReminderState.title)
    pause_note = ""
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    if user["pause_until"] and user["pause_until"] >= today:
        pause_note = (
            "\n\n⚠️ Сейчас включён щадящий режим/пауза, напоминания не придут. "
            "Сними паузу в настройках или через кнопку «Щадящий режим», если хочешь получить это напоминание."
        )
    await message.answer(
        "О чём напомнить? Коротко, без лишнего." + pause_note,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="rem:cancel")]]
        ),
    )


@router.message(Command("add_reminder"))
async def add_reminder_start(message: types.Message, state: FSMContext, db) -> None:
    await _start_add_reminder(
        message,
        state,
        db,
        message.from_user.id,
        message.from_user.full_name,
    )


@router.message(CustomReminderState.title, lambda m: m.text and not m.text.startswith("/"))
async def add_reminder_title(message: types.Message, state: FSMContext, user: dict | None = None) -> None:
    title = (message.text or "").strip()
    if title.lower() in {"отмена", "отменить"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard(user))
        return
    await state.update_data(title=title)
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
            [InlineKeyboardButton(text="❌ Отмена", callback_data="rem:cancel")],
        ]
    )
    await message.answer("Когда напомнить?", reply_markup=kb)


@router.message(CustomReminderState.when, lambda m: m.text and not m.text.startswith("/"))
async def add_reminder_when_text(message: types.Message, state: FSMContext, user: dict | None = None) -> None:
    if (message.text or "").strip().lower() in {"отмена", "отменить"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard(user))
        return
    await message.answer(
        "Выбери вариант кнопками выше или нажми «❌ Отмена».",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="rem:cancel")]]
        ),
    )


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
            reply_markup=main_menu_keyboard(row_to_dict(user)),
        )
        await callback.answer()
        return
    if choice == "weekly":
        await state.update_data(when_choice="weekly")
        await state.set_state(CustomReminderState.time)
        await callback.message.answer("Введи время (HH:MM). Потом выберем день недели.", reply_markup=main_menu_keyboard(row_to_dict(user)))
        await callback.answer()
        return
    if choice in ("today", "tomorrow", "custom"):
        await state.update_data(when_choice=choice)
        await state.set_state(CustomReminderState.time)
        await callback.message.answer("Введи время в формате HH:MM (например 09:00). Потом выберем повтор.")
        await callback.answer()


@router.message(CustomReminderState.time, lambda m: m.text and not m.text.startswith("/"))
async def add_reminder_time(message: types.Message, state: FSMContext, db) -> None:
    text = (message.text or "").strip()
    if text.lower() in {"отмена", "отменить"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard(await _existing_menu_user(db, message.from_user.id)))
        return
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


@router.message(CustomReminderState.frequency, lambda m: m.text and not m.text.startswith("/"))
async def add_reminder_frequency(message: types.Message, state: FSMContext, db) -> None:
    raw = (message.text or "").strip()
    if raw.lower() in {"отмена", "отменить"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard(await _existing_menu_user(db, message.from_user.id)))
        return
    try:
        freq = int(raw)
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


@router.message(CustomReminderState.weekday, lambda m: m.text and not m.text.startswith("/"))
async def add_reminder_weekday_text(message: types.Message, state: FSMContext, user: dict | None = None) -> None:
    if (message.text or "").strip().lower() in {"отмена", "отменить"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard(user))
        return
    await message.answer(
        "Выбери день недели кнопками выше или нажми «❌ Отмена».",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="rem:cancel")]]
        ),
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
    await message.answer(ack(f"{desc}{note}\n\nПосмотреть и удалить старые можно в разделе ⏰ Напоминания."), reply_markup=main_menu_keyboard(user_dict))


@router.message(Command("reminders"))
async def list_reminders(message: types.Message, state: FSMContext, db) -> None:
    await state.clear()
    urow = await repo.get_user_by_telegram_id(db, message.from_user.id)
    user = row_to_dict(urow) if urow else None
    if not user:
        await message.answer(register_text())
        return
    reminders = await repo.list_custom_reminders(db, user["id"])
    kb = list_keyboard(reminders, with_add=True)
    text = "Твои напоминания (✏️ изменить, 🗑 удалить):"
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    if user["pause_until"] and user["pause_until"] >= today:
        text = "⚠️ Внимание: включён щадящий режим/пауза, напоминания не придут до выхода из паузы.\n\n" + text
    if not reminders:
        text = "Пока нет своих напоминаний. Нажми «Добавить», чтобы создать первое."
    await message.answer(text, reply_markup=kb)


@router.message(
    lambda m: m.text
    and m.text.lower().strip() in {"напоминания", "напоминание", "⏰ напоминания", "🔔 напоминания", "напоминания!"}
)
async def list_reminders_button(message: types.Message, state: FSMContext, db) -> None:
    await state.clear()
    urow = await repo.get_user_by_telegram_id(db, message.from_user.id)
    user = row_to_dict(urow) if urow else None
    if not user:
        await message.answer(register_text())
        return
    reminders = await repo.list_custom_reminders(db, user["id"])
    kb = list_keyboard(reminders, with_add=True)
    text = "Твои напоминания (✏️ изменить, 🗑 удалить):"
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
    text = "Твои напоминания (✏️ изменить, 🗑 удалить):"
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


@router.callback_query(lambda c: c.data and c.data == "rem:cancel")
async def reminders_cancel_add(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏰ К напоминаниям", callback_data="rem:list")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="main:menu")],
        ]
    )
    await safe_edit(callback.message, "Отменила создание напоминания.", reply_markup=kb)
    await callback.answer()


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


@router.callback_query(lambda c: c.data == "rem:cancel_edit")
async def reminder_cancel_edit(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await state.clear()
    row = await repo.get_user_by_telegram_id(db, callback.from_user.id)
    user = dict(row) if row else None
    await callback.message.answer("Ок, отменила редактирование.", reply_markup=main_menu_keyboard(user))
    await callback.answer("Отменено")


@router.message(
    StateFilter(CustomReminderState.edit_choice, CustomReminderState.edit_wd),
    lambda m: bool(m.text and not m.text.startswith("/")),
)
async def reminder_edit_fallback_text(message: types.Message, state: FSMContext, db) -> None:
    raw = (message.text or "").strip().lower()
    if raw in {"отмена", "отменить", "назад"}:
        await state.clear()
        await message.answer(
            "Отменила.",
            reply_markup=main_menu_keyboard(await _existing_menu_user(db, message.from_user.id)),
        )
        return
    await message.answer(
        "Выбери действие кнопками выше или нажми «Отмена».",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="rem:cancel_edit")]]
        ),
    )


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


@router.message(CustomReminderState.edit_time, lambda m: m.text and not m.text.startswith("/"))
async def reminder_edit_time(message: types.Message, state: FSMContext, db) -> None:
    raw = (message.text or "").strip()
    if raw.lower() in {"отмена", "отменить"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard(await _existing_menu_user(db, message.from_user.id)))
        return
    data = await state.get_data()
    reminder_id = data.get("edit_id")
    if not reminder_id:
        await message.answer("Не нашла напоминание. Попробуй снова.")
        await state.clear()
        return
    user = await _ensure_user(message, db, message.from_user.id, message.from_user.full_name)
    abs_time, plus_hours, weekday = parse_when(raw)
    if abs_time:
        hhmm = abs_time
    elif plus_hours is not None:
        from utils.time import format_time_local
        tz = user["timezone"] if user else "UTC"
        now_utc = datetime.datetime.utcnow()
        hhmm = format_time_local(now_utc + datetime.timedelta(hours=plus_hours), tz)
    else:
        hhmm = raw
        if not parse_hhmm(hhmm):
            await message.answer(
                texts.error("не распознала время. Формат HH:MM или «через 2 часа»."),
            )
            return
        parts = hhmm.split(":")
        hhmm = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    await repo.update_custom_reminder_time(db, user["id"], reminder_id, hhmm)
    await state.clear()
    await message.answer(ack(f"Время обновлено: {hhmm}"), reply_markup=main_menu_keyboard(await _existing_menu_user(db, message.from_user.id)))


@router.message(CustomReminderState.edit_freq, lambda m: m.text and not m.text.startswith("/"))
async def reminder_edit_freq(message: types.Message, state: FSMContext, db) -> None:
    raw = (message.text or "").strip()
    if raw.lower() in {"отмена", "отменить"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard(await _existing_menu_user(db, message.from_user.id)))
        return
    data = await state.get_data()
    reminder_id = data.get("edit_id")
    if not reminder_id:
        await message.answer("Не нашла напоминание. Попробуй снова.")
        await state.clear()
        return
    try:
        freq = int(raw)
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
    await message.answer(ack(f"Частота обновлена: каждые {freq} д."), reply_markup=main_menu_keyboard(await _existing_menu_user(db, message.from_user.id)))


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
    await callback.message.answer("День недели обновлен.", reply_markup=main_menu_keyboard(user))
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


def _try_resolve_missing_time(raw: str, user: dict, pending: dict) -> dict | None:
    """Попробовать извлечь дату/время из ответа пользователя в parsed_confirm."""
    tz = user.get("timezone", "UTC")
    now_utc = datetime.datetime.utcnow()
    sched = resolve_temporal_expression(
        date_hint="",
        time_hint="",
        raw_fragment=raw,
        now_utc=now_utc,
        tz_str=tz,
    )
    if not sched.is_ambiguous and sched.resolved_time:
        updated = dict(pending)
        updated["reminder_time"] = sched.resolved_time
        if sched.resolved_date:
            try:
                today_iso = local_date_str(now_utc, tz)
                today_d = datetime.date.fromisoformat(today_iso)
                due_d = datetime.date.fromisoformat(sched.resolved_date)
                delta = max(0, (due_d - today_d).days)
                updated["day_offset"] = delta
                freq = int(updated.get("frequency_days") or 9999)
                if freq >= 9999:
                    updated["last_sent"] = compute_one_time_last_sent(sched.resolved_date, today_iso, freq)
                else:
                    updated["last_sent"] = _compute_last_sent(freq, delta, tz)
            except Exception:
                pass
        return updated

    m_hour = re.match(r"^\s*(\d{1,2})\s*$", raw)
    if m_hour and 0 <= int(m_hour.group(1)) <= 23:
        raw_norm = f"{int(m_hour.group(1)):02d}:00"
    else:
        raw_norm = raw
    parsed = parse_command(f"напомни {raw_norm}")
    if parsed and parsed.type == "reminder":
        p = dict(parsed.payload)
        if p.get("time") or p.get("rel_hours") or p.get("rel_minutes"):
            p["title"] = pending.get("title")
            candidate = _build_pending_from_payload(p, user, base=pending)
            if candidate.get("reminder_time"):
                return candidate
    return None


@router.message(CustomReminderState.parsed_confirm, lambda m: m.text and not m.text.startswith("/"))
async def reminder_parsed_confirm_text(message: types.Message, state: FSMContext, db) -> None:
    raw = (message.text or "").strip()
    low = raw.lower()
    data = await state.get_data()
    pending = data.get("pending_parsed")
    urow = await repo.get_user_by_telegram_id(db, message.from_user.id)
    user = row_to_dict(urow) if urow else None

    if low in {"да", "ок", "окей", "сохрани", "сохранить", "ага"}:
        if not pending or not user:
            await state.clear()
            await message.answer("Черновик напоминания уже неактуален.", reply_markup=main_menu_keyboard(user))
            return
        await _persist_parsed_reminder(message, state, db, user, pending)
        return

    if low in {"нет", "отмена", "не надо", "отменить"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard(user))
        return

    if pending and user and not pending.get("reminder_time"):
        resolved_pending = _try_resolve_missing_time(raw, user, pending)
        if resolved_pending is not None:
            await _update_preview_from_pending(message, state, user, resolved_pending)
            return

    await state.clear()
    from handlers.inbox import inbox_free_text

    await inbox_free_text(message, state, db)


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
        await callback.message.answer("Отменила создание напоминания.", reply_markup=main_menu_keyboard(user))
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


@router.message(CustomReminderState.parsed_edit_title, lambda m: m.text and not m.text.startswith("/"))
async def reminder_parsed_edit_title(message: types.Message, state: FSMContext, db) -> None:
    raw = (message.text or "").strip()
    if raw.lower() in {"отмена", "отменить"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard(await _existing_menu_user(db, message.from_user.id)))
        return
    data = await state.get_data()
    pending = data.get("pending_parsed") or {}
    pending["title"] = raw
    urow = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if not urow:
        await message.answer(register_text())
        await state.clear()
        return
    user = row_to_dict(urow)
    await state.set_state(CustomReminderState.parsed_confirm)
    await _update_preview_from_pending(message, state, user, pending)


@router.message(CustomReminderState.parsed_edit_time, lambda m: m.text and not m.text.startswith("/"))
async def reminder_parsed_edit_time(message: types.Message, state: FSMContext, db) -> None:
    raw = (message.text or "").strip()
    if raw.lower() in {"отмена", "отменить"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard(await _existing_menu_user(db, message.from_user.id)))
        return
    data = await state.get_data()
    pending = data.get("pending_parsed") or {}
    urow = await repo.get_user_by_telegram_id(db, message.from_user.id)
    if not urow:
        await message.answer(register_text())
        await state.clear()
        return
    user = row_to_dict(urow)
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


@router.message(CustomReminderState.parsed_edit_freq, lambda m: m.text and not m.text.startswith("/"))
async def reminder_parsed_edit_freq(message: types.Message, state: FSMContext, db) -> None:
    raw = (message.text or "").strip()
    if raw.lower() in {"отмена", "отменить"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard(await _existing_menu_user(db, message.from_user.id)))
        return
    data = await state.get_data()
    pending = data.get("pending_parsed") or {}
    try:
        freq = int(raw)
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


@router.message(CustomReminderState.parsed_edit_weekday, lambda m: m.text and not m.text.startswith("/"))
async def reminder_parsed_edit_weekday(message: types.Message, state: FSMContext, db) -> None:
    raw = (message.text or "").strip()
    if raw.lower() in {"отмена", "отменить"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard(await _existing_menu_user(db, message.from_user.id)))
        return
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
    rem = await repo.get_custom_reminder(db, user["id"], reminder_id)
    if not rem:
        await callback.answer("Не нашла напоминание", show_alert=True)
        return
    rem_d = row_to_dict(rem)
    title = rem_d.get("title") or "Напоминание"
    rtime = rem_d.get("reminder_time") or "--:--"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"customdel_yes:{reminder_id}")],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="rem:list")],
        ]
    )
    await safe_edit(callback.message, f"Удалить напоминание «{title}» ({rtime})?", reply_markup=kb)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("customdel_yes:"))
async def delete_reminder_confirm(callback: types.CallbackQuery, db) -> None:
    reminder_id = int(callback.data.split(":")[1])
    urow = await repo.get_user_by_telegram_id(db, callback.from_user.id)
    user = row_to_dict(urow) if urow else None
    if not user:
        await callback.answer(register_text(), show_alert=True)
        return
    rem = await repo.get_custom_reminder(db, user["id"], reminder_id)
    if not rem:
        await callback.answer("Не нашла напоминание", show_alert=True)
        return
    title = row_to_dict(rem).get("title") or "Напоминание"
    await repo.delete_custom_reminder(db, user["id"], reminder_id)
    reminders = await repo.list_custom_reminders(db, user["id"])
    await callback.answer(f"Убрала напоминание «{title}».")
    if reminders:
        await safe_edit(
            callback.message,
            "Твои напоминания (✏️ изменить, 🗑 удалить):",
            reply_markup=list_keyboard(reminders),
        )
    else:
        empty_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить", callback_data="rem:add")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="main:menu")],
            ]
        )
        await safe_edit(
            callback.message,
            "Пока нет своих напоминаний. Нажми «Добавить», чтобы создать первое.",
            reply_markup=empty_kb,
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
        await callback.message.answer(
            "Что напомнить? Введи короткий текст.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="rem:cancel")]]
            ),
        )
    elif action == "list":
        await state.clear()
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
            await callback.message.answer("Твои напоминания (✏️ изменить, 🗑 удалить):", reply_markup=list_keyboard(reminders))
    await callback.answer()
