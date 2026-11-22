"""Calendar pre-alert message persistence and post-event cleanup tests."""

from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from db import repositories as repo
from db.database import connect, init_db
from services import calendar_service


class CalendarAlertCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hidl_calendar_alerts_")
        self.db = await connect(f"sqlite:///{(Path(self.tmp.name) / 'calendar.db').as_posix()}")
        await init_db(self.db)
        self.tg_id = 640_512_003
        self.user_id = await repo.create_user(
            self.db, telegram_id=self.tg_id, name="Календарный тест", timezone="Asia/Tokyo",
            wake_up_time="08:00", sleep_time="23:00",
        )
        self.user = dict(await repo.get_user(self.db, self.user_id))
        self.now = dt.datetime(2026, 11, 20, 13, 45, 0)  # 22:45 in Asia/Tokyo
        self.event = {
            "external_uid": "meeting-overnight-local",
            "event_date": "2026-11-20",
            "start_time": "23:00",
            "end_time": "23:55",
            "is_all_day": 0,
            "title": "Закрывающая встреча",
            "location": "",
            "description": "",
            "is_busy": 1,
        }
        self.bot = MagicMock()
        self.bot.send_message = AsyncMock(return_value=SimpleNamespace(
            chat=SimpleNamespace(id=self.tg_id), message_id=9001,
        ))
        self.bot.delete_message = AsyncMock(return_value=True)
        self.bot.edit_message_reply_markup = AsyncMock(return_value=True)

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    async def _event_row(self, event=None):
        return await repo.upsert_single_calendar_event(
            self.db, self.user_id, "ics", event or self.event,
        )

    async def test_alert_send_persists_message_and_live_meeting_is_not_deleted(self):
        row = await self._event_row()
        sent = await calendar_service.process_calendar_alerts_tick(self.db, self.bot, now_utc=self.now)
        self.assertEqual(sent, 1)
        saved = await self._event_row()
        self.assertEqual(saved["pre_alert_sent"], 1)
        self.assertEqual(saved["alert_chat_id"], self.tg_id)
        self.assertEqual(saved["alert_message_id"], 9001)

        # At 23:30 local the meeting is still in progress, so its card remains.
        during = await calendar_service.process_calendar_alerts_tick(
            self.db, self.bot, now_utc=dt.datetime(2026, 11, 20, 14, 30, 0),
        )
        self.assertEqual(during, 0)
        self.bot.delete_message.assert_not_awaited()
        self.assertEqual((await self._event_row())["alert_message_id"], 9001)

    async def test_midnight_cleanup_ignores_local_date_pause_and_quiet_and_survives_sync(self):
        await self._event_row()
        await calendar_service.process_calendar_alerts_tick(self.db, self.bot, now_utc=self.now)

        # During the meeting, both replacement paths must retain alert identity.
        changed = {**self.event, "title": "Обновлённое название"}
        inserted = await repo.replace_calendar_events_window(
            self.db, self.user_id, "ics", "2026-11-20", "2026-11-20", [changed],
        )
        self.assertEqual(inserted, 1)
        row = await self._event_row(changed)
        self.assertEqual(row["alert_chat_id"], self.tg_id)
        self.assertEqual(row["alert_message_id"], 9001)

        updated = await repo.upsert_single_calendar_event(
            self.db, self.user_id, "ics", {**changed, "title": "Синхронизировано повторно"},
        )
        self.assertEqual(updated["alert_chat_id"], self.tg_id)
        self.assertEqual(updated["alert_message_id"], 9001)

        # Event ends at 23:55 Tokyo time. At 00:05 next local day, quiet and
        # pause are active; cleanup still runs and does not send another alert.
        await self.db.execute(
            "UPDATE users SET quiet_mode=1,pause_until='2026-11-21' WHERE id=?", (self.user_id,),
        )
        await self.db.commit()
        after_midnight_utc = dt.datetime(2026, 11, 20, 15, 5, 0)
        await calendar_service.process_calendar_alerts_tick(self.db, self.bot, now_utc=after_midnight_utc)

        self.bot.delete_message.assert_awaited_once_with(chat_id=self.tg_id, message_id=9001)
        self.assertEqual(self.bot.send_message.await_count, 1)
        cleaned = await self._event_row(changed)
        self.assertEqual(cleaned["pre_alert_sent"], 1)
        self.assertIsNone(cleaned["alert_chat_id"])
        self.assertIsNone(cleaned["alert_message_id"])

        # Cleared identity prevents an endless cleanup retry on future ticks.
        await calendar_service.process_calendar_alerts_tick(
            self.db, self.bot, now_utc=after_midnight_utc + dt.timedelta(minutes=1),
        )
        self.bot.delete_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_real_cross_midnight_end_uses_alert_end_utc_not_display_2359(self):
        event = {
            **self.event,
            "external_uid": "actual-cross-midnight",
            "start_time": "23:30",
            # Calendar display normalizes a cross-date event to this day's end.
            "end_time": "23:59",
        }
        row = await self._event_row(event)
        # The provider's real end is 00:30 next day in Asia/Tokyo (15:30 UTC).
        await self.db.execute(
            "UPDATE calendar_events SET alert_end_utc=? WHERE id=?",
            ("2026-11-20T15:30:00Z", row["id"]),
        )
        await repo.mark_calendar_event_alert_sent(
            self.db, int(row["id"]), chat_id=self.tg_id, message_id=9051,
        )

        # 00:01 local next day is after the normalized display end but before
        # the real provider end, so the card must remain.
        await calendar_service.process_calendar_alerts_tick(
            self.db, self.bot, now_utc=dt.datetime(2026, 11, 20, 15, 1, 0),
        )
        self.bot.delete_message.assert_not_awaited()
        active = await repo.list_calendar_events_with_active_alert(self.db)
        self.assertEqual(active[0]["alert_message_id"], 9051)

        await calendar_service.process_calendar_alerts_tick(
            self.db, self.bot, now_utc=dt.datetime(2026, 11, 20, 15, 30, 0),
        )
        self.bot.delete_message.assert_awaited_once_with(chat_id=self.tg_id, message_id=9051)

    async def test_delete_failure_falls_back_and_clears_identity_once(self):
        event = {**self.event, "external_uid": "expired-with-delete-error"}
        row = await self._event_row(event)
        await repo.mark_calendar_event_alert_sent(
            self.db, int(row["id"]), chat_id=self.tg_id, message_id=9101,
        )
        self.bot.delete_message = AsyncMock(side_effect=RuntimeError("message is older than 48 hours"))
        self.bot.edit_message_reply_markup = AsyncMock(side_effect=RuntimeError("message is already gone"))
        after = dt.datetime(2026, 11, 20, 15, 5, 0)

        await calendar_service.process_calendar_alerts_tick(self.db, self.bot, now_utc=after)
        self.bot.delete_message.assert_awaited_once_with(chat_id=self.tg_id, message_id=9101)
        self.bot.edit_message_reply_markup.assert_awaited_once_with(
            chat_id=self.tg_id, message_id=9101, reply_markup=None,
        )
        saved = dict(await (await self.db.execute("SELECT * FROM calendar_events WHERE id=?", (row["id"],))).fetchone())
        self.assertEqual(saved["pre_alert_sent"], 1)
        self.assertIsNone(saved["alert_message_id"])

        await calendar_service.process_calendar_alerts_tick(
            self.db, self.bot, now_utc=after + dt.timedelta(minutes=1),
        )
        self.bot.delete_message.assert_awaited_once()

    async def test_invalid_or_all_day_event_with_saved_alert_is_cleaned(self):
        for uid, overrides in (
            ("all-day-active-card", {"is_all_day": 1}),
            ("invalid-time-active-card", {"start_time": "bad", "end_time": "also-bad"}),
        ):
            event = {**self.event, "external_uid": uid, **overrides}
            row = await self._event_row(event)
            await repo.mark_calendar_event_alert_sent(
                self.db, int(row["id"]), chat_id=self.tg_id, message_id=9200 + len(uid),
            )

        await calendar_service.process_calendar_alerts_tick(
            self.db, self.bot, now_utc=self.now,
        )
        self.assertEqual(self.bot.delete_message.await_count, 2)
        active = await repo.list_calendar_events_with_active_alert(self.db)
        self.assertEqual(active, [])

    async def test_alert_send_without_message_object_still_marks_pre_alert_sent(self):
        row = await self._event_row()
        self.bot.send_message = AsyncMock(return_value=True)
        sent = await calendar_service.process_calendar_alerts_tick(self.db, self.bot, now_utc=self.now)
        self.assertEqual(sent, 1)
        saved = await self._event_row()
        self.assertEqual(saved["id"], row["id"])
        self.assertEqual(saved["pre_alert_sent"], 1)
        self.assertIsNone(saved["alert_chat_id"])
        self.assertIsNone(saved["alert_message_id"])

    async def test_dismiss_and_shift_nudges_edit_existing_message_not_send_another(self):
        from handlers import schedule

        message = MagicMock()
        message.chat.id = self.tg_id
        message.message_id = 9301
        message.edit_text = AsyncMock(return_value=True)
        message.answer = AsyncMock()
        callback = MagicMock()
        callback.message = message
        callback.from_user.id = self.tg_id
        callback.from_user.full_name = "Календарный тест"
        callback.answer = AsyncMock()
        callback.data = "cal:dismiss"
        await schedule.cal_dismiss_nudge(callback)
        message.edit_text.assert_awaited_once()
        message.answer.assert_not_awaited()

        message.edit_text.reset_mock()
        message.edit_text.side_effect = RuntimeError("Bad Request: message is not modified")
        callback.data = "cal:shift_bed:22:30"
        await schedule.cal_shift_bedtime(callback, self.db)
        message.edit_text.assert_awaited_once()
        message.answer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
