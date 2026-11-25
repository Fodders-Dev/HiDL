from __future__ import annotations

import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from db import repositories as repo
from db.database import connect, init_db
from handlers import feedback, settings, start
import llm_client


class _MemFSM:
    def __init__(self) -> None:
        self._state = None
        self._data: dict = {}

    async def set_state(self, st) -> None:
        self._state = st

    async def get_state(self):
        return self._state

    async def update_data(self, **kw) -> None:
        self._data.update(kw)

    async def get_data(self) -> dict:
        return dict(self._data)

    async def clear(self) -> None:
        self._state = None
        self._data.clear()


def _fake_cb(data: str, *, tg_id: int, full_name: str = "User", bot=None):
    msg = SimpleNamespace(
        text="",
        chat=SimpleNamespace(id=tg_id),
        from_user=SimpleNamespace(id=tg_id, full_name=full_name, first_name=full_name),
        bot=bot,
        answer=AsyncMock(),
        edit_text=AsyncMock(),
        edit_reply_markup=AsyncMock(),
    )
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=tg_id, full_name=full_name, first_name=full_name, username="testuser"),
        message=msg,
        bot=bot,
        answer=AsyncMock(),
    )


class FriendsReadyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        llm_client.reset_daily_quota_counters()

    async def asyncTearDown(self) -> None:
        llm_client.reset_daily_quota_counters()
        await self.db.close()

    async def test_1_fast_preset_onboarding_calm_and_full_plus_wow_card(self) -> None:
        # User 1 picks calm mode via presets only
        st1 = _MemFSM()
        tg1 = SimpleNamespace(id=950001, first_name="Макс", full_name="Макс")
        msg_start = SimpleNamespace(text="/start", from_user=tg1, answer=AsyncMock())
        await start.cmd_start(msg_start, st1, self.db)

        cb_name = _fake_cb("regname:Макс", tg_id=950001, full_name="Макс")
        await start.regname_choice(cb_name, st1)
        cb_gender = _fake_cb("reggender:male", tg_id=950001, full_name="Макс")
        await start.reggender_choice(cb_gender, st1)
        cb_mode = _fake_cb("regmode:calm", tg_id=950001, full_name="Макс")
        await start.regmode_choice(cb_mode, st1)
        cb_tz = _fake_cb("regtz:Europe/Moscow", tg_id=950001, full_name="Макс")
        await start.regtz_choice(cb_tz, st1)
        cb_wake = _fake_cb("regwake:08:00", tg_id=950001, full_name="Макс")
        await start.regwake_choice(cb_wake, st1)
        cb_sleep = _fake_cb("regsleep:23:30", tg_id=950001, full_name="Макс")
        await start.regsleep_choice(cb_sleep, st1, self.db)

        u1 = dict(await repo.get_user_by_telegram_id(self.db, 950001))
        self.assertEqual(u1["timezone"], "Europe/Moscow")
        self.assertEqual(u1["wake_up_time"], "08:00")
        self.assertEqual(u1["sleep_time"], "23:30")
        self.assertEqual(int(u1["pulse_enabled"]), 0)
        self.assertEqual(cb_sleep.message.answer.await_count, 2)
        welcome_text = cb_sleep.message.answer.await_args_list[0].args[0]
        wow_text = cb_sleep.message.answer.await_args_list[1].args[0]
        self.assertIn("⚙ Ещё → ⚙ Настройки", welcome_text)
        self.assertIn("Что можно попробовать прямо сейчас", wow_text)

        # User 2 picks full mode -> pulse_enabled == 1
        st2 = _MemFSM()
        await start.regname_choice(_fake_cb("regname:Оля", tg_id=950002, full_name="Оля"), st2)
        await start.reggender_choice(_fake_cb("reggender:female", tg_id=950002, full_name="Оля"), st2)
        await start.regmode_choice(_fake_cb("regmode:full", tg_id=950002, full_name="Оля"), st2)
        await start.regtz_choice(_fake_cb("regtz:Asia/Yekaterinburg", tg_id=950002, full_name="Оля"), st2)
        await start.regwake_choice(_fake_cb("regwake:07:30", tg_id=950002, full_name="Оля"), st2)
        await start.regsleep_choice(_fake_cb("regsleep:23:00", tg_id=950002, full_name="Оля"), st2, self.db)

        u2 = dict(await repo.get_user_by_telegram_id(self.db, 950002))
        self.assertEqual(int(u2["pulse_enabled"]), 1)

    async def test_2_help_hides_desk_when_unlinked_and_shows_when_linked(self) -> None:
        uid = await repo.create_user(self.db, 950010, "Друг", "Europe/Moscow", "08:00", "23:30")
        msg = SimpleNamespace(
            text="/help",
            from_user=SimpleNamespace(id=950010, full_name="Друг"),
            answer=AsyncMock(),
        )
        await start.cmd_help(msg, self.db)
        help_no_desk = msg.answer.await_args.args[0]
        self.assertNotIn("/desk", help_no_desk)
        self.assertIn("/feedback", help_no_desk)

        await self.db.execute(
            "INSERT INTO desk_links(user_id, desk_user_id, role, display_name, enabled, linked_at, updated_at) "
            "VALUES (?, ?, 'teacher', 'Дима', 1, '2026-11-25T00:00:00Z', '2026-11-25T00:00:00Z')",
            (uid, "desk-teacher-1"),
        )
        await self.db.commit()
        msg2 = SimpleNamespace(
            text="/help",
            from_user=SimpleNamespace(id=950010, full_name="Друг"),
            answer=AsyncMock(),
        )
        await start.cmd_help(msg2, self.db)
        self.assertIn("/desk", msg2.answer.await_args.args[0])

    async def test_3_kick_household_member_preserves_host_data_and_rotates_code(self) -> None:
        u1 = await repo.create_user(self.db, 950021, "Дима", "Europe/Moscow", "08:00", "23:30")
        u2 = await repo.create_user(self.db, 950022, "Гость", "Europe/Moscow", "08:00", "23:30")
        u3 = await repo.create_user(self.db, 950023, "Чужой", "Europe/Moscow", "08:00", "23:30")

        hid1 = await repo.get_or_create_household(self.db, u1)
        old_code = (await repo.get_household(self.db, hid1))["invite_code"]
        await repo.set_user_household(self.db, u2, hid1)
        await repo.upsert_pantry_item_merged(self.db, u1, name="Тофу", amount=200, unit="g")
        await repo.create_shopping_item(self.db, u1, "Молоко соевое", 1, "л")

        # Cannot kick user from another household
        self.assertIsNone(await repo.remove_household_member(self.db, u1, u3))

        fake_bot = SimpleNamespace(send_message=AsyncMock())
        cb_kick = _fake_cb(f"settings:household_kick_do:{u2}", tg_id=950021, full_name="Дима", bot=fake_bot)
        await settings.settings_select(cb_kick, _MemFSM(), self.db)

        u1_after = dict(await repo.get_user(self.db, u1))
        u2_after = dict(await repo.get_user(self.db, u2))
        self.assertEqual(int(u1_after["household_id"]), hid1)
        self.assertNotEqual(int(u2_after["household_id"]), hid1)

        new_house = await repo.get_household(self.db, hid1)
        self.assertNotEqual(new_house["invite_code"], old_code)
        self.assertEqual(len(await repo.list_pantry_items(self.db, u1)), 1)
        self.assertEqual(len(await repo.list_shopping_items(self.db, u1, scope="household")), 1)
        fake_bot.send_message.assert_awaited_once()
        self.assertEqual(fake_bot.send_message.call_args.args[0], 950022)

    async def test_4_delete_user_account_wipes_user_and_keeps_partner_household(self) -> None:
        u1 = await repo.create_user(self.db, 950031, "Партнёр", "Europe/Moscow", "08:00", "23:30")
        u2 = await repo.create_user(self.db, 950032, "Удаляемый", "Europe/Moscow", "08:00", "23:30")
        await repo.ensure_user_routines(self.db, u2)
        hid1 = await repo.get_or_create_household(self.db, u1)
        await repo.set_user_household(self.db, u2, hid1)
        await repo.upsert_pantry_item_merged(self.db, u1, name="Овсянка", amount=500, unit="g")
        await repo.add_weight(self.db, u2, 72.5)
        await repo.add_day_plan_item(self.db, u2, "2026-11-25", "Тестовая задача")

        cb_del = _fake_cb("settings:delete_account_confirm", tg_id=950032, full_name="Удаляемый")
        await settings.settings_select(cb_del, _MemFSM(), self.db)

        self.assertIsNone(await repo.get_user_by_telegram_id(self.db, 950032))
        self.assertIsNotNone(await repo.get_user_by_telegram_id(self.db, 950031))
        self.assertEqual(len(await repo.list_pantry_items(self.db, u1)), 1)
        weights = await self.db.execute_fetchall("SELECT * FROM weights WHERE user_id = ?", (u2,))
        self.assertEqual(len(weights), 0)
        plan_items = await self.db.execute_fetchall("SELECT * FROM day_plan_items WHERE user_id = ?", (u2,))
        self.assertEqual(len(plan_items), 0)

        # Solo user deletion also deletes solo household row
        await repo.delete_user_account(self.db, u1)
        self.assertIsNone(await repo.get_user(self.db, u1))
        self.assertIsNone(await repo.get_household(self.db, hid1))

    async def test_5_feedback_forwards_to_owner_and_cancel_clears_fsm(self) -> None:
        await repo.create_user(self.db, 950041, "Саша", "Europe/Moscow", "08:00", "23:30")
        fsm = _MemFSM()
        fake_bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())

        cb_start = _fake_cb("fb:start", tg_id=950041, full_name="Саша", bot=fake_bot)
        await feedback.feedback_start_cb(cb_start, fsm, self.db)
        self.assertEqual(await fsm.get_state(), feedback.FeedbackState.waiting_message)

        msg = SimpleNamespace(
            text="Нашла мелочь в меню",
            caption=None,
            photo=None,
            from_user=SimpleNamespace(id=950041, full_name="Саша", username="sasha"),
            bot=fake_bot,
            answer=AsyncMock(),
        )
        with patch.dict(os.environ, {"OWNER_TELEGRAM_ID": "777000"}, clear=False):
            await feedback.feedback_receive(msg, fsm, self.db)

        self.assertIsNone(await fsm.get_state())
        fake_bot.send_message.assert_awaited_once()
        self.assertEqual(fake_bot.send_message.call_args.args[0], 777000)
        forwarded = fake_bot.send_message.call_args.args[1]
        self.assertIn("950041", forwarded)
        self.assertIn("Нашла мелочь в меню", forwarded)
        self.assertIn("Передала сообщение автору", msg.answer.await_args.args[0])

        # Cancel flow
        await feedback.feedback_start_cb(cb_start, fsm, self.db)
        cb_cancel = _fake_cb("fb:cancel", tg_id=950041, full_name="Саша")
        await feedback.feedback_cancel_cb(cb_cancel, fsm, self.db)
        self.assertIsNone(await fsm.get_state())

    async def test_6_daily_llm_quota_limits_friends_and_exempts_owner(self) -> None:
        route = llm_client.LLMRoute(
            provider="openai",
            base_url="https://api.example.com/v1",
            api_key="test-key",
            model="gpt-test",
        )
        fake_ok = llm_client.LLMResult(ok=True, text="ok", provider="openai", model="gpt-test")
        client = llm_client.LLMClient()

        with (
            patch.object(llm_client.LLMClient, "_resolve_routes", return_value=(True, [route], 10.0, 200)),
            patch("llm_client._attempt_completion", AsyncMock(return_value=fake_ok)),
            patch.dict(
                os.environ,
                {"OWNER_TELEGRAM_ID": "777000", "LLM_DAILY_LIMIT_PER_USER": "2"},
                clear=False,
            ),
        ):
            tok_friend = llm_client.set_current_llm_user(950051)
            try:
                r1 = await client.complete_text("sys", "u1")
                r2 = await client.complete_text("sys", "u2")
                r3 = await client.complete_text("sys", "u3")
            finally:
                llm_client.reset_current_llm_user(tok_friend)

            self.assertTrue(r1.ok)
            self.assertTrue(r2.ok)
            self.assertFalse(r3.ok)
            self.assertEqual(r3.error, "daily_quota_exceeded")
            self.assertEqual(llm_client.get_user_daily_llm_usage(950051), 2)

            tok_owner = llm_client.set_current_llm_user(777000)
            try:
                for idx in range(4):
                    ro = await client.complete_text("sys", f"owner-{idx}")
                    self.assertTrue(ro.ok)
            finally:
                llm_client.reset_current_llm_user(tok_owner)

            # Without user context (background scheduler / system), quota does not block
            rsys = await client.complete_text("sys", "bg")
            self.assertTrue(rsys.ok)

    async def test_7_notifications_menu_pulse_and_pause_toggles(self) -> None:
        uid = await repo.create_user(self.db, 950061, "Лена", "Europe/Moscow", "08:00", "23:30")
        cb_pulse = _fake_cb("settings:notify:pulse", tg_id=950061, full_name="Лена")
        await settings.settings_select(cb_pulse, _MemFSM(), self.db)
        u_after = dict(await repo.get_user(self.db, uid))
        self.assertEqual(int(u_after["pulse_enabled"]), 0)

        cb_pause = _fake_cb("settings:notify:pause_toggle", tg_id=950061, full_name="Лена")
        await settings.settings_select(cb_pause, _MemFSM(), self.db)
        u_paused = dict(await repo.get_user(self.db, uid))
        self.assertTrue(bool(u_paused.get("pause_until")))

        await settings.settings_select(_fake_cb("settings:notify:pause_toggle", tg_id=950061, full_name="Лена"), _MemFSM(), self.db)
        u_resumed = dict(await repo.get_user(self.db, uid))
        self.assertFalse(bool(u_resumed.get("pause_until")))

    async def test_8_legacy_day_plans_morning_sent_migration_and_dedup(self) -> None:
        import datetime as dt
        from scheduler.reminder import ReminderScheduler

        legacy_db = await connect(":memory:")
        try:
            await legacy_db.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    wake_up_time TEXT NOT NULL,
                    sleep_time TEXT NOT NULL,
                    strictness TEXT DEFAULT 'neutral',
                    goals TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE day_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    plan_date TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    UNIQUE(user_id, plan_date)
                );
                """
            )
            await init_db(legacy_db)
            cols = {r["name"] for r in await legacy_db.execute_fetchall("PRAGMA table_info(day_plans);")}
            self.assertIn("morning_sent", cols)

            uid = await repo.create_user(legacy_db, 950081, "Федор", "Europe/Moscow", "08:00", "23:30")
            await repo.add_day_plan_item(legacy_db, uid, "2026-11-25", "Искупаться")
            bot = SimpleNamespace(send_message=AsyncMock())
            sched = ReminderScheduler(bot, legacy_db)
            now_utc = dt.datetime(2026, 11, 25, 5, 10, 0)  # 08:10 Europe/Moscow
            c1 = await sched._tick_day_plan(now_utc=now_utc)
            c2 = await sched._tick_day_plan(now_utc=now_utc + dt.timedelta(minutes=5))
            self.assertEqual(c1, 1)
            self.assertEqual(c2, 0)
            bot.send_message.assert_awaited_once()
            plan = dict(await repo.get_day_plan(legacy_db, uid, "2026-11-25"))
            self.assertEqual(plan["morning_sent"], "2026-11-25")
        finally:
            await legacy_db.close()


if __name__ == "__main__":
    unittest.main()
