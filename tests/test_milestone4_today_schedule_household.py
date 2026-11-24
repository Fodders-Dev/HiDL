import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from db import repositories as repo
from db.database import connect, init_db
from handlers import household, menu, schedule, stats
from keyboards.common import more_menu_keyboard
from scheduler.reminder import ReminderScheduler
from services import calendar_service
from utils.today import render_today


class _FakeMessage:
    def __init__(self, sender_id: int, chat_id: int):
        self.from_user = SimpleNamespace(id=sender_id, full_name="Bot", username="hidl_bot")
        self.chat = SimpleNamespace(id=chat_id)
        self.answers: list[tuple[str, dict]] = []
        self.edits: list[tuple[str, dict]] = []
        self.text = ""

    def model_copy(self, update: dict | None = None):
        clone = _FakeMessage(self.from_user.id, self.chat.id)
        clone.answers = self.answers
        clone.edits = self.edits
        clone.text = self.text
        if update:
            for k, v in update.items():
                setattr(clone, k, v)
        return clone

    async def answer(self, text: str, **kwargs):
        self.answers.append((text, kwargs))
        return SimpleNamespace(message_id=202, chat=self.chat)

    async def edit_text(self, text: str, **kwargs):
        self.edits.append((text, kwargs))
        return SimpleNamespace(message_id=202, chat=self.chat)

    async def edit_reply_markup(self, reply_markup=None):
        return None


class _FakeCallback:
    def __init__(self, data: str, user_id: int, full_name: str = "Реальный Юзер", bot_id: int = 999999999):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, full_name=full_name, username="realuser")
        self.message = _FakeMessage(sender_id=bot_id, chat_id=user_id)
        self.bot = None
        self.alerts: list[tuple[str | None, bool]] = []

    def model_copy(self, update: dict | None = None):
        clone = _FakeCallback(self.data, self.from_user.id, self.from_user.full_name)
        clone.message = self.message
        clone.bot = self.bot
        clone.alerts = self.alerts
        if update:
            for k, v in update.items():
                setattr(clone, k, v)
        return clone

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.alerts.append((text, show_alert))


class _FakeFSM:
    def __init__(self, initial: dict | None = None):
        self.data = dict(initial or {})
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, st):
        self.state = st

    async def clear(self):
        self.data.clear()
        self.state = None


class Milestone4TodayScheduleHouseholdTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.user_tg = 777001
        self.bot_tg = 999888777
        self.user_id = await repo.create_user(self.db, self.user_tg, "Юзер Один", "Europe/Moscow", "07:00", "23:30")
        self.user = dict(await repo.get_user(self.db, self.user_id))

    async def asyncTearDown(self):
        await self.db.close()

    async def test_bot_id_bug_fixed_for_stats_pantry_and_settings(self):
        await repo.add_points(self.db, self.user_id, 15, local_date="2026-11-25")
        await repo.create_pantry_item(self.db, self.user_id, "Тофу", 300, "g", None, "белок")

        cb_stats = _FakeCallback("stats:view", self.user_tg, bot_id=self.bot_tg)
        await stats.stats_view(cb_stats, self.db)
        self.assertIsNone(await repo.get_user_by_telegram_id(self.db, self.bot_tg))
        self.assertIn("15", cb_stats.message.answers[0][0])

        cb_pantry = _FakeCallback("food:pantry", self.user_tg, bot_id=self.bot_tg)
        await menu.food_callbacks(cb_pantry, self.db, _FakeFSM())
        self.assertIsNone(await repo.get_user_by_telegram_id(self.db, self.bot_tg))
        combined = " ".join(x[0] for x in cb_pantry.message.edits + cb_pantry.message.answers)
        self.assertIn("Тофу", combined)

        cb_tone = _FakeCallback("set:tone", self.user_tg, bot_id=self.bot_tg)
        await menu.settings_callbacks(cb_tone, self.db, _FakeFSM())
        self.assertGreater(len(cb_tone.message.edits), 0)
        tone_rm = cb_tone.message.edits[-1][1].get("reply_markup")
        self.assertIsNotNone(tone_rm)
        self.assertTrue(any(str(btn.callback_data or "").startswith("settings:") for row in tone_rm.inline_keyboard for btn in row))

    async def test_today_layout_compact_and_more_menu_navigation(self):
        today_str = datetime.date.today().isoformat()
        await repo.create_pantry_item(self.db, self.user_id, "Авокадо", 1, "шт", today_str, "фрукты")
        _, kb = await render_today(self.db, self.user)
        cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn("pantry:expiring", cbs)
        self.assertIn("kitchen:meal_plan", cbs)
        self.assertIn("stats:view", cbs)
        self.assertIn("money:report", cbs)
        self.assertTrue(all(len(row) <= 2 for row in kb.inline_keyboard))

        more_cbs = [btn.callback_data for row in more_menu_keyboard().inline_keyboard for btn in row]
        self.assertIn("dplan:list", more_cbs)
        self.assertIn("rem:list", more_cbs)

    async def test_schedule_suggest_refresh_date_switch_and_manage_delete(self):
        target_date = "2026-12-01"
        cb_date = _FakeCallback(f"sched:date:{target_date}", self.user_tg)
        await schedule.schedule_callbacks(cb_date, _FakeFSM(), self.db)
        self.assertIn(target_date, cb_date.message.edits[-1][0])

        cb_sug = _FakeCallback(f"sched:suggest:{target_date}:sport", self.user_tg)
        await schedule.schedule_callbacks(cb_sug, _FakeFSM(), self.db)
        self.assertIn("Спорт", cb_sug.message.edits[-1][0])

        blk_id = await repo.create_schedule_block(self.db, self.user_id, "Работа", "09:00", "18:00", "0,1,2,3,4")
        evs = await repo.list_schedule_events(self.db, self.user_id, target_date)
        self.assertEqual(len(evs), 1)

        cb_del_ev = _FakeCallback(f"sched:del_ev:{evs[0]['id']}:{target_date}", self.user_tg)
        await schedule.schedule_callbacks(cb_del_ev, _FakeFSM(), self.db)
        self.assertEqual(len(await repo.list_schedule_events(self.db, self.user_id, target_date)), 0)

        cb_del_blk = _FakeCallback(f"sched:del_blk:{blk_id}:{target_date}", self.user_tg)
        await schedule.schedule_callbacks(cb_del_blk, _FakeFSM(), self.db)
        self.assertEqual(len(await repo.list_schedule_blocks(self.db, self.user_id)), 0)

    async def test_local_schedule_event_pre_alert_sent_once(self):
        # Moscow is UTC+3; 14:00 local = 11:00 UTC. At 10:38 UTC, 22 mins before event.
        now_utc = datetime.datetime(2026, 11, 25, 10, 38)
        await repo.create_schedule_event(
            self.db, self.user_id, "2026-11-25", "14:00", "14:30", "Созвон локальный"
        )
        fake_bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=50, chat=SimpleNamespace(id=self.user_tg))))
        sent1 = await calendar_service.process_calendar_alerts_tick(self.db, fake_bot, now_utc=now_utc)
        self.assertEqual(sent1, 1)
        sent2 = await calendar_service.process_calendar_alerts_tick(self.db, fake_bot, now_utc=now_utc)
        self.assertEqual(sent2, 0)

    async def test_household_hub_dinner_vote_meal_plan_and_snooze_tick(self):
        p2_tg = 777002
        p2_id = await repo.create_user(self.db, p2_tg, "Партнёр Два", "Europe/Moscow", "08:00", "23:00")
        hid = await repo.get_or_create_household(self.db, self.user_id)
        await self.db.execute("UPDATE users SET household_id = ? WHERE id = ?", (hid, p2_id))
        await self.db.commit()

        _, hub_kb = await household._render_hub(self.db, self.user)
        hub_cbs = [btn.callback_data for row in hub_kb.inline_keyboard for btn in row]
        self.assertIn("kitchen:shoplist:household", hub_cbs)
        self.assertIn("kitchen:fridge", hub_cbs)

        poll = await repo.create_dinner_poll(
            self.db,
            hid,
            self.user_id,
            "2026-11-25",
            [{"id": "rice_veg", "title": "Рис с овощами", "time_min": 20}],
        )
        cb_pick = _FakeCallback(f"hh:vote:pick:{poll['id']}:0", self.user_tg)
        await household.hh_vote_pick_cb(cb_pick, self.db)
        plan_u1 = await repo.get_meal_plan_for_date(self.db, self.user_id, "2026-11-25")
        plan_u2 = await repo.get_meal_plan_for_date(self.db, p2_id, "2026-11-25")
        self.assertTrue(any(p["meal_slot"] == "dinner" and p["recipe_id"] == "rice_veg" for p in plan_u1))
        self.assertTrue(any(p["meal_slot"] == "dinner" and p["recipe_id"] == "rice_veg" for p in plan_u2))

        req = await repo.create_household_request(self.db, hid, self.user_id, "Юзер Один", "Купить хлеб")
        cb_snooze = _FakeCallback(f"hh:req:act:{req['id']}:snoozed", p2_tg, full_name="Партнёр Два")
        await household.hh_req_action_cb(cb_snooze, self.db)
        req_after = await repo.get_household_request(self.db, req["id"])
        self.assertEqual(req_after["status"], "snoozed")
        self.assertTrue(req_after["snooze_until_utc"])

        fake_bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=99, chat=SimpleNamespace(id=p2_tg))))
        sched = ReminderScheduler(fake_bot, self.db)
        # Before 30 mins: nothing due
        sent_early = await sched._tick_household_snoozes(now_utc=datetime.datetime.utcnow() + datetime.timedelta(minutes=10))
        self.assertEqual(sent_early, 0)
        # After 35 mins (mid-day UTC 10:00 so outside sleep): re-pings and resets to open
        future_day = datetime.datetime.utcnow() + datetime.timedelta(minutes=35)
        future_day = future_day.replace(hour=10, minute=0)
        if future_day <= datetime.datetime.fromisoformat(req_after["snooze_until_utc"]):
            future_day = datetime.datetime.fromisoformat(req_after["snooze_until_utc"]) + datetime.timedelta(minutes=5)
            # adjust to 10:00 UTC next day so sleep window doesn't skip
            future_day = (future_day + datetime.timedelta(days=1)).replace(hour=10, minute=0)
        sent_due = await sched._tick_household_snoozes(now_utc=future_day)
        self.assertGreaterEqual(sent_due, 1)
        req_reset = await repo.get_household_request(self.db, req["id"])
        self.assertEqual(req_reset["status"], "open")
