"""Regression coverage for one-current-card Pulse check-ins."""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from db import repositories as repo
from db.database import connect, ensure_columns, init_db


class PulseCardCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hidl_pulse_cards_")
        self.db = await connect(f"sqlite:///{(Path(self.tmp.name) / 'pulse.db').as_posix()}")
        await init_db(self.db)
        self.tg_id = 987_310_111
        self.user_id = await repo.create_user(
            self.db, telegram_id=self.tg_id, name="Проверка карточек", timezone="UTC",
            wake_up_time="08:00", sleep_time="23:00",
        )
        await repo.ensure_user_routines(self.db, self.user_id)
        self.user = dict(await repo.get_user(self.db, self.user_id))
        self.now = dt.datetime(2026, 11, 20, 12, 0, 0)

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def make_checkin(
        self, slot="afternoon", *, status="pending", ping_count=1,
        snooze_count=0, card_chat_id=7001, card_message_id=101, next_ping=None,
    ):
        stamp = (next_ping or self.now - dt.timedelta(minutes=1)).isoformat()
        checkin = await repo.upsert_pulse_checkin(
            self.db, self.user_id, self.now.date().isoformat(), slot,
            "tasks", stamp, stamp, status=status,
            meta_json='{"title":"Учебный блок"}',
        )
        await repo.update_pulse_checkin(
            self.db, int(checkin["id"]), status=status, ping_count=ping_count,
            snooze_count=snooze_count, card_chat_id=card_chat_id,
            card_message_id=card_message_id,
        )
        return await repo.get_pulse_checkin_by_id(self.db, int(checkin["id"]))

    @staticmethod
    def telegram_message(chat_id: int, message_id: int):
        return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=message_id)

    async def _run_tick(self, bot, checkin, now=None):
        from services import pulse_service

        with (
            patch.object(pulse_service, "compute_daily_slots", return_value=[]),
            patch.object(repo, "get_wellness", new=AsyncMock(return_value=None)),
        ):
            return await pulse_service.process_pulse_tick(self.db, bot, now_utc=now or self.now)

    async def test_reping_and_snooze_return_replace_and_delete_previous_card(self):
        checkin = await self.make_checkin()
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[
            self.telegram_message(7001, 102),
            self.telegram_message(7001, 103),
        ])
        bot.delete_message = AsyncMock(return_value=True)
        bot.edit_message_reply_markup = AsyncMock(return_value=True)

        await self._run_tick(bot, checkin)
        after_first = await repo.get_pulse_checkin_by_id(self.db, checkin["id"])
        self.assertEqual(after_first["card_message_id"], 102)
        bot.delete_message.assert_awaited_once_with(chat_id=7001, message_id=101)

        await repo.update_pulse_checkin(
            self.db, checkin["id"], status="snoozed", ping_count=1,
            next_ping_at_utc=(self.now - dt.timedelta(minutes=1)).isoformat(),
        )
        await self._run_tick(bot, await repo.get_pulse_checkin_by_id(self.db, checkin["id"]))
        after_return = await repo.get_pulse_checkin_by_id(self.db, checkin["id"])
        self.assertEqual(after_return["card_message_id"], 103)
        self.assertEqual(bot.delete_message.await_args_list[-1].kwargs,
                         {"chat_id": 7001, "message_id": 102})

    async def test_atomic_resolutions_are_idempotent_and_preserve_terminal_status(self):
        from services import pulse_service

        for slot, first_action, repeated_actions, terminal in (
            ("afternoon", "done", ("done", "skip", "snooze"), "done"),
            ("winddown", "skip", ("skip", "done"), "skipped"),
        ):
            checkin = await self.make_checkin(slot, card_message_id=201 if slot == "afternoon" else 202)
            first = await (pulse_service.complete_checkin(self.db, self.user, checkin["id"], self.now)
                           if first_action == "done" else
                           pulse_service.skip_checkin(self.db, self.user, checkin["id"], self.now))
            self.assertTrue(first["ok"])
            karma_before = await repo.get_or_create_karma_state(self.db, self.user_id)
            events_cur = await self.db.execute("SELECT COUNT(*) AS n FROM karma_events WHERE user_id=?", (self.user_id,))
            event_count_before = (await events_cur.fetchone())["n"]
            for action in repeated_actions:
                if action == "done":
                    result = await pulse_service.complete_checkin(self.db, self.user, checkin["id"], self.now)
                elif action == "skip":
                    result = await pulse_service.skip_checkin(self.db, self.user, checkin["id"], self.now)
                else:
                    result = await pulse_service.snooze_checkin(self.db, self.user, checkin["id"], 15, self.now)
                self.assertTrue(result.get("already_resolved"))
                self.assertEqual(result["status"], terminal)
                self.assertEqual(result["karma"]["delta"], 0)
            final = await repo.get_pulse_checkin_by_id(self.db, checkin["id"])
            self.assertEqual(final["status"], terminal)
            karma_after = await repo.get_or_create_karma_state(self.db, self.user_id)
            self.assertEqual(karma_after["score"], karma_before["score"])
            events_cur = await self.db.execute("SELECT COUNT(*) AS n FROM karma_events WHERE user_id=?", (self.user_id,))
            self.assertEqual((await events_cur.fetchone())["n"], event_count_before)

    async def test_stale_handler_taps_clear_markup_without_duplicate_replies(self):
        from handlers import pulse

        checkin = await self.make_checkin("afternoon", status="done", ping_count=1)
        callback = self._callback(f"pulse:skip:{checkin['id']}", 301)
        await pulse.cb_pulse_skip(callback, self.db)
        self.assertEqual((await repo.get_pulse_checkin_by_id(self.db, checkin["id"]))["status"], "done")
        callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
        callback.message.answer.assert_not_awaited()
        self.assertIn("Карточка уже закрыта", callback.answer.await_args.args[0])

    async def test_wake_completion_keeps_its_checklist_but_stale_copy_is_cleared(self):
        from handlers import pulse

        checkin = await self.make_checkin("wake", card_message_id=701)
        current = self._callback(f"pulse:done:{checkin['id']}", 701)
        await pulse.cb_pulse_done(current, self.db)
        self.assertTrue(current.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard)
        checklist_text = current.message.edit_text.await_args.args[0]

        current.message.edit_text.reset_mock()
        await pulse.cb_pulse_done(current, self.db)
        current.message.edit_text.assert_not_awaited()
        current.message.edit_reply_markup.assert_not_awaited()
        self.assertEqual((await repo.get_pulse_checkin_by_id(self.db, checkin["id"]))["status"], "done")
        self.assertTrue(checklist_text)

        stale_copy = self._callback(f"pulse:done:{checkin['id']}", 702)
        await pulse.cb_pulse_done(stale_copy, self.db)
        stale_copy.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
        stale_copy.message.edit_text.assert_not_awaited()

    async def test_snooze_edits_in_place_and_repeated_tap_on_same_card_has_no_penalty(self):
        from handlers import pulse

        checkin = await self.make_checkin("afternoon", ping_count=1, card_message_id=801)
        callback = self._callback(f"pulse:snooze:{checkin['id']}:15", 801)
        await pulse.cb_pulse_snooze(callback, self.db)
        callback.message.edit_text.assert_awaited_once()
        self.assertIsNone(callback.message.edit_text.await_args.kwargs["reply_markup"])
        after_first = await repo.get_pulse_checkin_by_id(self.db, checkin["id"])
        self.assertEqual(after_first["status"], "snoozed")
        self.assertEqual(after_first["card_message_id"], 801)
        first_count = after_first["snooze_count"]

        callback.message.edit_text.reset_mock()
        await pulse.cb_pulse_snooze(callback, self.db)
        after_repeat = await repo.get_pulse_checkin_by_id(self.db, checkin["id"])
        self.assertEqual(after_repeat["status"], "snoozed")
        self.assertEqual(after_repeat["snooze_count"], first_count)
        callback.message.edit_text.assert_not_awaited()
        callback.message.edit_reply_markup.assert_awaited_with(reply_markup=None)
        self.assertEqual(after_repeat["card_message_id"], 801)

    async def test_resolution_survives_delete_and_markup_cleanup_failures(self):
        from handlers import pulse

        checkin = await self.make_checkin("winddown", card_message_id=901)
        callback = self._callback(f"pulse:skip:{checkin['id']}", 902)
        callback.message.bot.delete_message = AsyncMock(side_effect=RuntimeError("delete unavailable"))
        callback.message.bot.edit_message_reply_markup = AsyncMock(side_effect=RuntimeError("markup unavailable"))
        await pulse.cb_pulse_skip(callback, self.db)
        self.assertEqual((await repo.get_pulse_checkin_by_id(self.db, checkin["id"]))["status"], "skipped")
        callback.message.edit_text.assert_awaited_once()
        callback.message.bot.delete_message.assert_awaited_once()
        callback.message.bot.edit_message_reply_markup.assert_awaited_once()

    def _callback(self, data: str, message_id: int):
        message = MagicMock()
        message.chat.id = 7001
        message.message_id = message_id
        message.bot.delete_message = AsyncMock(return_value=True)
        message.bot.edit_message_reply_markup = AsyncMock(return_value=True)
        message.edit_text = AsyncMock()
        message.edit_reply_markup = AsyncMock()
        message.answer = AsyncMock()
        callback = MagicMock()
        callback.data = data
        callback.from_user.id = self.tg_id
        callback.from_user.full_name = "Проверка карточек"
        callback.message = message
        callback.answer = AsyncMock()
        return callback

    async def test_send_race_closes_only_the_newly_sent_card(self):
        from services import pulse_service

        checkin = await self.make_checkin("afternoon", card_message_id=401)
        bot = MagicMock()
        sent_message = self.telegram_message(7001, 402)

        async def close_while_send_is_in_flight(**kwargs):
            result = await pulse_service.complete_checkin(self.db, self.user, checkin["id"], self.now)
            self.assertEqual(result["checkin"]["status"], "done")
            return sent_message

        bot.send_message = AsyncMock(side_effect=close_while_send_is_in_flight)
        bot.delete_message = AsyncMock(return_value=True)
        bot.edit_message_reply_markup = AsyncMock(return_value=True)
        await self._run_tick(bot, checkin)
        row = await repo.get_pulse_checkin_by_id(self.db, checkin["id"])
        self.assertEqual(row["status"], "done")
        bot.delete_message.assert_awaited_once_with(chat_id=7001, message_id=402)
        self.assertNotIn(401, [call.kwargs.get("message_id") for call in bot.delete_message.await_args_list])

    async def test_ignored_and_manual_closure_remove_prior_active_cards(self):
        from handlers import pulse

        checkin = await self.make_checkin("afternoon", ping_count=3, card_message_id=501)
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=self.telegram_message(7001, 502))
        bot.delete_message = AsyncMock(return_value=True)
        bot.edit_message_reply_markup = AsyncMock(return_value=True)
        await self._run_tick(bot, checkin)
        self.assertEqual((await repo.get_pulse_checkin_by_id(self.db, checkin["id"]))["status"], "ignored")
        bot.delete_message.assert_awaited_once_with(chat_id=7001, message_id=501)

        local_date = dt.datetime.now(dt.timezone.utc).date().isoformat()
        for slot, callback_data, mid in (
            ("wake", "pulse:manual_wake", 511),
            ("bedtime", "pulse:manual_bedtime", 512),
        ):
            row = await repo.upsert_pulse_checkin(
                self.db, self.user_id, local_date, slot, "sleep",
                self.now.isoformat(), self.now.isoformat(),
            )
            await repo.update_pulse_checkin(
                self.db, row["id"], card_chat_id=7001, card_message_id=mid,
            )
            callback = self._callback(callback_data, mid + 1)
            if slot == "wake":
                await pulse.cb_pulse_manual_wake(callback, self.db)
            else:
                await pulse.cb_pulse_bedtime(callback, self.db)
            callback.message.bot.delete_message.assert_awaited()

    async def test_safe_delete_falls_back_to_markup_clear_and_never_raises(self):
        from utils.sender import safe_delete_or_clear_markup

        bot = MagicMock()
        bot.delete_message = AsyncMock(side_effect=RuntimeError("message is too old"))
        bot.edit_message_reply_markup = AsyncMock(return_value=True)
        self.assertTrue(await safe_delete_or_clear_markup(bot, 7001, 601))
        bot.edit_message_reply_markup.assert_awaited_once_with(
            chat_id=7001, message_id=601, reply_markup=None,
        )
        self.assertFalse(await safe_delete_or_clear_markup(bot, None, 601))

        bot.edit_message_reply_markup = AsyncMock(side_effect=RuntimeError("edit unavailable"))
        self.assertFalse(await safe_delete_or_clear_markup(bot, 7001, 601))

    async def test_safe_edit_message_not_modified_does_not_send_duplicate(self):
        from utils.sender import safe_edit

        message = MagicMock()
        message.edit_text = AsyncMock(side_effect=RuntimeError("Bad Request: message is not modified"))
        message.answer = AsyncMock()
        await safe_edit(message, "same content")
        message.answer.assert_not_awaited()

    async def test_old_pulse_schema_migrates_card_ids_idempotently(self):
        old = await connect(f"sqlite:///{(Path(self.tmp.name) / 'old.db').as_posix()}")
        try:
            await init_db(old)
            await old.execute("ALTER TABLE pulse_checkins DROP COLUMN card_chat_id")
            await old.execute("ALTER TABLE pulse_checkins DROP COLUMN card_message_id")
            await old.commit()
            await ensure_columns(old)
            await ensure_columns(old)
            cur = await old.execute("PRAGMA table_info(pulse_checkins)")
            names = {row["name"] for row in await cur.fetchall()}
            self.assertIn("card_chat_id", names)
            self.assertIn("card_message_id", names)
        finally:
            await old.close()

    async def test_pulse_service_stays_aiogram_free_at_module_import(self):
        root = Path(__file__).resolve().parents[1]
        subprocess.check_call(
            [sys.executable, "-c", "import sys, services.pulse_service; assert 'aiogram' not in sys.modules"],
            cwd=str(root),
        )

    async def test_test_ping_tracks_replacement_and_deletes_only_previous_card(self):
        from services import pulse_service

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[
            self.telegram_message(7001, 1001),
            self.telegram_message(7001, 1002),
        ])
        bot.delete_message = AsyncMock(return_value=True)
        bot.edit_message_reply_markup = AsyncMock(return_value=True)
        await pulse_service.send_test_pulse_ping(self.db, bot, self.user, now_utc=self.now)
        first = await repo.get_pulse_checkin(self.db, self.user_id, self.now.date().isoformat(), "adhoc_test")
        self.assertEqual(first["card_message_id"], 1001)
        await pulse_service.send_test_pulse_ping(self.db, bot, self.user, now_utc=self.now + dt.timedelta(minutes=1))
        second = await repo.get_pulse_checkin(self.db, self.user_id, self.now.date().isoformat(), "adhoc_test")
        self.assertEqual(second["card_message_id"], 1002)
        bot.delete_message.assert_awaited_once_with(chat_id=7001, message_id=1001)

    async def test_failed_test_ping_replacement_keeps_existing_card(self):
        from services import pulse_service

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[
            self.telegram_message(7001, 1101),
            RuntimeError("send unavailable"),
        ])
        bot.delete_message = AsyncMock(return_value=True)
        bot.edit_message_reply_markup = AsyncMock(return_value=True)
        await pulse_service.send_test_pulse_ping(self.db, bot, self.user, now_utc=self.now)
        await pulse_service.send_test_pulse_ping(self.db, bot, self.user, now_utc=self.now + dt.timedelta(minutes=1))
        row = await repo.get_pulse_checkin(self.db, self.user_id, self.now.date().isoformat(), "adhoc_test")
        self.assertEqual(row["card_message_id"], 1101)
        bot.delete_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
