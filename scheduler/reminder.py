import datetime
from typing import Optional
from collections import defaultdict

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db import repositories as repo
from utils.time import local_date_str, should_trigger, tzinfo_from_string
from utils.logger import log_debug


class ReminderScheduler:
    """APS-based reminder scheduler for routine check-ins."""

    def __init__(self, bot: Bot, db_conn):
        self.bot = bot
        self.conn = db_conn
        self.scheduler = AsyncIOScheduler(timezone="UTC")

    def start(self) -> None:
        self.scheduler.add_job(self._tick, "interval", seconds=60, id="routine_tick")
        self.scheduler.add_job(self._tick_wellness, "interval", seconds=60, id="wellness_tick")
        self.scheduler.add_job(self._tick_bills, "cron", hour=9, minute=0, id="bills_ping")
        self.scheduler.add_job(self._tick_weekly_finance, "cron", hour=9, minute=0, id="weekly_finance")
        self.scheduler.add_job(self._reset_points_month, "cron", day=1, hour=0, minute=5, id="points_reset")
        self.scheduler.add_job(self._tick_care, "cron", hour=9, minute=15, id="care_tick")
        self.scheduler.add_job(self._tick_weight_prompt, "cron", hour=8, minute=30, id="weight_prompt")
        self.scheduler.add_job(self._weekly_home_plan, "cron", day_of_week="sun", hour=10, minute=0, id="home_plan_weekly")
        self.scheduler.add_job(self._tick_day_plan, "interval", minutes=5, id="day_plan_morning")
        self.scheduler.add_job(self._tick_meds, "interval", seconds=60, id="meds_tick")
        self.scheduler.start()

    async def _tick(self) -> None:
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user in users:
            user = dict(user)
            local_date = local_date_str(now_utc, user["timezone"])
            if user["pause_until"] and local_date <= (user["pause_until"] or ""):
                log_debug(f"[tick] skip user={user['id']} pause_until={user['pause_until']}")
                continue
            await repo.ensure_user_routines(self.conn, user["id"])
            routines = await repo.list_user_routines(self.conn, user["id"])
            for routine in routines:
                if routine["last_sent_date"] == local_date:
                    log_debug(f"[tick] routine already sent user={user['id']} routine={routine['routine_id']} date={local_date}")
                    continue
                if not should_trigger(
                    now_utc, user["timezone"], routine["reminder_time"], window_minutes=2
                ):
                    continue
                await self._send_routine(user, routine, local_date)
                await repo.set_routine_sent(
                    self.conn, user["id"], routine["routine_id"], local_date
                )
                await repo.upsert_user_task(
                    self.conn,
                    user["id"],
                    routine["routine_id"],
                    local_date,
                    status="pending",
                    note="",
                )
            await self._tick_custom(user, now_utc, local_date)

    async def _tick_day_plan(self) -> None:
        """Утренний пинг по плану дня: один раз около времени подъёма."""
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user_row in users:
            user = dict(user_row)
            local_date = local_date_str(now_utc, user["timezone"])
            if user["pause_until"] and local_date <= (user["pause_until"] or ""):
                continue
            plan = await repo.get_day_plan(self.conn, user["id"], local_date)
            if not plan:
                continue
            plan = dict(plan)
            if plan.get("morning_sent") == local_date:
                log_debug(f"[day_plan] already sent user={user['id']} date={local_date}")
                continue
            wake_time = user.get("wake_up_time") or "08:00"
            if not should_trigger(now_utc, user["timezone"], wake_time, window_minutes=15):
                continue
            items_rows = await repo.list_day_plan_items(self.conn, user["id"], local_date)
            items = [dict(r) for r in items_rows]
            if not items:
                continue
            important = [i for i in items if i.get("is_important")]
            extra = [i for i in items if not i.get("is_important")]
            lines = ["Доброе утро. Вчера ты запланировал(а) на сегодня:"]
            for it in important:
                lines.append(f"• {it['title']}")
            for it in extra:
                lines.append(f"• {it['title']}")
            lines.append("Что-то убрать или добавить?")
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Всё ок", callback_data="dplan:ok"),
                    ],
                    [
                        InlineKeyboardButton(text="Удалить пункт", callback_data="dplan:delmenu"),
                    ],
                    [
                        InlineKeyboardButton(text="Добавить дело", callback_data="dplan:add"),
                    ],
                ]
            )
            await self.bot.send_message(chat_id=user["telegram_id"], text="\n".join(lines), reply_markup=kb)
            await repo.mark_day_plan_morning_sent(self.conn, plan["id"], local_date)
            log_debug(f"[day_plan] sent user={user['id']} items={len(items)} date={local_date}")

    async def _tick_meds(self) -> None:
        """Пинг по витаминам/таблеткам на основе meds/med_logs."""
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user_row in users:
            user = dict(user_row)
            local_date = local_date_str(now_utc, user["timezone"])
            if user["pause_until"] and local_date <= (user["pause_until"] or ""):
                continue
            meds = await repo.list_meds(self.conn, user["id"], active_only=True)
            if not meds:
                continue
            wellness_row = await repo.get_wellness(self.conn, user["id"])
            tone = "neutral"
            if wellness_row:
                w = dict(wellness_row)
                tone = w.get("tone", "neutral")
            for med_row in meds:
                med = dict(med_row)
                times_raw = med.get("times", "")
                if not times_raw:
                    continue
                for t in times_raw.split(","):
                    t = t.strip()
                    if not t:
                        continue
                    existing = await repo.get_med_log_by_key(
                        self.conn, user["id"], med["id"], local_date, t
                    )
                    if existing:
                        # уже есть лог (ожидание или отмечено) — не шлём заново
                        continue
                    if not should_trigger(now_utc, user["timezone"], t, window_minutes=2):
                        continue
                    text = (
                        f"💊 Пора «{med['name']}»: {med['dose_text'] or 'принять дозу'}.\n"
                        "Ты уже принял(а)?"
                    )
                    if tone == "soft":
                        text += "\nЕсли сейчас не до этого — можно перенести на позже."
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="Принял(а)",
                                    callback_data="",  # будет подставлен после вставки лога
                                ),
                                InlineKeyboardButton(
                                    text="Позже",
                                    callback_data="",
                                ),
                            ]
                        ]
                    )
                    # сначала создаём лог, чтобы знать id
                    log_id = await repo.insert_med_log(
                        self.conn, user["id"], med["id"], local_date, t
                    )
                    keyboard.inline_keyboard[0][0].callback_data = f"medtake:{log_id}"
                    keyboard.inline_keyboard[0][1].callback_data = f"medskip:{log_id}"
                    await self.bot.send_message(
                        chat_id=user["telegram_id"],
                        text=text,
                        reply_markup=keyboard,
                    )

    async def _tick_custom(
        self, user: dict, now_utc: datetime.datetime, local_date: str
    ) -> None:
        reminders = await repo.list_custom_reminders(self.conn, user["id"])
        for reminder in reminders:
            reminder = dict(reminder)
            if reminder["last_sent_date"] == local_date:
                log_debug(f"[custom] skip already sent user={user['id']} rem={reminder['id']} date={local_date}")
                continue
            if reminder["last_sent_date"]:
                try:
                    last_date = datetime.date.fromisoformat(reminder["last_sent_date"])
                    current_date = datetime.date.fromisoformat(local_date)
                    delta_days = (current_date - last_date).days
                    if delta_days < reminder["frequency_days"]:
                        log_debug(
                            f"[custom] skip freq user={user['id']} rem={reminder['id']} "
                            f"delta_days={delta_days} freq_days={reminder['frequency_days']}"
                        )
                        continue
                except Exception:
                    pass
            # check weekday constraint if set
            if reminder.get("target_weekday") is not None:
                tz = user["timezone"]
                tzinfo = tzinfo_from_string(tz)
                local_dt = now_utc.replace(tzinfo=datetime.timezone.utc).astimezone(tzinfo)
                if local_dt.weekday() != reminder["target_weekday"]:
                    log_debug(
                        f"[custom] skip weekday user={user['id']} rem={reminder['id']} "
                        f"today_wd={local_dt.weekday()} target_wd={reminder['target_weekday']}"
                    )
                    continue
            if not should_trigger(
                now_utc, user["timezone"], reminder["reminder_time"], window_minutes=2
            ):
                log_debug(
                    f"[custom] not in window user={user['id']} rem={reminder['id']} "
                    f"time={reminder['reminder_time']} now_utc={now_utc.isoformat()}"
                )
                continue
            await self._send_custom(user, reminder, local_date)
            log_debug(f"[custom] send user={user['id']} rem={reminder['id']} time={reminder['reminder_time']} date={local_date}")
            await repo.set_custom_reminder_sent(self.conn, reminder["id"], local_date)
            await repo.log_custom_task(
                self.conn,
                reminder_id=reminder["id"],
                user_id=user["id"],
                reminder_date=local_date,
                status="pending",
            )

    async def _send_routine(
        self, user: dict, routine: dict, local_date: str
    ) -> Optional[None]:
        items = await repo.list_routine_steps_for_routine(self.conn, user["id"], routine["routine_id"])
        task = await repo.get_user_task(self.conn, user["id"], routine["routine_id"], local_date)
        done = set()
        if task and task["note"]:
            for part in task["note"].split(","):
                try:
                    done.add(int(part))
                except Exception:
                    continue
        lines = []
        items_list = [dict(i) for i in items]
        id_index = {row["id"]: idx for idx, row in enumerate(items_list)}
        for idx, item in enumerate(items_list):
            trigger_id = item.get("trigger_after_step_id")
            if trigger_id:
                parent_idx = id_index.get(trigger_id)
                if parent_idx is not None and parent_idx not in done:
                    continue
            if idx in done:
                lines.append(f"• <s>{item['title']}</s>")
            else:
                lines.append(f"• {item['title']}")
        task_lines = "\n".join(lines)
        text = (
            f"🕒 {routine['title']} ({routine['reminder_time']} локального времени)\n\n"
            f"{task_lines}\n\nОтметь статус:"
        )
        rows = [
            [
                InlineKeyboardButton(
                    text="Сделал(а) ✔",
                    callback_data=f"routine:{routine['routine_id']}:{local_date}:done",
                ),
                InlineKeyboardButton(
                    text="Напомнить позже",
                    callback_data=f"routine:{routine['routine_id']}:{local_date}:later",
                ),
                InlineKeyboardButton(
                    text="Пропустить",
                    callback_data=f"routine:{routine['routine_id']}:{local_date}:skip",
                ),
            ]
        ]
        for idx, item in enumerate(items_list):
            mark = "☑️" if idx in done else "⬜️"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{mark} {item['title'][:24]}",
                        callback_data=f"ritem:{routine['routine_id']}:{local_date}:{idx}",
                    )
                ]
            )
        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        await self.bot.send_message(
            chat_id=user["telegram_id"], text=text, reply_markup=keyboard
        )

    async def _send_custom(
        self, user: dict, reminder: dict, local_date: str
    ) -> Optional[None]:
        text = f"🔔 Напоминание: {reminder['title']}\nВремя: {reminder['reminder_time']} (локально)"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Сделал(а) ✔",
                        callback_data=f"custom:{reminder['id']}:{local_date}:done",
                    ),
                    InlineKeyboardButton(
                        text="Напомнить позже",
                        callback_data=f"custom:{reminder['id']}:{local_date}:later",
                    ),
                    InlineKeyboardButton(
                        text="Пропустить",
                        callback_data=f"custom:{reminder['id']}:{local_date}:skip",
                    ),
                ]
            ]
        )
        await self.bot.send_message(
            chat_id=user["telegram_id"], text=text, reply_markup=keyboard
        )
        log_debug(
            f"[custom] delivered user={user['id']} rem={reminder['id']} time={reminder['reminder_time']} date={local_date}"
        )

    async def _tick_wellness(self) -> None:
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user in users:
            user = dict(user)
            local_date = local_date_str(now_utc, user["timezone"])
            if user["pause_until"] and local_date <= (user["pause_until"] or ""):
                continue
            wellness_row = await repo.get_wellness(self.conn, user["id"])
            if not wellness_row:
                continue
            wellness = dict(wellness_row)
            water_times = (
                wellness.get("water_times", "11:00,16:00").split(",")
                if wellness.get("water_times")
                else []
            )
            meal_times = (
                wellness.get("meal_times", "13:00,19:00").split(",")
                if wellness.get("meal_times")
                else []
            )
            # Water reminders
            if wellness["water_enabled"]:
                for t in water_times:
                    key = f"{local_date}-{t}"
                    if wellness.get("water_last_key") == key:
                        continue
                    if should_trigger(now_utc, user["timezone"], t, window_minutes=2):
                        text = "💧 Напоминание: выпей стакан воды."
                        if wellness and wellness.get("tone") == "soft":
                            text += " Даже пару глотков — уже хорошо."
                        if wellness and wellness.get("tone") == "pushy":
                            text += " Сделай это сейчас."
                        await self.bot.send_message(chat_id=user["telegram_id"], text=text)
                        await repo.upsert_wellness(
                            self.conn, user["id"], water_last_key=key
                        )
                        wellness = await repo.get_wellness(self.conn, user["id"])
            # Meal reminders
            if wellness.get("meal_enabled"):
                for t in meal_times:
                    key = f"{local_date}-{t}"
                    if wellness.get("meal_last_key") == key:
                        continue
                    if should_trigger(now_utc, user["timezone"], t, window_minutes=2):
                        text = "🍲 Ты ел(а) за последние пару часов? Даже перекус пойдёт."
                        if wellness and wellness.get("tone") == "soft":
                            text += " Если нет — возьми что-то простое, я верю в тебя."
                        if wellness and wellness.get("tone") == "pushy":
                            text += " Не тянем, сходи за едой."
                        await self.bot.send_message(chat_id=user["telegram_id"], text=text)
                        await repo.upsert_wellness(
                            self.conn, user["id"], meal_last_key=key
                        )
                        wellness = await repo.get_wellness(self.conn, user["id"])

    async def _tick_bills(self) -> None:
        users = await repo.list_users(self.conn)
        now_utc = datetime.datetime.utcnow()
        for user in users:
            user = dict(user)
            local_date = local_date_str(now_utc, user["timezone"])
            bills = await repo.bills_due_soon(self.conn, user["id"], local_date, days_ahead=3)
            if not bills:
                continue
            lines = [f"{b['title']}: до {b['due_date']} (~{b['amount']:.0f} ₽)" for b in bills]
            text = "📅 Счета скоро к оплате:\n" + "\n".join(lines)
            await self.bot.send_message(chat_id=user["telegram_id"], text=text)

    async def _tick_weekly_finance(self) -> None:
        """Раз в день в 09:00 UTC: если у пользователя сегодня воскресенье (локально), отправить недельный отчёт."""
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user in users:
            user = dict(user)
            tzinfo = tzinfo_from_string(user["timezone"])
            local_dt = now_utc.replace(tzinfo=datetime.timezone.utc).astimezone(tzinfo)
            # воскресенье локально
            if local_dt.weekday() != 6:
                continue
            expenses = await repo.expenses_last_days(self.conn, user["id"], days=7)
            per_cat = defaultdict(float)
            total = 0.0
            for e in expenses:
                per_cat[e["category"]] += e["amount"]
                total += e["amount"]
            budget = await repo.get_budget(self.conn, user["id"])
            month_total = await repo.monthly_expense_sum(self.conn, user["id"])
            cat_limits = await repo.list_budget_categories(self.conn, user["id"])
            cat_lines = []
            for c in cat_limits:
                spent_cat = await repo.category_expense_sum(self.conn, user["id"], c["category"], days=30)
                over = "⚠️" if spent_cat > c["limit_amount"] > 0 else ""
                cat_lines.append(f"{over}{c['category']}: {spent_cat:.0f}/{c['limit_amount']:.0f}")
            text = "Недельный отчёт по деньгам:\n"
            if total == 0:
                text += "Нет записей за 7 дней.\n"
            else:
                text += f"Всего за 7 дней: {total:.0f}\n" + "\n".join(f"- {cat}: {amt:.0f}" for cat, amt in per_cat.items())
            text += f"\nМесяц: {month_total:.0f}"
            if budget and budget["monthly_limit"] > 0:
                text += f" / лимит {budget['monthly_limit']:.0f}"
                if month_total > budget["monthly_limit"]:
                    text += " ⚠️ превысил лимит"
            if cat_lines:
                text += "\nКатегории:\n" + "\n".join(cat_lines)
            await self.bot.send_message(chat_id=user["telegram_id"], text=text)

    async def _reset_points_month(self) -> None:
        """Первое число — обнулить помесячные очки."""
        current_month = datetime.datetime.utcnow().strftime("%Y-%m")
        await repo.reset_month_points(self.conn, current_month)

    async def _tick_care(self) -> None:
        """Раз в день напоминаем про здоровье/бумажки по интервалам."""
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user in users:
            user = dict(user)
            local_date = local_date_str(now_utc, user["timezone"])
            today = datetime.date.fromisoformat(local_date)
            care_items = [
                ("last_care_dentist", 180, "🦷 Давно не было стоматолога? Запишись на осмотр/чистку."),
                ("last_care_vision", 365, "👓 Проверь зрение, если давно не проверял(а)."),
                ("last_care_firstaid", 180, "🩹 Загляни в аптечку: сроки годности, что нужно докупить."),
                ("last_care_brush", 90, "🪥 Пора сменить щётку/насадку?"),
            ]
            for col, days, text in care_items:
                last = user.get(col) or ""
                due = True
                if last:
                    try:
                        last_dt = datetime.date.fromisoformat(last)
                        due = (today - last_dt).days >= days
                    except Exception:
                        due = True
                if due:
                    note_date = format_date_display(local_date)
                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="Отметить сделанным", callback_data=f"care:{col}:{local_date}")]
                        ]
                    )
                    await self.bot.send_message(
                        chat_id=user["telegram_id"],
                        text=f"{text}\nДата сегодня: {note_date}",
                        reply_markup=kb,
                    )

    async def _tick_weight_prompt(self) -> None:
        """Раз в день напоминаем про вес, если не спрашивали 7 дней."""
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user in users:
            user = dict(user)
            local_date = local_date_str(now_utc, user["timezone"])
            last = user.get("last_weight_prompt") or ""
            due = True
            if last:
                try:
                    last_dt = datetime.date.fromisoformat(last)
                    today = datetime.date.fromisoformat(local_date)
                    due = (today - last_dt).days >= 7
                except Exception:
                    due = True
            if due:
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="Обновить вес", callback_data="move:weight")]]
                )
                await self.bot.send_message(
                    chat_id=user["telegram_id"],
                    text="⚖ Обновишь вес? Коротко и без оценок — только цифра.",
                    reply_markup=kb,
                )
                await repo.update_weight_prompt(self.conn, user["id"], local_date)

    async def _weekly_home_plan(self) -> None:
        """По воскресеньям присылать план по дому на неделю."""
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user in users:
            user = dict(user)
            tzinfo = tzinfo_from_string(user["timezone"])
            local_dt = now_utc.replace(tzinfo=datetime.timezone.utc).astimezone(tzinfo)
            if local_dt.weekday() != 6:
                continue
            today = local_date_str(now_utc, user["timezone"])
            await repo.ensure_regular_tasks(self.conn, user["id"], today)
            tasks = await repo.list_regular_tasks(self.conn, user["id"], due_only=False)
            if not tasks:
                continue
            lines = ["План по дому на неделю:"]
            for t in tasks:
                lines.append(f"• {t['title']} — до {t['next_due_date']}")
            await self.bot.send_message(chat_id=user["telegram_id"], text="\n".join(lines))
