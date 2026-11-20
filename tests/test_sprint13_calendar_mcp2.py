"""Tests for Sprint 13: Google Calendar (.ics + OAuth 2.0), smart schedule, Pulse protection, and MCP 2.0."""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from aiohttp.test_utils import TestClient, TestServer
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from config import GoogleCalendarSettings, MCPSettings
from db import repositories as repo
from hidl.app import create_app
from mcp_server.app import create_mcp_app
from services import calendar_service, context_builder, pulse_service
from utils.time import local_date_str
from utils.today import render_today


class LocalBotProxy:
    """Minimal Telegram bot proxy capturing Dispatcher responses."""

    def __init__(self, real_bot=None):
        self.id = real_bot.id if real_bot else 1
        self.sent_messages: list[dict] = []
        self.edited_messages: list[dict] = []
        self.callbacks: list[dict] = []
        self._last_output: dict = {"message_id": 0, "text": "", "reply_markup": None}
        self._msg_seq = 14000

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
        if isinstance(method, (EditMessageReplyMarkup, DeleteMessage)):
            return True
        if isinstance(method, AnswerCallbackQuery):
            self.callbacks.append({"text": method.text, "show_alert": method.show_alert})
            return True
        return None


class AlertStubBot:
    """Lightweight bot mock for alert tick tests."""

    def __init__(self):
        self.messages: list[dict] = []

    async def send_message(self, *, chat_id: int, text: str, reply_markup=None, parse_mode=None):
        self.messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
            }
        )
        return True


SAMPLE_ICS = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "BEGIN:VEVENT",
        "UID:utc-sync-1",
        "DTSTART:20261120T100000Z",
        "DTEND:20261120T110000Z",
        "SUMMARY:Синхронизация\\, команда",
        "LOCATION:Zoom",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "DESCRIPTION:Ignore alarm description",
        "END:VALARM",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:msk-local-2",
        "DTSTART;TZID=Europe/Moscow:20261120T150000",
        "DTEND;TZID=Europe/Moscow:20261120T163000",
        "SUMMARY:Стоматолог",
        "LOCATION:Центр",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:all-day-3",
        "DTSTART;VALUE=DATE:20261120",
        "DTEND;VALUE=DATE:20261121",
        "SUMMARY:Выходной без пар",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:free-transp-4",
        "DTSTART;TZID=Europe/Moscow:20261120T170000",
        "DTEND;TZID=Europe/Moscow:20261120T180000",
        "TRANSP:TRANSPARENT",
        "SUMMARY:Фоновая заметка",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:cancelled-5",
        "STATUS:CANCELLED",
        "DTSTART:20261120T120000Z",
        "DTEND:20261120T123000Z",
        "SUMMARY:Отменено",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:daily-standup-6",
        "DTSTART;TZID=Europe/Moscow:20261120T093000",
        "DURATION:PT30M",
        "RRULE:FREQ=DAILY;COUNT=3",
        "EXDATE;TZID=Europe/Moscow:20261121T093000",
        "SUMMARY:Утренний стендап",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:weekly-fit-7",
        "DTSTART;TZID=Europe/Moscow:20261120T190000",
        "DTEND;TZID=Europe/Moscow:20261120T200000",
        "RRULE:FREQ=WEEKLY;BYDAY=FR,SU;UNTIL=20261123T235959Z",
        "SUMMARY:Зал",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
)


class Sprint13CalendarMCP2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="hidl_s13_")
        self.db_path = Path(self._tmpdir.name) / "sprint13_test.db"
        self.db_url = f"sqlite:///{self.db_path}"
        self._orig_sim_db = os.environ.get("HIDL_SIM_DB_URL")
        os.environ["HIDL_SIM_DB_URL"] = self.db_url
        self.ctx = await create_app(test_mode=True)
        self.db = self.ctx.db_conn
        self.dp = self.ctx.dp
        self.bot = LocalBotProxy(self.ctx.bot)
        self.tg_id = 666001122
        self.uid = await repo.create_user(
            self.db,
            self.tg_id,
            "CalendarTester",
            "Europe/Moscow",
            "07:00",
            "23:30",
        )
        await repo.upsert_wellness(self.db, self.uid, meal_profile="vegan")
        await repo.update_user_weekend_schedule(self.db, self.uid, "09:00", "23:30")
        self._clients: list[TestClient] = []
        self._seq = 1200

    async def asyncTearDown(self) -> None:
        for cl in self._clients:
            await cl.close()
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
            from_user=User(id=self.tg_id, is_bot=False, first_name="CalendarTester"),
            text=text,
        )
        upd = Update(update_id=self._seq, message=msg)
        await self.dp.feed_update(self.bot, upd, db=self.db)
        return self.bot._last_output

    async def _click_callback(self, cb_data: str, msg_id: int, text: str = "x") -> dict:
        self._seq += 1
        msg = Message(
            message_id=msg_id or self._seq,
            date=datetime.datetime.now(datetime.timezone.utc),
            chat=Chat(id=self.tg_id, type="private"),
            from_user=User(id=1, is_bot=True, first_name="HiDL"),
            text=text,
        )
        cq = CallbackQuery(
            id=f"cq13_{self._seq}",
            from_user=User(id=self.tg_id, is_bot=False, first_name="CalendarTester"),
            chat_instance="ci13",
            message=msg,
            data=cb_data,
        )
        upd = Update(update_id=self._seq, callback_query=cq)
        await self.dp.feed_update(self.bot, upd, db=self.db)
        return self.bot._last_output

    async def test_1_ics_parser_ssrf_rrule_exdate_and_sync_persistence(self):
        # SSRF checks
        for bad_url in (
            "http://127.0.0.1/cal.ics",
            "https://192.168.1.5/cal.ics",
            "https://localhost/cal.ics",
            "file:///etc/passwd",
        ):
            ok, _ = calendar_service.validate_ics_url(bad_url)
            self.assertFalse(ok, f"Should reject unsafe URL: {bad_url}")

        ok_pub, _ = calendar_service.validate_ics_url("https://calendar.google.com/calendar/ical/abc/private-xyz/basic.ics")
        self.assertTrue(ok_pub)

        w_start = datetime.date(2026, 11, 20)
        w_end = datetime.date(2026, 11, 23)
        parsed = calendar_service.parse_ics_events(SAMPLE_ICS, "Europe/Moscow", w_start, w_end)

        titles = {e["title"] for e in parsed}
        self.assertNotIn("Отменено", titles)
        self.assertIn("Синхронизация, команда", titles)

        # Check UTC -> MSK conversion: 10:00Z == 13:00 MSK
        utc_ev = next(e for e in parsed if e["external_uid"] == "utc-sync-1")
        self.assertEqual(utc_ev["start_time"], "13:00")
        self.assertEqual(utc_ev["end_time"], "14:00")
        self.assertEqual(utc_ev["description"], "")  # VALARM description ignored!

        # All-day and TRANSPARENT are non-busy
        all_day = next(e for e in parsed if e["external_uid"] == "all-day-3")
        self.assertEqual(all_day["is_all_day"], 1)
        self.assertEqual(all_day["is_busy"], 0)
        transp = next(e for e in parsed if e["external_uid"] == "free-transp-4")
        self.assertEqual(transp["is_busy"], 0)

        # EXDATE honored on 2026-11-21 for daily standup (2026-11-20 & 2026-11-22 present, 21 excluded)
        standup_dates = {e["event_date"] for e in parsed if e["external_uid"] == "daily-standup-6"}
        self.assertEqual(standup_dates, {"2026-11-20", "2026-11-22"})

        # Weekly BYDAY=FR,SU UNTIL 2026-11-23 -> 2026-11-20 (Fri) & 2026-11-22 (Sun)
        gym_dates = {e["event_date"] for e in parsed if e["external_uid"] == "weekly-fit-7"}
        self.assertEqual(gym_dates, {"2026-11-20", "2026-11-22"})

        # Sync via fake fetcher preserves pre_alert_sent across re-sync and deletes dropped events
        user = await self._user()
        await repo.upsert_calendar_connection(
            self.db,
            self.uid,
            "ics",
            ics_url="https://calendar.google.com/calendar/ical/test/private-123/basic.ics",
        )
        now = datetime.datetime(2026, 11, 20, 8, 0)

        async def _fake_fetch_v1(_url: str) -> str:
            return SAMPLE_ICS

        res1 = await calendar_service.sync_user_calendars(self.db, user, ics_fetcher=_fake_fetch_v1, now_utc=now)
        self.assertGreaterEqual(res1["synced_events"], 5)

        ev_rows = await repo.list_calendar_events_range(self.db, self.uid, "2026-11-20", "2026-11-20")
        stoma = next(e for e in ev_rows if e["external_uid"] == "msk-local-2")
        await repo.mark_calendar_event_alert_sent(self.db, int(stoma["id"]))

        # Re-sync with all-day-3 removed
        ics_v2 = SAMPLE_ICS.replace("UID:all-day-3", "UID:all-day-x").replace("SUMMARY:Выходной без пар", "STATUS:CANCELLED\r\nSUMMARY:X")

        async def _fake_fetch_v2(_url: str) -> str:
            return ics_v2

        await calendar_service.sync_user_calendars(self.db, user, ics_fetcher=_fake_fetch_v2, now_utc=now)
        ev_rows_2 = await repo.list_calendar_events_range(self.db, self.uid, "2026-11-20", "2026-11-20")
        stoma_2 = next(e for e in ev_rows_2 if e["external_uid"] == "msk-local-2")
        self.assertEqual(int(stoma_2["pre_alert_sent"]), 1)
        self.assertFalse(any(e["external_uid"] == "all-day-3" for e in ev_rows_2))

    async def test_2_google_oauth_callback_http_and_two_way_write(self):
        gcal_cfg = GoogleCalendarSettings(
            client_id="cid-test",
            client_secret="csec-test",
            redirect_uri="https://mcp.example.com/gcal/callback",
            scopes="https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly",
        )
        start_url = await calendar_service.build_gcal_oauth_start_url(self.db, self.uid, settings=gcal_cfg)
        self.assertIsNotNone(start_url)
        state = parse_qs(urlparse(start_url).query)["state"][0]

        mcp_settings = MCPSettings(
            host="127.0.0.1",
            port=8765,
            public_base_url="https://mcp.example.com",
            owner_telegram_id=self.tg_id,
            access_password="pass",
            bearer_token="tok-s13",
            allow_diary=True,
            readonly=False,
            access_token_ttl_sec=3600,
            refresh_token_ttl_sec=86400,
        )
        app = create_mcp_app(self.db, mcp_settings)
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)

        # Missing params -> 400
        bad_resp = await client.get("/gcal/callback")
        self.assertEqual(bad_resp.status, 400)

        async def _fake_exchange(_payload: dict) -> dict:
            return {"access_token": "goog_acc_1", "refresh_token": "goog_ref_1", "expires_in": 3600}

        async def _fake_gcal_get(_url: str, _tok: str, _params: dict) -> dict:
            today_str = local_date_str(datetime.datetime.utcnow(), "Europe/Moscow")
            return {
                "items": [
                    {
                        "id": "g_ev_1",
                        "summary": "Созвон с клиентом",
                        "location": "Google Meet",
                        "start": {"dateTime": f"{today_str}T14:00:00+03:00"},
                        "end": {"dateTime": f"{today_str}T15:00:00+03:00"},
                    }
                ]
            }

        with (
            patch("services.calendar_service.get_gcal_settings", return_value=gcal_cfg),
            patch("services.calendar_service._default_oauth_exchange", side_effect=_fake_exchange),
            patch("services.calendar_service._default_gcal_get", side_effect=_fake_gcal_get),
        ):
            ok_resp = await client.get(f"/gcal/callback?code=auth_code_1&state={state}")
            self.assertEqual(ok_resp.status, 200)
            html_body = await ok_resp.text()
            self.assertIn("подключён", html_body)

            # Replay same state -> 400
            replay_resp = await client.get(f"/gcal/callback?code=auth_code_1&state={state}")
            self.assertEqual(replay_resp.status, 400)

        # Two-way event write pushes to Google Calendar and caches locally
        posted: list[dict] = []

        async def _fake_post(url: str, tok: str, body: dict) -> dict:
            posted.append({"url": url, "tok": tok, "body": body})
            return {"id": "created_g_99", "iCalUID": "created_g_99@google.com"}

        user = await self._user()
        today_str = local_date_str(datetime.datetime.utcnow(), "Europe/Moscow")
        created_res = await calendar_service.create_unified_event(
            self.db,
            user,
            title="Врач",
            event_date=today_str,
            start_time="16:00",
            end_time="17:00",
            location="Клиника",
            poster=_fake_post,
            settings=gcal_cfg,
        )
        self.assertTrue(created_res["google_synced"])
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["body"]["summary"], "Врач")
        unified = await calendar_service.get_unified_events_for_date(self.db, self.uid, today_str)
        self.assertTrue(any(e["title"] == "Созвон с клиентом" for e in unified))
        self.assertTrue(any(e["title"] == "Врач" for e in unified))

    async def test_3_pre_event_alerts_and_busy_slot_pulse_protection(self):
        now = datetime.datetime(2026, 11, 20, 10, 0)  # 13:00 MSK
        local_date = local_date_str(now, "Europe/Moscow")

        # Meeting 1: busy right now 12:30–14:00 MSK
        await repo.upsert_single_calendar_event(
            self.db,
            self.uid,
            "ics",
            {
                "external_uid": "busy-now",
                "event_date": local_date,
                "start_time": "12:30",
                "end_time": "14:00",
                "is_all_day": 0,
                "title": "Важная встреча",
                "is_busy": 1,
            },
        )
        # Meeting 2: starts at 13:20 MSK (20 min from now, no location -> 15..30m window)
        await repo.upsert_single_calendar_event(
            self.db,
            self.uid,
            "ics",
            {
                "external_uid": "soon-zoom",
                "event_date": local_date,
                "start_time": "13:20",
                "end_time": "13:50",
                "is_all_day": 0,
                "title": "Созвон по дизайну",
                "location": "",
                "is_busy": 1,
            },
        )
        # Meeting 3: starts at 13:35 MSK with location (35 min from now -> 30..45m window)
        await repo.upsert_single_calendar_event(
            self.db,
            self.uid,
            "ics",
            {
                "external_uid": "soon-loc",
                "event_date": local_date,
                "start_time": "13:35",
                "end_time": "14:30",
                "is_all_day": 0,
                "title": "Встреча в офисе",
                "location": "ул. Ленина, 10",
                "is_busy": 1,
            },
        )

        # Schedule a due lunch checkin at 10:00 UTC (13:00 MSK) during the busy 12:30-14:00 meeting
        chk = await repo.upsert_pulse_checkin(
            self.db,
            self.uid,
            local_date,
            "lunch",
            "food",
            now.isoformat(),
            now.isoformat(),
        )
        karma_before = int((await repo.get_or_create_karma_state(self.db, self.uid))["score"])

        alert_bot = AlertStubBot()
        processed = await pulse_service.process_pulse_tick(self.db, alert_bot, now_utc=now)
        self.assertEqual(processed, 0)
        self.assertEqual(len(alert_bot.messages), 0)

        chk_after = await repo.get_pulse_checkin_by_id(self.db, int(chk["id"]))
        self.assertEqual(int(chk_after["ping_count"]), 0)
        # End is 14:00 MSK = 11:00 UTC + 5m = 11:05 UTC
        self.assertTrue(str(chk_after["next_ping_at_utc"]).startswith("2026-11-20T11:05:00"))
        karma_after = int((await repo.get_or_create_karma_state(self.db, self.uid))["score"])
        self.assertEqual(karma_after, karma_before)

        # Pre-event alerts send once for both soon events
        sent_cnt = await calendar_service.process_calendar_alerts_tick(self.db, alert_bot, now_utc=now)
        self.assertEqual(sent_cnt, 2)
        sent_cnt_2 = await calendar_service.process_calendar_alerts_tick(self.db, alert_bot, now_utc=now)
        self.assertEqual(sent_cnt_2, 0)
        texts = "\n".join(m["text"] for m in alert_bot.messages)
        self.assertIn("Созвон по дизайну", texts)
        self.assertIn("ул. Ленина, 10", texts)

    async def test_4_early_wake_bedtime_shift_nudge_and_one_tap_confirm(self):
        # Saturday 2026-11-21 21:30 MSK = 18:30 UTC (2 hours before 23:30 sleep!)
        sat_evening_utc = datetime.datetime(2026, 11, 21, 18, 30)
        sat_date = "2026-11-21"
        sun_date = "2026-11-22"

        # Weekend wake is 09:00 MSK, sleep 23:30. Put Sunday event at 07:00 MSK.
        await repo.upsert_single_calendar_event(
            self.db,
            self.uid,
            "ics",
            {
                "external_uid": "sun-early",
                "event_date": sun_date,
                "start_time": "07:00",
                "end_time": "08:00",
                "is_all_day": 0,
                "title": "Поезд в Питер",
                "is_busy": 1,
            },
        )
        # Seed evening pulse checkins so 1-tap callback updates them
        wind_chk = await repo.upsert_pulse_checkin(
            self.db, self.uid, sat_date, "winddown", "tasks",
            "2026-11-21T19:30:00", "2026-11-21T19:30:00",
        )
        bed_chk = await repo.upsert_pulse_checkin(
            self.db, self.uid, sat_date, "bedtime", "sleep",
            "2026-11-21T20:15:00", "2026-11-21T20:15:00",
        )

        alert_bot = AlertStubBot()
        cnt = await calendar_service.process_calendar_alerts_tick(self.db, alert_bot, now_utc=sat_evening_utc)
        self.assertEqual(cnt, 1)
        self.assertIn("Поезд в Питер", alert_bot.messages[0]["text"])
        self.assertIn("22:00", alert_bot.messages[0]["text"])

        # Same evening does not nag twice
        cnt2 = await calendar_service.process_calendar_alerts_tick(self.db, alert_bot, now_utc=sat_evening_utc)
        self.assertEqual(cnt2, 0)

        # Context builder sees tomorrow early event
        user = await self._user()
        snap = await context_builder.build_user_day_snapshot(self.db, user, now_utc=sat_evening_utc)
        llm_ctx = context_builder.format_context_for_llm(snap)
        self.assertIn("Раннее событие завтра: 07:00 Поезд в Питер", llm_ctx)

        # User clicks 1-tap shift button in Telegram
        today_local = local_date_str(datetime.datetime.utcnow(), "Europe/Moscow")
        await repo.upsert_single_calendar_event(
            self.db,
            self.uid,
            "ics",
            {
                "external_uid": "today-demo",
                "event_date": today_local,
                "start_time": "11:00",
                "end_time": "12:00",
                "is_all_day": 0,
                "title": "Демо дня",
                "is_busy": 1,
            },
        )
        today_html, _ = await render_today(self.db, user)
        self.assertIn("Календарь на сегодня:", today_html)
        self.assertIn("Демо дня", today_html)

        shift_out = await self._click_callback("cal:shift_bed:22:00", 14001, "nudge")
        self.assertIn("22:00", shift_out["text"])
        refreshed = await self._user()
        # Check DB updated (weekend or weekday sleep depending on current day when test runs)
        _, new_sleep, _ = pulse_service.get_effective_schedule(refreshed, today_local)
        self.assertEqual(new_sleep, "22:00")
        self.assertIsNotNone(wind_chk)
        self.assertIsNotNone(bed_chk)

    async def test_5_mcp_2_0_tools_resources_and_secret_safety(self):
        user = await self._user()
        today_local = local_date_str(datetime.datetime.utcnow(), "Europe/Moscow")
        await repo.upsert_calendar_connection(
            self.db,
            self.uid,
            "ics",
            ics_url="https://calendar.google.com/calendar/ical/secret/private-topsecret123/basic.ics",
        )
        await repo.upsert_single_calendar_event(
            self.db,
            self.uid,
            "ics",
            {
                "external_uid": "mcp-ev-1",
                "event_date": today_local,
                "start_time": "15:00",
                "end_time": "16:00",
                "is_all_day": 0,
                "title": "Созвон Архитектура",
                "is_busy": 1,
            },
        )
        await repo.upsert_calendar_connection(
            self.db,
            self.uid,
            "google_oauth",
            access_token="secret_access_tok_xyz",
            refresh_token="secret_refresh_tok_xyz",
        )

        mcp_settings = MCPSettings(
            host="127.0.0.1",
            port=8765,
            public_base_url="https://mcp.example.com",
            owner_telegram_id=self.tg_id,
            access_password="pass",
            bearer_token="tok-mcp2",
            allow_diary=True,
            readonly=False,
            access_token_ttl_sec=3600,
            refresh_token_ttl_sec=86400,
        )
        app = create_mcp_app(self.db, mcp_settings)
        client = TestClient(TestServer(app))
        await client.start_server()
        self._clients.append(client)

        async def _rpc(method: str, params: dict | None = None, cli: TestClient = client):
            resp = await cli.post(
                "/mcp",
                headers={"Authorization": "Bearer tok-mcp2"},
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            )
            return resp.status, await resp.json()

        _, init_res = await _rpc("initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(init_res["result"]["serverInfo"]["version"], "2.0.0")

        _, karma_r = await _rpc("tools/call", {"name": "hidl_get_karma", "arguments": {}})
        karma_data = json.loads(karma_r["result"]["content"][0]["text"])
        self.assertIn("score", karma_data)
        self.assertIn("debts", karma_data)

        _, port_r = await _rpc("tools/call", {"name": "hidl_get_portrait", "arguments": {}})
        port_data = json.loads(port_r["result"]["content"][0]["text"])
        self.assertEqual(port_data["meal_profile"], "vegan")
        self.assertEqual(port_data["schedule"]["weekend_wake"], "09:00")

        _, cal_r = await _rpc("tools/call", {"name": "hidl_get_calendar", "arguments": {"days": 2}})
        cal_text = cal_r["result"]["content"][0]["text"]
        self.assertNotIn("private-topsecret123", cal_text)
        self.assertNotIn("secret_access_tok_xyz", cal_text)
        self.assertNotIn("secret_refresh_tok_xyz", cal_text)
        cal_data = json.loads(cal_text)
        self.assertTrue(any(e["title"] == "Созвон Архитектура" for e in cal_data["events"]))

        # Write event through MCP (falling back cleanly to local if fake token unmocked)
        _, add_ev_r = await _rpc(
            "tools/call",
            {
                "name": "hidl_add_calendar_event",
                "arguments": {
                    "title": "Пробежка",
                    "date": today_local,
                    "start_time": "18:00",
                    "end_time": "18:45",
                },
            },
        )
        add_ev_data = json.loads(add_ev_r["result"]["content"][0]["text"])
        self.assertTrue(add_ev_data["ok"])

        # Set & get meal plan
        _, set_mp_r = await _rpc(
            "tools/call",
            {
                "name": "hidl_set_meal_plan",
                "arguments": {"mode": "generate", "days": 3, "add_missing_to_shopping": True},
            },
        )
        mp_data = json.loads(set_mp_r["result"]["content"][0]["text"])
        self.assertEqual(len(mp_data["items"]), 9)
        self.assertGreater(len(mp_data["added_to_shopping"]), 0)

        _, get_mp_r = await _rpc("tools/call", {"name": "hidl_get_meal_plan", "arguments": {"days": 3}})
        get_mp_data = json.loads(get_mp_r["result"]["content"][0]["text"])
        self.assertEqual(len(get_mp_data["items"]), 9)

        # Resources read for hidl://karma and hidl://calendar
        _, res_k = await _rpc("resources/read", {"uri": "hidl://karma"})
        self.assertIn("score", res_k["result"]["contents"][0]["text"])
        _, res_c = await _rpc("resources/read", {"uri": "hidl://calendar"})
        self.assertIn("Созвон Архитектура", res_c["result"]["contents"][0]["text"])


if __name__ == "__main__":
    unittest.main()
