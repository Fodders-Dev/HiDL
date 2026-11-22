"""Sprint 14 acceptance tests for the student profile and Desk bridge.

These tests use an isolated SQLite database and aiohttp's in-process test
server.  Telegram interactions are represented by small async mocks; no live
bot, Desk database, or external host is involved.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer

from config import get_desk_settings, get_mcp_settings
from db import repositories as repo
from db.database import connect, init_db


def _flatten_keyboard(markup) -> list[str]:
    return [button.text for row in markup.keyboard for button in row]


def _inline_texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


class Sprint14Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="hidl_sprint14_")
        db_file = Path(self._tmpdir.name) / "sprint14.db"
        self.db = await connect(f"sqlite:///{db_file.as_posix()}")
        await init_db(self.db)
        self.tg_id = 781_430_001
        self.user_id = await repo.create_user(
            self.db,
            telegram_id=self.tg_id,
            name="Тестовый пользователь",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:00",
        )
        await repo.ensure_user_routines(self.db, self.user_id)

    async def asyncTearDown(self):
        await self.db.close()
        self._tmpdir.cleanup()

    async def _user(self) -> dict:
        return dict(await repo.get_user(self.db, self.user_id))


class StudentProfileTests(Sprint14Base):
    async def test_mcp_import_does_not_load_aiogram(self):
        root = Path(__file__).resolve().parents[1]
        subprocess.check_call(
            [sys.executable, "-c", "import sys, mcp_server.app; assert 'aiogram' not in sys.modules"],
            cwd=str(root),
        )

    async def test_debug_logs_redact_single_use_desk_start_token(self):
        from aiogram.types import Chat, Message, Update, User
        from middlewares.debug_log import DebugLogMiddleware

        token = secrets.token_urlsafe(24)
        tg = User(id=self.tg_id, is_bot=False, first_name="Tester")
        message = Message(
            message_id=1, text=f"/start desk_{token}", date=dt.datetime.now(dt.timezone.utc),
            chat=Chat(id=self.tg_id, type="private"), from_user=tg,
        )
        logged = []
        async def handler(event, data):
            return None
        with patch("middlewares.debug_log.log_debug", side_effect=logged.append):
            await DebugLogMiddleware()(handler, Update(update_id=1, message=message), {})
        logs = "\n".join(logged)
        self.assertNotIn(token, logs)
        self.assertIn("[REDACTED]", logs)

    async def test_student_menu_and_today_hide_adult_household(self):
        from keyboards.common import main_menu_keyboard
        from utils.today import render_today

        await self.db.execute("UPDATE users SET profile_mode='student' WHERE id=?", (self.user_id,))
        await self.db.commit()
        user = await self._user()
        labels = _flatten_keyboard(main_menu_keyboard(user))
        self.assertEqual(
            labels,
            ["📅 Сегодня", "📚 Учёба / Desk", "🔔 Напоминания", "☕ Кафе фокуса", "😴 Сон и пауза", "⚙ Настройки"],
        )

        text, markup = await render_today(self.db, user)
        for adult_label in ("Регулярка по дому", "Финансы/Счета", "Продукты/Холодильник", "Меню дня", "Таблетки"):
            self.assertNotIn(adult_label, text)
        buttons = _inline_texts(markup)
        for adult_button in ("🧹 План по дому", "Финансы", "Таблетки", "Продукты"):
            self.assertNotIn(adult_button, buttons)
        self.assertTrue(any("План дня" in line for line in text.splitlines()))

    async def test_adult_default_and_explicit_keyboard_remain_compatible(self):
        from keyboards.common import main_menu_keyboard

        await self.db.execute("UPDATE users SET profile_mode='adult' WHERE id=?", (self.user_id,))
        await self.db.commit()
        adult = _flatten_keyboard(main_menu_keyboard(await self._user()))
        legacy = _flatten_keyboard(main_menu_keyboard())
        self.assertEqual(adult, legacy)
        self.assertIn("🍽 Еда и покупки", legacy)

    async def test_profile_mode_repository_accepts_only_supported_modes(self):
        self.assertEqual((await self._user()).get("profile_mode", "adult"), "adult")
        self.assertTrue(hasattr(repo, "update_user_profile_mode"))
        await repo.update_user_profile_mode(self.db, self.user_id, "student")
        self.assertEqual((await self._user())["profile_mode"], "student")
        await repo.update_user_profile_mode(self.db, self.user_id, "adult")
        self.assertEqual((await self._user())["profile_mode"], "adult")
        with self.assertRaises((ValueError, TypeError)):
            await repo.update_user_profile_mode(self.db, self.user_id, "other")

    async def test_student_settings_switch_is_one_tap_and_refreshes_menu(self):
        from handlers.settings import settings_select, settings_keyboard
        from keyboards.common import main_menu_keyboard

        await repo.update_user_profile_mode(self.db, self.user_id, "student")
        kb = settings_keyboard(await self._user())
        self.assertTrue(any("🎓 Режим" in label for label in _inline_texts(kb)))
        callback = MagicMock()
        callback.data = "settings:profile_mode:adult"
        callback.from_user.id = self.tg_id
        callback.from_user.full_name = "Тестовый пользователь"
        callback.message = MagicMock()
        callback.message.edit_text = AsyncMock()
        callback.message.answer = AsyncMock()
        callback.answer = AsyncMock()
        state = MagicMock()
        state.clear = AsyncMock()
        await settings_select(callback, state, self.db)
        self.assertEqual((await self._user())["profile_mode"], "adult")
        self.assertNotEqual(_flatten_keyboard(main_menu_keyboard(await self._user())), [])

    async def test_real_dispatcher_student_guards_and_adult_command_compatibility(self):
        from aiogram.types import Chat, Message, Update, User
        from tools.batch_test import BatchTestBot, BatchTestRenderer
        from hidl.app import create_app

        await repo.update_user_profile_mode(self.db, self.user_id, "student")
        db_file_cur = await self.db.execute("PRAGMA database_list")
        db_path = next(row["file"] for row in await db_file_cur.fetchall() if row["name"] == "main")
        database_url = f"sqlite:///{Path(db_path).as_posix()}"
        app = None
        with patch.dict(os.environ, {"HIDL_SIM_DB_URL": database_url}):
            app = await create_app(test_mode=True)
        capture = BatchTestRenderer()
        proxy = BatchTestBot(app, capture)
        tg = User(id=self.tg_id, is_bot=False, first_name="Tester")
        try:
            from services import desk_service
            with patch.dict(os.environ, {"BOT_USERNAME": "sprint14_test_bot"}):
                link = await desk_service.create_link_token(
                    app.db_conn, desk_user_id="dispatcher-student", role="student",
                    display_name="Новый ученик", timezone="Europe/Moscow",
                )
            linked_tg = User(id=self.tg_id + 2, is_bot=False, first_name="New")
            start_message = Message(
                message_id=100, text=f"/start desk_{link['token']}", date=dt.datetime.now(dt.timezone.utc),
                chat=Chat(id=linked_tg.id, type="private"), from_user=linked_tg,
            )
            await app.dp.feed_update(proxy, Update(update_id=100, message=start_message))
            linked_user = await repo.get_user_by_telegram_id(app.db_conn, linked_tg.id)
            self.assertIsNotNone(linked_user)
            self.assertEqual(linked_user["profile_mode"], "student")
            self.assertEqual(linked_user["wake_up_time"], "08:00")
            self.assertIn("Desk", capture.last_response)
            desk_message = Message(
                message_id=101, text="/desk", date=dt.datetime.now(dt.timezone.utc),
                chat=Chat(id=linked_tg.id, type="private"), from_user=linked_tg,
            )
            await app.dp.feed_update(proxy, Update(update_id=101, message=desk_message))
            self.assertIn("Учёба / Desk", capture.last_response)

            student_buttons = {
                "[R] 📅 Сегодня", "[R] 📚 Учёба / Desk", "[R] 🔔 Напоминания",
                "[R] ☕ Кафе фокуса", "[R] 😴 Сон и пауза", "[R] ⚙ Настройки",
            }

            async def send_private(text: str, update_id: int) -> None:
                update_message = Message(
                    message_id=update_id, text=text, date=dt.datetime.now(dt.timezone.utc),
                    chat=Chat(id=linked_tg.id, type="private"), from_user=linked_tg,
                )
                await app.dp.feed_update(proxy, Update(update_id=update_id, message=update_message))

            for index, action in enumerate(("☕ Кафе фокуса", "/add_reminder"), start=110):
                before = capture.last_response
                await send_private(action, index)
                prompt = capture.last_response
                self.assertTrue(prompt and prompt != before, f"{action} did not open a prompt")
                await send_private("отмена", index + 1)
                self.assertTrue(student_buttons.issubset(set(capture.last_buttons)), action)

            before_plan = capture.last_response
            await send_private("/plan_tomorrow", 115)
            plan_prompt = capture.last_response
            self.assertTrue(plan_prompt and plan_prompt != before_plan)
            await send_private("Разобрать конспект", 116)
            await send_private("нет", 117)
            self.assertTrue(student_buttons.issubset(set(capture.last_buttons)), "/plan_tomorrow")
            self.assertNotIn("по дому", capture.last_response.lower())

            await send_private("😴 Сон и пауза", 120)
            self.assertTrue(any("Пауза на сегодня" in button for button in capture.last_buttons))

            for idx, command in enumerate(("/budget", "/pantry", "/home", "/meds", "/ask_mom"), start=1):
                message = Message(
                    message_id=idx, text=command, date=dt.datetime.now(dt.timezone.utc),
                    chat=Chat(id=self.tg_id, type="private"), from_user=tg,
                )
                await app.dp.feed_update(proxy, Update(update_id=idx, message=message))
                self.assertIn("режиме «Ученик»", capture.last_response, command)
                self.assertTrue(any("Настройки" in button for button in capture.last_buttons), command)

            await repo.update_user_profile_mode(app.db_conn, self.user_id, "adult")
            message = Message(
                message_id=10, text="/budget", date=dt.datetime.now(dt.timezone.utc),
                chat=Chat(id=self.tg_id, type="private"), from_user=tg,
            )
            await app.dp.feed_update(proxy, Update(update_id=10, message=message))
            self.assertNotIn("режиме «Ученик»", capture.last_response)
        finally:
            await app.bot.session.close()
            await app.db_conn.close()

    async def test_student_natural_message_is_left_for_inbox(self):
        from handlers import natural

        await repo.update_user_profile_mode(self.db, self.user_id, "student")
        message = MagicMock()
        message.text = "купил молоко"
        message.from_user.id = self.tg_id
        message.from_user.full_name = "Тестовый пользователь"
        message.answer = AsyncMock()
        state = MagicMock()
        state.get_state = AsyncMock(return_value=None)
        with patch("handlers.natural.handle_free_text_inbox", new=AsyncMock()) as inbox, \
             patch("handlers.natural.repo.create_pantry_item", new=AsyncMock()) as pantry, \
             patch("handlers.natural.repo.add_expense", new=AsyncMock()) as expense:
            await natural.natural_handler(message, self.db, state)
            self.assertEqual(inbox.await_count, 1)
            pantry.assert_not_awaited()
            expense.assert_not_awaited()
            message.text = "потратил 500 рублей"
            await natural.natural_handler(message, self.db, state)
            self.assertEqual(inbox.await_count, 2)
            pantry.assert_not_awaited()
            expense.assert_not_awaited()

    async def test_student_tone_and_background_ticks_skip_household_prompts(self):
        from services import karma_service, pulse_service
        from scheduler.reminder import ReminderScheduler

        await repo.update_user_profile_mode(self.db, self.user_id, "student")
        user = await self._user()
        self.assertEqual(karma_service.get_karma_mode(0, user)["code"], "care")
        pulse_text, _ = pulse_service.build_pulse_message(
            {"id": 1, "slot_key": "wake", "domain": "sleep", "metadata_json": "{}"},
            {"score": 0, "last_event_summary": ""}, level=3, user=user,
        )
        self.assertNotIn("alarm", pulse_text.lower())
        self.assertNotIn("ТРЕТИЙ СИГНАЛ", pulse_text)
        scheduler = ReminderScheduler(MagicMock(), self.db)
        for name in ("_tick_bills", "_tick_weekly_finance", "_tick_care", "_tick_weight_prompt", "_weekly_home_plan", "_tick_meds"):
            method = getattr(scheduler, name)
            scheduler.bot.send_message = AsyncMock()
            with patch("scheduler.reminder.repo.list_users", new=AsyncMock(return_value=[user])):
                await method()
            scheduler.bot.send_message.assert_not_awaited()


class DeskHttpTests(Sprint14Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.secret = secrets.token_urlsafe(32)
        self.env = patch.dict(os.environ, {
            "DESK_WEBHOOK_SECRET": self.secret,
            "BOT_USERNAME": "generated_test_bot",
            "DESK_PUBLIC_URL": "",
            "DESK_LINK_TOKEN_TTL_SEC": "900",
            "DESK_SUBMIT_BATCH_SEC": "600",
            "MCP_HOST": "",
            "MCP_PORT": "",
            "MCP_PUBLIC_BASE_URL": "",
            "MCP_OWNER_TELEGRAM_ID": str(self.tg_id),
            "MCP_ACCESS_PASSWORD": secrets.token_urlsafe(18),
            "MCP_BEARER_TOKEN": secrets.token_urlsafe(24),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.settings = get_desk_settings()
        from mcp_server.app import create_mcp_app

        mcp_cfg = get_mcp_settings()
        self.client = TestClient(TestServer(create_mcp_app(self.db, mcp_cfg)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await super().asyncTearDown()

    def _signed(self, body: bytes, timestamp: int | None = None) -> dict[str, str]:
        stamp = str(timestamp if timestamp is not None else int(dt.datetime.now(dt.timezone.utc).timestamp()))
        digest = hmac.new(self.secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
        return {"X-Desk-Timestamp": stamp, "X-Desk-Signature": f"sha256={digest}"}

    async def _post_signed(self, path: str, payload: dict, *, headers=None):
        body = json.dumps(payload, separators=(",", ":")).encode()
        hdrs = self._signed(body)
        if headers:
            hdrs.update(headers)
        return await self.client.post(path, data=body, headers={**hdrs, "Content-Type": "application/json"})

    async def _link(self, desk_user_id: str, role: str, telegram_id: int | None = None) -> None:
        from services import desk_service

        issued = await desk_service.create_link_token(
            self.db, desk_user_id=desk_user_id, role=role,
            display_name="Связанный пользователь", timezone="Europe/Moscow",
        )
        result = await desk_service.consume_link_token(self.db, issued["token"], telegram_id or self.tg_id)
        self.assertEqual(result["status"], "linked")

    async def test_link_token_hmac_validation_and_link_lookup_delete(self):
        payload = {"deskUserId": "desk-user-student", "role": "student", "displayName": "Ученик", "timezone": "Europe/Moscow"}
        body = json.dumps(payload, separators=(",", ":")).encode()
        bad = await self.client.post("/integrations/desk/link-tokens", data=body, headers={
            "Content-Type": "application/json", "X-Desk-Timestamp": str(int(dt.datetime.now().timestamp())),
            "X-Desk-Signature": "sha256=" + secrets.token_hex(32),
        })
        self.assertEqual(bad.status, 401)
        expired = await self.client.post("/integrations/desk/link-tokens", data=body, headers=self._signed(body, int(dt.datetime.now().timestamp()) - 301))
        self.assertEqual(expired.status, 401)
        future = await self.client.post("/integrations/desk/link-tokens", data=body, headers=self._signed(body, int(dt.datetime.now().timestamp()) + 301))
        self.assertEqual(future.status, 401)

        response = await self._post_signed("/integrations/desk/link-tokens", payload)
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertTrue(result["ok"])
        token = result["token"]
        expected_prefix = f"{self.settings.telegram_link_base_url.rstrip('/')}/{self.settings.bot_username}?start=desk_"
        self.assertTrue(result["deepLink"].startswith(expected_prefix))
        self.assertIn(token, result["deepLink"])

        # Consume through the same bot start handler that receives Telegram's
        # /start deep link, then verify one-time consumption and link APIs.
        from handlers import desk

        message = MagicMock()
        message.from_user.id = self.tg_id
        message.from_user.first_name = "Test"
        message.text = f"/start desk_{token}"
        message.answer = AsyncMock()
        state = MagicMock()
        state.clear = AsyncMock()
        await desk.cmd_desk_start(message, state, self.db)
        self.assertIn("Ученик", str(message.answer.await_args_list))
        self.assertEqual((await self._user())["profile_mode"], "adult")
        offered_markup = message.answer.await_args.kwargs["reply_markup"]
        offered_data = [button.callback_data for row in offered_markup.inline_keyboard for button in row]
        self.assertIn("settings:profile_mode:student", offered_data)
        self.assertIn("desk:keep_adult", offered_data)

        link_get = await self.client.get("/integrations/desk/links/desk-user-student", headers=self._signed(b""))
        self.assertEqual(link_get.status, 200)
        self.assertTrue((await link_get.json())["linked"])
        await desk.cmd_desk_start(message, state, self.db)
        self.assertIn("использована", message.answer.await_args.args[0])

        delete = await self.client.delete("/integrations/desk/links/desk-user-student", headers=self._signed(b""))
        self.assertEqual(delete.status, 200)
        self.assertTrue((await delete.json())["unlinked"])
        after = await self.client.get("/integrations/desk/links/desk-user-student", headers=self._signed(b""))
        self.assertFalse((await after.json())["linked"])

    async def test_webhook_dedupes_and_updates_desk_plan_schedule(self):
        await repo.update_user_profile_mode(self.db, self.user_id, "student")
        await self._link("desk-student", "student")
        lesson = {
            "schemaVersion": 1, "eventId": "evt-lesson-s14", "type": "lesson.reminder_15m",
            "occurredAt": "2026-11-20T13:45:00Z", "recipient": {"deskUserId": "desk-student", "role": "student"},
            "data": {"lessonId": "lesson-physics", "title": "Физика", "subject": "Физика", "startsAt": "2026-11-20T14:00:00Z", "endsAt": "2026-11-20T14:45:00Z", "url": ""},
        }
        first = await self._post_signed("/integrations/desk/webhook", lesson, headers={"X-Desk-Event-Id": lesson["eventId"]})
        self.assertEqual(first.status, 200)
        dup = await self._post_signed("/integrations/desk/webhook", lesson, headers={"X-Desk-Event-Id": lesson["eventId"]})
        self.assertEqual(dup.status, 200)
        self.assertTrue((await dup.json())["duplicate"])

        cur = await self.db.execute("SELECT * FROM schedule_events WHERE external_key=?", ("desk:lesson:lesson-physics",))
        self.assertIsNotNone(await cur.fetchone())
        cur = await self.db.execute("SELECT * FROM day_plan_items WHERE external_key=?", ("desk:lesson:lesson-physics",))
        self.assertIsNotNone(await cur.fetchone())
        cur = await self.db.execute("SELECT COUNT(*) AS n FROM desk_events WHERE event_id=?", (lesson["eventId"],))
        self.assertEqual((await cur.fetchone())["n"], 1)
        cur = await self.db.execute("SELECT COUNT(*) AS n FROM desk_outbox WHERE event_id=?", (lesson["eventId"],))
        self.assertEqual((await cur.fetchone())["n"], 1)

        homework = {
            "schemaVersion": 1, "eventId": "evt-homework-s14", "type": "homework.assigned",
            "occurredAt": "2026-11-20T13:45:00Z", "recipient": {"deskUserId": "desk-student", "role": "student"},
            "data": {"homeworkId": "hw-history", "title": "Прочитать главу", "subject": "История", "dueAt": "2026-11-22T15:00:00Z", "url": ""},
        }
        hw_resp = await self._post_signed("/integrations/desk/webhook", homework, headers={"X-Desk-Event-Id": homework["eventId"]})
        self.assertEqual(hw_resp.status, 200)
        cur = await self.db.execute("SELECT * FROM day_plan_items WHERE external_key=?", ("desk:hw:hw-history",))
        self.assertIsNotNone(await cur.fetchone())

    async def test_homework_assigned_optional_due_date_is_notification_only_and_idempotent(self):
        """Missing/null dueAt is allowed for assigned events without inventing a date or schedule item."""
        await self._link("desk-student", "student")

        async def post_assignment(event_id: str, homework_id: str, *, include_due: bool, due_at=None):
            data = {"homeworkId": homework_id, "title": f"Assignment {homework_id}", "subject": "Math", "url": ""}
            if include_due:
                data["dueAt"] = due_at
            payload = {
                "schemaVersion": 1,
                "eventId": event_id,
                "type": "homework.assigned",
                "occurredAt": "2026-11-20T13:45:00Z",
                "recipient": {"deskUserId": "desk-student", "role": "student"},
                "data": data,
            }
            first = await self._post_signed(
                "/integrations/desk/webhook", payload,
                headers={"X-Desk-Event-Id": event_id},
            )
            self.assertEqual(first.status, 200, await first.text())
            duplicate = await self._post_signed(
                "/integrations/desk/webhook", payload,
                headers={"X-Desk-Event-Id": event_id},
            )
            self.assertEqual(duplicate.status, 200)
            self.assertTrue((await duplicate.json())["duplicate"])
            cur = await self.db.execute("SELECT COUNT(*) AS n FROM desk_events WHERE event_id=?", (event_id,))
            self.assertEqual((await cur.fetchone())["n"], 1)
            cur = await self.db.execute("SELECT COUNT(*) AS n FROM desk_outbox WHERE event_id=? AND event_type='homework.assigned'", (event_id,))
            self.assertEqual((await cur.fetchone())["n"], 1)

        for event_id, homework_id, include_due, due_at in (
            ("evt-hw-no-date", "hw-no-date", False, None),
            ("evt-hw-null-date", "hw-null-date", True, None),
        ):
            await post_assignment(event_id, homework_id, include_due=include_due, due_at=due_at)
            external_key = f"desk:hw:{homework_id}"
            cur = await self.db.execute("SELECT COUNT(*) AS n FROM day_plan_items WHERE external_key=?", (external_key,))
            self.assertEqual((await cur.fetchone())["n"], 0)
            cur = await self.db.execute("SELECT COUNT(*) AS n FROM schedule_events WHERE external_key=?", (external_key,))
            self.assertEqual((await cur.fetchone())["n"], 0)

        cur = await self.db.execute("SELECT COUNT(*) AS n FROM day_plans WHERE user_id=?", (self.user_id,))
        self.assertEqual((await cur.fetchone())["n"], 0)
        cur = await self.db.execute("SELECT COUNT(*) AS n FROM schedule_events WHERE user_id=? AND source='desk'", (self.user_id,))
        self.assertEqual((await cur.fetchone())["n"], 0)

        # A real due time is converted to the recipient's local calendar date.
        await post_assignment("evt-hw-dated", "hw-dated", include_due=True, due_at="2026-11-20T22:30:00Z")
        cur = await self.db.execute(
            "SELECT d.plan_date,COUNT(*) AS n FROM day_plan_items p "
            "JOIN day_plans d ON d.id=p.plan_id WHERE p.external_key=? GROUP BY d.plan_date",
            ("desk:hw:hw-dated",),
        )
        dated = await cur.fetchone()
        self.assertIsNotNone(dated)
        self.assertEqual(dated["plan_date"], "2026-11-21")
        self.assertEqual(dated["n"], 1)

        for suffix, include_due, due_at in (("missing", False, None), ("null", True, None)):
            event_id = f"evt-hw-due24-{suffix}"
            data = {"homeworkId": f"hw-due24-{suffix}", "title": "Due soon"}
            if include_due:
                data["dueAt"] = due_at
            payload = {
                "schemaVersion": 1, "eventId": event_id, "type": "homework.due_24h",
                "occurredAt": "2026-11-20T13:45:00Z",
                "recipient": {"deskUserId": "desk-student", "role": "student"}, "data": data,
            }
            response = await self._post_signed(
                "/integrations/desk/webhook", payload,
                headers={"X-Desk-Event-Id": event_id},
            )
            self.assertEqual(response.status, 400)
            cur = await self.db.execute("SELECT COUNT(*) AS n FROM desk_events WHERE event_id=?", (event_id,))
            self.assertEqual((await cur.fetchone())["n"], 0)
            cur = await self.db.execute("SELECT COUNT(*) AS n FROM desk_outbox WHERE event_id=?", (event_id,))
            self.assertEqual((await cur.fetchone())["n"], 0)

    async def test_graded_homework_closes_existing_item_and_unlinked_is_404(self):
        await self._link("desk-student", "student")
        cur = await self.db.execute("INSERT INTO day_plans(user_id,plan_date,created_at,updated_at) VALUES(?,?,datetime('now'),datetime('now'))", (self.user_id, "2026-11-22"))
        plan_id = cur.lastrowid
        await self.db.execute("INSERT INTO day_plan_items(plan_id,user_id,title,category,external_key,created_at,updated_at) VALUES(?,?,?,'desk',?,datetime('now'),datetime('now'))", (plan_id, self.user_id, "Контрольная", "desk:hw:hw-graded"))
        await self.db.commit()
        event = {"schemaVersion": 1, "eventId": "evt-graded-s14", "type": "homework.graded", "occurredAt": "2026-11-20T13:45:00Z", "recipient": {"deskUserId": "desk-student", "role": "student"}, "data": {"homeworkId": "hw-graded", "title": "Контрольная", "grade": "5"}}
        response = await self._post_signed("/integrations/desk/webhook", event, headers={"X-Desk-Event-Id": event["eventId"]})
        self.assertEqual(response.status, 200)
        cur = await self.db.execute("SELECT done FROM day_plan_items WHERE external_key=?", ("desk:hw:hw-graded",))
        self.assertEqual((await cur.fetchone())["done"], 1)

        event["eventId"] = "evt-unlinked-s14"
        event["recipient"]["deskUserId"] = "missing-desk-user"
        response = await self._post_signed("/integrations/desk/webhook", event, headers={"X-Desk-Event-Id": event["eventId"]})
        self.assertEqual(response.status, 404)

    async def _enqueue_for_scheduler(self, *, event_id: str, event_type: str, priority: str,
                                     text: str, due: str, batch_key: str = "", user_id: int | None = None) -> int:
        cur = await self.db.execute(
            "INSERT INTO desk_outbox(user_id,event_id,event_type,priority,batch_key,text,url,button_label,"
            "deliver_after_utc,status,attempts,last_error,created_at) VALUES(?,?,?,?,?,?,?, ?,?,'pending',0,'',?)",
            (user_id or self.user_id, event_id, event_type, priority, batch_key, text, "", "", due, due),
        )
        await self.db.commit()
        return int(cur.lastrowid)

    async def test_outbox_urgent_pause_sleep_and_teacher_batch_delivery(self):
        from scheduler.reminder import ReminderScheduler

        await self._link("desk-student", "student")
        fake_now = dt.datetime(2026, 11, 20, 1, 0, 0)
        due = "2026-11-20T00:00:00Z"
        await self.db.execute(
            "UPDATE users SET pause_until='2026-11-20',sleep_mode_enabled=1,sleep_target_sleep='22:00',"
            "sleep_target_wake='08:00',wake_up_time='08:00',timezone='Europe/Moscow' WHERE id=?",
            (self.user_id,),
        )
        await self.db.commit()
        urgent_id = await self._enqueue_for_scheduler(
            event_id="out-urgent-s14", event_type="lesson.reminder_15m", priority="urgent",
            text="Занятие через 15 минут", due=due,
        )
        pause_id = await self._enqueue_for_scheduler(
            event_id="out-pause-s14", event_type="homework.assigned", priority="normal",
            text="Новое задание", due=due,
        )
        bot = MagicMock()
        bot.send_message = AsyncMock()
        scheduler = ReminderScheduler(bot, self.db)
        sent = await scheduler._tick_desk_outbox(fake_now)
        self.assertEqual(sent, 1)
        self.assertEqual(bot.send_message.await_count, 1)
        cur = await self.db.execute("SELECT status FROM desk_outbox WHERE id=?", (urgent_id,))
        self.assertEqual((await cur.fetchone())["status"], "sent")
        cur = await self.db.execute("SELECT status FROM desk_outbox WHERE id=?", (pause_id,))
        self.assertEqual((await cur.fetchone())["status"], "suppressed_pause")

        # With pause cleared, normal homework is moved to wake + five minutes
        # while the student's sleep window remains active.
        await self.db.execute("UPDATE users SET pause_until=NULL WHERE id=?", (self.user_id,))
        await self.db.execute("UPDATE desk_outbox SET status='pending',deliver_after_utc=? WHERE id=?", (due, pause_id))
        await self.db.commit()
        bot.send_message.reset_mock()
        self.assertEqual(await scheduler._tick_desk_outbox(fake_now), 0)
        self.assertEqual(bot.send_message.await_count, 0)
        cur = await self.db.execute("SELECT status,deliver_after_utc FROM desk_outbox WHERE id=?", (pause_id,))
        deferred = await cur.fetchone()
        self.assertEqual(deferred["status"], "pending")
        self.assertEqual(deferred["deliver_after_utc"], "2026-11-20T05:05:00Z")

        # Teacher submissions sharing a batch key and deadline become one send.
        await self.db.execute("UPDATE users SET sleep_mode_enabled=0 WHERE id=?", (self.user_id,))
        await self.db.commit()
        teacher_tg_id = self.tg_id + 1
        teacher_uid = await repo.create_user(
            self.db, telegram_id=teacher_tg_id, name="Учитель", timezone="Europe/Moscow",
            wake_up_time="08:00", sleep_time="23:00",
        )
        await self._link("desk-teacher", "teacher", teacher_tg_id)
        from services import desk_service
        batch_start = dt.datetime(2026, 11, 20, 0, 0, tzinfo=dt.timezone.utc)
        for suffix, student in (("a", "StudentA"), ("b", "StudentB")):
            payload = {
                "schemaVersion": 1, "eventId": f"evt-submit-{suffix}", "type": "homework.submitted",
                "occurredAt": (batch_start + dt.timedelta(minutes=3 if suffix == "b" else 0)).isoformat().replace("+00:00", "Z"),
                "recipient": {"deskUserId": "desk-teacher", "role": "teacher"},
                "data": {"homeworkId": f"hw-{suffix}", "studentName": student, "title": f"Submission {suffix.upper()}", "url": ""},
            }
            await desk_service.ingest_event(
                self.db, payload,
                now=batch_start + dt.timedelta(minutes=3 if suffix == "b" else 0),
            )
        cur = await self.db.execute(
            "SELECT COUNT(DISTINCT deliver_after_utc) AS deadlines,MIN(deliver_after_utc) AS deadline "
            "FROM desk_outbox WHERE batch_key=?", (f"teacher_submit:{teacher_uid}",),
        )
        batch_info = await cur.fetchone()
        self.assertEqual(batch_info["deadlines"], 1)
        await self.db.execute(
            "UPDATE desk_outbox SET deliver_after_utc=? WHERE batch_key=?",
            ("2026-11-20T00:00:00Z", f"teacher_submit:{teacher_uid}"),
        )
        await self.db.commit()
        bot.send_message.reset_mock()
        count = await scheduler._tick_desk_outbox(fake_now)
        self.assertEqual(count, 2)
        self.assertEqual(bot.send_message.await_count, 1)
        delivered_text = bot.send_message.await_args.kwargs["text"]
        self.assertIn("Submission A", delivered_text)
        self.assertIn("Submission B", delivered_text)


if __name__ == "__main__":
    unittest.main()
