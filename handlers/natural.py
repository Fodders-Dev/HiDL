import datetime
import re

from aiogram import Router, types

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.time import parse_hhmm, local_date_str, format_time_local
from utils.user import ensure_user
from utils.nlp import match_simple_intent

router = Router()


def _extract_amount(text: str):
    m = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except Exception:
        return None


def _extract_time(text: str):
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if hh > 23 or mm > 59:
        return None
    return f"{hh:02d}:{mm:02d}"


@router.message(lambda m: m.text and not m.text.startswith("/"))
async def natural_handler(message: types.Message, db) -> None:
    text_original = message.text or ""
    text = text_original.lower()
    # быстрые намерения: сделал/позже/пропусти
    intent = match_simple_intent(text)
    if intent:
        user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
        today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
        # пытаемся закрыть рутину
        await repo.ensure_user_tasks_for_date(db, user["id"], today)
        tasks = await repo.get_tasks_for_day(db, user["id"], today)
        target_task = next((t for t in tasks if t["status"] not in {"done", "skip"}), None)
        if target_task:
            status_map = {"done": "done", "skip": "skip", "later": "later"}
            await repo.upsert_user_task(db, user["id"], target_task["routine_id"], today, status=status_map[intent])
            if intent == "done":
                await repo.add_points(db, user["id"], 5, local_date=today)
            await message.answer(f"Отметила рутину как {intent}.", reply_markup=main_menu_keyboard())
            return
        # если нет рутины — пытаемся с напоминаниями
        custom = await repo.list_custom_reminders(db, user["id"])
        status_map = {"done": "done", "skip": "skip", "later": "later"}
        for c in custom:
            await repo.log_custom_task(
                db,
                reminder_id=c["id"],
                user_id=user["id"],
                reminder_date=today,
                status=status_map[intent],
            )
            if intent == "done":
                await repo.add_points(db, user["id"], 3, local_date=today)
            await message.answer(f"Отметила: {c['title']} — {intent}.", reply_markup=main_menu_keyboard())
            return
        # если вообще нечего отмечать — падаем дальше по логике

    # траты
    if "потрат" in text or "запиши трату" in text or "стоило" in text:
        amount = _extract_amount(text)
        if amount is None:
            return
        words = text.split()
        category = "другое"
        for w in words:
            if w.isalpha() and not w.startswith("потрат") and not re.match(r"\d", w):
                category = w
                break
        user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
        await repo.add_expense(db, user["id"], amount, category)
        await message.answer(f"Записала трату: {amount:.0f} ({category}).", reply_markup=main_menu_keyboard())
        return

    # напоминание
    if "напомни" in text:
        title = message.text.strip()
        hhmm = _extract_time(text) or "09:00"
        user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
        today = local_date_str(datetime.datetime.utcnow(), user["timezone"])
        freq = 1
        target_weekday = None
        if "каждый день" in text:
            freq = 1
        elif "каждую неделю" in text or "раз в неделю" in text:
            freq = 7
            # если указали день недели
            days_map = {"пн":0,"пон":0,"вт":1,"ср":2,"чт":3,"пт":4,"сб":5,"вс":6}
            for key,val in days_map.items():
                if key in text:
                    target_weekday = val
                    break
        elif "месяц" in text:
            freq = 30
        last_sent = None
        if "завтра" in text:
            last_sent = today
        if "через" in text:
            m = re.search(r"через\s+(\d+)\s*час", text)
            if m:
                hours = int(m.group(1))
                future = datetime.datetime.utcnow() + datetime.timedelta(hours=hours)
                hhmm = format_time_local(future, user["timezone"])
        if "отложи" in text or "перенеси" in text:
            last_sent = today
            if "недел" in text:
                freq = 7
            days_map = {"пн":0,"пон":0,"вт":1,"ср":2,"чт":3,"пт":4,"сб":5,"вс":6}
            for key,val in days_map.items():
                if key in text:
                    target_weekday = val
                    break
        rid = await repo.create_custom_reminder(
            db,
            user_id=user["id"],
            title=title.replace("напомни", "").strip() or "Напоминание",
            reminder_time=hhmm,
            frequency_days=freq,
            target_weekday=target_weekday,
        )
        if last_sent:
            await repo.set_custom_reminder_sent(db, rid, last_sent)
        await message.answer(
            f"Я поставила напоминание «{title}» на {hhmm} (каждые {freq} д.).",
            reply_markup=main_menu_keyboard(),
        )
        return
    # если не распознали запрос — мягко подсказать про основные разделы
    await message.answer(
        "Я читаю это как обычное сообщение и не очень поняла, что сделать.\n\n"
        "Можешь спросить про еду, уборку, стирку, деньги или режим дня — или выбрать раздел кнопками снизу:\n"
        "Сегодня • Еда • Дом • Движение • Напоминания.",
        reply_markup=main_menu_keyboard(),
    )
