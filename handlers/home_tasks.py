import datetime
from typing import List, Optional

import aiosqlite

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.time import format_date_display, local_date_str
from utils.user import ensure_user

router = Router()


class CleanNowState(StatesGroup):
    choose_type = State()
    choose_energy = State()
    process = State()


class HomeFreqState(StatesGroup):
    wait_custom = State()


def _regular_keyboard(tasks):
    rows = []
    for t in tasks:
        from utils.rows import row_to_dict
        row = row_to_dict(t)
        status_icon = "✅ " if row.get("last_done_date") else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status_icon}{row['title']}", callback_data=f"hweek:done:{row['id']}"
                ),
                InlineKeyboardButton(text="⏭ +1", callback_data=f"hweek:later:1:{row['id']}"),
                InlineKeyboardButton(text="+3", callback_data=f"hweek:later:3:{row['id']}"),
                InlineKeyboardButton(text="+7", callback_data=f"hweek:later:7:{row['id']}"),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _all_tasks_keyboard(tasks):
    rows = []
    for t in tasks:
        row = dict(t)
        rows.append(
            [
                InlineKeyboardButton(text="✅", callback_data=f"hall:done:{row['id']}"),
                InlineKeyboardButton(text="⚙️ Частота", callback_data=f"hall:freq:{row['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"hall:hide:{row['id']}"),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _freq_presets_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="7", callback_data=f"hall:freqset:{task_id}:7"),
                InlineKeyboardButton(text="14", callback_data=f"hall:freqset:{task_id}:14"),
                InlineKeyboardButton(text="30", callback_data=f"hall:freqset:{task_id}:30"),
                InlineKeyboardButton(text="90", callback_data=f"hall:freqset:{task_id}:90"),
            ],
            [InlineKeyboardButton(text="Своя", callback_data=f"hall:freqset:{task_id}:custom")],
        ]
    )


def _format_task_line(t) -> str:
    from utils.rows import row_to_dict
    row = row_to_dict(t)
    status = "✅" if row.get("last_done_date") else "⏳"
    return f"{status} {row['title']} — каждые {row['frequency_days']} д., до {format_date_display(row['next_due_date'])}"


async def show_week_plan(message: types.Message, db) -> None:
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    await repo.ensure_regular_tasks(db, user["id"], today)
    tasks = await repo.list_regular_tasks(
        db, user["id"], local_date=today, due_in_days=7, include_inactive=False
    )
    if not tasks:
        await message.answer("План по дому на неделю: пока ничего срочного, можно выдохнуть.", reply_markup=main_menu_keyboard())
        return
    lines = ["План по дому на ближайшие 7 дней:"]
    for t in tasks:
        lines.append(f"• До {format_date_display(t['next_due_date'])} — {t['title']}")
    kb = _regular_keyboard(tasks)
    await message.answer("\n".join(lines), reply_markup=kb or main_menu_keyboard())


async def show_all_tasks(message: types.Message, db) -> None:
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    await repo.ensure_regular_tasks(db, user["id"], today)
    tasks = await repo.list_regular_tasks(db, user["id"], due_only=False, include_inactive=False)
    if not tasks:
        await message.answer("Пока нет дел по дому.", reply_markup=main_menu_keyboard())
        return
    lines = ["Все дела по дому:"]
    for t in tasks:
        lines.append(_format_task_line(t))
    kb = _all_tasks_keyboard(tasks)
    await message.answer("\n".join(lines), reply_markup=kb or main_menu_keyboard())


@router.callback_query(lambda c: c.data and c.data.startswith("hweek:done:"))
async def plan_mark_done(callback: types.CallbackQuery, db) -> None:
    task_id = int(callback.data.split(":")[2])
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    tasks = await repo.list_regular_tasks(db, user["id"], due_only=False, include_inactive=False)
    task = next((t for t in tasks if t["id"] == task_id), None)
    await repo.mark_regular_done(db, user["id"], task_id, today)
    if task is not None:
        task = dict(task)
    pts = (task.get("points") if task else 3) or 3
    await repo.add_points(db, user["id"], pts, local_date=today)
    await callback.answer("Готово")
    await _refresh_plan(callback, db)


async def _refresh_plan(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    tasks = await repo.list_regular_tasks(db, user["id"], local_date=today, due_in_days=7, include_inactive=False)
    if not tasks:
        try:
            await callback.message.edit_text("План по дому на неделю пуст — всё чисто.", reply_markup=None)
        except Exception:
            await callback.message.answer("План по дому на неделю пуст — всё чисто.", reply_markup=main_menu_keyboard())
        return
    lines = ["План по дому на ближайшие 7 дней:"]
    for t in tasks:
        row = dict(t)
        lines.append(f"• До {format_date_display(row['next_due_date'])} — {row['title']}")
    kb = _regular_keyboard(tasks)
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    except Exception:
        await callback.message.answer("\n".join(lines), reply_markup=kb or main_menu_keyboard())


@router.callback_query(lambda c: c.data and c.data.startswith("hweek:later:"))
async def plan_postpone(callback: types.CallbackQuery, db) -> None:
    _, _, days, task_id = callback.data.split(":")
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.postpone_regular_task(db, user["id"], int(task_id), int(days))
    await callback.answer(f"Отложила на +{days} д.")
    await _refresh_plan(callback, db)


@router.callback_query(lambda c: c.data and c.data.startswith("hall:done:"))
async def all_done(callback: types.CallbackQuery, db) -> None:
    task_id = int(callback.data.split(":")[2])
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    tasks = await repo.list_regular_tasks(db, user["id"], due_only=False, include_inactive=False)
    task = next((t for t in tasks if t["id"] == task_id), None)
    await repo.mark_regular_done(db, user["id"], task_id, today)
    if task is not None:
        task = dict(task)
    pts = (task.get("points") if task else 3) or 3
    await repo.add_points(db, user["id"], pts, local_date=today)
    await callback.answer("Отметила")
    await _refresh_all(callback, db)


@router.callback_query(lambda c: c.data and c.data.startswith("hall:hide:"))
async def all_hide(callback: types.CallbackQuery, db) -> None:
    task_id = int(callback.data.split(":")[2])
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.deactivate_regular_task(db, user["id"], task_id)
    await callback.answer("Скрыла задачу")
    await _refresh_all(callback, db)


@router.callback_query(lambda c: c.data and c.data.startswith("hall:freq:"))
async def all_freq(callback: types.CallbackQuery, state: FSMContext) -> None:
    task_id = int(callback.data.split(":")[2])
    await callback.message.answer("Выбери новую частоту (дни) или введи свою цифру сообщением.", reply_markup=_freq_presets_keyboard(task_id))
    await state.update_data(freq_task_id=task_id)
    await state.set_state(HomeFreqState.wait_custom)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("hall:freqset:"))
async def freq_set(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    _, _, task_id, days = callback.data.split(":")
    if days == "custom":
        await callback.answer()
        return
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.set_regular_frequency(db, user["id"], int(task_id), int(days))
    await callback.answer("Обновила частоту")
    await _refresh_all(callback, db)
    await state.clear()


@router.message(HomeFreqState.wait_custom)
async def freq_custom(message: types.Message, state: FSMContext, db) -> None:
    data = await state.get_data()
    task_id = data.get("freq_task_id")
    try:
        days = int(message.text.strip())
    except Exception:
        await message.answer("Нужно число дней, например 14.")
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.set_regular_frequency(db, user["id"], int(task_id), days)
    await message.answer(f"Частота обновлена: каждые {days} дней.")
    await state.clear()
    await show_all_tasks(message, db)


async def _refresh_all(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    tasks = await repo.list_regular_tasks(db, user["id"], due_only=False, include_inactive=False)
    if not tasks:
        await callback.message.edit_text("Пока нет дел по дому.", reply_markup=None)
        return
    lines = ["Все дела по дому:"]
    for t in tasks:
        lines.append(_format_task_line(t))
    kb = _all_tasks_keyboard(tasks)
    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    except Exception:
        await callback.message.answer("\n".join(lines), reply_markup=kb or main_menu_keyboard())


# --- Уборка сейчас ---

def _clean_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✨ Быстрый порядок", callback_data="clean:type:surface")],
            [InlineKeyboardButton(text="🧹 Нормальная уборка", callback_data="clean:type:normal")],
            [InlineKeyboardButton(text="🧽 Одна зона поглубже", callback_data="clean:type:deep")],
        ]
    )


def _clean_energy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Почти не живая", callback_data="clean:energy:low")],
            [InlineKeyboardButton(text="Могу нормально", callback_data="clean:energy:mid")],
            [InlineKeyboardButton(text="Готова поработать", callback_data="clean:energy:high")],
        ]
    )


async def start_clean_now(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CleanNowState.choose_type)
    await callback.message.answer("Что делаем по дому?", reply_markup=_clean_type_keyboard())
    await callback.answer()


def _surface_steps(energy: str) -> List[dict]:
    steps = [
        {"text": "Собери одежду в одну корзину/стопку", "points": 1},
        {"text": "Протри стол или главную поверхность", "points": 1},
        {"text": "Разгрузи раковину или замочи посуду", "points": 1},
        {"text": "Вынеси мусор, если ведро полное", "points": 2},
    ]
    target = 3 if energy == "low" else (4 if energy == "mid" else 5)
    return steps[:target]


def _zone_steps(zone: str, energy: str) -> List[dict]:
    base = {
        "kitchen": [
            {"text": "Разобрать одну полку/ящик на кухне", "points": 2},
            {"text": "Протереть фасады шкафов и ручки", "points": 2},
            {"text": "Плита/стол: протереть жирные пятна", "points": 2},
            {"text": "Пол/плинтус в кухне быстро пройтись", "points": 3},
        ],
        "bathroom": [
            {"text": "Протереть раковину и кран", "points": 2},
            {"text": "Быстро пройтись по унитазу/сиденью", "points": 2},
            {"text": "Душ/ванна: ополоснуть стены, протереть уголки", "points": 3},
            {"text": "Сменить полотенца, проветрить", "points": 2},
        ],
        "room": [
            {"text": "Разобрать одну поверхность (стол/тумба)", "points": 2},
            {"text": "Собрать мелочи в коробку «разобрать позже»", "points": 1},
            {"text": "Пропылесосить/пройтись влажной салфеткой под кроватью/диваном", "points": 3},
            {"text": "Протереть пыль на видимых местах", "points": 2},
        ],
        "hallway": [
            {"text": "Разложить обувь, убрать грязь у входа", "points": 2},
            {"text": "Протереть зеркало/полку в прихожей", "points": 1},
            {"text": "Быстро пройтись пылесосом/шваброй у входа", "points": 3},
        ],
    }
    steps = base.get(zone, base["room"])
    target = 3 if energy == "low" else (4 if energy == "mid" else 5)
    return steps[:target]


def _normal_steps(home_tasks: List[aiosqlite.Row], energy: str) -> List[dict]:
    steps: List[dict] = []
    for t in home_tasks[:2]:
        row = dict(t)
        points = row.get("points") or 3
        steps.append({"text": f"{row['title']} (по плану)", "points": points, "task_id": row["id"]})
    steps.extend(_surface_steps(energy))
    target = 4 if energy == "low" else (5 if energy == "mid" else 7)
    return steps[:target]


async def _build_steps(db, user_id: int, energy: str, clean_type: str, today: str) -> List[dict]:
    tasks = await repo.list_regular_tasks(db, user_id, local_date=today, due_in_days=7, include_inactive=False)
    if clean_type == "surface":
        return _surface_steps(energy)
    if clean_type == "normal":
        return _normal_steps(tasks, energy)
    if tasks:
        first = dict(tasks[0])
        zone = first.get("zone") or "room"
    else:
        zone = "room"
    return _zone_steps(zone, energy)


def _steps_keyboard(steps: List[dict]) -> InlineKeyboardMarkup:
    rows = []
    for idx, step in enumerate(steps):
        status = step.get("status", "pending")
        label = "✅" if status == "done" else ("⏭" if status == "skip" else "•")
        rows.append(
            [
                InlineKeyboardButton(text=f"{label} {idx+1}", callback_data=f"clean:mark:done:{idx}"),
                InlineKeyboardButton(text="Пропустить", callback_data=f"clean:mark:skip:{idx}"),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _steps_text(steps: List[dict]) -> str:
    lines = ["Сделай эти шаги:"]
    for idx, step in enumerate(steps):
        status = step.get("status", "pending")
        prefix = "✅" if status == "done" else ("⏭" if status == "skip" else "•")
        lines.append(f"{prefix} {idx+1}. {step['text']}")
    return "\n".join(lines)


@router.callback_query(lambda c: c.data and c.data.startswith("clean:type:"))
async def clean_choose_energy(callback: types.CallbackQuery, state: FSMContext) -> None:
    clean_type = callback.data.split(":")[2]
    await state.update_data(clean_type=clean_type)
    await state.set_state(CleanNowState.choose_energy)
    await callback.message.answer("Сколько сил есть?", reply_markup=_clean_energy_keyboard())
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("clean:energy:"))
async def clean_generate(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    energy = callback.data.split(":")[2]
    data = await state.get_data()
    clean_type = data.get("clean_type", "surface")
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    steps = await _build_steps(db, user["id"], energy, clean_type, today)
    await state.update_data(steps=steps, energy=energy, today=today)
    text = _steps_text(steps)
    kb = _steps_keyboard(steps)
    await callback.message.answer(text, reply_markup=kb)
    await state.set_state(CleanNowState.process)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("clean:mark:"))
async def clean_mark(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    parts = callback.data.split(":")
    action = parts[2]
    idx = int(parts[3])
    data = await state.get_data()
    steps: List[dict] = data.get("steps", [])
    if idx >= len(steps):
        await callback.answer()
        return
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    today = data.get("today") or local_date_str(datetime.datetime.utcnow(), user["timezone"])
    step = steps[idx]
    if step.get("status") in ("done", "skip"):
        await callback.answer("Уже отмечено")
        return
    step["status"] = "done" if action == "done" else "skip"
    if action == "done":
        points = step.get("points", 2)
        await repo.add_points(db, user["id"], points, local_date=today)
        if step.get("task_id"):
            await repo.mark_regular_done(db, user["id"], step["task_id"], today)
    steps[idx] = step
    await state.update_data(steps=steps, today=today)
    pending = [s for s in steps if s.get("status") == "pending"]
    kb = _steps_keyboard(steps)
    text = _steps_text(steps)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    if not pending:
        done_cnt = len([s for s in steps if s.get("status") == "done"])
        total_points = sum(s.get("points", 0) for s in steps if s.get("status") == "done")
        await callback.message.answer(
            f"Ты закрыла {done_cnt} из {len(steps)} шагов, +{total_points} очков.\nДома уже заметно легче — можешь остановиться или сделать ещё один круг позже.",
            reply_markup=main_menu_keyboard(),
        )
        await state.clear()
    await callback.answer("Обновлено")


@router.callback_query(lambda c: c.data and c.data.startswith("care:"))
async def care_mark(callback: types.CallbackQuery, db) -> None:
    _, col, date_str = callback.data.split(":")
    user = await repo.get_user_by_telegram_id(db, callback.from_user.id)
    if not user:
        await callback.answer(register_text(), show_alert=True)
        return
    await repo.update_care_date(db, user["id"], col, date_str)
    await callback.answer("Отметила.")
    await callback.message.edit_text("Отметила заботу как выполненную.", reply_markup=None)


# Fallbacks на старые кнопки
@router.callback_query(lambda c: c.data and c.data.startswith("home:regular"))
async def home_regular_entry(callback: types.CallbackQuery, db) -> None:
    await show_week_plan(callback.message, db)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("home:regular_all"))
async def home_regular_all(callback: types.CallbackQuery, db) -> None:
    await show_all_tasks(callback.message, db)
    await callback.answer()


@router.message(Command("home_audit"))
async def home_audit(message: types.Message, db) -> None:
    await show_week_plan(message, db)


@router.message(Command("home_audit_setup"))
async def home_audit_setup(message: types.Message, state: FSMContext, db) -> None:
    """Упрощённый аудит: создаём базовые задачи и показываем план."""
    await state.clear()
    await show_week_plan(message, db)


@router.message(Command("home_plan"))
async def home_plan(message: types.Message, db) -> None:
    await show_week_plan(message, db)


# Алиасы для старых колбэков reg:*
@router.callback_query(lambda c: c.data and c.data.startswith("reg:"))
async def legacy_reg(callback: types.CallbackQuery, db) -> None:
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer()
        return
    action = parts[1]
    task_id = int(parts[2])
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    if action == "done":
        tasks = await repo.list_regular_tasks(db, user["id"], due_only=False, include_inactive=False)
        task = next((t for t in tasks if t["id"] == task_id), None)
        await repo.mark_regular_done(db, user["id"], task_id, today)
        if task is not None:
            task = dict(task)
        pts = (task.get("points") if task else 3) or 3
        await repo.add_points(db, user["id"], pts, local_date=today)
        await callback.answer("Готово")
    elif action.startswith("later"):
        days = 1
        if "later3" in action:
            days = 3
        elif "later7" in action:
            days = 7
        await repo.postpone_regular_task(db, user["id"], task_id, days)
        await callback.answer(f"Отложила на +{days} д.")
    await _refresh_plan(callback, db)
