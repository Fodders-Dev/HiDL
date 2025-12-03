import datetime
from typing import List

from aiogram import Router, types

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.tone import tone_ack


def _visible_steps(items: list[dict], done: set[int]) -> list[tuple[int, dict]]:
    """
    Вернуть список (индекс, шаг) только для тех шагов, которые нужно показывать.

    Шаг с trigger_after_step_id появляется только после того, как
    родительский шаг отмечен выполненным.
    """
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


def _render_routine_text(title: str, reminder_time: str, items, done: set[int]) -> str:
    text_lines = []
    visible = _visible_steps(items, done)
    has_pills = False
    for idx, it in visible:
        title_l = (it.get("title") or "").lower()
        if "таблет" in title_l or "витамин" in title_l:
            has_pills = True
        if idx in done:
            text_lines.append(f"• <s>{it['title']}</s>")
        else:
            text_lines.append(f"• {it['title']}")
    header = f"🕒 {title} ({reminder_time})\n\n" + "\n".join(text_lines)
    footer = "\n\nОтметь статус:"
    if has_pills:
        footer += (
            "\n\nНапоминание про таблетки — это только чтобы не забыть. "
            "Если что-то меняешь в приёме или чувствуешь себя хуже обычного, лучше обсуди это с врачом."
        )
    return header + footer


def _build_routine_keyboard(
    routine_id: int, local_date: str, items, done: set[int], status: str
) -> types.InlineKeyboardMarkup:
    kb_rows = [
        [
            types.InlineKeyboardButton(text="Сделал(а) ✔", callback_data=f"routine:{routine_id}:{local_date}:done"),
            types.InlineKeyboardButton(text="Напомнить позже", callback_data=f"routine:{routine_id}:{local_date}:later"),
            types.InlineKeyboardButton(text="Пропустить", callback_data=f"routine:{routine_id}:{local_date}:skip"),
        ]
    ]
    for idx, it in _visible_steps(items, done):
        mark = "☑️" if idx in done else "⬜️"
        kb_rows.append(
            [
                types.InlineKeyboardButton(
                    text=f"{mark} {it['title'][:24]}",
                    callback_data=f"ritem:{routine_id}:{local_date}:{idx}",
                )
            ]
        )
    if status != "done":
        kb_rows.append(
            [
                types.InlineKeyboardButton(
                    text="Закончить сейчас", callback_data=f"ritemfinish:{routine_id}:{local_date}"
                )
            ]
        )
    return types.InlineKeyboardMarkup(inline_keyboard=kb_rows)
from utils.today import render_today
from utils.user import ensure_user

router = Router()


@router.callback_query(lambda c: c.data and c.data.startswith("ritem:"))
async def routine_item_toggle(callback: types.CallbackQuery, db) -> None:
    _, routine_id, local_date, idx = callback.data.split(":")
    routine_id = int(routine_id)
    idx = int(idx)
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    task = await repo.get_user_task(db, user["id"], routine_id, local_date)
    done = set()
    if task and task["note"]:
        for part in task["note"].split(","):
            try:
                done.add(int(part))
            except Exception:
                continue
    prev_status = task["status"] if task else "pending"
    added = False
    if idx in done:
        done.remove(idx)
    else:
        done.add(idx)
        added = True
        await repo.add_points(db, user["id"], 1, local_date=local_date)
    await repo.update_task_note(db, user["id"], routine_id, local_date, ",".join(str(i) for i in sorted(done)))
    items_rows = await repo.list_routine_steps_for_routine(db, user["id"], routine_id, include_inactive=True)
    items = [dict(i) for i in items_rows]
    routine_row = await repo.get_user_routine(db, user["id"], routine_id)
    routine = dict(routine_row) if routine_row else {}
    reminder_time = routine.get("reminder_time") or routine.get("default_time") or ""
    title = routine.get("title", "Рутина")
    # автозакрытие рутины, если все видимые пункты отмечены
    new_status = prev_status
    visible = _visible_steps(items, done)
    visible_indices = {idx for idx, _ in visible}
    if visible_indices and visible_indices.issubset(done) and prev_status != "done":
        await repo.upsert_user_task(db, user["id"], routine_id, local_date, status="done")
        new_status = "done"
    text = _render_routine_text(title, reminder_time, items, done)
    kb = _build_routine_keyboard(routine_id, local_date, items, done, new_status)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer("Отметила." if added else "Сняла отметку.")
    # если все видимые шаги закрыты — отправить краткое подтверждение и убрать клавиатуру
    if visible_indices and visible_indices.issubset(done):
        try:
            await callback.message.edit_text(f"🕒 {title} завершена ✔", reply_markup=None)
        except Exception:
            await callback.message.answer(f"🕒 {title} завершена ✔", reply_markup=None)


@router.callback_query(lambda c: c.data and c.data.startswith("ritemfinish:"))
async def routine_finish(callback: types.CallbackQuery, db) -> None:
    _, routine_id, local_date = callback.data.split(":")
    routine_id = int(routine_id)
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    task = await repo.get_user_task(db, user["id"], routine_id, local_date)
    done = set()
    if task and task["note"]:
        for part in task["note"].split(","):
            try:
                done.add(int(part))
            except Exception:
                continue
    items = [
        dict(i)
        for i in await repo.list_routine_steps_for_routine(
            db, user["id"], routine_id, include_inactive=True
        )
    ]
    visible = _visible_steps(items, done)
    # начислить очки за отмеченные видимые пункты
    points = len([idx for idx, _ in visible if idx in done])
    if points > 0:
        await repo.add_points(db, user["id"], points, local_date=local_date)
    await repo.upsert_user_task(db, user["id"], routine_id, local_date, status="done")
    routine_row = await repo.get_user_routine(db, user["id"], routine_id)
    routine = dict(routine_row) if routine_row else {}
    title = routine.get("title", "Рутина")
    await callback.answer("Закончила.")
    await callback.message.edit_text(
        tone_ack("soft", f"{title} завершена, +{points} очков"), reply_markup=main_menu_keyboard()
    )
