import asyncio
import datetime
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from db import repositories as repo
from db.database import connect, init_db
from handlers import (
    custom_reminders,
    day_plan,
    finance,
    focus_cafe,
    guides,
    home_tasks,
    meds,
    routine_items,
    start,
    wellness,
)
from scheduler.reminder import ReminderScheduler
from utils.finance import _payday_bounds, payday_summary
from utils.pager import start_paged
from utils.time import is_valid_timezone, local_date_str, tzinfo_from_string


class _FakeMessage:
    def __init__(self, user_id: int, full_name: str = "Tester", text: str = ""):
        self.from_user = SimpleNamespace(id=user_id, full_name=full_name, first_name=full_name, username="tester")
        self.chat = SimpleNamespace(id=user_id)
        self.text = text
        self.answers: list[tuple[str, dict]] = []
        self.edits: list[tuple[str, dict]] = []
        self.markup_edits: list[object] = []
        self.bot = None

    async def answer(self, text: str, **kwargs):
        self.answers.append((text, kwargs))
        return SimpleNamespace(message_id=301, chat=self.chat)

    async def edit_text(self, text: str, **kwargs):
        self.edits.append((text, kwargs))
        return SimpleNamespace(message_id=301, chat=self.chat)

    async def edit_reply_markup(self, reply_markup=None):
        self.markup_edits.append(reply_markup)
        return None


class _FakeCallback:
    def __init__(self, data: str, user_id: int, full_name: str = "Tester"):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, full_name=full_name, username="tester")
        self.message = _FakeMessage(user_id=user_id, full_name=full_name)
        self.bot = None
        self.alerts: list[tuple[str | None, bool]] = []

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

    async def get_state(self):
        return self.state

    async def clear(self):
        self.data.clear()
        self.state = None


class Milestone5StabilityAndSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.user_tg = 888001
        self.user_id = await repo.create_user(self.db, self.user_tg, "Юзер Пять", "UTC", "08:00", "23:30")
        self.user = dict(await repo.get_user(self.db, self.user_id))

    async def asyncTearDown(self):
        for t in list(wellness._ACTIVE_FOCUS_TASKS.values()):
            if not t.done():
                t.cancel()
        wellness._ACTIVE_FOCUS_TASKS.clear()
        await self.db.close()

    async def test_finance_payday_day31_overspend_and_expense_delete(self):
        s1, n1, _ = _payday_bounds("2026-01-31", 31)
        s2, n2, _ = _payday_bounds("2026-02-28", 31)
        self.assertEqual(s1, "2026-01-31")
        self.assertEqual(n1, "2026-02-28")
        self.assertEqual(s2, "2026-02-28")
        self.assertEqual(n2, "2026-03-31")

        await repo.upsert_budget(self.db, self.user_id, monthly_limit=1000, payday_day=31)
        exp_id = await repo.add_expense(self.db, self.user_id, 1350, "еда")
        today_str = datetime.date.today().isoformat()
        summary = await payday_summary(self.db, self.user, today_str)
        self.assertIsNotNone(summary)
        self.assertIn("350", summary)

        # Weekly report should not crash on payday_day=31
        week_txt = await finance._compose_spent_week(self.db, self.user)
        self.assertIn("Траты", week_txt)

        # Delete expense via money:del_exp
        cb_del = _FakeCallback(f"money:del_exp:{exp_id}", self.user_tg)
        await finance.money_callbacks(cb_del, _FakeFSM(), self.db)
        left = await repo.expenses_last_days(self.db, self.user_id, days=30)
        self.assertEqual(len(left), 0)

    async def test_home_tasks_quick_finish_living_zone_and_force_new(self):
        steps = await home_tasks._generate_flow(["living"], "normal")
        self.assertTrue(any("Гостиная" in s["text"] for s in steps))

        # quick_mark completion must not crash with NameError on home_menu_keyboard
        fsm = _FakeFSM({
            "quick_scenario": "kitchen_10",
            "quick_steps": [{"text": "Протереть стол", "points": 2, "status": "pending"}],
        })
        cb_done = _FakeCallback("quick:mark:done:kitchen_10:0", self.user_tg)
        await home_tasks.quick_mark(cb_done, self.db, fsm)
        self.assertGreater(len(cb_done.message.answers), 0)
        self.assertIn("Готово", cb_done.message.answers[0][0])

        # quick:cancel exits cleanly
        fsm_c = _FakeFSM({"quick_scenario": "kitchen_10", "quick_steps": []})
        cb_cancel = _FakeCallback("quick:cancel", self.user_tg)
        await home_tasks.quick_cancel(cb_cancel, fsm_c)
        self.assertEqual(fsm_c.data, {})

        # cl2_force_new abandons active session instead of looping back to prompt
        await repo.create_cleaning_session(self.db, self.user_id, "fast", '["kitchen"]', '[]')
        cb_force = _FakeCallback("cl2:new_force", self.user_tg)
        await home_tasks.cl2_force_new(cb_force, self.db, _FakeFSM())
        self.assertIsNone(await repo.get_active_session(self.db, self.user_id))
        self.assertTrue(any("зоны" in ans[0].lower() for ans in cb_force.message.answers))

    async def test_guides_expiring_no_nameerror_and_last_page_hides_next(self):
        msg = _FakeMessage(self.user_tg, "Юзер Пять")
        await guides._suggest_from_expiring(msg, self.db)
        self.assertGreater(len(msg.answers), 0)

        long_text = "\n".join(f"Пункт {i}: " + "x" * 120 for i in range(40))
        key, chunks = start_paged(long_text, self.user_tg)
        self.assertGreater(len(chunks), 1)
        last_idx = len(chunks) - 1
        cb_last = _FakeCallback(f"page:{key}:{last_idx}", self.user_tg)
        await guides.paginate(cb_last)
        self.assertNotIn("Дальше", repr(cb_last.message.answers[-1][1].get("reply_markup")))

    async def test_meds_pause_finish_retime_not_swallowed(self):
        med_id = await repo.create_med(self.db, self.user_id, "Магний", "1 таб", "once_daily", "09:00", None, "")
        fsm = _FakeFSM()
        meds_cb_handler = next(h for h in meds.router.callback_query.handlers if h.callback is meds.meds_callbacks)
        cb_pause = _FakeCallback(f"med:pause7:{med_id}", self.user_tg)
        matched_pause = True
        for f in meds_cb_handler.filters:
            res = await f.call(cb_pause)
            if not res:
                matched_pause = False
                break
        self.assertFalse(matched_pause)

        await meds.med_pause(cb_pause, self.db)
        med_row = await repo.get_med(self.db, med_id)
        self.assertFalse(bool(med_row["active"]))

        cb_retime = _FakeCallback(f"med:retime:{med_id}", self.user_tg)
        matched_retime = True
        for f in meds_cb_handler.filters:
            res = await f.call(cb_retime)
            if not res:
                matched_retime = False
                break
        self.assertFalse(matched_retime)
        await meds.med_retime_start(cb_retime, fsm, self.db)
        self.assertEqual(fsm.state, meds.MedState.retime)

    async def test_timezone_offset_and_onboarding_hhmm_and_menu_text(self):
        tz_half = tzinfo_from_string("UTC+3:30")
        self.assertEqual(tz_half.utcoffset(None), datetime.timedelta(hours=3, minutes=30))
        self.assertTrue(is_valid_timezone("UTC+3:30"))
        self.assertFalse(is_valid_timezone("UTC+3:99"))
        self.assertFalse(is_valid_timezone("UTC+99"))

        fsm = _FakeFSM({"name": "Тест", "gender": "male"})
        msg_tz = _FakeMessage(self.user_tg, "Тест", text=datetime.datetime.utcnow().strftime("%H:%M"))
        await start.reg_timezone(msg_tz, fsm)
        self.assertEqual(fsm.state, start.Registration.wake_up)
        self.assertTrue(is_valid_timezone(fsm.data["timezone"]))

    async def test_wellness_non_blocking_focus_and_water_affirm_ticks(self):
        await repo.upsert_wellness(
            self.db,
            self.user_id,
            water_enabled=1,
            water_times=datetime.datetime.utcnow().strftime("%H:%M"),
            focus_mode=1,
            focus_work=25,
            focus_rest=5,
            affirm_enabled=1,
            affirm_hours=json.dumps([datetime.datetime.utcnow().hour]),
        )
        msg = _FakeMessage(self.user_tg, "Юзер")
        msg.bot = SimpleNamespace(send_message=AsyncMock())
        # Must return immediately rather than blocking 25 minutes
        await asyncio.wait_for(wellness.focus_mode(msg, self.db), timeout=1.0)
        self.assertIn(self.user_tg, wellness._ACTIVE_FOCUS_TASKS)
        cb_stop = _FakeCallback("focus:stop", self.user_tg)
        await wellness.focus_again(cb_stop)
        self.assertNotIn(self.user_tg, wellness._ACTIVE_FOCUS_TASKS)

        # Scheduler sends water & affirmations even when pulse_enabled=1 (default)
        bot_mock = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)))
        sched = ReminderScheduler(bot_mock, self.db)
        await sched._tick_wellness()
        await sched._tick_affirmations()
        self.assertGreaterEqual(bot_mock.send_message.await_count, 2)

    async def test_routine_no_double_points_focus_cooldown_custom_dedupe_day_plan_del(self):
        today = local_date_str(datetime.datetime.utcnow(), "UTC")
        await repo.ensure_user_routines(self.db, self.user_id)
        await repo.ensure_routine_steps(self.db, self.user_id)
        rlist = await repo.list_user_routines(self.db, self.user_id)
        rid = int(dict(rlist[0])["routine_id"])

        # Toggle step 0 -> +1 pt, Finish -> +1 completion bonus (NOT re-awarding step 0!)
        await routine_items.routine_item_toggle(_FakeCallback(f"ritem:{rid}:{today}:0", self.user_tg), self.db)
        await routine_items.routine_finish(_FakeCallback(f"ritemfinish:{rid}:{today}", self.user_tg), self.db)
        # Re-clicking finish on already-done task -> 0 extra points
        await routine_items.routine_finish(_FakeCallback(f"ritemfinish:{rid}:{today}", self.user_tg), self.db)
        pts = await repo.points_today(self.db, self.user_id, today)
        self.assertEqual(pts, 2)

        # Focus cafe blocks start during cooldown
        until = (datetime.datetime.utcnow() + datetime.timedelta(minutes=20)).isoformat()
        await self.db.execute("UPDATE users SET focus_cooldown_until = ? WHERE id = ?", (until, self.user_id))
        await self.db.commit()
        cb_cafe = _FakeCallback("cafe:duration:25", self.user_tg)
        await focus_cafe._start_session(cb_cafe, _FakeFSM({"task": "Код"}), self.db, 25)
        self.assertIsNone(await repo.get_active_focus_session(self.db, self.user_id))

        # Custom reminders dedupe double done + persist later snooze in pulse_checkins
        rem_id = await repo.create_custom_reminder(self.db, self.user_id, "Позвонить", "12:00")
        before_pts = await repo.points_today(self.db, self.user_id, today)
        await custom_reminders.custom_action(_FakeCallback(f"custom:{rem_id}:{today}:done", self.user_tg), self.db)
        await custom_reminders.custom_action(_FakeCallback(f"custom:{rem_id}:{today}:done", self.user_tg), self.db)
        self.assertEqual(await repo.points_today(self.db, self.user_id, today), before_pts + 3)

        await custom_reminders.custom_action(_FakeCallback(f"custom:{rem_id}:{today}:later", self.user_tg), self.db)
        snooze_chk = await repo.get_pulse_checkin(self.db, self.user_id, today, f"custom_{rem_id}")
        self.assertIsNotNone(snooze_chk)

        # Day plan delete keeps interactive ✅ buttons for remaining items
        i1 = await repo.add_day_plan_item(self.db, self.user_id, today, "Дело 1", is_important=True)
        i2 = await repo.add_day_plan_item(self.db, self.user_id, today, "Дело 2", is_important=False)
        cb_dp_del = _FakeCallback(f"dplan:del:{i1}", self.user_tg)
        await day_plan.day_plan_callbacks(cb_dp_del, _FakeFSM(), self.db)
        edit_kb = cb_dp_del.message.edits[-1][1]["reply_markup"]
        self.assertTrue(any(f"dplan:done:{i2}:list:0" == b.callback_data for row in edit_kb.inline_keyboard for b in row))
