import datetime
import html
import re
from dataclasses import dataclass

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import get_gcal_settings
from db import repositories as repo
from keyboards.common import main_menu_keyboard
from services import calendar_service, pulse_service
from utils.menu_match import match_menu_label
from utils.rows import rows_to_dicts
from utils.sender import safe_edit
from utils.time import local_date_str, parse_hhmm
from utils.user import ensure_user
from utils.profile import is_student_mode

router = Router()


class ScheduleState(StatesGroup):
    add_block = State()
    add_event = State()
    add_ics = State()


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="sched:cancel")]]
    )


async def _handle_cancel(message: types.Message, state: FSMContext, user: dict | None = None) -> bool:
    if (message.text or "").strip().lower() in {"отмена", "назад"}:
        await state.clear()
        await message.answer("Отменила.", reply_markup=main_menu_keyboard(user))
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


def _schedule_keyboard(local_date: str, connected: bool = False, user: dict | None = None) -> InlineKeyboardMarkup:
    gcal_row = [InlineKeyboardButton(text="🔗 Google Calendar", callback_data="sched:gcal")]
    if connected:
        gcal_row.append(InlineKeyboardButton(text="🔄 Синхр.", callback_data="sched:sync"))
    student = is_student_mode(user)
    rows = [
            [
                InlineKeyboardButton(text="➕ Блок (работа/учёба/дорога)", callback_data="sched:add_block"),
                InlineKeyboardButton(text="➕ Событие", callback_data="sched:add_event"),
            ],
            gcal_row,
    ]
    if not student:
        rows.extend([
            [InlineKeyboardButton(text="⚡ Вставить спорт 30м", callback_data=f"sched:suggest:{local_date}:sport"),
             InlineKeyboardButton(text="🍳 Вставить готовку 45м", callback_data=f"sched:suggest:{local_date}:cook")],
            [InlineKeyboardButton(text="🛒 Вставить покупки 60м", callback_data=f"sched:suggest:{local_date}:shop")],
        ])
    else:
        rows.append([InlineKeyboardButton(text="⚡ Вставить спорт 30м", callback_data=f"sched:suggest:{local_date}:sport")])
    rows.append([InlineKeyboardButton(text="⬅️ Меню", callback_data="main:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_schedule_text(db, user: dict, local_date: str) -> tuple[str, list[Interval], list[tuple[int, int]]]:
    weekday = datetime.date.fromisoformat(local_date).weekday()
    blocks = rows_to_dicts(await repo.list_schedule_blocks(db, user["id"]))
    unified_events = await calendar_service.get_unified_events_for_date(db, user["id"], local_date)
    conns = await repo.list_user_calendar_connections(db, user["id"], enabled_only=True)

    wake, sleep, _ = pulse_service.get_effective_schedule(user, local_date)
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

    all_day_lines: list[str] = []
    timed_event_lines: list[tuple[int, str]] = []
    for e in unified_events:
        title_str = str(e.get("title") or "Событие")
        title_esc = html.escape(title_str)
        loc = str(e.get("location") or "").strip()
        loc_suffix = (
            f" ({html.escape(loc)})"
            if loc and loc.casefold() not in title_str.casefold()
            else ""
        )
        icon = "📅" if e.get("is_external") else "•"
        if e.get("is_all_day"):
            all_day_lines.append(f"{icon} Весь день — {title_esc}{loc_suffix}")
            continue
        st = parse_hhmm(e.get("start_time") or "")
        en = parse_hhmm(e.get("end_time") or "")
        if not st or not en:
            continue
        s = _to_min(st)
        t = _to_min(en)
        if t <= s:
            continue
        timed_event_lines.append((s, f"{icon} {_from_min(s)}–{_from_min(t)} — {title_esc}{loc_suffix}"))
        if e.get("is_busy", 1):
            busy.append(Interval(s, t, e.get("title") or "Событие", "event"))

    slots = _free_slots(wake_min, sleep_min, busy)

    lines = [f"🗓 <b>Расписание на {local_date}</b>"]
    lines.append(f"🛌 Сон: {sleep}–{wake}")
    if conns:
        prov_labels = ["OAuth" if c.get("provider") == "google_oauth" else ".ics" for c in conns]
        last_sync = str(conns[0].get("last_sync_at") or "")[:16].replace("T", " ")
        sync_note = f", синхр. {last_sync} UTC" if last_sync else ""
        lines.append(f"🔗 Google Calendar: подключён ({', '.join(prov_labels)}{sync_note})")
    else:
        lines.append("🔗 Google Calendar: не подключён (нажми кнопку ниже)")

    if blocks:
        lines.append("\n<b>Фиксировано:</b>")
        fixed = [x for x in busy if x.kind == "block"]
        if fixed:
            for it in sorted(fixed, key=lambda x: x.start):
                lines.append(f"• {_from_min(it.start)}–{_from_min(it.end)} — {html.escape(str(it.title))}")
        else:
            lines.append("• пока нет блоков (работа/учёба/дорога)")
    else:
        lines.append("\n<b>Фиксировано:</b>\n• пока нет блоков (работа/учёба/дорога)")

    if all_day_lines or timed_event_lines:
        lines.append("\n<b>Сегодняшние события:</b>")
        lines.extend(all_day_lines)
        for _, txt_line in sorted(timed_event_lines, key=lambda p: p[0]):
            lines.append(txt_line)
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


async def _gcal_setup_payload(db, user: dict) -> tuple[str, InlineKeyboardMarkup]:
    conns = await repo.list_user_calendar_connections(db, int(user["id"]), enabled_only=True)
    cfg = get_gcal_settings()
    lines = ["🔗 <b>Интеграция с Google Calendar</b>"]
    if conns:
        for c in conns:
            prov_name = "Google OAuth (чтение + запись)" if c.get("provider") == "google_oauth" else "Секретная ссылка .ics (чтение)"
            sync_ts = str(c.get("last_sync_at") or "")[:16].replace("T", " ")
            sync_info = f"синхр. {sync_ts} UTC" if sync_ts else "ещё не синхронизировано"
            err_info = f"\n  ⚠️ {html.escape(str(c['last_error']))}" if c.get("last_error") else ""
            lines.append(f"• ✅ <b>{prov_name}</b> — {sync_info}{err_info}")
    else:
        lines.append(
            "Пока не подключён.\n\n"
            "<b>Быстрый способ (за 30 сек):</b> вставь секретный адрес в формате iCal (<code>.ics</code>) из настроек Google Календаря — "
            "я буду обновлять события каждые 15 минут, предупреждать до встреч, не дёргать на созвонах и советовать отбой пораньше перед утренними делами."
        )

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="📋 Вставить .ics ссылку", callback_data="sched:gcal_ics")],
    ]
    if cfg.oauth_configured:
        oauth_url = await calendar_service.build_gcal_oauth_start_url(db, int(user["id"]), settings=cfg)
        if oauth_url:
            rows.append([InlineKeyboardButton(text="🔐 Войти через Google (OAuth)", url=oauth_url)])
    else:
        rows.append([InlineKeyboardButton(text="🔐 Google OAuth (чтение+запись)", callback_data="sched:gcal_oauth")])

    if conns:
        rows.append([
            InlineKeyboardButton(text="🔄 Синхронизировать", callback_data="sched:sync"),
            InlineKeyboardButton(text="🔌 Отключить", callback_data="sched:gcal_disconnect"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ К расписанию", callback_data="sched:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("schedule"))
@router.message(lambda m: match_menu_label(m.text, {"расписание"}))
async def schedule_today(message: types.Message, state: FSMContext, db) -> None:
    await state.clear()
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    local_date = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    conns = await repo.list_user_calendar_connections(db, user["id"], enabled_only=True)
    text, _, _ = await _render_schedule_text(db, user, local_date)
    await message.answer(text, reply_markup=_schedule_keyboard(local_date, connected=bool(conns), user=user), parse_mode="HTML")


@router.message(Command("gcal"))
async def gcal_cmd(message: types.Message, state: FSMContext, db) -> None:
    await state.clear()
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        url = parts[1].strip()
        ok_url, err_msg = calendar_service.validate_ics_url(url)
        if not ok_url:
            await message.answer(f"❌ {html.escape(err_msg)}")
            return
        await repo.upsert_calendar_connection(db, user["id"], "ics", label="Google Calendar (.ics)", ics_url=url)
        sync_res = await calendar_service.sync_user_calendars(db, user)
        local_date = local_date_str(datetime.datetime.utcnow(), user["timezone"])
        text, _, _ = await _render_schedule_text(db, user, local_date)
        await message.answer(
            f"✅ <b>Google Calendar подключён!</b> Загружено событий: {sync_res['synced_events']}.\n\n{text}",
            reply_markup=_schedule_keyboard(local_date, connected=True, user=user),
            parse_mode="HTML",
        )
        return
    txt, kb = await _gcal_setup_payload(db, user)
    await message.answer(txt, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data and c.data.startswith("sched:"))
async def schedule_callbacks(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    data = (callback.data or "").split(":")
    action = data[1] if len(data) > 1 else ""
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    local_date = local_date_str(datetime.datetime.utcnow(), user["timezone"])

    if action == "cancel":
        await state.clear()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("Отменила.", reply_markup=main_menu_keyboard(user))
        await callback.answer("Отменила.")
        return

    if action == "home":
        conns = await repo.list_user_calendar_connections(db, user["id"], enabled_only=True)
        text, _, _ = await _render_schedule_text(db, user, local_date)
        await safe_edit(callback.message, text, reply_markup=_schedule_keyboard(local_date, connected=bool(conns), user=user), parse_mode="HTML")
        await callback.answer()
        return

    if action == "gcal":
        txt, kb = await _gcal_setup_payload(db, user)
        await safe_edit(callback.message, txt, reply_markup=kb, parse_mode="HTML")
        await callback.answer()
        return

    if action == "gcal_ics":
        await state.set_state(ScheduleState.add_ics)
        await callback.message.answer(
            "Пришли мне секретную ссылку формата <b>iCal (.ics)</b> из Google Календаря:\n"
            "<i>Настройки календаря → Интеграция календаря → Секретный адрес в формате iCal (https://...)</i>",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        await callback.answer()
        return

    if action == "gcal_oauth":
        cfg = get_gcal_settings()
        if not cfg.oauth_configured:
            redir = html.escape(cfg.redirect_uri or "/gcal/callback")
            await callback.message.answer(
                "🔐 <b>Двусторонний Google OAuth пока не настроен на сервере.</b>\n"
                "Пока можешь за 30 секунд подключить чтение через <b>.ics</b> ссылку, "
                f"а для записи в Google Calendar нужно добавить <code>GCAL_CLIENT_ID</code> и <code>GCAL_CLIENT_SECRET</code> "
                f"(Redirect URI: <code>{redir}</code>).",
                parse_mode="HTML",
            )
            await callback.answer()
            return
        url = await calendar_service.build_gcal_oauth_start_url(db, user["id"], settings=cfg)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔗 Авторизовать Google", url=url)]])
        await callback.message.answer("Открой ссылку и разреши доступ к календарю:", reply_markup=kb)
        await callback.answer()
        return

    if action == "gcal_disconnect":
        await repo.delete_calendar_connections(db, user["id"])
        await callback.answer("Календарь отключён")
        txt, kb = await _gcal_setup_payload(db, user)
        await safe_edit(callback.message, txt, reply_markup=kb, parse_mode="HTML")
        return

    if action == "sync":
        res = await calendar_service.sync_user_calendars(db, user)
        conns = await repo.list_user_calendar_connections(db, user["id"], enabled_only=True)
        await callback.answer(f"Синхронизировано событий: {res['synced_events']}", show_alert=True)
        text, _, _ = await _render_schedule_text(db, user, local_date)
        await safe_edit(callback.message, text, reply_markup=_schedule_keyboard(local_date, connected=bool(conns), user=user), parse_mode="HTML")
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
        if is_student_mode(user) and kind in {"cook", "shop"}:
            await callback.answer("Этот бытовой вариант скрыт в профиле ученика.", show_alert=True)
            return
        text, _, slots = await _render_schedule_text(db, user, target_date)
        duration = {"sport": 30, "cook": 45, "shop": 60}.get(kind, 30)
        title = {"sport": "Спорт", "cook": "Готовка", "shop": "Покупки"}.get(kind, "Событие")
        picked = _pick_slot(slots, duration)
        if not picked:
            await callback.answer("Нет свободного окна.", show_alert=True)
            return
        a, b = picked
        await calendar_service.create_unified_event(
            db,
            user,
            title=title,
            event_date=target_date,
            start_time=_from_min(a),
            end_time=_from_min(b),
            category=kind or "misc",
            push_to_google=True,
        )
        conns = await repo.list_user_calendar_connections(db, user["id"], enabled_only=True)
        await callback.message.answer(text, reply_markup=_schedule_keyboard(target_date, connected=bool(conns), user=user), parse_mode="HTML")
        await callback.answer("Добавила")
        return

    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("cal:shift_bed:"))
async def cal_shift_bedtime(callback: types.CallbackQuery, db) -> None:
    target_hhmm = callback.data.split(":", 2)[2].strip()
    if not parse_hhmm(target_hhmm):
        await callback.answer("Неверный формат времени", show_alert=True)
        return
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    local_date = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    eff_wake, _, is_weekend = pulse_service.get_effective_schedule(user, local_date)
    if is_weekend:
        await repo.update_user_weekend_schedule(db, user["id"], eff_wake, target_hhmm)
    else:
        await repo.update_user_sleep(db, user["id"], target_hhmm)

    refreshed_row = await repo.get_user(db, user["id"])
    refreshed = dict(refreshed_row) if refreshed_row else user
    wellness = await repo.get_wellness(db, user["id"])
    slots = pulse_service.compute_daily_slots(refreshed, dict(wellness) if wellness else None, local_date)
    for s_key in ("winddown", "bedtime"):
        spec = next((s for s in slots if s["slot_key"] == s_key), None)
        chk = await repo.get_pulse_checkin(db, user["id"], local_date, s_key)
        if spec and chk and chk.get("status") in {"pending", "snoozed"}:
            iso = spec["scheduled_at_utc"].isoformat()
            await repo.update_pulse_checkin(db, int(chk["id"]), scheduled_at_utc=iso, next_ping_at_utc=iso)

    await safe_edit(
        callback.message,
        f"🌙 Отбой сдвинут на <b>{html.escape(target_hhmm)}</b>, Пульс на вечер обновлён!",
        parse_mode="HTML",
    )
    await callback.answer("Готово!")


@router.callback_query(lambda c: c.data == "cal:dismiss")
async def cal_dismiss_nudge(callback: types.CallbackQuery) -> None:
    await safe_edit(callback.message, "👍 Хорошо, оставляем обычный график отбоя.", parse_mode="HTML")
    await callback.answer()


@router.message(ScheduleState.add_ics, lambda m: bool(m.text and not m.text.startswith("/")))
async def schedule_add_ics(message: types.Message, state: FSMContext, db) -> None:
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    if await _handle_cancel(message, state, user):
        return
    url = (message.text or "").strip()
    ok_url, err_msg = calendar_service.validate_ics_url(url)
    if not ok_url:
        await message.answer(f"❌ {html.escape(err_msg)}\nПопробуй ещё раз:", reply_markup=_cancel_kb())
        return
    await repo.upsert_calendar_connection(db, user["id"], "ics", label="Google Calendar (.ics)", ics_url=url)
    res = await calendar_service.sync_user_calendars(db, user)
    await state.clear()
    local_date = local_date_str(datetime.datetime.utcnow(), user["timezone"])
    text, _, _ = await _render_schedule_text(db, user, local_date)
    await message.answer(
        f"✅ <b>Google Calendar (.ics) подключён!</b> Событий загружено: {res['synced_events']}.\n\n{text}",
        reply_markup=_schedule_keyboard(local_date, connected=True, user=user),
        parse_mode="HTML",
    )


@router.message(ScheduleState.add_block, lambda m: bool(m.text and not m.text.startswith("/")))
async def schedule_add_block(message: types.Message, state: FSMContext, db) -> None:
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    if await _handle_cancel(message, state, user):
        return
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
    await message.answer("✅ Сохранила блок.", reply_markup=main_menu_keyboard(user))


@router.message(ScheduleState.add_event, lambda m: bool(m.text and not m.text.startswith("/")))
async def schedule_add_event(message: types.Message, state: FSMContext, db) -> None:
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    if await _handle_cancel(message, state, user):
        return
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
    res = await calendar_service.create_unified_event(
        db,
        dict(user),
        title=title,
        event_date=local_date,
        start_time=_hhmm_str(st),
        end_time=_hhmm_str(en),
        category="manual",
        push_to_google=True,
    )
    await state.clear()
    if res.get("google_synced"):
        await message.answer("✅ Добавила событие (и синхронизировала с Google Calendar).", reply_markup=main_menu_keyboard(user))
    else:
        note = f"\nℹ️ {res['google_error']}" if res.get("google_error") else ""
        await message.answer(f"✅ Добавила событие.{note}", reply_markup=main_menu_keyboard(user))
