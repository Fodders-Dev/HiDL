import asyncio
import datetime

from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.tone import tone_short_ack
from utils.today import render_today
from utils.user import ensure_user

router = Router()


def _parse_done_list(note: str) -> set[int]:
    done = set()
    for part in note.split(","):
        try:
            done.add(int(part))
        except Exception:
            continue
    return done


def _note_from_set(done: set[int]) -> str:
    return ",".join(str(i) for i in sorted(done))


def _routine_keyboard(routine_id: int, local_date: str, items, done_items: set[int]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="Закончить",
                callback_data=f"routine:{routine_id}:{local_date}:done",
            ),
            InlineKeyboardButton(
                text="Напомнить позже",
                callback_data=f"routine:{routine_id}:{local_date}:later",
            ),
            InlineKeyboardButton(
                text="Пропустить",
                callback_data=f"routine:{routine_id}:{local_date}:skip",
            ),
        ]
    ]
    for idx, item in enumerate(items):
        mark = "☑️" if idx in done_items else "⬜️"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {item['title'][:24]}",
                    callback_data=f"ritem:{routine_id}:{local_date}:{idx}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _routine_text(title: str, local_time: str, items, done_items: set[int]) -> str:
    lines = []
    for idx, item in enumerate(items):
        if idx in done_items:
            lines.append(f"• <s>{item['title']}</s>")
        else:
            lines.append(f"• {item['title']}")
    tasks = "\n".join(lines)
    return f"🕒 {title} ({local_time})\n\n{tasks}\n\nОтметь статус:"


async def _remind_later(bot, conn, user, routine_id: int, local_date: str):
    await asyncio.sleep(30 * 60)
    task = await repo.get_user_task(conn, user["id"], routine_id, local_date)
    done = _parse_done_list(task["note"]) if task and task["note"] else set()
    items = await repo.get_routine_items(conn, routine_id)
    user_routine = await repo.get_user_routine(conn, user["id"], routine_id)
    reminder_time = ""
    title = "Рутина"
    if user_routine:
        reminder_time = user_routine["reminder_time"] or user_routine.get("default_time") or ""
        title = user_routine.get("title", title)
    text = await _routine_text(title, reminder_time or "позже", items, done)
    await bot.send_message(
        chat_id=user["telegram_id"],
        text="Напоминание позже:\n" + text,
        reply_markup=_routine_keyboard(routine_id, local_date, items, done),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("routine:"))
async def routine_action(callback: CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    # allow quick intents from text
    from utils.nlp import match_simple_intent
    intent = match_simple_intent(callback.message.text or "")

    _, routine_id, local_date, action = callback.data.split(":")
    routine_id = int(routine_id)
    routine_row = await repo.get_routine_by_id(db, routine_id)
    if not routine_row:
        await callback.answer("Рутина не найдена", show_alert=True)
        return
    routine = dict(routine_row)

    tone = "neutral"
    wellness_row = await repo.get_wellness(db, user["id"])
    if wellness_row:
        tone = wellness_row["tone"]
    status_map = {"done": "done", "skip": "skip", "later": "later"}
    status = status_map.get(action, "pending")
    await repo.upsert_user_task(
        db, user["id"], routine_id, local_date, status=status
    )

    if action == "done":
        await callback.answer("Отлично! Рутину закрыла, очки — по отмеченным пунктам.")
    elif action == "skip":
        await callback.answer("Пропустили, завтра попробуем снова.")
    elif action == "later":
        await callback.answer("Напомню через 30 минут.")
        asyncio.create_task(
            _remind_later(callback.message.bot, db, user, routine_id, local_date)
        )
    else:
        await callback.answer("Статус обновлён")

    # Обновить текст напоминания, если это было оно
    try:
        task = await repo.get_user_task(db, user["id"], routine_id, local_date)
        done = _parse_done_list(task["note"]) if task and task["note"] else set()
        items = await repo.get_routine_items(db, routine_id)
        user_routine = await repo.get_user_routine(db, user["id"], routine_id)
        reminder_time = ""
        title = routine.get("title", "Рутина")
        if user_routine:
            reminder_time = user_routine["reminder_time"] or user_routine.get("default_time") or ""
            title = user_routine.get("title", title)
        text = await _routine_text(title, reminder_time, items, done)
        kb = _routine_keyboard(routine_id, local_date, items, done)
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
