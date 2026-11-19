import datetime
import re
from dataclasses import dataclass

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.menu_match import match_menu_label
from utils.time import local_date_str, parse_hhmm
from utils.user import ensure_user
from utils.rows import rows_to_dicts

router = Router()


class ScheduleState(StatesGroup):
    add_block = State()
    add_event = State()


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="sched:cancel")]]
    )


async def _handle_cancel(message: types.Message, state: FSMContext) -> bool:
    if (message.text or "").strip().lower() in {"отмена", "назад"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard())
        return True
    return False


@dataclass(frozen=True)
class Interval:
    start: int  # minutes from 00:00
    end: int
    title: str
    kind: str  # sleep | block | event | suggestion


def _to_min(hhmm: str | datetime.time) -> int:
    if isinstance(hhmm, datetime.time):
        return hhmm.hour * 60 + hhmm.minute
    hh, mm = map(int, str(hhmm).split(":"))
    return hh * 60 + mm


def _hhmm_str(value: str | datetime.time | None, default: str = "") -> str:
    if isinstance(value, datetime.time):
        return value.strftime("%H:%M")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _from_min(m: int) -> str:
    m = max(0, min(24 * 60, int(m)))
    hh = m // 60
    mm = m % 60
    return f"{hh:02d}:{mm:02d}"


def _merge_intervals(intervals: list[Interval]) -> list[Interval]:
    if not intervals:
        return []
    items = sorted(intervals, key=lambda x: (x.start, x.end))
    out: list[Interval] = [items[0]]
    for cur in items[1:]:
        prev = out[-1]
        if cur.start <= prev.end:
            out[-1] = Interval(prev.start, max(prev.end, cur.end), prev.title, prev.kind)
        else:
            out.append(cur)
    return out


def _subtract_slot(slots: list[tuple[int, int]], used: tuple[int, int]) -> list[tuple[int, int]]:
    a, b = used
    out: list[tuple[int, int]] = []
    for s, e in slots:
        if e <= a or s >= b:
            out.append((s, e))
            continue
        if s < a:
            out.append((s, a))
        if e > b:
            out.append((b, e))
    return [(x, y) for x, y in out if y - x >= 10]


def _free_slots(wake_min: int, sleep_min: int, busy: list[Interval]) -> list[tuple[int, int]]:
    if sleep_min <= wake_min:
        # fallback: treat as full day awake
        wake_min = 0
        sleep_min = 24 * 60
    busy = [b for b in busy if b.end > wake_min and b.start < sleep_min]
    merged = _merge_intervals([Interval(max(wake_min, b.start), min(sleep_min, b.end), b.title, b.kind) for b in busy])
    slots: list[tuple[int, int]] = []
    cur = wake_min
    for b in merged:
        if b.start > cur:
            slots.append((cur, b.start))
        cur = max(cur, b.end)
    if cur < sleep_min:
        slots.append((cur, sleep_min))
    return [(a, b) for a, b in slots if b - a >= 10]


def _pick_slot(slots: list[tuple[int, int]], duration: int) -> tuple[int, int] | None:
    for a, b in slots:
        if b - a >= duration:
            return a, a + duration
    return None


def _parse_weekdays(raw: str) -> str | None:
    txt = (raw or "").strip().lower()
    if not txt:
        return None
    if txt in {"пн-пт", "будни", "по будням"}:
        return "0,1,2,3,4"
    if txt in {"вс", "воскресенье"}:
        return "6"
    if txt in {"сб", "суббота"}:
        return "5"
    if txt in {"ежедневно", "каждый день", "каждый"}:
        return "0,1,2,3,4,5,6"
    day_map = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
    parts = re.split(r"[,\s]+", txt)
    days: set[int] = set()
    for p in parts:
        p = p.strip(".")
        if not p:
            continue
        if p in day_map:
            days.add(day_map[p])
    if not days:
        return None
    return ",".join(str(x) for x in sorted(days))


def _schedule_keyboard(local_date: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Блок (работа/учёба/дорога)", callback_data="sched:add_block"),
                InlineKeyboardButton(text="➕ Событие", callback_data="sched:add_event"),
            ],
            [
                InlineKeyboardButton(text="⚡ Вставить спорт 30м", callback_data=f"sched:suggest:{local_date}:sport"),
                InlineKeyboardButton(text="🍳 Вставить готовку 45м", callback_data=f"sched:suggest:{local_date}:cook"),
            ],
            [
                InlineKeyboardButton(text="🛒 Вставить покупки 60м", callback_data=f"sched:suggest:{local_date}:shop"),
            ],
            [InlineKeyboardButton(text="⬅️ Меню", callback_data="main:menu")],
        ]
    )


async def _render_schedule_text(db, user: dict, local_date: str) -> tuple[str, list[Interval], list[tuple[int, int]]]:
    weekday = datetime.date.fromisoformat(local_date).weekday()
    blocks = rows_to_dicts(await repo.list_schedule_blocks(db, user["id"]))
    events = rows_to_dicts(await repo.list_schedule_events(db, user["id"], local_date))

    wake = _hhmm_str(parse_hhmm(user.get("wake_up_time") or "08:00"), "08:00")
    sleep = _hhmm_str(parse_hhmm(user.get("sleep_time") or "23:00"), "23:00")
    wake_min = _to_min(wake)
    sleep_min = _to_min(sleep)

    busy: list[Interval] = []
    # sleep block (outside awake window)
    busy.append(Interval(0, wake_min, "Сон", "sleep"))
    busy.append(Interval(sleep_min, 24 * 60, "Сон", "sleep"))

    for b in blocks:
        wds = {int(x) for x in (b.get("weekdays") or "").split(",") if str(x).strip().isdigit()}
        if wds and weekday not in wds:
            continue
        st = parse_hhmm(b.get("start_time") or "")
        en = parse_hhmm(b.get("end_time") or "")
        if not st or not en:
            continue
        s = _to_min(st)
        e = _to_min(en)
        if e <= s:
            continue
        busy.append(Interval(s, e, b.get("title") or "Блок", "block"))

    for e in events:
        st = parse_hhmm(e.get("start_time") or "")
        en = parse_hhmm(e.get("end_time") or "")
        if not st or not en:
            continue
        s = _to_min(st)
        t = _to_min(en)
        if t <= s:
            continue
        busy.append(Interval(s, t, e.get("title") or "Событие", "event"))

    slots = _free_slots(wake_min, sleep_min, busy)

    lines = [f"🗓 <b>Расписание на {local_date}</b>"]
    lines.append(f"🛌 Сон: {sleep}–{wake}")
    if blocks:
        lines.append("\n<b>Фиксировано:</b>")
        fixed = [x for x in busy if x.kind == "block"]
        if fixed:
            for it in sorted(fixed, key=lambda x: x.start):
                lines.append(f"• {_from_min(it.start)}–{_from_min(it.end)} — {it.title}")
        else:
            lines.append("• пока нет блоков (работа/учёба/дорога)")
    else:
        lines.append("\n<b>Фиксировано:</b>\n• пока нет блоков (работа/учёба/дорога)")

    if events:
        lines.append("\n<b>Сегодняшние события:</b>")
        for it in sorted([x for x in busy if x.kind == "event"], key=lambda x: x.start):
            lines.append(f"• {_from_min(it.start)}–{_from_min(it.end)} — {it.title}")
    else:
        lines.append("\n<b>Сегодняшние события:</b>\n• пока пусто")

    # suggestions
    sug = []
    tmp_slots = list(slots)
    sport = _pick_slot(tmp_slots, 30)
    if sport:
        sug.append(("Спорт 30м", sport))
        tmp_slots = _subtract_slot(tmp_slots, sport)
    cook = _pick_slot(tmp_slots, 45)
    if cook:
        sug.append(("Готовка 45м", cook))
        tmp_slots = _subtract_slot(tmp_slots, cook)
    shop = _pick_slot(tmp_slots, 60)
    if shop:
        sug.append(("Покупки 60м", shop))
    if sug:
        lines.append("\n<b>Куда можно встроить:</b>")
        for title, (a, b) in sug:
            lines.append(f"• {_from_min(a)}–{_from_min(b)} — {title}")
    else:
        lines.append("\n<b>Куда можно встроить:</b>\n• не вижу свободных окон — можно ослабить блоки или сдвинуть сон")

    return "\n".join(lines), busy, slots


@router.message(Command("schedule"))
@router.message(lambda m: match_menu_label(m.text, {"расписание"}))
async def schedule_today(message: types.Message, state: FSMContext, db) -> None:
    await state.clear()
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    local_date = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    text, _, _ = await _render_schedule_text(db, user, local_date)
    await message.answer(text, reply_markup=_schedule_keyboard(local_date), parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("sched:"))
async def schedule_callbacks(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    data = (callback.data or "").split(":")
    action = data[1] if len(data) > 1 else ""
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    local_date = local_date_str(datetime.datetime.utcnow(), user["timezone"])

    if action == "cancel":
        await state.clear()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("Отменила.", reply_markup=main_menu_keyboard())
        await callback.answer("Отменила.")
        return

    if action == "add_block":
        await state.set_state(ScheduleState.add_block)
        await callback.message.answer(
            "Добавим фиксированный блок.\n"
            "Напиши так:\n"
            "<b>Работа/Учёба; 09:00-18:00; пн-пт</b>\n"
            "Можно: пн,вт,ср или каждый день.",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        await callback.answer()
        return

    if action == "add_event":
        await state.set_state(ScheduleState.add_event)
        await callback.message.answer(
            "Добавим событие на сегодня.\n"
            "Напиши так:\n"
            "<b>Спорт; 19:00-19:30</b>",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        await callback.answer()
        return

    if action == "suggest":
        target_date = data[2] if len(data) > 2 else local_date
        kind = data[3] if len(data) > 3 else ""
        text, _, slots = await _render_schedule_text(db, user, target_date)
        duration = {"sport": 30, "cook": 45, "shop": 60}.get(kind, 30)
        title = {"sport": "Спорт", "cook": "Готовка", "shop": "Покупки"}.get(kind, "Событие")
        picked = _pick_slot(slots, duration)
        if not picked:
            await callback.answer("Нет свободного окна.", show_alert=True)
            return
        a, b = picked
        await repo.create_schedule_event(
            db,
            user_id=user["id"],
            event_date=target_date,
            start_time=_from_min(a),
            end_time=_from_min(b),
            title=title,
            category=kind or "misc",
            source="suggested",
        )
        await callback.message.answer(text, reply_markup=_schedule_keyboard(target_date), parse_mode="HTML")
        await callback.answer("Добавила")
        return

    await callback.answer()


@router.message(ScheduleState.add_block, lambda m: bool(m.text and not m.text.startswith("/")))
async def schedule_add_block(message: types.Message, state: FSMContext, db) -> None:
    if await _handle_cancel(message, state):
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    raw = (message.text or "").strip()
    parts = [p.strip() for p in raw.split(";")]
    if len(parts) < 2:
        await message.answer("Нужно: <Название; 09:00-18:00; дни>. Попробуй ещё раз.", reply_markup=_cancel_kb())
        return
    title = parts[0] or "Блок"
    m = re.search(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", parts[1])
    if not m:
        await message.answer("Не вижу время. Формат: 09:00-18:00", reply_markup=_cancel_kb())
        return
    st = parse_hhmm(m.group(1))
    en = parse_hhmm(m.group(2))
    if not st or not en:
        await message.answer("Время должно быть HH:MM.", reply_markup=_cancel_kb())
        return
    weekdays = _parse_weekdays(parts[2] if len(parts) > 2 else "пн-пт") or "0,1,2,3,4"
    await repo.create_schedule_block(db, user["id"], title, _hhmm_str(st), _hhmm_str(en), weekdays)
    await state.clear()
    await message.answer("✅ Сохранила блок.", reply_markup=main_menu_keyboard())


@router.message(ScheduleState.add_event, lambda m: bool(m.text and not m.text.startswith("/")))
async def schedule_add_event(message: types.Message, state: FSMContext, db) -> None:
    if await _handle_cancel(message, state):
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    local_date = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    raw = (message.text or "").strip()
    parts = [p.strip() for p in raw.split(";")]
    if len(parts) < 2:
        await message.answer("Нужно: <Название; 19:00-19:30>. Попробуй ещё раз.", reply_markup=_cancel_kb())
        return
    title = parts[0] or "Событие"
    m = re.search(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", parts[1])
    if not m:
        await message.answer("Не вижу время. Формат: 19:00-19:30", reply_markup=_cancel_kb())
        return
    st = parse_hhmm(m.group(1))
    en = parse_hhmm(m.group(2))
    if not st or not en:
        await message.answer("Время должно быть HH:MM.", reply_markup=_cancel_kb())
        return
    await repo.create_schedule_event(db, user["id"], local_date, _hhmm_str(st), _hhmm_str(en), title, category="manual", source="manual")
    await state.clear()
    await message.answer("✅ Добавила событие.", reply_markup=main_menu_keyboard())
