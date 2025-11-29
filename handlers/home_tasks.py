import datetime

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.time import local_date_str

router = Router()


class HomeAuditState(StatesGroup):
    step = State()
    during_start = State()


AUDIT_QUESTIONS = [
    ("Поменять полотенца", 4, "Когда последний раз менял(а) полотенца для тела?"),
    ("Поменять постельное", 14, "Когда менял(а) постельное бельё?"),
    ("Полы/пылесос", 7, "Когда была влажная уборка пола?"),
    ("Ванна/раковина/унитаз", 7, "Когда нормально мыл(а) санузел?"),
    ("Разбор холодильника", 30, "Когда разбирал(а) холодильник (выкидывал просрочку/протирал полки)?"),
    ("Ревизия стиралки (манжета/фильтр)", 60, "Когда чистил(а) стиралку (фильтр/резинка/горячая стирка без вещей)?"),
]


def audit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="audit:ans:0"),
                InlineKeyboardButton(text="На этой неделе", callback_data="audit:ans:3"),
            ],
            [
                InlineKeyboardButton(text="Больше недели", callback_data="audit:ans:10"),
                InlineKeyboardButton(text="Не помню", callback_data="audit:ans:14"),
            ],
        ]
    )


def _regular_keyboard(tasks):
    rows = []
    for t in tasks:
        status_icon = "✅ " if t.get("last_done_date") else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{status_icon}{t['title']}", callback_data=f"reg:done:{t['id']}"
                ),
                InlineKeyboardButton(
                    text="⏭ +1", callback_data=f"reg:later1:{t['id']}"
                ),
                InlineKeyboardButton(
                    text="+3", callback_data=f"reg:later3:{t['id']}"
                ),
                InlineKeyboardButton(
                    text="+7", callback_data=f"reg:later7:{t['id']}"
                ),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _freq_keyboard(tasks):
    rows = []
    for t in tasks:
        rows.append(
            [
                InlineKeyboardButton(text=f"{t['title']}", callback_data=f"freq:sel:{t['id']}"),
                InlineKeyboardButton(text="-", callback_data=f"freq:dec:{t['id']}"),
                InlineKeyboardButton(text="+", callback_data=f"freq:inc:{t['id']}"),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _send_all_regular(message: types.Message, db):
    from utils.user import ensure_user
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await repo.ensure_regular_tasks(db, user["id"])
    tasks = await repo.list_regular_tasks(db, user["id"], due_only=False)
    if not tasks:
        await message.answer("Пока нет регулярных дел.", reply_markup=main_menu_keyboard())
        return
    lines = ["Все регулярные дела:"]
    for t in tasks:
        from utils.time import format_date_display
        status = "✅" if t["last_done_date"] else "⏳"
        lines.append(f"{status} {t['title']} — до {format_date_display(t['next_due_date'])}")
    kb = _regular_keyboard(tasks)
    await message.answer("\n".join(lines), reply_markup=kb or main_menu_keyboard())


async def _send_audit(message: types.Message, db):
    from utils.user import ensure_user
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    await repo.ensure_regular_tasks(db, user["id"], today)
    due = await repo.list_regular_tasks(db, user["id"], due_only=True, local_date=today)
    if not due:
        next_due = await repo.next_regular_task_date(db, user["id"])
        extra = f" Все ок, следующий пункт до {next_due}." if next_due else ""
        await message.answer("Регулярные дела: пока ничего не горит." + extra, reply_markup=main_menu_keyboard())
        return
    lines = ["Регулярные дела, которые пора сделать:"]
    for t in due:
        from utils.time import format_date_display
        lines.append(f"• {t['title']} (дата: {format_date_display(t['next_due_date'])})")
    kb = _regular_keyboard(due)
    await message.answer("\n".join(lines), reply_markup=kb or main_menu_keyboard())


@router.message(Command("home_audit"))
async def home_audit(message: types.Message, db) -> None:
    await _send_audit(message, db)


@router.message(Command("home_plan"))
async def home_plan(message: types.Message, db) -> None:
    from utils.user import ensure_user
    from utils.time import format_date_display
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    await repo.ensure_regular_tasks(db, user["id"], today)
    tasks = await repo.list_regular_tasks(db, user["id"], due_only=False)
    lines = ["План по дому:"]
    for t in tasks:
        lines.append(f"• {t['title']} — каждые {t['frequency_days']} д., следующий дедлайн: {format_date_display(t['next_due_date'])}")
    lines.append("\nЕсли хочешь настроить частоту — жми «✏ Изменить частоту» ниже.")
    kb_rows = [[InlineKeyboardButton(text="✏ Изменить частоту", callback_data="plan:edit")]]
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@router.message(Command("home_audit_setup"))
async def home_audit_setup(message: types.Message, state: FSMContext, db) -> None:
    from utils.user import ensure_user
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await state.set_state(HomeAuditState.step)
    await state.update_data(step=0, during_start=False)
    title, freq, prompt = AUDIT_QUESTIONS[0]
    await message.answer(f"Домашний аудит.\n\n{prompt}", reply_markup=audit_keyboard())


@router.callback_query(lambda c: c.data and c.data.startswith("home:regular"))
async def home_regular_entry(callback: types.CallbackQuery, db) -> None:
    await _send_audit(callback.message, db)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("home:regular_all"))
async def home_regular_all(callback: types.CallbackQuery, db) -> None:
    await _send_all_regular(callback.message, db)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("home:plan"))
async def home_plan_cb(callback: types.CallbackQuery, db) -> None:
    await home_plan(callback.message, db)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("plan:edit"))
async def plan_edit(callback: types.CallbackQuery, db) -> None:
    from utils.user import ensure_user
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    await repo.ensure_regular_tasks(db, user["id"], today)
    tasks = await repo.list_regular_tasks(db, user["id"], due_only=False)
    kb = _freq_keyboard(tasks)
    lines = [f"{t['title']}: каждые {t['frequency_days']} д." for t in tasks]
    await callback.message.answer("Частоты:\n" + "\n".join(lines), reply_markup=kb or main_menu_keyboard())
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("freq:"))
async def freq_change(callback: types.CallbackQuery, db) -> None:
    parts = callback.data.split(":")
    action = parts[1]
    task_id = int(parts[2])
    user = await repo.get_user_by_telegram_id(db, callback.from_user.id)
    if not user:
        await callback.answer("Нужно зарегистрироваться: нажми /start", show_alert=True)
        return
    # fetch task
    tasks = await repo.list_regular_tasks(db, user["id"], due_only=False)
    task = next((t for t in tasks if t["id"] == task_id), None)
    if not task:
        await callback.answer("Не нашла задачу", show_alert=True)
        return
    freq = task["frequency_days"]
    if action == "inc":
        freq += 7 if freq >= 14 else 3
    elif action == "dec":
        freq = max(3, freq - (7 if freq > 14 else 3))
    await repo.set_regular_frequency(db, user["id"], task_id, freq)
    tasks = await repo.list_regular_tasks(db, user["id"], due_only=False)
    kb = _freq_keyboard(tasks)
    lines = [f"{t['title']}: каждые {t['frequency_days']} д." for t in tasks]
    await callback.message.edit_text("Частоты:\n" + "\n".join(lines), reply_markup=kb)
    await callback.answer("Обновила.")


@router.callback_query(lambda c: c.data and c.data.startswith("care:"))
async def care_mark(callback: types.CallbackQuery, db) -> None:
    _, col, date_str = callback.data.split(":")
    user = await repo.get_user_by_telegram_id(db, callback.from_user.id)
    if not user:
        await callback.answer("Нужно зарегистрироваться: /start", show_alert=True)
        return
    await repo.update_care_date(db, user["id"], col, date_str)
    await callback.answer("Отметила.")
    await callback.message.edit_text("Отметила заботу как выполненную.", reply_markup=None)


@router.callback_query(lambda c: c.data and c.data.startswith("home:audit"))
async def home_audit_cb(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    await home_audit_setup(callback.message, state, db)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("reg:"))
async def regular_actions(callback: types.CallbackQuery, db) -> None:
    parts = callback.data.split(":")
    action, task_id = parts[1], int(parts[2])
    from utils.user import ensure_user
    from utils.today import render_today
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    if action == "done":
        await repo.mark_regular_done(db, user["id"], task_id, today)
        await repo.add_points(db, user["id"], 4, local_date=today)
        await callback.answer("Готово")
    elif action.startswith("later"):
        days = 1
        if "later3" in action:
            days = 3
        elif "later7" in action:
            days = 7
        await repo.postpone_regular_task(db, user["id"], task_id, days)
        await callback.answer(f"Перенёс на +{days} д.")
    # обновить список дел по дому, не перерисовывая /today
    due = await repo.list_regular_tasks(db, user["id"], due_only=True, local_date=today)
    if not due:
        next_due = await repo.next_regular_task_date(db, user["id"])
        extra = f" Все ок, следующий пункт до {next_due}." if next_due else ""
        text = "Регулярные дела: пока ничего не горит." + extra
        kb = None
    else:
        from utils.time import format_date_display
        lines = ["Регулярные дела, которые пора сделать:"]
        for t in due:
            lines.append(f"• {t['title']} (дата: {format_date_display(t['next_due_date'])})")
        kb = _regular_keyboard(due)
        text = "\n".join(lines)
    try:
        await callback.message.edit_text(text, reply_markup=kb or main_menu_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=kb or main_menu_keyboard())


@router.callback_query(lambda c: c.data and c.data.startswith("audit:ans:"))
async def audit_answer(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    user = await repo.get_user_by_telegram_id(db, callback.from_user.id)
    if not user:
        await callback.answer("Нужно зарегистрироваться: нажми /start", show_alert=True)
        return
    data = await state.get_data()
    step = data.get("step", 0)
    offset_days = int(callback.data.split(":")[2])
    today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    title, freq, prompt = AUDIT_QUESTIONS[step]
    # last_done_date = today - offset
    last_done_date = (
        (datetime.date.fromisoformat(today) - datetime.timedelta(days=offset_days)).isoformat()
        if offset_days >= 0
        else None
    )
    next_due = datetime.date.fromisoformat(today) + datetime.timedelta(days=freq - offset_days if offset_days < freq else 0)
    await repo.upsert_regular_task(
        db,
        user_id=user["id"],
        title=title,
        frequency_days=freq,
        last_done_date=last_done_date,
        next_due_date=next_due.isoformat(),
    )
    step += 1
    if step >= len(AUDIT_QUESTIONS):
        await state.clear()
        await callback.message.answer(
            "План по дому обновлён. Проверю дедлайны и буду напоминать в разделе Дом и в «Сегодня».",
            reply_markup=main_menu_keyboard(),
        )
        await _send_audit(callback.message, db)
        await callback.answer("Сохранено")
        return
    await state.update_data(step=step)
    _, _, next_prompt = AUDIT_QUESTIONS[step]
    await callback.message.answer(next_prompt, reply_markup=audit_keyboard())
    await callback.answer("Сохранено")
