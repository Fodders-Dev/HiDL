import datetime
from typing import List

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.time import local_date_str
from utils.user import ensure_user


router = Router()


class RoutineEditState(StatesGroup):
    wait_title = State()
    wait_rename = State()
    wait_trigger_parent = State()


ROUTINE_TITLES = {
    "morning": "Утро",
    "day": "День",
    "evening": "Вечер",
}


def _routine_type_from_id(routine_row) -> str:
    key = routine_row["routine_key"]
    if key in ("morning", "day", "evening"):
        return key
    return "day"


async def _render_steps(message: types.Message, db, user_id: int, routine_type: str) -> None:
    await repo.ensure_routine_steps(db, user_id)
    routine = await repo.get_routine_by_key(db, routine_type)
    if not routine:
        await message.answer("Не нашла такую рутину.")
        return
    steps_rows = await repo.list_routine_steps_for_routine(db, user_id, routine["id"], include_inactive=True)
    steps = [dict(s) for s in steps_rows]
    if not steps:
        await message.answer("В этой рутине пока нет шагов.", reply_markup=main_menu_keyboard())
        return
    title = ROUTINE_TITLES.get(routine_type, routine.get("title", routine_type))
    lines = [f"{title} — шаги:"]
    for idx, step in enumerate(steps, start=1):
        mark = "👁" if step.get("is_active") else "🚫"
        after = step.get("trigger_after_step_id")
        trigger_label = ""
        if after:
            parent = next((s for s in steps if s["id"] == after), None)
            if parent:
                trigger_label = f" (после «{parent['title'][:16]}»)"
        lines.append(f"{idx}. {mark} {step['title']}{trigger_label}")
    kb_rows: List[List[types.InlineKeyboardButton]] = []
    for step in steps:
        kb_rows.append(
            [
                types.InlineKeyboardButton(
                    text=("👁 " if step.get("is_active") else "🚫 ") + step["title"][:20],
                    callback_data=f"rstep:toggle:{step['id']}",
                ),
                types.InlineKeyboardButton(
                    text="✏️", callback_data=f"rstep:rename:{step['id']}"
                ),
                types.InlineKeyboardButton(
                    text="↪️ после шага", callback_data=f"rstep:trigger:{step['id']}"
                ),
            ]
        )
    kb_rows.append(
        [
            types.InlineKeyboardButton(
                text="➕ Добавить шаг", callback_data=f"rstep:add:{routine_type}"
            )
        ]
    )
    kb = types.InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await message.answer("\n".join(lines), reply_markup=kb)


@router.message(Command("routine_steps"))
async def routine_steps_entry(message: types.Message, db) -> None:
    """Редактирование шагов утренней/дневной/вечерней рутины."""
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    text = (
        "Настройка рутин.\n"
        "Выбери, какую рутину хочешь подправить:"
    )
    kb = types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(text="Утро", callback_data="rstep:routine:morning"),
                types.InlineKeyboardButton(text="День", callback_data="rstep:routine:day"),
                types.InlineKeyboardButton(text="Вечер", callback_data="rstep:routine:evening"),
            ]
        ]
    )
    await message.answer(text, reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("rstep:routine:"))
async def routine_steps_choose(callback: types.CallbackQuery, db) -> None:
    routine_type = callback.data.split(":")[2]
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await _render_steps(callback.message, db, user["id"], routine_type)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("rstep:toggle:"))
async def routine_step_toggle(callback: types.CallbackQuery, db) -> None:
    step_id = int(callback.data.split(":")[2])
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.toggle_routine_step(db, user["id"], step_id)
    routine = await repo.get_routine_by_step(db, user["id"], step_id)
    routine_type = routine["routine_type"] if routine else "day"
    await _render_steps(callback.message, db, user["id"], routine_type)
    await callback.answer("Переключила шаг.")


@router.callback_query(lambda c: c.data and c.data.startswith("rstep:add:"))
async def routine_step_add_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    routine_type = callback.data.split(":")[2]
    await state.update_data(routine_type=routine_type)
    await state.set_state(RoutineEditState.wait_title)
    await callback.message.answer(
        "Напиши новый шаг для этой рутины. Например: выпить таблетки, проверить сумку или подготовить одежду.",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer()


@router.message(RoutineEditState.wait_title)
async def routine_step_add_finish(message: types.Message, state: FSMContext, db) -> None:
    data = await state.get_data()
    routine_type = data.get("routine_type", "day")
    title = (message.text or "").strip()
    if not title:
        await message.answer("Пустой шаг не подойдёт. Напиши короткое действие, которое тебе поможет.")
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.add_routine_step(db, user["id"], routine_type, title, after_step_id=None, points=1)
    await state.clear()
    await message.answer("Добавила шаг в рутину.", reply_markup=main_menu_keyboard())
    await _render_steps(message, db, user["id"], routine_type)


@router.callback_query(lambda c: c.data and c.data.startswith("rstep:rename:"))
async def routine_step_rename_start(callback: types.CallbackQuery, state: FSMContext) -> None:
    step_id = int(callback.data.split(":")[2])
    await state.update_data(rename_step_id=step_id)
    await state.set_state(RoutineEditState.wait_rename)
    await callback.message.answer(
        "Напиши новый текст для этого шага. Старайся коротко и по делу.",
        reply_markup=main_menu_keyboard(),
    )
    await callback.answer()


@router.message(RoutineEditState.wait_rename)
async def routine_step_rename_finish(message: types.Message, state: FSMContext, db) -> None:
    data = await state.get_data()
    step_id = data.get("rename_step_id")
    if not step_id:
        await state.clear()
        await message.answer("Не получилось обновить шаг, попробуем ещё раз позже.")
        return
    title = (message.text or "").strip()
    if not title:
        await message.answer("Нужно хоть пару слов — иначе не понятно, что делать.")
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.update_routine_step_title(db, user["id"], int(step_id), title)
    routine = await repo.get_routine_by_step(db, user["id"], int(step_id))
    routine_type = routine["routine_type"] if routine else "day"
    await state.clear()
    await message.answer("Переименовала шаг.", reply_markup=main_menu_keyboard())
    await _render_steps(message, db, user["id"], routine_type)


@router.callback_query(lambda c: c.data and c.data.startswith("rstep:trigger:"))
async def routine_step_trigger_start(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    step_id = int(callback.data.split(":")[2])
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    routine = await repo.get_routine_by_step(db, user["id"], step_id)
    if not routine:
        await callback.answer()
        return
    routine_type = routine["routine_type"]
    steps_rows = await repo.list_routine_steps_for_routine(db, user["id"], routine["routine_id"], include_inactive=True)
    steps = [dict(s) for s in steps_rows]
    kb_rows: List[List[types.InlineKeyboardButton]] = []
    kb_rows.append(
        [types.InlineKeyboardButton(text="Всегда показывать", callback_data=f"rstep:trigger_set:{step_id}:0")]
    )
    for s in steps:
        if s["id"] == step_id:
            continue
        kb_rows.append(
            [
                types.InlineKeyboardButton(
                    text=s["title"][:24],
                    callback_data=f"rstep:trigger_set:{step_id}:{s['id']}",
                )
            ]
        )
    kb = types.InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await callback.message.answer(
        "После какого шага показывать этот пункт? Можно выбрать «Всегда» или привязать к другому шагу.",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("rstep:trigger_set:"))
async def routine_step_trigger_set(callback: types.CallbackQuery, db) -> None:
    _, _, step_id_str, parent_id_str = callback.data.split(":")
    step_id = int(step_id_str)
    parent_id = int(parent_id_str)
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    trigger_id = None if parent_id == 0 else parent_id
    await repo.set_routine_step_trigger(db, user["id"], step_id, trigger_id)
    routine = await repo.get_routine_by_step(db, user["id"], step_id)
    routine_type = routine["routine_type"] if routine else "day"
    await callback.message.answer("Обновила привязку шага.", reply_markup=main_menu_keyboard())
    await _render_steps(callback.message, db, user["id"], routine_type)
    await callback.answer()

