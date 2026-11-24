import datetime
import html
from typing import List, Optional
import json

import aiosqlite

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import home_menu_keyboard, main_menu_keyboard
from services import household_service
from utils.sender import safe_edit, safe_edit_markup
from utils.rows import row_to_dict, rows_to_dicts
from utils.time import format_date_display, local_date_str
from utils.user import ensure_user
from utils.texts import register_text

router = Router()


class CleanNowState(StatesGroup):
    choose_type = State()
    choose_energy = State()
    choose_zone = State()
    process = State()


class QuickCleanState(StatesGroup):
    active = State()


def _init_steps(steps: List[dict]) -> List[dict]:
    """Ensure each step carries status pending by default."""
    out: List[dict] = []
    for step in steps:
        s = dict(step)
        s.setdefault("status", "pending")
        out.append(s)
    return out


class HomeFreqState(StatesGroup):
    wait_custom = State()


def _regular_keyboard(tasks):
    rows = []
    for t in tasks:
        row = row_to_dict(t)
        if not row.get("title"):
            continue
        if not row.get("id"):
            continue
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
    rows.append([InlineKeyboardButton(text="📋 Все дела по дому", callback_data="home:all")])
    rows.append([InlineKeyboardButton(text="⬅️ Дом", callback_data="home:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Дом", callback_data="home:menu")]]
    )


def _all_tasks_keyboard(tasks):
    rows = []
    for t in tasks:
        row = row_to_dict(t)
        zone = row.get("zone") or "misc"
        short = _zone_icon(zone)
        title_short = str(row.get("title") or "Дело")[:18]
        rows.append(
            [
                InlineKeyboardButton(text=f"{short} ✅ {title_short}", callback_data=f"hall:done:{row['id']}"),
                InlineKeyboardButton(text="⚙️ Частота", callback_data=f"hall:freq:{row['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"hall:hide:{row['id']}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Дом", callback_data="home:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Дом", callback_data="home:menu")]]
    )


def _paginate_tasks(tasks: list[dict], page: int, per_page: int = 6) -> tuple[list[dict], InlineKeyboardMarkup | None]:
    """Разбить список задач на страницы и вернуть клавиатуру навигации."""
    total_pages = max(1, (len(tasks) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    slice_tasks = tasks[start : start + per_page]
    kb_rows = []
    for t in slice_tasks:
        zone = t.get("zone") or "misc"
        short = _zone_icon(zone)
        title_short = str(t.get("title") or "Дело")[:18]
        kb_rows.append(
            [
                InlineKeyboardButton(text=f"{short} ✅ {title_short}", callback_data=f"hall:done:{t['id']}"),
                InlineKeyboardButton(text="⚙️ Частота", callback_data=f"hall:freq:{t['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"hall:hide:{t['id']}"),
            ]
        )
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"hall:page:{page-1}"))
        nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="hall:page:noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"hall:page:{page+1}"))
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton(text="⬅️ Дом", callback_data="home:menu")])
    return slice_tasks, InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None


def _zone_icon(zone: str) -> str:
    return {
        "kitchen": "🍳",
        "bathroom": "🚿",
        "bedroom": "🛏",
        "hallway": "🚪",
        "laundry": "🧺",
        "fridge": "🧊",
        "misc": "🧰",
    }.get(zone or "misc", "🧰")


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
    row = row_to_dict(t)
    if not row.get("title"):
        return ""
    status = "✅" if row.get("last_done_date") else "⏳"
    zone_icon = _zone_icon(row.get("zone"))
    freq = row.get("frequency_days") or row.get("freq_days") or 7
    next_due = row.get("next_due_date") or row.get("next_due") or ""
    next_disp = format_date_display(next_due) if next_due else "—"
    by_name = str(row.get("last_done_by_name") or "").strip()
    actor_note = f" · закрыл(а): {by_name}" if (row.get("last_done_date") and by_name) else ""
    return f"{status} {zone_icon} {row['title']} — каждые {freq} д., до {next_disp}{actor_note}"


async def _complete_regular_task_and_notify(db, user, callback: types.CallbackQuery, task_id: int, today: str) -> None:
    u_dict = dict(user)
    tasks = rows_to_dicts(await repo.list_regular_tasks(db, u_dict["id"], due_only=False, include_inactive=False))
    task = next((t for t in tasks if int(t.get("id") or 0) == int(task_id)), None)
    actor_name = str(u_dict.get("name") or "")
    await repo.mark_regular_done(db, u_dict["id"], int(task_id), today, actor_name=actor_name)
    pts = (task.get("points") if task else 3) or 3
    await repo.add_points(db, u_dict["id"], pts, local_date=today)
    if task:
        await household_service.notify_household_partners(
            getattr(callback, "bot", None),
            db,
            u_dict,
            f"🧹 <b>{html.escape(actor_name or 'Партнёр')}</b> закрыл(а) дело по дому: {html.escape(str(task.get('title') or ''))} ✅",
        )


async def show_week_plan(
    message: types.Message,
    db,
    tg_user_id: int | None = None,
    tg_full_name: str | None = None,
) -> None:
    uid = tg_user_id if tg_user_id is not None else message.from_user.id
    uname = tg_full_name if tg_full_name is not None else message.from_user.full_name
    user = await ensure_user(db, uid, uname)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    await repo.ensure_regular_tasks(db, user["id"], today)
    tasks = await repo.list_regular_tasks(
        db, user["id"], local_date=today, due_in_days=7, include_inactive=False
    )
    tasks = rows_to_dicts(tasks)
    if not tasks:
        await message.answer("План по дому на неделю: пока ничего срочного, можно выдохнуть.", reply_markup=main_menu_keyboard())
        return
    lines = ["План по дому на ближайшие 7 дней:"]
    for t in tasks[:7]:
        lines.append(f"• До {format_date_display(t['next_due_date'])} — {t['title']}")
    if len(tasks) > 7:
        lines.append(f"…и ещё {len(tasks) - 7} дел (показаны ближайшие).")
    kb = _regular_keyboard(tasks)
    await message.answer("\n".join(lines), reply_markup=kb or main_menu_keyboard())


async def show_all_tasks(
    message: types.Message,
    db,
    tg_user_id: int | None = None,
    tg_full_name: str | None = None,
) -> None:
    uid = tg_user_id if tg_user_id is not None else message.from_user.id
    uname = tg_full_name if tg_full_name is not None else message.from_user.full_name
    user = await ensure_user(db, uid, uname)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    await repo.ensure_regular_tasks(db, user["id"], today)
    tasks = rows_to_dicts(
        await repo.list_regular_tasks(db, user["id"], due_only=False, include_inactive=False)
    )
    if not tasks:
        await message.answer("Пока нет дел по дому.", reply_markup=main_menu_keyboard())
        return
    lines = ["Все дела по дому:"]
    page_tasks, kb = _paginate_tasks(tasks, page=0)
    for t in page_tasks:
        lines.append(_format_task_line(t))
    lines.append("\nТап по задаче: ✅ выполнить, ⚙️ настроить частоту, 🗑 скрыть из списков.")
    await message.answer("\n".join(lines), reply_markup=kb or main_menu_keyboard())


@router.callback_query(lambda c: c.data and c.data.startswith("hweek:done:"))
async def plan_mark_done(callback: types.CallbackQuery, db) -> None:
    task_id = int(callback.data.split(":")[2])
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    await _complete_regular_task_and_notify(db, user, callback, task_id, today)
    await callback.answer("Готово")
    await _refresh_plan(callback, db)


async def _refresh_plan(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    tasks = rows_to_dicts(
        await repo.list_regular_tasks(db, user["id"], local_date=today, due_in_days=7, include_inactive=False)
    )
    if not tasks:
        try:
            await callback.message.edit_text("План по дому на неделю пуст — всё чисто.", reply_markup=None)
        except Exception:
            await callback.message.answer("План по дому на неделю пуст — всё чисто.", reply_markup=main_menu_keyboard())
        return
    lines = ["План по дому на ближайшие 7 дней:"]
    for t in tasks[:7]:
        lines.append(f"• До {format_date_display(t['next_due_date'])} — {t['title']}")
    if len(tasks) > 7:
        lines.append(f"…и ещё {len(tasks) - 7} дел (показаны ближайшие).")
    kb = _regular_keyboard(tasks)
    await safe_edit(callback.message, "\n".join(lines), reply_markup=kb or main_menu_keyboard())


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
    await _complete_regular_task_and_notify(db, user, callback, task_id, today)
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


@router.message(HomeFreqState.wait_custom, lambda m: bool(m.text and not m.text.startswith("/")))
async def freq_custom(message: types.Message, state: FSMContext, db) -> None:
    raw = (message.text or "").strip()
    if raw.lower() in {"отмена", "назад"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return
    data = await state.get_data()
    task_id = data.get("freq_task_id")
    try:
        days = int(raw)
        if days < 1 or days > 365:
            raise ValueError
    except Exception:
        from utils import texts

        await message.answer(
            texts.error("нужно число дней (от 1 до 365), например 14."),
        )
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.set_regular_frequency(db, user["id"], int(task_id), days)
    await message.answer(f"Частота обновлена: каждые {days} дней.")
    await state.clear()
    await show_all_tasks(message, db)


async def _refresh_all(callback: types.CallbackQuery, db) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    tasks = rows_to_dicts(await repo.list_regular_tasks(db, user["id"], due_only=False, include_inactive=False))
    if not tasks:
        await safe_edit(callback.message, "Пока нет дел по дому.", reply_markup=None)
        return
    lines = ["Все дела по дому:"]
    page = 0
    # если callback от пагинации, постараемся вытащить номер страницы
    if callback.data.startswith("hall:page:") and callback.data != "hall:page:noop":
        try:
            page = int(callback.data.split(":")[2])
        except Exception:
            page = 0
    page_tasks, kb = _paginate_tasks(tasks, page=page)
    for t in page_tasks:
        line = _format_task_line(t)
        if line:
            lines.append(line)
    lines.append(f"\nВсего задач: {len(tasks)}. Показано {len(page_tasks)}.")
    await safe_edit(callback.message, "\n".join(lines), reply_markup=kb or main_menu_keyboard())


@router.callback_query(lambda c: c.data and c.data.startswith("hall:page:"))
async def hall_page(callback: types.CallbackQuery, db) -> None:
    if callback.data == "hall:page:noop":
        await callback.answer()
        return
    await _refresh_all(callback, db)
    await callback.answer()


# --- Cleaning 2.0 Session Logic ---

class CleanState(StatesGroup):
    choosing_zones = State()
    choosing_mode = State()
    active_session = State()


ZONES_CONFIG = {
    "kitchen": "🍳 Кухня",
    "bathroom": "🛁 Ванна/Туалет",
    "bedroom": "🛏 Спальня",
    "living": "🛋 Гостиная",
    "hallway": "🚪 Прихожая",
    "floors": "🧹 Полы (везде)",
}

CLEAN_MODES = {
    "maintenance": "✨ Поддерживающая (15-20 мин)",
    "deep": "🧽 Основательная (час+)",
}


def _zones_keyboard(selected: List[str]) -> InlineKeyboardMarkup:
    rows = []
    for key, label in ZONES_CONFIG.items():
        icon = "✅ " if key in selected else "⬜ "
        rows.append([InlineKeyboardButton(text=f"{icon}{label}", callback_data=f"cl2:toggle:{key}")])
    
    action_text = "🚀 Начать" if selected else "Выбери зоны"
    rows.append([InlineKeyboardButton(text=action_text, callback_data="cl2:confirm")])
    rows.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="home:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _modes_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, label in CLEAN_MODES.items():
        rows.append([InlineKeyboardButton(text=label, callback_data=f"cl2:mode:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _session_keyboard(session_id: int, current_idx: int, total: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Готово (+XP)", callback_data=f"cl2:step:done:{session_id}"),
                InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"cl2:step:skip:{session_id}"),
            ],
            [InlineKeyboardButton(text="⏸ Пауза / Стоп", callback_data=f"cl2:pause:{session_id}")],
        ]
    )


async def _generate_flow(zones: List[str], mode: str) -> List[dict]:
    """Генерация умного сценария уборки по фазам."""
    flow = []
    
    # helper
    def add(text, points=1, phase="main"):
        flow.append({"text": text, "points": points, "phase": phase, "status": "pending"})

    # Phase 0: Prep / Soak (Deep only)
    if mode == "deep":
        if "kitchen" in zones:
            add("Замочи посуду и залей плиту средством", 2, "prep")
        if "bathroom" in zones:
            add("Залей унитаз и раковину средством", 2, "prep")
    
    # Phase 1: Global Basics (Trash & Tidy)
    add("Пройдись с пакетом: собери весь явный мусор", 2, "trash")
    add("Собери одежду/вещи, которые валяются не на месте", 2, "tidy")
    
    # Phase 2: Zones
    if "kitchen" in zones:
        add("Кухня: разбери одну полку или поверхность", 2, "zone")
        add("Кухня: протри фасады и ручки", 2, "zone")
        if mode == "deep":
            add("Кухня: смой средство с плиты и протри насухо", 2, "zone")
    
    if "bathroom" in zones:
        add("Ванная: протри зеркало", 1, "zone")
        if mode == "deep":
            add("Ванная: почисти унитаз и смой средство", 3, "zone")
            add("Ванная: ополоснуть ванну/душ", 2, "zone")
        else:
             add("Ванная: быстро протри раковину", 2, "zone")

    if "bedroom" in zones:
        add("Спальня: заправь кровать аккуратно", 1, "zone")
        add("Спальня: протри пыль с тумбочек", 2, "zone")

    if "living" in zones:
        add("Гостиная: разбери вещи на столе и диване", 2, "zone")
        add("Гостиная: протри пыль с поверхностей", 2, "zone")

    if "hallway" in zones:
        add("Прихожая: расставь обувь ровно", 1, "zone")
        add("Прихожая: протри входной коврик или пол у двери", 2, "zone")

    # Phase 3: Floors (if explicitly selected or deep mode included)
    if "floors" in zones or (mode == "deep" and len(zones) > 2):
        add("Пропылесось основные проходы", 3, "floors")
        if mode == "deep":
             add("Протри полы влажной тряпкой", 4, "floors")

    # Phase 4: Finish
    add("Вынеси мусор, если набралось", 2, "finish")
    add("Проветри и похвали себя!", 1, "finish")
    
    return flow


@router.callback_query(lambda c: c.data == "home:now")
async def start_clean_now(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    # Check active session
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    existing = await repo.get_active_session(db, user["id"])
    
    if existing:
        # Prompt to resume
        text = "У тебя есть незавершенная уборка. Продолжим?"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Продолжить", callback_data=f"cl2:resume:{existing['id']}")],
            [InlineKeyboardButton(text="🗑 Начать новую", callback_data="cl2:new_force")]
        ])
        await callback.message.answer(text, reply_markup=kb)
        return

    # Start new selection
    await state.set_state(CleanState.choosing_zones)
    await state.update_data(selected_zones=[])
    await callback.message.answer("Выбери зоны для уборки (можно несколько):", reply_markup=_zones_keyboard([]))
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("cl2:toggle:"))
async def cl2_toggle_zone(callback: types.CallbackQuery, state: FSMContext) -> None:
    zone = callback.data.split(":")[2]
    data = await state.get_data()
    selected = data.get("selected_zones", [])
    
    if zone in selected:
        selected.remove(zone)
    else:
        selected.append(zone)
    
    await state.update_data(selected_zones=selected)
    await safe_edit_markup(callback.message, reply_markup=_zones_keyboard(selected))
    await callback.answer()


@router.callback_query(lambda c: c.data == "cl2:confirm")
async def cl2_confirm_zones(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    selected = data.get("selected_zones", [])
    if not selected:
        await callback.answer("Выбери хотя бы одну зону!", show_alert=True)
        return
    
    await state.set_state(CleanState.choosing_mode)
    await callback.message.edit_text("Какой режим уборки?", reply_markup=_modes_keyboard())


@router.callback_query(lambda c: c.data == "cl2:new_force")
async def cl2_force_new(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    await repo.abandon_active_cleaning_sessions(db, user["id"])
    await state.clear()
    await start_clean_now(callback, db, state)


@router.callback_query(lambda c: c.data and c.data.startswith("cl2:mode:"))
async def cl2_start_session(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    mode = callback.data.split(":")[2]
    data = await state.get_data()
    selected = data.get("selected_zones", [])
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    
    # Generate Steps
    steps = await _generate_flow(selected, mode)
    zones_json = json.dumps(selected)
    steps_json = json.dumps(steps, ensure_ascii=False)
    
    # Create DB Session
    session_id = await repo.create_cleaning_session(db, user["id"], mode, zones_json, steps_json)
    
    # Render Step 1
    await _render_step(callback.message, session_id, 0, steps)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("cl2:resume:"))
async def cl2_resume(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    session_id = int(callback.data.split(":")[2])
    # Fetch session to get steps
    # We need a repo function to get session by ID or re-use active. 
    # get_active_session returns ROW.
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    session = await repo.get_active_session(db, user["id"])
    
    if not session or session["id"] != session_id:
        await callback.answer("Сессия не найдена или завершена.", show_alert=True)
        await start_clean_now(callback, db, state)
        return

    steps = json.loads(session["steps_json"])
    idx = session["current_step_index"]
    await _render_step(callback.message, session_id, idx, steps)


async def _render_step(message: types.Message, session_id: int, idx: int, steps: List[dict]) -> None:
    if idx >= len(steps):
        # Completed
        await safe_edit(message, "🎉 Уборка завершена! Ты молодец!", reply_markup=None)
        await message.answer("Что дальше?", reply_markup=main_menu_keyboard())
        return

    step = steps[idx]
    total = len(steps)
    progress_bar = "▓" * int((idx / total) * 10) + "░" * (10 - int((idx / total) * 10))
    
    text = (
        f"🧹 Уборка: Шаг {idx + 1}/{total}\n"
        f"[{progress_bar}]\n\n"
        f"👉 **{step['text']}**\n"
        f"(+{step['points']} XP)"
    )
    
    await safe_edit(message, text, reply_markup=_session_keyboard(session_id, idx, total))


@router.callback_query(lambda c: c.data and c.data.startswith("cl2:step:"))
async def cl2_step_action(callback: types.CallbackQuery, db) -> None:
    _, _, action, session_id_str = callback.data.split(":")
    session_id = int(session_id_str)
    
    # Load session
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    session = await repo.get_active_session(db, user["id"])
    
    if not session or session["id"] != session_id:
        await callback.answer("Сессия устарела.")
        return

    steps = json.loads(session["steps_json"])
    idx = session["current_step_index"]
    
    # Award points if done
    if action == "done" and idx < len(steps):
        pts = steps[idx].get("points", 1)
        # We need to mark step as done in JSON? Or just move index?
        # Ideally update JSON too for history, but for now moving index is enough for progress.
        # But user wants "steps_json" in DB to be updated? 
        # Plan said: "steps_json" stores status.
        steps[idx]["status"] = "done"
        await repo.add_points(db, user["id"], pts, local_date=local_date_str(datetime.datetime.utcnow(), user["timezone"]))
    elif action == "skip":
        steps[idx]["status"] = "skipped"
        
    next_idx = idx + 1
    
    if next_idx >= len(steps):
        await repo.complete_session(db, session_id)
        await safe_edit(
            callback.message,
            "🎉 Уборка завершена!\nВсе шаги пройдены. Дом стал чище, а ты — круче.",
            reply_markup=None,
        )
        await callback.message.answer("Что дальше?", reply_markup=main_menu_keyboard())
    else:
        # Update DB
        new_json = json.dumps(steps, ensure_ascii=False)
        # We need a repo function to update JSON + index. 
        # Currently `update_session_progress` only updates index.
        # I will update `update_session_progress` in next tool call or usage `execute` here?
        # Using direct execute for now to be safe or assuming I should add it.
        # Wait, I can't easily modify repo from here.
        # I will rely on `update_session_progress` updating index.
        # And I'll run a raw query to update steps_json if I want to persist status.
        await repo.update_session_progress(db, session_id, next_idx)
        
        # Also update json
        await db.execute("UPDATE cleaning_sessions SET steps_json = ? WHERE id = ?", (new_json, session_id))
        await db.commit()
        
        await _render_step(callback.message, session_id, next_idx, steps)
    
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("cl2:pause:"))
async def cl2_pause(callback: types.CallbackQuery) -> None:
    await callback.message.edit_text("⏸ Уборка на паузе. Возвращайся, когда будешь готова (кнопка в меню).", reply_markup=None)
    await callback.answer()



# --- Быстрые сценарии зон ---

QUICK_PRESETS = {
    "floors": {
        "title": "Только полы",
        "zone": "hallway",
        "steps": [
            {"text": "Собери мусор/крошки с пола в пакет, вынеси если полон", "points": 2},
            {"text": "Разложи обувь, убери лишнее из прохода", "points": 1},
            {"text": "Быстро пройтись пылесосом по проходам", "points": 3},
            {"text": "Влажная салфетка/швабра по самым грязным местам", "points": 3},
        ],
    },
    "bathroom": {
        "title": "Только ванна/туалет",
        "zone": "bathroom",
        "steps": [
            {"text": "Налей средство в унитаз, сбрызни раковину/кран", "points": 1},
            {"text": "Протри зеркало/раковину, сполосни стены в душе", "points": 3},
            {"text": "Сиденье/ободок унитаза — пройди ершиком со средством", "points": 3},
            {"text": "Собери и вынеси мусор из санузла, замени полотенце", "points": 2},
        ],
    },
    "kitchen": {
        "title": "Только кухня",
        "zone": "kitchen",
        "steps": [
            {"text": "Убери посуду в раковину/ПММ, замочи сложное", "points": 2},
            {"text": "Сбрызни плиту/стол средством, дай поработать 5 мин", "points": 1},
            {"text": "Протри стол/рабочую поверхность, ручки шкафов", "points": 3},
            {"text": "Быстро пройдись по полу (веник/пылесос)", "points": 2},
        ],
    },
    "sink": {
        "title": "Только раковина и посуда",
        "zone": "kitchen",
        "steps": [
            {"text": "Собери посуду в раковину, слей остатки еды", "points": 1},
            {"text": "Замочи пригоревшее/сложное, включи ПММ если есть", "points": 2},
            {"text": "Быстро вымой посуду по приоритету: тарелки → ложки → чашки", "points": 3},
            {"text": "Протри раковину и кран, убери губки/тряпки, вынеси мусор", "points": 2},
        ],
    },
}

def _quick_menu_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for key, preset in QUICK_PRESETS.items():
        title = preset.get("title") or key
        rows.append([InlineKeyboardButton(text=title, callback_data=f"quick:start:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="home:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _quick_steps_text(scenario: str, steps: list[dict]) -> str:
    title = QUICK_PRESETS.get(scenario, {}).get("title", "Уборка")
    lines = [f"{title} — шаги. Можно остановиться в любой момент:"]
    for idx, step in enumerate(steps):
        status = step.get("status", "pending")
        prefix = "✅" if status == "done" else ("⏭" if status == "skip" else "•")
        lines.append(f"{prefix} {idx+1}. {step['text']}")
    return "\n".join(lines)


def _quick_steps_kb(scenario: str, steps: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for idx, step in enumerate(steps):
        status = step.get("status", "pending")
        label = "✅" if status == "done" else ("⏭" if status == "skip" else "•")
        rows.append(
            [
                InlineKeyboardButton(text=f"{label} {idx+1}", callback_data=f"quick:mark:done:{scenario}:{idx}"),
                InlineKeyboardButton(text="Пропустить", callback_data=f"quick:mark:skip:{scenario}:{idx}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="❌ Выйти", callback_data="quick:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(lambda c: c.data == "quick:cancel")
async def quick_cancel(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(callback.message, "Быстрый сценарий закрыт 👌", reply_markup=home_menu_keyboard())
    await callback.answer("Вышли")


async def _start_quick(callback: types.CallbackQuery, scenario: str, state: FSMContext, db) -> None:
    preset = QUICK_PRESETS.get(scenario)
    if not preset:
        await callback.answer()
        return
    await state.set_state(QuickCleanState.active)
    steps = _init_steps([dict(s) for s in preset["steps"]])
    await state.update_data(quick_scenario=scenario, quick_steps=steps, quick_zone=preset.get("zone"))
    text = _quick_steps_text(scenario, steps)
    kb = _quick_steps_kb(scenario, steps)
    await callback.message.answer(text, reply_markup=kb)
    await callback.answer("Поехали!")


@router.callback_query(lambda c: c.data and c.data.startswith("quick:start:"))
async def quick_start(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    # если есть активный быстрый сценарий — покажем его
    resumed = await _resume_quick(callback.message, state)
    if resumed:
        await callback.answer("Продолжаем быстрый сценарий.")
        return
    scenario = callback.data.split(":")[2]
    await _start_quick(callback, scenario, state, db)


@router.callback_query(lambda c: c.data and c.data.startswith("quick:mark:"))
async def quick_mark(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    _, _, action, scenario, idx_str = callback.data.split(":")
    data = await state.get_data()
    steps = data.get("quick_steps", [])
    if not steps:
        await callback.answer()
        return
    idx = int(idx_str)
    if idx >= len(steps):
        await callback.answer()
        return
    step = steps[idx]
    if step.get("status") in ("done", "skip"):
        await callback.answer("Уже отмечено")
        return
    step["status"] = "done" if action == "done" else "skip"
    steps[idx] = step
    await state.update_data(quick_steps=steps)
    done_points = sum(s.get("points", 0) for s in steps if s.get("status") == "done")
    pending = [s for s in steps if s.get("status") == "pending"]
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    if action == "done":
        await repo.add_points(db, user["id"], step.get("points", 2), local_date=today)
    if not pending:
        summary = (
            f"Готово: {QUICK_PRESETS.get(scenario, {}).get('title','Уборка')} закрыта.\n"
            f"Шагов выполнено {len([s for s in steps if s.get('status')=='done'])}/{len(steps)}, +{done_points} очков."
        )
        await state.clear()
        await callback.message.answer(summary, reply_markup=home_menu_keyboard())
        await callback.answer("Завершено")
        return
    text = _quick_steps_text(scenario, steps)
    kb = _quick_steps_kb(scenario, steps)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer("Отметила")


async def _resume_clean(message: types.Message, state: FSMContext) -> bool:
    """Показать текущий сценарий уборки, если он в процессе."""
    data = await state.get_data()
    steps = data.get("steps")
    if not steps:
        return False
    clean_state = await state.get_state()
    if clean_state != CleanNowState.process:
        return False
    text = _steps_text(steps)
    kb = _steps_keyboard(steps)
    try:
        await message.answer("Продолжаем уборку:", reply_markup=None)
    except Exception:
        pass
    await message.answer(text, reply_markup=kb)
    return True


async def _resume_quick(message: types.Message, state: FSMContext) -> bool:
    data = await state.get_data()
    steps = data.get("quick_steps")
    scenario = data.get("quick_scenario")
    if not steps or not scenario:
        return False
    quick_state = await state.get_state()
    if quick_state != QuickCleanState.active:
        return False
    text = _quick_steps_text(scenario, steps)
    kb = _quick_steps_kb(scenario, steps)
    await message.answer("Продолжаем быстрый сценарий:", reply_markup=kb)
    return True


async def _resume_any_cleanup(message: types.Message, state: FSMContext) -> bool:
    """Пробуем возобновить любой из сценариев уборки (основной или быстрый)."""
    if await _resume_quick(message, state):
        return True
    if await _resume_clean(message, state):
        return True
    return False


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


async def send_smell_menu(message: types.Message) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🧺 Запах стиралки", callback_data="ask:odor:wash"),
                InlineKeyboardButton(text="🍽 Раковина/кухня", callback_data="ask:odor:kitchen"),
            ],
            [
                InlineKeyboardButton(text="🛁 Ванна/туалет", callback_data="ask:odor:bathroom"),
                InlineKeyboardButton(text="🏠 Комната/общий запах", callback_data="ask:odor:room"),
            ],
            [InlineKeyboardButton(text="🧼 Стирать вещи", callback_data="ask:start:laundry")],
        ]
    )
    await message.answer(
        "Запахи и стирка: выбери, что сейчас беспокоит — стиралка, раковина, ванна или общий запах в комнате. Дам короткие шаги без шейминга.",
        reply_markup=kb,
    )


# Fallbacks на старые кнопки
@router.callback_query(lambda c: c.data and c.data.startswith("home:regular"))
async def home_regular_entry(callback: types.CallbackQuery, db) -> None:
    await show_week_plan(
        callback.message,
        db,
        tg_user_id=callback.from_user.id,
        tg_full_name=callback.from_user.full_name,
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("home:regular_all"))
async def home_regular_all(callback: types.CallbackQuery, db) -> None:
    await show_all_tasks(
        callback.message,
        db,
        tg_user_id=callback.from_user.id,
        tg_full_name=callback.from_user.full_name,
    )
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
        await _complete_regular_task_and_notify(db, user, callback, task_id, today)
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
