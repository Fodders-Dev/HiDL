"""Tests for Sprint 10: RDR2 karma engine, unified 5-checkpoint daily pulse, snoozes, night detector, and 6-button UI."""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from db.database import connect, init_db
from db import repositories as repo
from hidl.app import create_app
from keyboards.common import main_menu_keyboard
from scheduler.reminder import ReminderScheduler
from services import karma_service, pulse_service
from utils.time import local_date_str


class LocalBotProxy:
    """Minimal Telegram bot proxy capturing sent/edited messages for Dispatcher tests."""

    def __init__(self, real_bot=None):
        self.id = real_bot.id if real_bot else 1
        self.sent_messages: list[dict] = []
        self.edited_messages: list[dict] = []
        self.edited_markups: list[dict] = []
        self.callbacks: list[dict] = []
        self._last_output: dict = {"message_id": 0, "text": "", "reply_markup": None}
        self._msg_seq = 9000

    async def __call__(self, method):
        if isinstance(method, SendMessage):
            self._msg_seq += 1
            entry = {
                "message_id": self._msg_seq,
                "chat_id": method.chat_id,
                "text": method.text or "",
                "reply_markup": method.reply_markup,
            }
            self.sent_messages.append(entry)
            self._last_output = entry
            return Message(
                message_id=self._msg_seq,
                date=datetime.datetime.now(datetime.timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=User(id=1, is_bot=True, first_name="HiDL"),
                text=method.text or "",
            )
        if isinstance(method, EditMessageText):
            entry = {
                "message_id": method.message_id,
                "text": method.text or "",
                "reply_markup": method.reply_markup,
            }
            self.edited_messages.append(entry)
            self._last_output = entry
            return True
        if isinstance(method, EditMessageReplyMarkup):
            entry = {
                "message_id": method.message_id,
                "reply_markup": method.reply_markup,
            }
            self.edited_markups.append(entry)
            if self._last_output.get("message_id") == method.message_id:
                self._last_output = {
                    **self._last_output,
                    "reply_markup": method.reply_markup,
                }
            return True
        if isinstance(method, DeleteMessage):
            return True
        if isinstance(method, AnswerCallbackQuery):
            self.callbacks.append({"text": method.text, "show_alert": method.show_alert})
            return True
        return None

    async def send_message(self, chat_id: int, text: str, reply_markup=None, **kwargs):
        return await self(SendMessage(chat_id=chat_id, text=text, reply_markup=reply_markup))


class Sprint10KarmaPulseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "sprint10_test.db"
        self.db_url = f"sqlite:///{self.db_path}"
        self._orig_sim_db = os.environ.get("HIDL_SIM_DB_URL")
        os.environ["HIDL_SIM_DB_URL"] = self.db_url
        self.ctx = await create_app(test_mode=True)
        self.db = self.ctx.db_conn
        self.dp = self.ctx.dp
        self.bot = LocalBotProxy(self.ctx.bot)
        self.tg_id = 777001122
        self.uid = await repo.create_user(
            self.db,
            self.tg_id,
            "Tester",
            "Europe/Moscow",
            "6:50",
            "23:30",
        )
        self._seq = 100

    async def asyncTearDown(self) -> None:
        try:
            await self.db.close()
        except Exception:
            pass
        try:
            await self.ctx.bot.session.close()
        except Exception:
            pass
        if self._orig_sim_db is None:
            os.environ.pop("HIDL_SIM_DB_URL", None)
        else:
            os.environ["HIDL_SIM_DB_URL"] = self._orig_sim_db
        self._tmpdir.cleanup()

    async def _user(self) -> dict:
        row = await repo.get_user(self.db, self.uid)
        return dict(row)

    async def _send_text(self, text: str) -> dict:
        self._seq += 1
        msg = Message(
            message_id=self._seq,
            date=datetime.datetime.now(datetime.timezone.utc),
            chat=Chat(id=self.tg_id, type="private"),
            from_user=User(id=self.tg_id, is_bot=False, first_name="Tester"),
            text=text,
        )
        await self.dp.feed_update(self.bot, Update(update_id=self._seq, message=msg))
        return self.bot._last_output

    async def _click(self, data: str) -> dict:
        self._seq += 1
        msg = Message(
            message_id=self.bot._last_output.get("message_id") or 1,
            date=datetime.datetime.now(datetime.timezone.utc),
            chat=Chat(id=self.tg_id, type="private"),
            from_user=User(id=1, is_bot=True, first_name="HiDL"),
            text=self.bot._last_output.get("text") or "menu",
        )
        cb = CallbackQuery(
            id=f"cb_{self._seq}",
            chat_instance="ci",
            from_user=User(id=self.tg_id, is_bot=False, first_name="Tester"),
            message=msg,
            data=data,
        )
        await self.dp.feed_update(self.bot, Update(update_id=self._seq, callback_query=cb))
        return self.bot._last_output

    async def test_1_karma_modes_clamp_and_domain_debts(self) -> None:
        self.assertEqual(karma_service.get_karma_mode(95)["code"], "proud")
        self.assertEqual(karma_service.get_karma_mode(70)["code"], "care")
        self.assertEqual(karma_service.get_karma_mode(35)["code"], "strict")
        self.assertEqual(karma_service.get_karma_mode(10)["code"], "alarm")

        user = await self._user()
        r1 = await karma_service.record_karma_event(
            self.db, user, domain="sleep", event_type="checkin_ignored", delta=-10, reason="Игнор сна 1"
        )
        r2 = await karma_service.record_karma_event(
            self.db, user, domain="sleep", event_type="night_catch", delta=-5, reason="Ночной онлайн"
        )
        self.assertEqual(r2["score_after"], 55)
        self.assertEqual(r2["state"]["sleep_debt"], 4)
        self.assertEqual(r2["state"]["sleep_streak"], 0)
        callout = karma_service.domain_debt_callout(r2["state"], domain="sleep")
        self.assertIn("долг", callout.lower())

        # Clamping at 100 and 0
        await karma_service.record_karma_event(
            self.db, user, domain="sleep", event_type="bonus", delta=100, reason="Сверхсерия"
        )
        st_max = await repo.get_or_create_karma_state(self.db, self.uid)
        self.assertEqual(st_max["score"], 100)
        await karma_service.record_karma_event(
            self.db, user, domain="tasks", event_type="penalty", delta=-200, reason="Сброс"
        )
        st_min = await repo.get_or_create_karma_state(self.db, self.uid)
        self.assertEqual(st_min["score"], 0)

    async def test_2_escalation_ladder_1_2_3_to_ignored(self) -> None:
        # User MSK (UTC+3), wake = 06:50 MSK = 03:50 UTC on 2026-11-19 (Thursday)
        t0 = datetime.datetime(2026, 11, 19, 3, 50)
        await pulse_service.process_pulse_tick(self.db, self.bot, now_utc=t0)
        self.assertEqual(len(self.bot.sent_messages), 1)
        self.assertIn("Доброе утро", self.bot.sent_messages[-1]["text"])

        checkin = await repo.get_pulse_checkin(self.db, self.uid, "2026-11-19", "wake")
        self.assertIsNotNone(checkin)
        self.assertEqual(checkin["ping_count"], 1)

        # +15 min -> Level 2
        t1 = t0 + datetime.timedelta(minutes=15)
        await pulse_service.process_pulse_tick(self.db, self.bot, now_utc=t1)
        self.assertEqual(len(self.bot.sent_messages), 2)
        self.assertIn("Просыпайся", self.bot.sent_messages[-1]["text"])

        # +20 min -> Level 3 CAPS
        t2 = t1 + datetime.timedelta(minutes=20)
        await pulse_service.process_pulse_tick(self.db, self.bot, now_utc=t2)
        self.assertEqual(len(self.bot.sent_messages), 3)
        self.assertIn("ТРЕТИЙ СИГНАЛ", self.bot.sent_messages[-1]["text"])

        # +20 min -> Marked ignored (-10 karma)
        t3 = t2 + datetime.timedelta(minutes=20)
        await pulse_service.process_pulse_tick(self.db, self.bot, now_utc=t3)
        self.assertEqual(len(self.bot.sent_messages), 4)
        self.assertIn("игнор 3/3", self.bot.sent_messages[-1]["text"].lower())

        after = await repo.get_pulse_checkin(self.db, self.uid, "2026-11-19", "wake")
        self.assertEqual(after["status"], "ignored")
        state = await repo.get_or_create_karma_state(self.db, self.uid)
        self.assertEqual(state["score"], 60)

    async def test_3_honest_snooze_and_repeat_penalty(self) -> None:
        t0 = datetime.datetime(2026, 11, 19, 10, 0)  # 13:00 MSK lunch
        await pulse_service.process_pulse_tick(self.db, self.bot, now_utc=t0)
        self.assertEqual(len(self.bot.sent_messages), 1)
        checkin = await repo.get_pulse_checkin(self.db, self.uid, "2026-11-19", "lunch")
        self.assertIsNotNone(checkin)

        user = await self._user()
        s1 = await pulse_service.snooze_checkin(self.db, user, checkin["id"], 10, now_utc=t0)
        self.assertTrue(s1["ok"])
        self.assertEqual(s1["karma"]["delta"], 0)

        # +5 min: should NOT fire yet
        await pulse_service.process_pulse_tick(
            self.db, self.bot, now_utc=t0 + datetime.timedelta(minutes=5)
        )
        self.assertEqual(len(self.bot.sent_messages), 1)

        # +10 min: snooze reminder fires
        t_due = t0 + datetime.timedelta(minutes=10)
        await pulse_service.process_pulse_tick(self.db, self.bot, now_utc=t_due)
        self.assertEqual(len(self.bot.sent_messages), 2)
        self.assertIn("переносу", self.bot.sent_messages[-1]["text"].lower())

        # Repeat snooze deducts -3 karma
        s2 = await pulse_service.snooze_checkin(self.db, user, checkin["id"], 15, now_utc=t_due)
        self.assertEqual(s2["karma"]["delta"], -3)
        self.assertEqual(s2["karma"]["score_after"], 67)

    async def test_4_sick_freeze_24h_and_monthly_limit(self) -> None:
        t0 = datetime.datetime(2026, 11, 19, 3, 50)
        user = await self._user()
        res1 = await karma_service.activate_sick_freeze(self.db, user, now_utc=t0)
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["remaining"], 1)

        # Negative karma clamped to 0 while frozen
        user = await self._user()
        neg = await karma_service.record_karma_event(
            self.db, user, domain="sleep", event_type="checkin_skip", delta=-4, reason="Пропуск при болезни", now_utc=t0
        )
        self.assertTrue(neg["frozen"])
        self.assertEqual(neg["delta"], 0)
        self.assertEqual(neg["score_after"], 70)

        # Ping 1 fires, then after +15 min under sick freeze it quietly skips instead of escalating/penalizing
        await pulse_service.process_pulse_tick(self.db, self.bot, now_utc=t0)
        self.assertEqual(len(self.bot.sent_messages), 1)
        await pulse_service.process_pulse_tick(
            self.db, self.bot, now_utc=t0 + datetime.timedelta(minutes=15)
        )
        self.assertEqual(len(self.bot.sent_messages), 1)
        chk = await repo.get_pulse_checkin(self.db, self.uid, "2026-11-19", "wake")
        self.assertEqual(chk["status"], "skipped")

        # 2nd activation after expiry + 3rd rejected in same month
        t_next = t0 + datetime.timedelta(hours=25)
        user = await self._user()
        res2 = await karma_service.activate_sick_freeze(self.db, user, now_utc=t_next)
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["remaining"], 0)

        t_third = t_next + datetime.timedelta(hours=25)
        user = await self._user()
        res3 = await karma_service.activate_sick_freeze(self.db, user, now_utc=t_third)
        self.assertFalse(res3["ok"])
        self.assertTrue(res3["limit_reached"])

    async def test_5_weekend_schedule_hmm_parsing_and_rollover(self) -> None:
        await repo.update_user_weekend_schedule(self.db, self.uid, "10:00", "01:00")
        user = await self._user()

        # 2026-11-20 is Friday, 2026-11-21 is Saturday
        w_fri, s_fri, is_we_fri = pulse_service.get_effective_schedule(user, "2026-11-20")
        self.assertFalse(is_we_fri)
        self.assertEqual((w_fri, s_fri), ("06:50", "23:30"))

        w_sat, s_sat, is_we_sat = pulse_service.get_effective_schedule(user, "2026-11-21")
        self.assertTrue(is_we_sat)
        self.assertEqual((w_sat, s_sat), ("10:00", "01:00"))

        slots_sat = pulse_service.compute_daily_slots(user, None, "2026-11-21")
        by_key = {s["slot_key"]: s for s in slots_sat}
        self.assertEqual(by_key["wake"]["local_hhmm"], "10:00")
        self.assertEqual(by_key["winddown"]["local_hhmm"], "00:00")
        self.assertEqual(by_key["bedtime"]["local_hhmm"], "00:45")
        # 2026-11-22 00:45 MSK = 2026-11-21 21:45 UTC
        self.assertEqual(by_key["bedtime"]["scheduled_at_utc"], datetime.datetime(2026, 11, 21, 21, 45))

        # Day plan rollover moves only unfinished items without duplicating existing tomorrow items
        await repo.add_day_plan_item(self.db, self.uid, "2026-11-19", "Сделанная задача", is_important=True)
        items_today = [dict(r) for r in await repo.list_day_plan_items(self.db, self.uid, "2026-11-19")]
        await repo.mark_day_plan_item_done(self.db, items_today[0]["id"], True)
        await repo.add_day_plan_item(self.db, self.uid, "2026-11-19", "Хвост 1", is_important=True)
        await repo.add_day_plan_item(self.db, self.uid, "2026-11-19", "Хвост 2", is_important=False)
        await repo.add_day_plan_item(self.db, self.uid, "2026-11-20", "хвост 2", is_important=False)

        moved = await repo.rollover_unfinished_day_plan_items(self.db, self.uid, "2026-11-19", "2026-11-20")
        self.assertEqual(moved, 1)
        rem_today = [dict(r) for r in await repo.list_day_plan_items(self.db, self.uid, "2026-11-19")]
        self.assertEqual([i["title"] for i in rem_today], ["Сделанная задача"])
        titles_tmrw = [dict(r)["title"] for r in await repo.list_day_plan_items(self.db, self.uid, "2026-11-20")]
        self.assertEqual(len(titles_tmrw), 2)
        self.assertIn("Хвост 1", titles_tmrw)

        # Idempotent 2nd rollover
        moved2 = await repo.rollover_unfinished_day_plan_items(self.db, self.uid, "2026-11-19", "2026-11-20")
        self.assertEqual(moved2, 0)

    async def test_6_night_detector_across_midnight_and_cooldown(self) -> None:
        t_confirm = datetime.datetime(2026, 11, 19, 20, 15)  # 23:15 MSK
        user = await self._user()
        res = await pulse_service.confirm_bedtime(self.db, user, now_utc=t_confirm)
        self.assertTrue(res["ok"])
        self.assertEqual(res["karma"]["score_after"], 75)

        # Within 5 min grace -> None
        user = await self._user()
        self.assertIsNone(
            await pulse_service.check_night_violation(
                self.db, user, now_utc=t_confirm + datetime.timedelta(minutes=3)
            )
        )

        # 01:30 MSK (22:30 UTC) -> Caught! (-5 sleep karma -> 70)
        t_night = datetime.datetime(2026, 11, 19, 22, 30)
        notice = await pulse_service.check_night_violation(self.db, user, now_utc=t_night)
        self.assertIsNotNone(notice)
        self.assertIn("-5", notice)
        st = await repo.get_or_create_karma_state(self.db, self.uid)
        self.assertEqual(st["score"], 70)

        # Cooldown within 30 min -> no duplicate penalty
        user = await self._user()
        self.assertIsNone(
            await pulse_service.check_night_violation(
                self.db, user, now_utc=t_night + datetime.timedelta(minutes=10)
            )
        )

        # 02:15 MSK (23:15 UTC) after cooldown -> caught again
        notice2 = await pulse_service.check_night_violation(
            self.db, user, now_utc=t_night + datetime.timedelta(minutes=45)
        )
        self.assertIsNotNone(notice2)

        # Morning wake tick clears sleep_confirmed -> 10:00 MSK (07:00 UTC) does NOT trigger night catch
        await pulse_service.process_pulse_tick(
            self.db, self.bot, now_utc=datetime.datetime(2026, 11, 20, 3, 55)
        )
        user_morning = await self._user()
        self.assertIsNone(user_morning.get("sleep_confirmed_at"))
        self.assertIsNone(
            await pulse_service.check_night_violation(
                self.db, user_morning, now_utc=datetime.datetime(2026, 11, 20, 7, 0)
            )
        )

    async def test_7_legacy_suppression_keeps_custom_reminders_active(self) -> None:
        user = await self._user()
        self.assertFalse(ReminderScheduler._legacy_pings_active(user))
        await repo.create_custom_reminder(self.db, self.uid, "Выпить витамин", "14:00", 1)

        sched = ReminderScheduler(self.bot, self.db)
        with patch(
            "scheduler.reminder.datetime",
            SimpleNamespace(
                datetime=SimpleNamespace(
                    utcnow=lambda: datetime.datetime(2026, 11, 19, 11, 0),  # 14:00 MSK
                    fromisoformat=datetime.datetime.fromisoformat,
                    strptime=datetime.datetime.strptime,
                    combine=datetime.datetime.combine,
                ),
                date=datetime.date,
                time=datetime.time,
                timedelta=datetime.timedelta,
                timezone=datetime.timezone,
            ),
        ):
            await sched._tick_wellness()
            await sched._tick_affirmations()
            await sched._tick_daily_brief()
            await sched._tick_care()
        self.assertEqual(len(self.bot.sent_messages), 0)

        # Custom reminder still fires at 14:00 MSK (11:00 UTC)
        await sched._tick_custom(user, datetime.datetime(2026, 11, 19, 11, 0), "2026-11-19")
        self.assertEqual(len(self.bot.sent_messages), 1)
        self.assertIn("Выпить витамин", self.bot.sent_messages[-1]["text"])

    async def test_8_six_button_ui_hub_more_and_legacy_real_snoozes(self) -> None:
        kb = main_menu_keyboard()
        labels = [btn.text for row in kb.keyboard for btn in row]
        self.assertEqual(
            labels,
            [
                "📅 Сегодня",
                "🍽 Еда и покупки",
                "🌙 Сон и карма",
                "🗓 Расписание",
                "📓 Дневник",
                "⚙ Ещё",
            ],
        )

        # ⚙ Ещё opens extra sections
        out_more = await self._send_text("⚙ Ещё")
        self.assertIn("Дополнительные разделы", out_more["text"])
        await self._click("more:home")
        self.assertIn("Дом:", self.bot._last_output["text"])

        # /today includes Karma + Pulse line & 🌙 Сон и карма button
        out_today = await self._send_text("📅 Сегодня")
        self.assertIn("Карма:", out_today["text"])
        self.assertIn("Пульс дня:", out_today["text"])
        btn_texts = [
            b.text
            for row in (out_today["reply_markup"].inline_keyboard if out_today["reply_markup"] else [])
            for b in row
        ]
        self.assertIn("🌙 Сон и карма", btn_texts)

        # 🌙 Сон и карма opens hub & sick freeze flow
        out_hub = await self._send_text("🌙 Сон и карма")
        self.assertIn("Сон, режим и карма", out_hub["text"])
        await self._click("pulse:manual_wake")
        self.assertIn("Доброе утро", self.bot._last_output["text"])

        # Hygiene tick button in wake checklist
        today_str = local_date_str(datetime.datetime.utcnow(), "Europe/Moscow")
        wake_chk = await repo.get_pulse_checkin(self.db, self.uid, today_str, "wake")
        self.assertIsNotNone(wake_chk)
        await self._click(f"pulse:hygiene:{wake_chk['id']}:water")
        wake_chk_after = await repo.get_pulse_checkin_by_id(self.db, wake_chk["id"])
        self.assertTrue(json.loads(wake_chk_after["meta_json"]).get("water_done"))

        # Legacy later buttons now create real snoozed pulse checkins!
        await self._send_text("ping anchor")
        await self._click(f"mealconfirm:{today_str}:later")
        meal_snz = await repo.get_pulse_checkin(self.db, self.uid, today_str, "lunch_snooze")
        self.assertIsNotNone(meal_snz)
        self.assertEqual(meal_snz["status"], "snoozed")

        await self._click("sleep:morning:snooze")
        wake_snz = await repo.get_pulse_checkin(self.db, self.uid, today_str, "wake")
        self.assertEqual(wake_snz["status"], "snoozed")
