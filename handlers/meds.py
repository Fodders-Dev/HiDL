import asyncio
import datetime
import logging
from typing import List

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.time import local_date_str
from utils.user import ensure_user
from utils import texts
from utils.vitamins import vitamin_names, get_vitamin

router = Router()
logger = logging.getLogger(__name__)


class MedState(StatesGroup):
    name = State()
    dose = State()
    schedule = State()
    times = State()


def _meds_menu_keyboard(meds_rows) -> InlineKeyboardMarkup:
    rows = []
    for m in meds_rows:
        row = dict(m)
        status = "✅" if row.get("active") else "❌"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status} {row.get('name')[:24]}",
                    callback_data=f"med:toggle:{row['id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Добавить", callback_data="med:add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("meds"))
async def meds_menu(message: types.Message, db) -> None:
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    meds_rows = await repo.list_meds(db, user["id"], active_only=False)
    if not meds_rows:
        await message.answer(
            "Витамины и таблетки пока не заведены.\n"
            "Нажми «Добавить», чтобы я напоминала о чём-то конкретном.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить", callback_data="med:add")]]
            ),
        )
        return
    lines = ["Твои витамины и таблетки:"]
    for m in meds_rows:
        row = dict(m)
        status = "активно" if row.get("active") else "выключено"
        times = row.get("times", "")
        dose = row.get("dose_text", "")
        lines.append(f"• {row.get('name')} — {dose}, в {times} ({status})")
    await message.answer("\n".join(lines), reply_markup=_meds_menu_keyboard(meds_rows))


@router.callback_query(lambda c: c.data and c.data.startswith("med:"))
async def meds_callbacks(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    if action == "add":
        await state.set_state(MedState.name)
        await callback.message.answer(
            "Как называется то, о чём тебе напоминать? Например: магний, витамин D, таблетки от давления.",
            reply_markup=main_menu_keyboard(),
        )
        await callback.answer()
        return
    if action == "toggle" and len(parts) > 2:
        med_id = int(parts[2])
        med = await repo.get_med(db, med_id)
        if not med:
            await callback.answer("Не нашла запись.", show_alert=True)
            return
        active = bool(med["active"])
        await repo.set_med_active(db, med_id, not active)
        await callback.answer("Обновила.")
        meds_rows = await repo.list_meds(db, user["id"], active_only=False)
        try:
            await callback.message.edit_reply_markup(reply_markup=_meds_menu_keyboard(meds_rows))
        except Exception:
            pass
        return
    await callback.answer()


@router.message(MedState.name)
async def med_name(message: types.Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не должно быть пустым.")
        return
    await state.update_data(name=name)
    await state.set_state(MedState.dose)
    await message.answer(
        "Сколько и в какой форме ты это принимаешь? Например: 1 таблетка, 5 капель, половина таблетки.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(MedState.dose)
async def med_dose(message: types.Message, state: FSMContext) -> None:
    dose = (message.text or "").strip()
    await state.update_data(dose=dose)
    await state.set_state(MedState.schedule)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1 раз в день", callback_data="medsched:once"),
                InlineKeyboardButton(text="2 раза в день", callback_data="medsched:twice"),
            ],
            [
                InlineKeyboardButton(text="Свой режим", callback_data="medsched:custom"),
            ],
        ]
    )
    await message.answer(
        "Сколько раз в день ты принимаешь это обычно?",
        reply_markup=kb,
    )


@router.callback_query(lambda c: c.data and c.data.startswith("medsched:"))
async def med_schedule(callback: types.CallbackQuery, state: FSMContext) -> None:
    choice = callback.data.split(":")[1]
    await state.update_data(schedule_type=choice)
    await state.set_state(MedState.times)
    if choice == "once":
        await callback.message.answer(
            "Введи время в формате HH:MM, например 09:00.",
            reply_markup=main_menu_keyboard(),
        )
    elif choice == "twice":
        await callback.message.answer(
            "Введи два времени через запятую, например 09:00,21:00.",
            reply_markup=main_menu_keyboard(),
        )
    else:
        await callback.message.answer(
            "Введи 1–3 времени через запятую, например 08:00,14:00,21:00.",
            reply_markup=main_menu_keyboard(),
        )
    await callback.answer()


def _parse_times(raw: str) -> List[str]:
    from utils.time import parse_hhmm

    parts = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if not parse_hhmm(piece):
            raise ValueError
        hh, mm = piece.split(":")
        parts.append(f"{int(hh):02d}:{int(mm):02d}")
    if not parts:
        raise ValueError
    return parts


@router.message(MedState.times)
async def med_times(message: types.Message, state: FSMContext, db) -> None:
    data = await state.get_data()
    schedule_type = data.get("schedule_type", "once")
    raw = (message.text or "").strip()
    try:
        times = _parse_times(raw)
    except Exception:
        await message.answer(
            texts.error("время нужно в формате HH:MM, например 09:00 или 09:00,21:00."),
        )
        return
    # нормализуем schedule_type относительно количества времён
    if schedule_type == "once" and len(times) > 1:
        schedule_type = "custom_times"
    if schedule_type == "twice" and len(times) == 1:
        schedule_type = "once_daily"
    mapping = {
        "once": "once_daily",
        "twice": "twice_daily",
        "custom": "custom_times",
    }
    schedule_type = mapping.get(schedule_type, schedule_type)
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    name = data.get("name", "Добавка")
    dose = data.get("dose", "")
    times_str = ",".join(times)
    med_id = await repo.create_med(
        db,
        user["id"],
        name=name,
        dose_text=dose,
        schedule_type=schedule_type,
        times=times_str,
        days_of_week=None,
        notes="",
    )
    await state.clear()
    await message.answer(
        f"Ок, буду напоминать про «{name}»: {dose or 'по одной дозе'} в {times_str}.",
        reply_markup=main_menu_keyboard(),
    )
    logger.info(
        "med.created",
        extra={"user_id": user["id"], "med_id": med_id, "schedule_type": schedule_type, "times": times},
    )


async def _med_later(bot, db, log_id: int) -> None:
    await asyncio.sleep(30 * 60)
    log = await repo.get_med_log(db, log_id)
    if not log or log["taken_at"]:
        return
    med = await repo.get_med(db, log["med_id"])
    if not med:
        return
    user = await repo.get_user(db, log["user_id"])
    if not user:
        return
    text = f"Напоминание позже: {med['name']} ({med['dose_text']})."
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Принял(а)", callback_data=f"medtake:{log_id}"),
                InlineKeyboardButton(text="Пропустить", callback_data=f"medskip:{log_id}"),
            ]
        ]
    )
    await bot.send_message(chat_id=user["telegram_id"], text=text, reply_markup=kb)


@router.callback_query(lambda c: c.data and (c.data.startswith("medtake:") or c.data.startswith("medskip:")))
async def med_take_or_skip(callback: types.CallbackQuery, db) -> None:
    log_id = int(callback.data.split(":")[1])
    log = await repo.get_med_log(db, log_id)
    if not log:
        await callback.answer("Не нашла напоминание.", show_alert=True)
        return
    if callback.data.startswith("medtake:"):
        await repo.set_med_taken(db, log_id, None)
        await callback.answer("Отметила приём.")
        logger.info("med.taken", extra={"log_id": log_id, "med_id": log["med_id"], "user_id": log["user_id"]})
    else:
        await callback.answer("Ок, пропускаем.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.message(Command("vitamins_info"))
async def vitamins_info(message: types.Message) -> None:
    names = vitamin_names()
    if not names:
        await message.answer(
            "У меня пока нет отдельного справочника по витаминам. "
            "Но главное: любые добавки лучше обсуждать с врачом, а не назначать себе самому.",
            reply_markup=main_menu_keyboard(),
        )
        return
    lines = [
        "Про какие витамины рассказать? Напиши название одним словом, например: витамин D, магний, B12.",
        "Сейчас доступны:",
        ", ".join(names),
        "",
        "Напоминание: я не врач. То, что организм «чувствует себя неважно», ещё не значит, что «не хватает витаминов». "
        "Если хочешь что-то принимать регулярно — лучше обсуди это с доктором.",
    ]
    await message.answer("\n".join(lines), reply_markup=main_menu_keyboard())


@router.message(lambda m: m.text and m.text.lower().startswith("витамин"))
async def vitamins_info_free(message: types.Message) -> None:
    name = (message.text or "").strip()
    info = get_vitamin(name)
    if not info:
        await message.answer(
            "Я не нашла такой витамин в своём маленьком справочнике. "
            "Но в любом случае любые добавки лучше обсуждать с врачом.",
            reply_markup=main_menu_keyboard(),
        )
        return
    sources = info.get("food_sources") or []
    note = info.get("short_note", "")
    lines = [f"Про {info.get('name')}:"]
    if sources:
        lines.append("Где его обычно ищут в еде:")
        lines.extend(f"• {s}" for s in sources)
    if note:
        lines.append("")
        lines.append(note)
    lines.append(
        "\nЯ не врач. То, что организм «чувствует себя неважно», ещё не значит, что «не хватает витаминов». "
        "Если хочешь что-то принимать регулярно — лучше обсуди это с доктором."
    )
    await message.answer("\n".join(lines), reply_markup=main_menu_keyboard())
