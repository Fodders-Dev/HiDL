import datetime
from typing import Optional
from collections import defaultdict

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db import repositories as repo
from services import calendar_service, pulse_service
from utils.time import format_date_display, local_date_str, should_trigger, tzinfo_from_string
from utils.gender import done_button_label, button_label, g
from utils.logger import log_debug


def _hhmm_to_min(hhmm: str) -> Optional[int]:
    try:
        dt = datetime.datetime.strptime(hhmm, "%H:%M")
    except Exception:
        return None
    return dt.hour * 60 + dt.minute


def _min_to_hhmm(minutes: int) -> str:
    m = int(minutes) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def _delta_minutes(current: int, target: int) -> int:
    diff = target - current
    if diff > 720:
        diff -= 1440
    if diff < -720:
        diff += 1440
    return diff


def _sleep_window_active(user: dict, now_utc: datetime.datetime) -> bool:
    if not user.get("sleep_mode_enabled"):
        return False
    sleep_start = user.get("sleep_target_sleep") or user.get("sleep_time") or "23:00"
    sleep_end = user.get("sleep_target_wake") or user.get("wake_up_time") or "08:00"
    start_min = _hhmm_to_min(sleep_start)
    end_min = _hhmm_to_min(sleep_end)
    if start_min is None or end_min is None:
        return False
    tzinfo = tzinfo_from_string(user.get("timezone", "UTC"))
    local_dt = now_utc.replace(tzinfo=datetime.timezone.utc).astimezone(tzinfo)
    local_min = local_dt.hour * 60 + local_dt.minute
    if start_min == end_min:
        return True
    if start_min < end_min:
        return start_min <= local_min < end_min
    return local_min >= start_min or local_min < end_min


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
        self.scheduler.add_job(self._tick_day_plan_evening, "interval", minutes=15, id="day_plan_evening")
        self.scheduler.add_job(self._tick_meds, "interval", seconds=60, id="meds_tick")
        self.scheduler.add_job(self._tick_affirmations, "interval", minutes=5, id="affirmations_tick")
        self.scheduler.add_job(self._tick_focus, "interval", seconds=60, id="focus_tick")
        self.scheduler.add_job(self._tick_sleep_mode, "interval", minutes=5, id="sleep_mode_tick")
        self.scheduler.add_job(self._tick_daily_brief, "interval", minutes=5, id="daily_brief_tick")
        self.scheduler.add_job(self._tick_pulse, "interval", seconds=60, id="pulse_tick")
        self.scheduler.add_job(self._tick_calendar_sync, "interval", minutes=15, id="calendar_sync_tick")
        self.scheduler.add_job(self._tick_calendar_alerts, "interval", seconds=60, id="calendar_alerts_tick")
        self.scheduler.start()

    @staticmethod
    def _legacy_pings_active(user: dict) -> bool:
        """Legacy-кроны активны только если включены явно или отключён единый Пульс."""
        return bool(user.get("legacy_pings_enabled", 0)) or not bool(
            user.get("pulse_enabled", 1)
        )

    async def _tick_pulse(self) -> None:
        await pulse_service.process_pulse_tick(self.conn, self.bot)

    async def _tick_calendar_sync(self) -> None:
        await calendar_service.process_calendar_sync_tick(self.conn)

    async def _tick_calendar_alerts(self) -> None:
        await calendar_service.process_calendar_alerts_tick(self.conn, self.bot)

    async def _safe_send_message(
        self,
        user: dict,
        local_date: str,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        **kwargs,
    ) -> bool:
        telegram_id = user.get("telegram_id")
        if not telegram_id:
            log_debug(f"[send] missing telegram_id user={user.get('id')}")
            return False
        try:
            await self.bot.send_message(
                chat_id=telegram_id, text=text, reply_markup=reply_markup, **kwargs
            )
            return True
        except TelegramForbiddenError as e:
            msg = str(e)
            log_debug(
                f"[send] forbidden user={user.get('id')} chat_id={telegram_id} err={msg}"
            )
            try:
                if "bots can't send messages to bots" in msg:
                    await repo.set_user_pause(self.conn, user["id"], "9999-12-31")
                else:
                    pause_until = (
                        datetime.date.fromisoformat(local_date)
                        + datetime.timedelta(days=7)
                    ).isoformat()
                    await repo.set_user_pause(self.conn, user["id"], pause_until)
            except Exception:
                pass
            return False
        except TelegramAPIError as e:
            log_debug(
                f"[send] telegram api error user={user.get('id')} chat_id={telegram_id} err={e}"
            )
            return False
        except Exception as e:
            log_debug(
                f"[send] unexpected error user={user.get('id')} chat_id={telegram_id} err={e}"
            )
            return False

    async def _tick(self) -> None:
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user in users:
            user = dict(user)
            local_date = local_date_str(now_utc, user["timezone"])
            if user["pause_until"] and local_date <= (user["pause_until"] or ""):
                log_debug(f"[tick] skip user={user['id']} pause_until={user['pause_until']}")
                continue
            if user.get("quiet_mode"):
                await self._tick_custom(user, now_utc, local_date)
                continue
            if _sleep_window_active(user, now_utc):
                await self._tick_custom(user, now_utc, local_date)
                continue
            if self._legacy_pings_active(user):
                await repo.ensure_user_routines(self.conn, user["id"])
                routines = await repo.list_user_routines(self.conn, user["id"])
                for routine in routines:
                    if routine["last_sent_date"] == local_date:
                        log_debug(f"[tick] routine already sent user={user['id']} routine={routine['routine_id']} date={local_date}")
                        continue
                    if not should_trigger(
                        now_utc, user["timezone"], routine["reminder_time"], window_minutes=5
                    ):
                        continue
                    sent = await self._send_routine(user, routine, local_date)
                    if not sent:
                        continue
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
            if user.get("quiet_mode"):
                continue
            if not self._legacy_pings_active(user):
                continue
            if _sleep_window_active(user, now_utc):
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
            sent = await self._safe_send_message(
                user, local_date, "\n".join(lines), reply_markup=kb
            )
            if sent:
                await repo.mark_day_plan_morning_sent(self.conn, plan["id"], local_date)
                log_debug(
                    f"[day_plan] sent user={user['id']} items={len(items)} date={local_date}"
                )

    async def _tick_day_plan_evening(self) -> None:
        """Вечернее напоминание о планировании завтрашнего дня."""
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user_row in users:
            user = dict(user_row)
            local_date = local_date_str(now_utc, user["timezone"])
            
            # Проверки паузы
            if user["pause_until"] and local_date <= (user["pause_until"] or ""):
                continue
            if user.get("quiet_mode"):
                continue
            if not self._legacy_pings_active(user):
                continue
            if _sleep_window_active(user, now_utc):
                continue

            # Определяем целевое время (сон - 1 час, дефолт 22:00)
            sleep_time = user.get("sleep_time") or "23:00"
            try:
                dt_sleep = datetime.datetime.strptime(sleep_time, "%H:%M")
                dt_target = dt_sleep - datetime.timedelta(hours=1)
                target_time = dt_target.strftime("%H:%M")
            except ValueError:
                target_time = "22:00"
            
            # Проверяем окно времени (15 минут)
            if not should_trigger(now_utc, user["timezone"], target_time, window_minutes=15):
                continue

            # Проверяем, есть ли УЖЕ план на завтра
            local_dt_today = datetime.datetime.strptime(local_date, "%Y-%m-%d").date()
            tomorrow_date = (local_dt_today + datetime.timedelta(days=1)).isoformat()
            
            existing_plan = await repo.get_day_plan(self.conn, user["id"], tomorrow_date)
            if existing_plan:
                # План уже есть, не надоедаем
                continue
            
            # Отправляем напоминание
            text = (
                "🌙 Самое время скинуть мысли из головы и набросать план на завтра.\n"
                "Это поможет спать спокойнее (+1 💎 за планирование)."
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📝 Спланировать", callback_data="dmenu:plan_tomorrow")]
                ]
            )
            # Внимание: dmenu:plan_tomorrow нужно поддержать в handlers/menu.py или ловить команду /plan_tomorrow
            # Сейчас /plan_tomorrow это команда. Добавим коллбэк позже или используем текст.
            # Лучше всего текст "Спланировать" (эмуляция команды) или новый callback.
            # В menu.py нет обработчика dmenu. Добавим callback который триггерит команду.
            
            # Чтобы не усложнять, отправим просто текст с предложением нажать команду
            # Но кнопка удобнее. Пусть будет callback, который мы добавим в menu.py
            
            sent = await self._safe_send_message(user, local_date, text, reply_markup=kb)
            if sent:
                log_debug(
                    f"[day_plan_evening] sent prompt user={user['id']} date={local_date}"
                )

    async def _tick_meds(self) -> None:
        """Пинг по витаминам/таблеткам на основе meds/med_logs."""
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user_row in users:
            user = dict(user_row)
            local_date = local_date_str(now_utc, user["timezone"])
            if user["pause_until"] and local_date <= (user["pause_until"] or ""):
                continue
            if user.get("quiet_mode"):
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
                        f"{g(user, 'Ты уже принял?', 'Ты уже приняла?', 'Ты уже принял(а)?')}"
                    )
                    if tone == "soft":
                        text += "\nЕсли сейчас не до этого — можно перенести на позже."
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=button_label(user, "Принял ✅", "Приняла ✅"),
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
                    sent = await self._safe_send_message(
                        user, local_date, text, reply_markup=keyboard
                    )
                    if not sent:
                        await self.conn.execute(
                            "DELETE FROM med_logs WHERE id = ?", (log_id,)
                        )
                        await self.conn.commit()

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
                now_utc, user["timezone"], reminder["reminder_time"], window_minutes=5
            ):
                log_debug(
                    f"[custom] not in window user={user['id']} rem={reminder['id']} "
                    f"time={reminder['reminder_time']} now_utc={now_utc.isoformat()}"
                )
                continue
            sent = await self._send_custom(user, reminder, local_date)
            if not sent:
                continue
            log_debug(
                f"[custom] send user={user['id']} rem={reminder['id']} time={reminder['reminder_time']} date={local_date}"
            )
            await repo.set_custom_reminder_sent(self.conn, reminder["id"], local_date)
            await repo.log_custom_task(
                self.conn,
                reminder_id=reminder["id"],
                user_id=user["id"],
                reminder_date=local_date,
                status="pending",
            )
            # One-time reminders: hide from list after first send.
            if int(reminder.get("frequency_days") or 1) >= 9999:
                await repo.archive_custom_reminder(self.conn, user["id"], reminder["id"])

    async def _send_routine(
        self, user: dict, routine: dict, local_date: str
    ) -> bool:
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
            f"Если сил мало — выбери один пункт. Этого уже достаточно.\n\n"
            f"{task_lines}\n\nОтметь статус:"
        )
        rows = [
            [
                InlineKeyboardButton(
                    text=done_button_label(user),
                    callback_data=f"routine:{routine['routine_id']}:{local_date}:done",
                ),
                InlineKeyboardButton(
                    text="Позже",
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
        return await self._safe_send_message(user, local_date, text, reply_markup=keyboard)

    async def _send_custom(
        self, user: dict, reminder: dict, local_date: str
    ) -> bool:
        text = f"🔔 Напоминание: {reminder['title']}\nВремя: {reminder['reminder_time']} (локально)"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=done_button_label(user),
                        callback_data=f"custom:{reminder['id']}:{local_date}:done",
                    ),
                    InlineKeyboardButton(
                        text="Позже",
                        callback_data=f"custom:{reminder['id']}:{local_date}:later",
                    ),
                    InlineKeyboardButton(
                        text="Пропустить",
                        callback_data=f"custom:{reminder['id']}:{local_date}:skip",
                    ),
                ]
            ]
        )
        sent = await self._safe_send_message(user, local_date, text, reply_markup=keyboard)
        if sent:
            log_debug(
                f"[custom] delivered user={user['id']} rem={reminder['id']} time={reminder['reminder_time']} date={local_date}"
            )
        return sent

    async def _tick_wellness(self) -> None:
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user in users:
            user = dict(user)
            local_date = local_date_str(now_utc, user["timezone"])
            if user["pause_until"] and local_date <= (user["pause_until"] or ""):
                continue
            if user.get("quiet_mode"):
                continue
            if not self._legacy_pings_active(user):
                continue
            if _sleep_window_active(user, now_utc):
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
                        keyboard = InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="💧 Выпил!",
                                        callback_data=f"waterconfirm:{local_date}:yes"
                                    ),
                                    InlineKeyboardButton(
                                        text="⏰ Позже",
                                        callback_data=f"waterconfirm:{local_date}:later"
                                    ),
                                ]
                            ]
                        )
                        sent = await self._safe_send_message(
                            user, local_date, text, reply_markup=keyboard
                        )
                        if sent:
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
                        # Персонализация по полу
                        gender = user.get("gender", "neutral")
                        if gender == "female":
                            ate_word = "ела"
                        elif gender == "male":
                            ate_word = "ел"
                        else:
                            ate_word = "ел(а)"
                        
                        text = f"🍽 Привет! Ты {ate_word} за последние пару часов?\nДаже небольшой перекус даст тебе энергии 💪"
                        if wellness and wellness.get("tone") == "soft":
                            text += "\n\nЕсли сейчас не до этого — можно напомнить попозже."
                        if wellness and wellness.get("tone") == "pushy":
                            text += "\n\nНе откладывай — организму нужна энергия!"
                        
                        keyboard = InlineKeyboardMarkup(
                            inline_keyboard=[
                                [
                                    InlineKeyboardButton(
                                        text="✅ Да!",
                                        callback_data=f"mealconfirm:{local_date}:yes"
                                    ),
                                    InlineKeyboardButton(
                                        text="⏰ Напомни попозже",
                                        callback_data=f"mealconfirm:{local_date}:later"
                                    ),
                                ]
                            ]
                        )
                        sent = await self._safe_send_message(
                            user, local_date, text, reply_markup=keyboard
                        )
                        if sent:
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
            if user.get("pause_until") and local_date <= (user["pause_until"] or ""):
                continue
            if user.get("quiet_mode"):
                continue
            if _sleep_window_active(user, now_utc):
                continue
            bills = await repo.bills_due_soon(self.conn, user["id"], local_date, days_ahead=3)
            if not bills:
                continue
            lines = [f"{b['title']}: до {b['due_date']} (~{b['amount']:.0f} ₽)" for b in bills]
            text = "📅 Счета скоро к оплате:\n" + "\n".join(lines)
            await self._safe_send_message(user, local_date, text)

    async def _tick_weekly_finance(self) -> None:
        """Раз в день в 09:00 UTC: если у пользователя сегодня воскресенье (локально), отправить недельный отчёт."""
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user in users:
            user = dict(user)
            local_date = local_date_str(now_utc, user["timezone"])
            if user.get("pause_until") and local_date <= (user["pause_until"] or ""):
                continue
            if user.get("quiet_mode"):
                continue
            if _sleep_window_active(user, now_utc):
                continue
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
            await self._safe_send_message(user, local_date, text)

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
            if user.get("pause_until") and local_date <= (user["pause_until"] or ""):
                continue
            if user.get("quiet_mode"):
                continue
            if not self._legacy_pings_active(user):
                continue
            if _sleep_window_active(user, now_utc):
                continue
            today = datetime.date.fromisoformat(local_date)
            tzinfo = tzinfo_from_string(user["timezone"])
            if now_utc.replace(tzinfo=datetime.timezone.utc).astimezone(tzinfo).weekday() != 0:
                continue
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
                    await self._safe_send_message(
                        user, local_date, f"{text}\nДата сегодня: {note_date}", reply_markup=kb
                    )

    async def _tick_weight_prompt(self) -> None:
        """Раз в день напоминаем про вес, если не спрашивали 7 дней."""
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user in users:
            user = dict(user)
            local_date = local_date_str(now_utc, user["timezone"])
            if user.get("pause_until") and local_date <= (user["pause_until"] or ""):
                continue
            if user.get("quiet_mode"):
                continue
            if not self._legacy_pings_active(user):
                continue
            if _sleep_window_active(user, now_utc):
                continue
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
                sent = await self._safe_send_message(
                    user,
                    local_date,
                    "⚖ Обновишь вес? Коротко и без оценок — только цифра.",
                    reply_markup=kb,
                )
                if sent:
                    await repo.update_weight_prompt(self.conn, user["id"], local_date)

    async def _weekly_home_plan(self) -> None:
        """По воскресеньям присылать план по дому на неделю."""
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user in users:
            user = dict(user)
            today = local_date_str(now_utc, user["timezone"])
            if user.get("pause_until") and today <= (user.get("pause_until") or ""):
                continue
            if user.get("quiet_mode"):
                continue
            tzinfo = tzinfo_from_string(user["timezone"])
            local_dt = now_utc.replace(tzinfo=datetime.timezone.utc).astimezone(tzinfo)
            if local_dt.weekday() != 6:
                continue
            await repo.ensure_regular_tasks(self.conn, user["id"], today)
            tasks = await repo.list_regular_tasks(self.conn, user["id"], due_only=False)
            if not tasks:
                continue
            lines = ["План по дому на неделю:"]
            for t in tasks:
                lines.append(f"• {t['title']} — до {t['next_due_date']}")
            await self._safe_send_message(user, today, "\n".join(lines))

    async def _tick_affirmations(self) -> None:
        """Отправка аффирмаций по расписанию пользователя."""
        import json
        from services.knowledge import get_knowledge_service
        
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        
        for user_row in users:
            user = dict(user_row)
            local_date = local_date_str(now_utc, user["timezone"])
            
            # Проверяем паузу
            if user["pause_until"] and local_date <= (user["pause_until"] or ""):
                continue
            if user.get("quiet_mode"):
                continue
            if not self._legacy_pings_active(user):
                continue
            
            # Получаем настройки wellness
            wellness_row = await repo.get_wellness(self.conn, user["id"])
            if not wellness_row:
                continue
            wellness = dict(wellness_row)
            
            # Проверяем включены ли аффирмации
            if not wellness.get("affirm_enabled", 0):
                continue
            
            # Получаем часы отправки
            affirm_hours_raw = wellness.get("affirm_hours", "[9]")
            try:
                affirm_hours = json.loads(affirm_hours_raw) if affirm_hours_raw else [9]
            except:
                affirm_hours = [9]
            
            # Проверяем локальное время
            tzinfo = tzinfo_from_string(user["timezone"])
            local_dt = now_utc.replace(tzinfo=datetime.timezone.utc).astimezone(tzinfo)
            current_hour = local_dt.hour
            
            # Проверяем попадает ли час в список
            if current_hour not in affirm_hours:
                continue
            
            # Создаём ключ для предотвращения повторной отправки
            affirm_key = f"affirm:{local_date}:{current_hour}"
            last_key = wellness.get("affirm_last_key", "")
            if last_key == affirm_key:
                continue
            
            # Получаем категории
            categories_raw = wellness.get("affirm_categories", '["motivation","calm"]')
            try:
                categories = json.loads(categories_raw) if categories_raw else ["motivation", "calm"]
            except:
                categories = ["motivation", "calm"]
            
            # Получаем аффирмацию
            ks = get_knowledge_service()
            affirmation = ks.get_random_affirmation(categories=categories)
            
            if not affirmation:
                continue
            
            # Отправляем
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🌟 Ещё одну", callback_data="affirm:more"),
                        InlineKeyboardButton(text="💚 Спасибо", callback_data="affirm:thanks"),
                    ]
                ]
            )
            
            sent = await self._safe_send_message(
                user, local_date, affirmation, reply_markup=keyboard
            )
            if not sent:
                continue

            # Сохраняем ключ
            await repo.upsert_wellness(self.conn, user["id"], affirm_last_key=affirm_key)
            log_debug(
                f"[affirmations] sent to user={user['id']} hour={current_hour} date={local_date}"
            )

    async def _tick_focus(self) -> None:
        now_utc = datetime.datetime.utcnow()
        sessions = await repo.list_active_focus_sessions(self.conn)
        if not sessions:
            return
        for session in sessions:
            user_row = await repo.get_user(self.conn, session["user_id"])
            if not user_row:
                continue
            user = dict(user_row)
            local_date = local_date_str(now_utc, user["timezone"])
            if user["pause_until"] and local_date <= (user["pause_until"] or ""):
                continue
            try:
                checkin_ts = datetime.datetime.fromisoformat(session["checkin_ts"])
                end_ts = datetime.datetime.fromisoformat(session["end_ts"])
            except Exception:
                continue

            if not session.get("checkin_sent") and now_utc >= checkin_ts:
                text = f"Середина сессии «{session['task_title']}». Ты в плане?"
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=button_label(user, "✅ В плане", "✅ В плане", "✅ В плане"),
                                callback_data=f"cafe:checkin:ok:{session['id']}",
                            ),
                            InlineKeyboardButton(
                                text=button_label(user, "⚠️ Сбился", "⚠️ Сбилась", "⚠️ Сбился(ась)"),
                                callback_data=f"cafe:checkin:off:{session['id']}",
                            ),
                        ]
                    ]
                )
                sent = await self._safe_send_message(
                    user, local_date, text, reply_markup=keyboard
                )
                if sent:
                    await repo.mark_focus_checkin_sent(self.conn, session["id"])

            if not session.get("end_sent") and now_utc >= end_ts:
                text = f"Время вышло. Как итог по «{session['task_title']}»?"
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=done_button_label(user),
                                callback_data=f"cafe:finish:done:{session['id']}",
                            ),
                            InlineKeyboardButton(
                                text=button_label(user, "🟡 Частично", "🟡 Частично", "🟡 Частично"),
                                callback_data=f"cafe:finish:partial:{session['id']}",
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                text=button_label(user, "❌ Не сделал", "❌ Не сделала", "❌ Не сделал(а)"),
                                callback_data=f"cafe:finish:fail:{session['id']}",
                            )
                        ],
                    ]
                )
                sent = await self._safe_send_message(
                    user, local_date, text, reply_markup=keyboard
                )
                if sent:
                    await repo.mark_focus_end_sent(self.conn, session["id"])

            if session.get("end_sent") and not session.get("result"):
                grace = end_ts + datetime.timedelta(minutes=30)
                if now_utc >= grace:
                    await repo.complete_focus_session(self.conn, session["id"], "missed")
                    strikes = await repo.update_focus_strikes(self.conn, user["id"], 1)
                    if strikes >= 2:
                        cooldown_until = (
                            now_utc + datetime.timedelta(hours=6)
                        ).isoformat()
                        await repo.set_focus_cooldown(
                            self.conn, user["id"], cooldown_until
                        )

    async def _maybe_shift_sleep(self, user: dict, local_date: str) -> None:
        target_sleep = user.get("sleep_target_sleep")
        target_wake = user.get("sleep_target_wake")
        if not target_sleep or not target_wake:
            return
        shift_step = int(user.get("sleep_shift_step") or 30)
        shift_every = max(1, int(user.get("sleep_shift_every") or 2))
        last_shift = user.get("sleep_last_shift_date") or ""
        if last_shift:
            try:
                last_dt = datetime.date.fromisoformat(last_shift)
                current_dt = datetime.date.fromisoformat(local_date)
                if (current_dt - last_dt).days < shift_every:
                    return
            except Exception:
                pass

        current_sleep = user.get("sleep_time") or target_sleep
        current_wake = user.get("wake_up_time") or target_wake
        cur_sleep_min = _hhmm_to_min(current_sleep)
        cur_wake_min = _hhmm_to_min(current_wake)
        tgt_sleep_min = _hhmm_to_min(target_sleep)
        tgt_wake_min = _hhmm_to_min(target_wake)
        if None in (cur_sleep_min, cur_wake_min, tgt_sleep_min, tgt_wake_min):
            return

        delta_sleep = _delta_minutes(cur_sleep_min, tgt_sleep_min)
        delta_wake = _delta_minutes(cur_wake_min, tgt_wake_min)
        if delta_sleep == 0 and delta_wake == 0:
            return

        step_sleep = max(-shift_step, min(shift_step, delta_sleep))
        step_wake = max(-shift_step, min(shift_step, delta_wake))
        new_sleep = _min_to_hhmm(cur_sleep_min + step_sleep)
        new_wake = _min_to_hhmm(cur_wake_min + step_wake)

        await repo.update_user_schedule(self.conn, user["id"], new_wake, new_sleep)
        await repo.update_sleep_last_shift(self.conn, user["id"], local_date)

    async def _tick_sleep_mode(self) -> None:
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user_row in users:
            user = dict(user_row)
            if not user.get("sleep_mode_enabled"):
                continue
            local_date = local_date_str(now_utc, user["timezone"])
            if user["pause_until"] and local_date <= (user["pause_until"] or ""):
                continue
            if user.get("quiet_mode"):
                continue
            if not self._legacy_pings_active(user):
                continue

            await self._maybe_shift_sleep(user, local_date)

            target_sleep = user.get("sleep_target_sleep") or user.get("sleep_time") or "23:00"
            target_wake = user.get("sleep_target_wake") or user.get("wake_up_time") or "08:00"

            evening_sent = user.get("sleep_last_evening_date") == local_date
            if not evening_sent:
                try:
                    dt_sleep = datetime.datetime.strptime(target_sleep, "%H:%M")
                    dt_target = dt_sleep - datetime.timedelta(minutes=75)
                    target_time = dt_target.strftime("%H:%M")
                except Exception:
                    target_time = "21:30"
                if should_trigger(now_utc, user["timezone"], target_time, window_minutes=10):
                    text = "🌙 Время замедляться. Давай мягко свернем день, чтобы уснуть вовремя."
                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(text="Ок, начинаю", callback_data="sleep:evening:ok"),
                                InlineKeyboardButton(text="Сегодня позже", callback_data="sleep:evening:later"),
                            ]
                        ]
                    )
                    sent = await self._safe_send_message(user, local_date, text, reply_markup=kb)
                    if sent:
                        await repo.update_sleep_ping(self.conn, user["id"], "evening", local_date)

            morning_sent = user.get("sleep_last_morning_date") == local_date
            if not morning_sent and should_trigger(now_utc, user["timezone"], target_wake, window_minutes=15):
                text = "☀️ Доброе утро. Мягкий старт: вода + свет/движение 2 минуты."
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="Я встал(а)", callback_data="sleep:morning:up"),
                            InlineKeyboardButton(text="Ещё 10 мин", callback_data="sleep:morning:snooze"),
                        ]
                    ]
                )
                sent = await self._safe_send_message(user, local_date, text, reply_markup=kb)
                if sent:
                    await repo.update_sleep_ping(self.conn, user["id"], "morning", local_date)

    async def _tick_daily_brief(self) -> None:
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user_row in users:
            user = dict(user_row)
            local_date = local_date_str(now_utc, user["timezone"])
            if user["pause_until"] and local_date <= (user["pause_until"] or ""):
                continue
            if user.get("quiet_mode"):
                continue
            if not self._legacy_pings_active(user):
                continue
            if _sleep_window_active(user, now_utc):
                continue
            if user.get("daily_brief_last_date") == local_date:
                continue

            wake_time = user.get("wake_up_time") or "08:00"
            if not should_trigger(now_utc, user["timezone"], wake_time, window_minutes=30):
                continue

            plan = await repo.get_day_plan(self.conn, user["id"], local_date)
            if plan:
                # если у пользователя уже есть план, он получит отдельный утренний пинг
                continue

            morning = await repo.get_routine_by_key(self.conn, "morning")
            steps = []
            if morning:
                steps_rows = await repo.list_routine_steps_for_routine(
                    self.conn, user["id"], morning["id"]
                )
                steps = [dict(r).get("title") for r in steps_rows if dict(r).get("title")]
            if not steps:
                steps = [
                    "Стакан воды",
                    "Открыть окно на 2–5 минут",
                    "Выбрать 1 главное дело на сегодня",
                ]

            focus_steps = steps[:3]
            micro = focus_steps[0] if focus_steps else "Сделать один маленький шаг"
            lines = ["☀️ Коротко на сегодня:"]
            for item in focus_steps:
                lines.append(f"• {item}")
            lines.append(f"Микро‑шаг: {micro}")

            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📅 Сегодня", callback_data="today:menu")],
                ]
            )

            sent = await self._safe_send_message(user, local_date, "\n".join(lines), reply_markup=kb)
            if sent:
                await repo.update_daily_brief_sent(self.conn, user["id"], local_date)
