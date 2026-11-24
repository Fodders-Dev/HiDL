import datetime
import html as _html_mod
import json
import os
import time
from typing import Optional
from collections import defaultdict

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from db import repositories as repo
from services import calendar_service, coach_knowledge, fitness_nutrition, inbox_service, karma_service, pulse_service
from utils.time import format_date_display, format_time_local, local_date_str, parse_hhmm, should_trigger, tzinfo_from_string
from utils.gender import done_button_label, button_label, g
from utils.logger import log_debug
from utils.profile import is_student_mode


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
        self._db_file_path: Optional[str] = None

    async def _resolve_db_file_path(self) -> str:
        if self._db_file_path is None:
            rows = await self.conn.execute_fetchall("PRAGMA database_list;")
            main_row = next((r for r in rows if r["name"] == "main"), None)
            self._db_file_path = str(main_row["file"] or "") if main_row else ""
        return self._db_file_path

    async def write_bot_heartbeat(self) -> None:
        db_path = await self._resolve_db_file_path()
        if not db_path or db_path == ":memory:" or db_path.startswith("file::memory:"):
            return
        hb_dir = os.path.dirname(db_path) or "."
        hb_path = os.path.join(hb_dir, "bot_heartbeat.json")
        tmp_path = f"{hb_path}.tmp"
        payload = {
            "ts": time.time(),
            "iso": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp_path, hb_path)

    async def _tick_bot_heartbeat(self) -> None:
        try:
            await self.write_bot_heartbeat()
        except Exception as exc:
            log_debug(f"[heartbeat] write failed: {exc}")

    def start(self) -> None:
        self.scheduler.add_job(
            self._tick_bot_heartbeat,
            "interval",
            seconds=60,
            id="bot_heartbeat_tick",
            next_run_time=datetime.datetime.now(datetime.timezone.utc),
        )
        self.scheduler.add_job(self._tick_desk_outbox, "interval", seconds=20, id="desk_outbox_tick")
        self.scheduler.add_job(self._tick, "interval", seconds=60, id="routine_tick")
        self.scheduler.add_job(self._tick_wellness, "interval", seconds=60, id="wellness_tick")
        self.scheduler.add_job(self._tick_bills, "cron", hour=9, minute=0, id="bills_ping")
        self.scheduler.add_job(self._tick_weekly_finance, "cron", hour=9, minute=0, id="weekly_finance")
        self.scheduler.add_job(self._reset_points_month, "cron", day=1, hour=0, minute=5, id="points_reset")
        self.scheduler.add_job(self._tick_care, "cron", hour=9, minute=15, id="care_tick")
        self.scheduler.add_job(self._tick_weight_prompt, "interval", minutes=30, id="weight_prompt")
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
        self.scheduler.add_job(self._tick_pending_meals, "interval", minutes=2, id="pending_meals_autocommit")
        self.scheduler.add_job(self._tick_nutrition_evening, "interval", minutes=15, id="nutrition_evening_checkin")
        self.scheduler.add_job(self._tick_household_snoozes, "interval", seconds=60, id="household_snooze_tick")
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

    async def _tick_household_snoozes(
        self, now_utc: datetime.datetime | None = None
    ) -> int:
        """Напомнить об отложенных просьбах по дому после 30-минутной паузы."""
        now = now_utc or datetime.datetime.utcnow()
        due_reqs = await repo.list_due_snoozed_household_requests(self.conn, now)
        sent = 0
        for req in due_reqs:
            rid = int(req["id"])
            hid = int(req["household_id"])
            from_uid = int(req.get("from_user_id") or 0)
            from_name = str(req.get("from_name") or "Партнёр")
            body = str(req.get("text") or "")
            members = await repo.list_household_members(self.conn, hid)
            targets = [m for m in members if int(m["id"]) != from_uid]
            if not targets and members:
                targets = members
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="👍 Принято", callback_data=f"hh:req:act:{rid}:accepted"),
                        InlineKeyboardButton(text="⏰ Ещё 30 мин", callback_data=f"hh:req:act:{rid}:snoozed"),
                    ],
                    [
                        InlineKeyboardButton(text="✅ Готово", callback_data=f"hh:req:act:{rid}:done"),
                        InlineKeyboardButton(text="📋 Все просьбы", callback_data="hh:req:list"),
                    ],
                ]
            )
            msg_text = (
                f"⏰ <b>Напоминание о просьбе #{rid}</b> (от {_html_mod.escape(from_name)}):\n"
                f"{_html_mod.escape(body)}"
            )
            for member in targets:
                m_dict = dict(member)
                local_date = local_date_str(now, m_dict.get("timezone") or "UTC")
                if m_dict.get("quiet_mode") or (m_dict.get("pause_until") and local_date <= str(m_dict["pause_until"])):
                    continue
                if _sleep_window_active(m_dict, now):
                    continue
                if await self._safe_send_message(m_dict, local_date, msg_text, reply_markup=kb, parse_mode="HTML"):
                    sent += 1
            await repo.update_household_request_status(self.conn, hid, rid, "open", now_utc=now)
        return sent

    async def _tick_desk_outbox(self, now_utc: datetime.datetime | None = None) -> int:
        """Drain a bounded set of due Desk batches on an isolated DB connection."""
        now_value = now_utc or datetime.datetime.utcnow()
        now = now_value.astimezone(datetime.timezone.utc).replace(tzinfo=None) if now_value.tzinfo else now_value
        from db.database import independent_connection
        async with independent_connection(self.conn) as writer:
            delivered = 0
            for _ in range(50):
                delivered += await self._deliver_desk_outbox_group(writer, now)
                # A zero result can mean a suppressed/deferred group was
                # processed. Continue only while another eligible row exists.
                cur = await writer.execute(
                    "SELECT 1 FROM desk_outbox WHERE (status='pending' AND deliver_after_utc<=?) "
                    "OR (status='sending' AND last_error LIKE 'lease:%' AND substr(last_error,7)<=?) LIMIT 1",
                    (now.strftime("%Y-%m-%dT%H:%M:%SZ"), (now - datetime.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")),
                )
                if not await cur.fetchone():
                    break
            return delivered

    async def _deliver_desk_outbox_group(self, writer, now: datetime.datetime) -> int:
        """Atomically claim and process exactly one priority-ordered group."""
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        lease_before = (now - datetime.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # A short lease prevents two scheduler processes from delivering the
        # same rows while allowing a crashed process's claim to be recovered.
        await writer.execute("BEGIN IMMEDIATE")
        try:
            cursor = await writer.execute(
                "SELECT id,batch_key,deliver_after_utc FROM desk_outbox WHERE (status='pending' AND deliver_after_utc<=?) "
                "OR (status='sending' AND last_error LIKE 'lease:%' AND substr(last_error,7)<=?) "
                "ORDER BY CASE priority WHEN 'urgent' THEN 0 ELSE 1 END,deliver_after_utc,id LIMIT 1",
                (stamp, lease_before),
            )
            candidate = await cursor.fetchone()
            ids = []
            if candidate:
                if candidate["batch_key"]:
                    grouped = await writer.execute(
                        "SELECT id FROM desk_outbox WHERE batch_key=? AND deliver_after_utc=? AND "
                        "((status='pending' AND deliver_after_utc<=?) OR (status='sending' AND last_error LIKE 'lease:%' AND substr(last_error,7)<=?))",
                        (candidate["batch_key"], candidate["deliver_after_utc"], stamp, lease_before),
                    )
                    ids = [int(row["id"]) for row in await grouped.fetchall()]
                else:
                    ids = [int(candidate["id"])]
            if ids:
                marks = ",".join("?" for _ in ids)
                await writer.execute(
                    f"UPDATE desk_outbox SET status='sending',last_error=? WHERE id IN ({marks}) AND status IN ('pending','sending')",
                    (f"lease:{stamp}", *ids),
                )
            await writer.commit()
        except Exception:
            await writer.rollback()
            raise
        if not ids:
            return 0

        marks = ",".join("?" for _ in ids)
        cur = await writer.execute(
            f"SELECT o.*,l.user_id AS linked_user_id,u.telegram_id,u.timezone,u.pause_until,u.quiet_mode,u.sleep_mode_enabled,"
            f"u.sleep_target_sleep,u.sleep_target_wake,u.sleep_time,u.wake_up_time,u.sleep_confirmed_at "
            f"FROM desk_outbox o JOIN users u ON u.id=o.user_id LEFT JOIN desk_links l ON l.user_id=o.user_id AND l.enabled=1 WHERE o.id IN ({marks}) AND o.status='sending' AND o.last_error=? "
            "ORDER BY o.user_id,o.batch_key,o.deliver_after_utc,o.id", (*ids, f"lease:{stamp}"),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        unlinked_ids = [int(row["id"]) for row in rows if row.get("linked_user_id") is None]
        if unlinked_ids:
            unlinked_marks = ",".join("?" for _ in unlinked_ids)
            await writer.execute(
                f"UPDATE desk_outbox SET status='suppressed_unlinked',last_error='' WHERE id IN ({unlinked_marks}) AND status='sending' AND last_error=?",
                (*unlinked_ids, f"lease:{stamp}"),
            )
            await writer.commit()
        rows = [row for row in rows if row.get("linked_user_id") is not None]
        # Group only teacher submissions that share both batch key and deadline.
        groups: dict[tuple, list[dict]] = {}
        for row in rows:
            key = (row["user_id"], row["batch_key"], row["deliver_after_utc"]) if row["batch_key"] else (row["id"],)
            groups.setdefault(key, []).append(row)

        sent_count = 0
        for batch in groups.values():
            first = batch[0]
            urgent = first["priority"] == "urgent"
            user = dict(first)
            user["id"] = int(first["user_id"])
            actual_user = await repo.get_user(writer, int(first["user_id"]))
            if actual_user:
                user.update(dict(actual_user))
            local_date = local_date_str(now, user.get("timezone") or "UTC")
            ids_here = [int(row["id"]) for row in batch]
            id_marks = ",".join("?" for _ in ids_here)
            if not urgent and user.get("pause_until") and local_date <= str(user["pause_until"]):
                await writer.execute(
                    f"UPDATE desk_outbox SET status='suppressed_pause',last_error='' WHERE id IN ({id_marks}) AND status='sending' AND last_error=?",
                    (*ids_here, f"lease:{stamp}"),
                )
                await writer.commit()
                continue
            confirmed_sleep = pulse_service._confirmed_sleep_active(user, now)
            if not urgent and (_sleep_window_active(user, now) or confirmed_sleep):
                tz = tzinfo_from_string(user.get("timezone") or "UTC")
                local_now = now.replace(tzinfo=datetime.timezone.utc).astimezone(tz)
                wake = user.get("wake_up_time") or "08:00"
                try:
                    hour, minute = map(int, wake.split(":"))
                    wake_local = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    # If the configured wake time has passed but a shifted
                    # sleep target still keeps the user asleep, use that
                    # same-day target before rolling to tomorrow.
                    if wake_local <= local_now:
                        target_wake = user.get("sleep_target_wake") or ""
                        try:
                            target_hour, target_minute = map(int, target_wake.split(":"))
                            shifted_wake = local_now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
                        except (TypeError, ValueError):
                            shifted_wake = wake_local
                        wake_local = shifted_wake if shifted_wake > local_now else wake_local + datetime.timedelta(days=1)
                    defer_local = wake_local + datetime.timedelta(minutes=5)
                    defer = defer_local.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    await writer.execute(
                        f"UPDATE desk_outbox SET status='pending',deliver_after_utc=?,last_error='' WHERE id IN ({id_marks}) AND status='sending' AND last_error=?",
                        (defer, *ids_here, f"lease:{stamp}"),
                    )
                    await writer.commit()
                    continue
                except (TypeError, ValueError):
                    pass
            text = "\n".join(str(row["text"]) for row in batch)
            # Telegram caps text at 4096 characters; leave room for a clear
            # truncation marker when several teacher submissions were grouped.
            if len(text) > 4000:
                text = text[:3980].rstrip() + "…\n(Остальные сдачи доступны в Desk.)"
            button = first.get("button_label") or "Открыть"
            markup = None
            if first.get("url"):
                markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=button, url=first["url"])]] )
            ok = await self._safe_send_message(
                user, local_date, text, reply_markup=markup, parse_mode=None,
                disable_notification=bool(not urgent and user.get("quiet_mode")),
            )
            if ok:
                await writer.execute(
                    f"UPDATE desk_outbox SET status='sent',sent_at=?,attempts=attempts+1,last_error='' WHERE id IN ({id_marks}) AND status='sending' AND last_error=?",
                    (stamp, *ids_here, f"lease:{stamp}"),
                )
                sent_count += len(batch)
            else:
                await writer.execute(
                    f"UPDATE desk_outbox SET attempts=attempts+1,status=CASE WHEN attempts+1>=5 THEN 'failed' ELSE 'pending' END,"
                    f"deliver_after_utc=?,last_error='telegram_send_failed' WHERE id IN ({id_marks}) AND status='sending' AND last_error=?",
                    ((now + datetime.timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), *ids_here, f"lease:{stamp}"),
                )
            await writer.commit()
        return sent_count

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

    async def _tick_day_plan(self, now_utc: Optional[datetime.datetime] = None) -> int:
        """Утренний пинг по делам дня (в локальном утреннем окне + автоподтягивание хвостов)."""
        now = now_utc or datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        sent_count = 0
        for user_row in users:
            user = dict(user_row)
            if is_student_mode(user):
                continue
            tz = str(user.get("timezone") or "UTC")
            local_date = local_date_str(now, tz)
            if user.get("pause_until") and local_date <= (user["pause_until"] or ""):
                continue
            if user.get("quiet_mode"):
                continue
            if _sleep_window_active(user, now) or pulse_service._confirmed_sleep_active(user, now):
                continue
            if pulse_service.is_day_off(user, local_date):
                continue

            local_hhmm = format_time_local(now, tz)
            now_t = parse_hhmm(local_hhmm)
            eff_wake, _, _ = pulse_service.get_effective_schedule(user, local_date)
            wake_t = parse_hhmm(eff_wake) or datetime.time(8, 0)
            if now_t is None:
                continue
            now_m = now_t.hour * 60 + now_t.minute
            wake_m = wake_t.hour * 60 + wake_t.minute
            if not (0 <= ((now_m - wake_m) % (24 * 60)) <= 180):
                continue

            uid = int(user["id"])
            existing_plan = await repo.get_day_plan(self.conn, uid, local_date)
            if existing_plan and dict(existing_plan).get("morning_sent") == local_date:
                continue

            await repo.sync_overdue_day_plan_items(self.conn, uid, local_date)
            items_rows = await repo.list_day_plan_items(self.conn, uid, local_date)
            open_items = [dict(r) for r in items_rows if not r["done"]]
            if not open_items:
                continue

            plan = await repo.get_day_plan(self.conn, uid, local_date)
            if not plan:
                continue

            lines = [f"📋 <b>Твои дела на сегодня ({len(open_items)}):</b>"]
            for idx, it in enumerate(open_items[:6], 1):
                star = "⭐ " if it.get("is_important") else ""
                lines.append(f"{idx}. {star}{_html_mod.escape(str(it['title']))}")
            if len(open_items) > 6:
                lines.append(f"<i>…и ещё {len(open_items) - 6} в плане дня</i>")
            lines.append("\nОтмечай закрытые прямо кнопками ниже:")

            kb_rows = [
                [
                    InlineKeyboardButton(
                        text=f"✅ {str(it['title'])[:24]}",
                        callback_data=f"dplan:done:{it['id']}:list:0",
                    )
                ]
                for it in open_items[:3]
            ]
            kb_rows.append(
                [
                    InlineKeyboardButton(text="📅 Весь план", callback_data="dplan:list"),
                    InlineKeyboardButton(text="➕ Добавить", callback_data="dplan:add"),
                ]
            )
            sent = await self._safe_send_message(
                user,
                local_date,
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
                parse_mode="HTML",
            )
            if sent:
                await repo.mark_day_plan_morning_sent(self.conn, plan["id"], local_date)
                sent_count += 1
                log_debug(f"[day_plan] sent user={uid} items={len(open_items)} date={local_date}")
        return sent_count

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
            student_mode = is_student_mode(user)
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
            if wellness.get("meal_enabled") and not student_mode:
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
            if is_student_mode(user):
                continue
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
            if is_student_mode(user):
                continue
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
            if is_student_mode(user):
                continue
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

    async def _tick_weight_prompt(self, now_utc: Optional[datetime.datetime] = None) -> int:
        """Утренние умные напоминания взвеситься + контроль пропусков в локальном окне подъёма."""
        now = now_utc or datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        sent_count = 0
        for user_row in users:
            user = dict(user_row)
            if is_student_mode(user):
                continue
            goal = str(user.get("weight_goal") or "loss").strip().lower()
            if goal == "mindful":
                continue
            tz = str(user.get("timezone") or "UTC")
            local_date = local_date_str(now, tz)
            if user.get("pause_until") and local_date <= (user["pause_until"] or ""):
                continue
            if user.get("quiet_mode"):
                continue
            if _sleep_window_active(user, now) or pulse_service._confirmed_sleep_active(user, now):
                continue
            if pulse_service.is_day_off(user, local_date):
                continue
            if str(user.get("weight_skip_until") or "").strip() >= local_date:
                continue

            last = str(user.get("last_weight_prompt") or "").strip()
            if last == local_date:
                continue

            local_hhmm = format_time_local(now, tz)
            now_t = parse_hhmm(local_hhmm)
            eff_wake, _, _ = pulse_service.get_effective_schedule(user, local_date)
            wake_t = parse_hhmm(eff_wake) or datetime.time(8, 0)
            if now_t is None:
                continue
            now_m = now_t.hour * 60 + now_t.minute
            wake_m = wake_t.hour * 60 + wake_t.minute
            if not (0 <= ((now_m - wake_m) % (24 * 60)) <= 150):
                continue

            uid = int(user["id"])
            today_d = datetime.date.fromisoformat(local_date)
            latest = await repo.get_latest_weight(self.conn, uid)
            days_since: Optional[int] = None
            if latest is None:
                eq = repo.get_user_equipment(user)
                has_profile = ("smart_scales" in eq) or float(user.get("height_cm") or 0) > 0
                if not has_profile:
                    continue
                if last:
                    try:
                        if (today_d - datetime.date.fromisoformat(last)).days < 7:
                            continue
                    except Exception:
                        pass
            else:
                raw_c = str(latest.get("created_at") or "").strip().rstrip("Z")
                try:
                    c_dt = datetime.datetime.fromisoformat(raw_c)
                    if c_dt.tzinfo is not None:
                        c_dt = c_dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
                    latest_local = local_date_str(c_dt, tz)
                    days_since = (today_d - datetime.date.fromisoformat(latest_local)).days
                except Exception:
                    days_since = 3
                if days_since < 3:
                    continue

            coach = str(user.get("coach_mode") or "balanced").strip().lower()
            cards = coach_knowledge.select_coach_principles(
                {"missed_weigh_in": True, "weight_goal": goal},
                "взвешивание весы",
                limit=1,
            )
            tip_line = f"\n\n<i>💡 {_html_mod.escape(cards[0]['action_rule'])}</i>" if cards else ""

            since_7d = (today_d - datetime.timedelta(days=6)).isoformat()
            already_fined_7d = (
                await repo.has_karma_event_recent(self.conn, uid, "weigh_in_missed", since_7d)
                if (days_since is not None and days_since >= 7 and coach != "gentle")
                else True
            )
            should_fine = days_since is not None and days_since >= 7 and coach != "gentle" and not already_fined_7d
            if days_since is not None and days_since >= 7 and coach != "gentle":
                k_state_now = await repo.get_or_create_karma_state(self.conn, uid)
                cur_score = int(k_state_now.get("score") if k_state_now.get("score") is not None else 70)
                if should_fine:
                    sick_now = karma_service.is_sick_freeze_active(user, now)
                    karma_line = (
                        "<b>(заморозка ЧП защитила карму)</b> "
                        if sick_now
                        else f"<b>Карма -5 → {max(0, cur_score - 5)}/100.</b> "
                    )
                else:
                    karma_line = ""
                msg = (
                    f"⚖ <b>Неделя без весов ({days_since} дн.)!</b>\n"
                    f"{karma_line}Когда прячешься от цифр, легко незаметно откатиться на пару кг.\n"
                    "Встань на весы натощак и скинь скрин/файл Xiaomi прямо сюда в чат."
                    f"{tip_line}"
                )
            elif days_since is None:
                msg = (
                    "⚖ <b>Утренний чекпоинт весов</b>\n"
                    "Сделай первый замер натощак и пришли скрин/файл из Xiaomi (или число) — "
                    "я пересчитаю КБЖУ под реальный состав тела."
                    f"{tip_line}"
                )
            else:
                msg = (
                    f"⚖ <b>Утреннее взвешивание</b> (прошло {days_since} дн.)\n"
                    "Утром натощак — самая честная цифра. Скинь отчёт/скрин Xiaomi прямо сюда, "
                    "чтобы сверить тренд жира, воды и мышц."
                    f"{tip_line}"
                )

            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="📸 Загрузить весы", callback_data="fit:scale_add"),
                        InlineKeyboardButton(text="⏭ Позже", callback_data="fit:weigh_skip"),
                    ]
                ]
            )
            sent = await self._safe_send_message(
                user,
                local_date,
                msg,
                reply_markup=kb,
                parse_mode="HTML",
            )
            if sent:
                if should_fine:
                    await karma_service.record_karma_event(
                        self.conn,
                        user,
                        domain="tasks",
                        event_type="weigh_in_missed",
                        delta=-5,
                        reason="Пропуск весов 7+ дней",
                        now_utc=now,
                    )
                await repo.update_weight_prompt(self.conn, uid, local_date)
                sent_count += 1
        return sent_count

    async def _weekly_home_plan(self) -> None:
        """По воскресеньям присылать план по дому на неделю."""
        now_utc = datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        for user in users:
            user = dict(user)
            if is_student_mode(user):
                continue
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
            ideas = [
                dict(r)
                for r in await repo.list_journal_entries(self.conn, user["id"], entry_type="idea", limit=4)
            ]
            if not tasks and not ideas:
                continue
            lines: list[str] = []
            if tasks:
                lines.append("🏠 <b>План по дому на неделю:</b>")
                for t in tasks:
                    lines.append(f"• {_html_mod.escape(str(t['title']))} — до {t['next_due_date']}")
            if ideas:
                if lines:
                    lines.append("")
                lines.append("💡 <b>Твои идеи и цели (перевести в задачу на неделю?):</b>")
                for idx, idea in enumerate(ideas, 1):
                    txt = str(idea.get("title") or idea.get("user_text") or "").strip()
                    if txt:
                        lines.append(f"{idx}. {_html_mod.escape(txt)}")
            kb_rows = [
                [
                    InlineKeyboardButton(
                        text=f"➡️ В задачу: {str(idea.get('title') or idea.get('user_text') or 'Идея')[:22]}",
                        callback_data=f"diary:to_task:{idea['id']}",
                    )
                ]
                for idea in ideas[:3]
            ]
            kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
            await self._safe_send_message(user, today, "\n".join(lines), reply_markup=kb, parse_mode="HTML")

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

    async def _tick_pending_meals(self, now_utc: Optional[datetime.datetime] = None) -> int:
        """Тихо зафиксировать просроченные черновики КБЖУ, если пользователь не стал уточнять."""
        now = now_utc or datetime.datetime.utcnow()
        due_rows = await repo.list_due_pending_meals(self.conn, now_utc=now)
        committed = 0
        for row in due_rows:
            uid = int(row["user_id"])
            user_row = await repo.get_user(self.conn, uid)
            if not user_row or is_student_mode(user_row):
                await repo.resolve_pending_meal(self.conn, int(row["id"]), status="cancelled")
                continue
            await inbox_service.commit_pending_meal(
                self.conn,
                dict(user_row),
                row,
                mult=1.0,
                status="auto_committed",
                now_utc=now,
            )
            committed += 1
        return committed

    async def _tick_nutrition_evening(self, now_utc: Optional[datetime.datetime] = None) -> int:
        """Один короткий вечерний разбор дня по КБЖУ, цели и движению (не чаще 1 раза/день)."""
        now = now_utc or datetime.datetime.utcnow()
        users = await repo.list_users(self.conn)
        sent_count = 0
        for user_row in users:
            user = dict(user_row)
            if is_student_mode(user):
                continue
            if not int(user.get("nutrition_checkin_enabled", 1)):
                continue
            goal = str(user.get("weight_goal") or "loss").strip().lower()
            if goal == "mindful":
                continue
            tz = str(user.get("timezone") or "UTC")
            local_date = local_date_str(now, tz)
            if user.get("pause_until") and local_date <= (user["pause_until"] or ""):
                continue
            if user.get("quiet_mode"):
                continue
            if str(user.get("nutrition_checkin_last_date") or "") == local_date:
                continue
            if _sleep_window_active(user, now) or pulse_service._confirmed_sleep_active(user, now):
                continue

            local_hhmm = format_time_local(now, tz)
            now_t = parse_hhmm(local_hhmm)
            _, eff_sleep, _ = pulse_service.get_effective_schedule(user, local_date)
            sleep_t = parse_hhmm(eff_sleep)
            if now_t is None or sleep_t is None:
                continue
            now_m = now_t.hour * 60 + now_t.minute
            sleep_m = sleep_t.hour * 60 + sleep_t.minute
            if sleep_m < now_m and sleep_m <= 360:
                sleep_m += 24 * 60
            mins_left = sleep_m - now_m
            if not (60 <= mins_left <= 135):
                continue

            uid = int(user["id"])
            advice = await fitness_nutrition.get_daily_nutrition_advice(self.conn, user, now_utc=now)
            week = await repo.workout_stats_week(self.conn, uid, local_date)
            meals_cnt = int(advice["eaten"].get("meals_count") or 0)
            kcal_burned_today = int(week.get("today_kcal_burned") or 0)
            if meals_cnt == 0 and kcal_burned_today == 0 and advice["targets"].get("is_estimated", True):
                continue

            tgt = advice["targets"]
            eat = advice["eaten"]
            rem = advice["remaining"]
            coach = str(user.get("coach_mode") or "balanced").strip().lower()

            if coach == "gentle":
                tail = (
                    f"Остаток ~{rem['kcal']} ккал — если голодно, выбери спокойный лёгкий перекус."
                    if meals_cnt > 0
                    else "Если забылось отметить еду — ничего страшного, главное высыпайся!"
                )
                head = "🌙 <b>Мягкий вечерний итог дня</b>"
            elif coach == "strict":
                p_miss = int(rem["protein_g"])
                tail = (
                    f"По белку недобор {p_miss} г — завтра докинь белка в первый приём пищи!"
                    if p_miss >= 25
                    else "По КБЖУ и белку держишь хороший ориентир, так держать!"
                )
                head = "🔥 <b>Вечерний чекпоинт тренера</b>"
            else:
                tail = advice["summary_line"]
                head = "🎯 <b>Вечерний разбор дня</b>"

            late_night = now_t.hour >= 21 or now_t.hour < 5
            mov_txt = (
                f"Движение сегодня: ~{kcal_burned_today} ккал"
                if kcal_burned_today > 0
                else (
                    "Движение: на сегодня фокус на сон, 3-мин старт перенеси на завтра утром"
                    if late_night
                    else "Движение: ещё можно успеть 3-минутный микро-старт"
                )
            )
            karma_note = ""
            karma_event_spec: Optional[Tuple[str, str, int, str]] = None
            k_state_now = await repo.get_or_create_karma_state(self.conn, uid)
            cur_score = int(k_state_now.get("score") if k_state_now.get("score") is not None else 70)
            sick_now = karma_service.is_sick_freeze_active(user, now)
            if meals_cnt > 0 and float(eat.get("protein_g") or 0) >= float(tgt["protein_g"]) * 0.8 and int(eat["kcal"]) <= int(tgt["kcal"]) + 150:
                if not await repo.has_karma_event_today(self.conn, uid, local_date, "nutrition_on_track"):
                    karma_event_spec = ("food", "nutrition_on_track", 3, "Точный день по КБЖУ")
                    preview_after = cur_score if sick_now else min(100, cur_score + 3)
                    karma_note = f"\n✨ <b>+3 кармы за точный день по КБЖУ</b> ({preview_after}/100)"
            elif coach == "strict" and int(week.get("count") or 0) == 0 and (
                int(eat["kcal"]) > int(tgt["kcal"]) + 250 or (meals_cnt == 0 and not tgt.get("is_estimated", True))
            ):
                if not sick_now and not await repo.has_karma_event_today(
                    self.conn, uid, local_date, "fitness_discipline_miss"
                ):
                    karma_event_spec = ("tasks", "fitness_discipline_miss", -4, "Сбой режима питания/движения")
                    preview_after = max(0, cur_score - 4)
                    karma_note = (
                        f"\n⚠️ <b>Карма -4 → {preview_after}/100:</b> "
                        "завтра правило «Никогда не пропускай дважды» — начни с 3-мин старта и белковой еды."
                    )

            wellness_row = await repo.get_wellness(self.conn, uid)
            meal_prof = str((dict(wellness_row) if wellness_row else {}).get("meal_profile") or "")
            cards = coach_knowledge.select_coach_principles(
                {
                    "week_workouts": week,
                    "nutrition_advice": advice,
                    "meal_profile": meal_prof,
                    "weight_goal": goal,
                },
                limit=1,
            )
            coach_tip = f"\n📚 <i>{_html_mod.escape(cards[0]['action_rule'])}</i>" if cards else ""

            msg = (
                f"{head}\n"
                f"🍽 КБЖУ: <b>{eat['kcal']}</b> / {tgt['kcal']} ккал (Б {eat['protein_g']:g}/{tgt['protein_g']}г)\n"
                f"🏃 {mov_txt}\n"
                f"💡 {tail}{karma_note}{coach_tip}"
            )
            second_btn = (
                InlineKeyboardButton(text="🌙 Сон и карма", callback_data="pulse:hub")
                if late_night
                else InlineKeyboardButton(text="🫠 3 мин сейчас", callback_data="fit:lazy")
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🔥 Мой КБЖУ", callback_data="kbju:today"),
                        second_btn,
                    ]
                ]
            )
            sent = await self._safe_send_message(user, local_date, msg, reply_markup=kb, parse_mode="HTML")
            if sent:
                if karma_event_spec is not None:
                    ev_dom, ev_type, ev_delta, ev_reason = karma_event_spec
                    if ev_delta > 0:
                        await repo.add_points(self.conn, uid, ev_delta, local_date=local_date)
                    await karma_service.record_karma_event(
                        self.conn,
                        user,
                        domain=ev_dom,
                        event_type=ev_type,
                        delta=ev_delta,
                        reason=ev_reason,
                        now_utc=now,
                    )
                await repo.update_user_coach_settings(
                    self.conn, uid, nutrition_checkin_last_date=local_date
                )
                sent_count += 1
        return sent_count
