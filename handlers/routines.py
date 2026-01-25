import asyncio
import datetime

from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from utils.tone import tone_short_ack
from utils.user import ensure_user
from utils.gender import done_button_label

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

def _visible_steps(items: list[dict], done: set[int]) -> list[tuple[int, dict]]:
    index_by_id: dict[int, int] = {}
    for idx, raw in enumerate(items):
        try:
            index_by_id[int(raw["id"])] = idx
        except Exception:
            continue
    visible: list[tuple[int, dict]] = []
    for idx, raw in enumerate(items):
        it = dict(raw)
        if not it.get("is_active", 1):
            continue
        parent_id = it.get("trigger_after_step_id")
        if parent_id:
            parent_idx = index_by_id.get(int(parent_id))
            if parent_idx is None or parent_idx not in done:
                continue
        visible.append((idx, it))
    return visible


def _routine_keyboard(
    user: dict,
    routine_id: int,
    local_date: str,
    items: list[dict],
    done_items: set[int],
    status: str,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=done_button_label(user),
                callback_data=f"routine:{routine_id}:{local_date}:done",
            ),
            InlineKeyboardButton(
                text="Позже",
                callback_data=f"routine:{routine_id}:{local_date}:later",
            ),
            InlineKeyboardButton(
                text="Пропустить",
                callback_data=f"routine:{routine_id}:{local_date}:skip",
            ),
        ]
    ]
    for idx, item in _visible_steps(items, done_items):
        mark = "☑️" if idx in done_items else "⬜️"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark} {item['title'][:24]}",
                    callback_data=f"ritem:{routine_id}:{local_date}:{idx}",
                )
            ]
        )
    if status != "done":
        rows.append(
            [
                InlineKeyboardButton(
                    text="Закончить без отметок",
                    callback_data=f"ritemfinish:{routine_id}:{local_date}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _routine_text(title: str, local_time: str, items: list[dict], done_items: set[int]) -> str:
    lines = []
    has_pills = False
    for idx, item in _visible_steps(items, done_items):
        title_l = (item.get("title") or "").lower()
        if "таблет" in title_l or "витамин" in title_l:
            has_pills = True
        if idx in done_items:
            lines.append(f"• <s>{item['title']}</s>")
        else:
            lines.append(f"• {item['title']}")
    tasks = "\n".join(lines)
    footer = "\n\nЕсли сил мало — выбери один пункт. Этого уже достаточно.\n\nОтметь статус:"
    if has_pills:
        footer += (
            "\n\nНапоминание про таблетки — только чтобы не забыть. "
            "Если что-то меняешь в приёме или чувствуешь себя хуже обычного, лучше обсуди это с врачом."
        )
    return f"🕒 {title} ({local_time})\n\n{tasks}" + footer


async def _remind_later(bot, conn, user, routine_id: int, local_date: str):
    await asyncio.sleep(30 * 60)
    task = await repo.get_user_task(conn, user["id"], routine_id, local_date)
    done = _parse_done_list(task["note"]) if task and task["note"] else set()
    items = [
        dict(i)
        for i in await repo.list_routine_steps_for_routine(
            conn, user["id"], routine_id, include_inactive=True
        )
    ]
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
        reply_markup=_routine_keyboard(user, routine_id, local_date, items, done, status=(task["status"] if task else "pending")),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("routine:"))
async def routine_action(callback: CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)

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
    affirm_mode = (wellness_row or {}).get("affirm_mode", "off")
    status_map = {"done": "done", "skip": "skip", "later": "later"}
    status = status_map.get(action, "pending")
    await repo.upsert_user_task(
        db, user["id"], routine_id, local_date, status=status
    )

    if action == "done":
        await callback.answer("Готово. Рутину закрыли — очки по отмеченным пунктам.")
    elif action == "skip":
        await callback.answer("Ок, пропустим. Завтра попробуем снова.")
    elif action == "later":
        await callback.answer("Ок, напомню через 30 минут.")
        asyncio.create_task(
            _remind_later(callback.message.bot, db, user, routine_id, local_date)
        )
    else:
        await callback.answer("Статус обновлён")

    # Обновить текст напоминания, если это было оно
    try:
        task = await repo.get_user_task(db, user["id"], routine_id, local_date)
        done = _parse_done_list(task["note"]) if task and task["note"] else set()
        items = [
            dict(i)
            for i in await repo.list_routine_steps_for_routine(
                db, user["id"], routine_id, include_inactive=True
            )
        ]
        user_routine = await repo.get_user_routine(db, user["id"], routine_id)
        reminder_time = ""
        title = routine.get("title", "Рутина")
        if user_routine:
            reminder_time = user_routine["reminder_time"] or user_routine.get("default_time") or ""
            title = user_routine.get("title", title)
        text = await _routine_text(title, reminder_time, items, done)
        kb = _routine_keyboard(user, routine_id, local_date, items, done, status=(task["status"] if task else "pending"))
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
