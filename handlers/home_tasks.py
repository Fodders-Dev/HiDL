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
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _all_tasks_keyboard(tasks):
    rows = []
    for t in tasks:
        row = row_to_dict(t)
        zone = row.get("zone") or "misc"
        short = _zone_icon(zone)
        rows.append(
            [
                InlineKeyboardButton(text=f"{short} ✅", callback_data=f"hall:done:{row['id']}"),
                InlineKeyboardButton(text="⚙️ Частота", callback_data=f"hall:freq:{row['id']}"),
                InlineKeyboardButton(text="🗑", callback_data=f"hall:hide:{row['id']}"),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


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
        kb_rows.append(
            [
                InlineKeyboardButton(text=f"{short} ✅", callback_data=f"hall:done:{t['id']}"),
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
    return f"{status} {zone_icon} {row['title']} — каждые {freq} д., до {next_disp}"


async def show_week_plan(message: types.Message, db) -> None:
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
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


async def show_all_tasks(message: types.Message, db) -> None:
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
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
    tasks = rows_to_dicts(await repo.list_regular_tasks(db, user["id"], due_only=False, include_inactive=False))
    task = next((t for t in tasks if t.get("id") == task_id), None)
    await repo.mark_regular_done(db, user["id"], task_id, today)
    pts = (task.get("points") if task else 3) or 3
    await repo.add_points(db, user["id"], pts, local_date=today)
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
    tasks = rows_to_dicts(await repo.list_regular_tasks(db, user["id"], due_only=False, include_inactive=False))
    task = next((t for t in tasks if t.get("id") == task_id), None)
    await repo.mark_regular_done(db, user["id"], task_id, today)
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
        from utils import texts

        await message.answer(
            texts.error("нужно число дней, например 14."),
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


def _zone_keyboard() -> InlineKeyboardMarkup:
    """Выбор типа уборки/зоны для сценария «Уборка сейчас»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Квартира в целом", callback_data="clean:zone:flat")],
            [
                InlineKeyboardButton(text="🛁 Только ванна/туалет", callback_data="clean:zone:bathroom"),
                InlineKeyboardButton(text="🍳 Только кухня", callback_data="clean:zone:kitchen"),
            ],
            [
                InlineKeyboardButton(text="🧹 Только полы", callback_data="clean:zone:floors"),
            ],
        ]
    )


def _quick_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚡ Только полы", callback_data="quick:start:floors"),
                InlineKeyboardButton(text="🛁 Ванна/туалет", callback_data="quick:start:bathroom"),
            ],
            [
                InlineKeyboardButton(text="🍳 Только кухня", callback_data="quick:start:kitchen"),
                InlineKeyboardButton(text="🧺 Раковина и посуда", callback_data="quick:start:sink"),
            ],
        ]
    )


async def start_clean_now(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    # если уже есть активный сценарий уборки — покажем его, не стирая state
    resumed = await _resume_any_cleanup(callback.message, state)
    if resumed:
        await callback.answer("Продолжаем там, где остановились.")
        return
    await state.clear()
    # лёгкое предложение проветрить, без очков и обязательности
    air_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Открыла(открыл)", callback_data="clean:air:ok"),
                InlineKeyboardButton(text="Пропустить", callback_data="clean:air:skip"),
            ]
        ]
    )
    await callback.message.answer(
        "Пока начнём убираться, можно открыть окно/форточку на 5–10 минут — воздух сам сделает часть работы.",
        reply_markup=air_kb,
    )
    await state.set_state(CleanNowState.choose_type)
    await callback.message.answer("Что делаем по дому?", reply_markup=_clean_type_keyboard())
    await callback.message.answer(
        "Нужен короткий сценарий по зоне? Выбирай ниже.",
        reply_markup=_quick_menu_keyboard(),
    )
    await callback.answer()


def _surface_steps(energy: str) -> List[dict]:
    steps = [
        {"text": "Собери одежду в одну корзину/стопку", "points": 1},
        {"text": "Протри стол или главную поверхность", "points": 1},
        {"text": "Разгрузи раковину или замочи посуду", "points": 1},
        {"text": "Вынеси мусор, если ведро полное", "points": 2},
    ]
    target = 3 if energy == "low" else (4 if energy == "mid" else 5)
    return _init_steps(steps[:target])


def _base_prep_steps(zone: str) -> List[dict]:
    """Быстрые подготовительные шаги — замачивание и «фоновые» процессы."""
    common = [
        {"text": "Собери явный мусор в пакет, вынеси если полон", "points": 2},
        {"text": "Собери посуду в раковину/ПММ и замочи", "points": 2},
        {"text": "Собери одежду: грязное в корзину, остальное в одну стопку", "points": 1},
        {"text": "Если есть стиралка с бельём — запусти стирку при подходящем режиме", "points": 2},
    ]
    soak = []
    if zone == "bathroom":
        soak.append({"text": "Налей средство в унитаз и оставь. Сбрызни раковину/кран.", "points": 1})
    if zone == "kitchen":
        soak.append({"text": "Сбрызни плиту/рабочую поверхность средством, пусть поработает.", "points": 1})
    return _init_steps(soak + common)


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
        "floors": [
            {"text": "Собрать крупный мусор и крошки с пола", "points": 2},
            {"text": "Пройтись пылесосом/веником по основным проходам", "points": 3},
            {"text": "Протереть влажной тряпкой самые грязные участки", "points": 3},
        ],
    }
    steps = base.get(zone, base["room"])
    target = 3 if energy == "low" else (4 if energy == "mid" else 5)
    return _init_steps(steps[:target])


def _normal_steps(home_tasks: List[dict], energy: str) -> List[dict]:
    steps: List[dict] = []
    for t_raw in home_tasks[:2]:
        t = row_to_dict(t_raw)
        if not t.get("title") or not t.get("id"):
            continue
        points = t.get("points") or 3
        steps.append({"text": f"{t['title']} (по плану)", "points": points, "task_id": t["id"]})
    steps.extend(_surface_steps(energy))
    target = 4 if energy == "low" else (5 if energy == "mid" else 7)
    return _init_steps(steps[:target])


async def _build_steps(db, user_id: int, energy: str, clean_type: str, today: str, zone: str) -> List[dict]:
    tasks = rows_to_dicts(
        await repo.list_regular_tasks(db, user_id, local_date=today, due_in_days=7, include_inactive=False)
    )
    steps: List[dict] = []
    # параллельные процессы (замачивание/стирка) только для обычной/глубокой уборки
    if clean_type != "surface":
        steps.extend(_base_prep_steps(zone))
    if clean_type == "surface":
        steps.extend(_surface_steps(energy))
    elif clean_type == "normal":
        steps.extend(_normal_steps(tasks, energy))
    else:
        steps.extend(_zone_steps(zone, energy))
    if zone == "bathroom":
        steps.append({"text": "Вернись к унитазу/раковине: смой средство и протри", "points": 2})
    elif zone == "kitchen":
        steps.append({"text": "Вернись к плите/поверхности: протри после замачивания", "points": 2})
    # ограничим 7 шагами максимум
    return _init_steps(steps[:7])


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


def _finish_touches(zone: str) -> str:
    common = [
        "Вынеси мусор, если пакет полон.",
        "Протри вокруг раковины/крана, чтобы не было подтёков.",
        "Быстро глянь на вход: обувь/коврик по местам.",
    ]
    if zone == "bedroom":
        common.append("Если меняла постельное — отметь, что готово, +очки.")
    if zone == "kitchen":
        common.append("Если духовка давно не чистилась — можно заглянуть внутрь и решить, не пора ли её помыть.")
    return "Финишные штрихи:\n" + "\n".join(f"• {t}" for t in common)


@router.callback_query(lambda c: c.data and c.data.startswith("clean:air:"))
async def clean_air(callback: types.CallbackQuery) -> None:
    """Ответ на предложение проветрить — без очков и дополнительной логики."""
    action = callback.data.split(":")[2]
    if action == "ok":
        await callback.answer("Отлично, пусть свежий воздух помогает.")
    else:
        await callback.answer("Хорошо, тогда двигаемся без окна.")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


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
    await state.update_data(energy=energy, clean_type=clean_type)
    await state.set_state(CleanNowState.choose_zone)
    await callback.message.answer("Где навести порядок в первую очередь?", reply_markup=_zone_keyboard())
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("clean:zone:"))
async def clean_choose_zone(callback: types.CallbackQuery, db, state: FSMContext) -> None:
    zone = callback.data.split(":")[2]
    data = await state.get_data()
    clean_type = data.get("clean_type", "surface")
    energy = data.get("energy", "mid")
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    steps = await _build_steps(db, user["id"], energy, clean_type, today, zone)
    await state.update_data(steps=steps, energy=energy, today=today, zone=zone)
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
        zone = data.get("zone", "room")
        finish = _finish_touches(zone)
        summary = (
            f"Ты закрыла {done_cnt} из {len(steps)} шагов, +{total_points} очков.\n"
            f"{finish}\n\nДома уже заметно легче — можешь остановиться или сделать ещё один круг позже."
        )
        extra_kb = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text="🔁 Ещё круг", callback_data="clean:again")],
                [types.InlineKeyboardButton(text="🏠 Дом", callback_data="home:menu")],
            ]
        )
        await callback.message.answer(summary, reply_markup=extra_kb)
        await state.clear()
    await callback.answer("Обновлено")


@router.callback_query(lambda c: c.data and c.data == "clean:again")
async def clean_again(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await state.clear()
    await start_clean_now(callback, db, state)


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


def _quick_steps_text(scenario: str, steps: list[dict]) -> str:
    title = QUICK_PRESETS.get(scenario, {}).get("title", "Уборка")
    lines = [f"{title} — шаги:"]
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
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        "Запахи и стирка: выбери, что беспокоит. Дам короткие шаги без шейминга.",
        reply_markup=kb,
    )


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
        tasks = rows_to_dicts(await repo.list_regular_tasks(db, user["id"], due_only=False, include_inactive=False))
        task = next((t for t in tasks if t.get("id") == task_id), None)
        await repo.mark_regular_done(db, user["id"], task_id, today)
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
