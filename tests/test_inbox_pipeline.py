"""Comprehensive tests for Voice/Text Inbox, LLM/STT transport, Diary, and audit fixes."""

from __future__ import annotations

import datetime
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    GetFile,
    SendDocument,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User, Voice

import llm_client
from config import get_llm_settings, get_stt_settings
from db import repositories as repo
from hidl.app import create_app
from llm_client import LLMClient, LLMResult, LLMRoute
from services import inbox_service, stt
from services.inbox_parser import analyze_inbox_text, resolve_temporal_expression
from utils.menu_match import is_single_clause_reminder, looks_like_expense, match_menu_label


class CaptureBotProxy:
    """Fake Bot proxy capturing Telegram API calls made by Dispatcher."""

    def __init__(self, real_bot, voice_bytes: bytes = b"OGG_FAKE_AUDIO"):
        self._real_bot = real_bot
        self.id = real_bot.id
        self.voice_bytes = voice_bytes
        self.sent_messages: list[dict] = []
        self.edited_messages: list[dict] = []
        self.documents: list[dict] = []
        self.callbacks: list[dict] = []
        self._last_output: dict = {"text": "", "reply_markup": None}
        self._msg_seq = 5000

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
            return True
        if isinstance(method, DeleteMessage):
            return True
        if isinstance(method, AnswerCallbackQuery):
            self.callbacks.append({"text": method.text, "show_alert": method.show_alert})
            return True
        if isinstance(method, SendDocument):
            self._msg_seq += 1
            raw_bytes = getattr(method.document, "data", b"")
            self.documents.append(
                {
                    "filename": getattr(method.document, "filename", "export.json"),
                    "data": raw_bytes,
                    "caption": method.caption or "",
                }
            )
            return Message(
                message_id=self._msg_seq,
                date=datetime.datetime.now(datetime.timezone.utc),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=User(id=1, is_bot=True, first_name="HiDL"),
                text=method.caption or "",
            )
        if isinstance(method, GetFile):
            return SimpleNamespace(file_id=method.file_id, file_path="voice/mock.ogg")
        return None

    async def get_file(self, file_id: str):
        return SimpleNamespace(file_id=file_id, file_path="voice/mock.ogg")

    async def download_file(self, file_path: str, destination):
        destination.write(self.voice_bytes)

    def last_output(self) -> dict:
        return self._last_output


def _find_callback_data(reply_markup, label_part: str) -> str | None:
    if not reply_markup or not getattr(reply_markup, "inline_keyboard", None):
        return None
    for row in reply_markup.inline_keyboard:
        for btn in row:
            if label_part in (btn.text or ""):
                return btn.callback_data
    return None


class StubLLMClient(LLMClient):
    """Test double for LLMClient returning predetermined LLMResult."""

    def __init__(self, result: LLMResult, configured: bool = True):
        super().__init__()
        self._result = result
        self._configured = configured
        self.calls = 0

    def is_configured(self) -> bool:
        return self._configured

    async def complete_json(self, system_prompt: str, user_prompt: str, **kwargs) -> LLMResult:
        self.calls += 1
        return self._result


class _FakeResponse:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self, handler, recorded_calls: list[dict]):
        self._handler = handler
        self._recorded = recorded_calls

    def post(self, url, **kwargs):
        self._recorded.append({"url": url, **kwargs})
        return self._handler(url, **kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class InboxPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._prev_sim_db = os.environ.get("HIDL_SIM_DB_URL")
        os.environ["HIDL_SIM_DB_URL"] = "sqlite:///:memory:"
        self.ctx = await create_app(test_mode=True)
        self.proxy = CaptureBotProxy(self.ctx.bot)
        self.tg_id = 777001
        self.user_id = await repo.create_user(
            self.ctx.db_conn,
            telegram_id=self.tg_id,
            name="Тестер",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:30",
            gender="male",
        )
        await repo.ensure_user_routines(self.ctx.db_conn, self.user_id)
        self.user = dict(await repo.get_user(self.ctx.db_conn, self.user_id))
        self._msg_id = 100

    async def asyncTearDown(self):
        await self.ctx.db_conn.close()
        await self.ctx.bot.session.close()
        if self._prev_sim_db is None:
            os.environ.pop("HIDL_SIM_DB_URL", None)
        else:
            os.environ["HIDL_SIM_DB_URL"] = self._prev_sim_db

    async def _send_text(self, text: str, *, message_id: int | None = None) -> Message:
        mid = message_id if message_id is not None else self._next_id()
        msg = Message(
            message_id=mid,
            date=datetime.datetime.now(datetime.timezone.utc),
            chat=Chat(id=self.tg_id, type="private"),
            from_user=User(id=self.tg_id, is_bot=False, first_name="Tester"),
            text=text,
        )
        await self.ctx.dp.feed_update(self.proxy, Update(update_id=mid, message=msg))
        return msg

    async def _send_voice(self, file_id: str = "voice_1", duration: int = 12, *, message_id: int | None = None):
        mid = message_id if message_id is not None else self._next_id()
        voice = Voice(file_id=file_id, file_unique_id=f"uniq_{file_id}", duration=duration, mime_type="audio/ogg")
        msg = Message(
            message_id=mid,
            date=datetime.datetime.now(datetime.timezone.utc),
            chat=Chat(id=self.tg_id, type="private"),
            from_user=User(id=self.tg_id, is_bot=False, first_name="Tester"),
            voice=voice,
        )
        await self.ctx.dp.feed_update(self.proxy, Update(update_id=mid, message=msg))
        return msg

    async def _click_callback(self, callback_data: str, message_id: int, text: str = "msg"):
        cid = self._next_id()
        cb_msg = Message(
            message_id=message_id,
            date=datetime.datetime.now(datetime.timezone.utc),
            chat=Chat(id=self.tg_id, type="private"),
            from_user=User(id=1, is_bot=True, first_name="HiDL"),
            text=text,
        )
        cb = CallbackQuery(
            id=f"cb_{cid}",
            chat_instance="test",
            from_user=User(id=self.tg_id, is_bot=False, first_name="Tester"),
            message=cb_msg,
            data=callback_data,
        )
        await self.ctx.dp.feed_update(self.proxy, Update(update_id=cid, callback_query=cb))

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def test_1_mixed_voice_and_text_plus_idea_guardrail(self):
        mixed = (
            "Надо заняться поздравлением папе, дома закончилась гречка. "
            "Сегодня устал после уроков. Ещё придумал идею для игрового бота."
        )
        # Voice message flow via Telegram Update with mocked STT + offline deterministic parser
        with patch("services.stt.stt_enabled", return_value=True), patch(
            "services.stt.transcribe_voice",
            return_value=stt.STTResult(ok=True, text=mixed, provider="groq"),
        ), patch.dict(os.environ, {"LLM_ENABLED": "0"}, clear=False):
            await self._send_voice(file_id="v_mixed_1")

        out = self.proxy.sent_messages[-1]
        self.assertIn("Расшифровала", out["text"])
        self.assertIn("Задача", out["text"])
        self.assertIn("Покупки", out["text"])
        self.assertIn("Дневник", out["text"])
        self.assertIn("Идея", out["text"])
        self.assertIsNotNone(_find_callback_data(out["reply_markup"], "Исправить"))
        self.assertIsNotNone(_find_callback_data(out["reply_markup"], "Отменить всё"))

        today = "2026-11-19"
        # Plan item exists for local today (or check all day_plan_items for user)
        cur = await self.ctx.db_conn.execute("SELECT * FROM day_plan_items WHERE user_id = ?", (self.user_id,))
        plan_rows = [dict(r) for r in await cur.fetchall()]
        self.assertEqual(len(plan_rows), 1)
        self.assertIn("поздравлением папе", plan_rows[0]["title"].lower())

        shop_rows = [dict(r) for r in await repo.list_shopping_items(self.ctx.db_conn, self.user_id)]
        self.assertEqual(len(shop_rows), 1)
        self.assertEqual(shop_rows[0]["item_name"], "Гречка")

        diary_rows = await repo.list_journal_entries(self.ctx.db_conn, self.user_id, entry_type="diary")
        idea_rows = await repo.list_journal_entries(self.ctx.db_conn, self.user_id, entry_type="idea")
        self.assertEqual(len(diary_rows), 1)
        self.assertEqual(diary_rows[0]["source"], "voice")
        self.assertEqual(diary_rows[0]["user_text"], "Сегодня устал после уроков")
        self.assertEqual(len(idea_rows), 1)
        self.assertIn("игрового бота", idea_rows[0]["title"].lower())

        # Guardrail: even if LLM misclassifies a wish as "task", Python forces "idea"
        bad_llm = StubLLMClient(
            LLMResult(
                ok=True,
                text='{"items":[{"kind":"task","title":"Сделать игру","source_quote":"Хочу когда-нибудь сделать игру","model_note":"гипотеза"}]}',
                data={
                    "items": [
                        {
                            "kind": "task",
                            "title": "Сделать игру",
                            "source_quote": "Хочу когда-нибудь сделать игру",
                            "model_note": "можно начать с прототипа",
                        }
                    ]
                },
                provider="openai",
                model="test-model",
            )
        )
        outcome = await inbox_service.capture_and_process_text(
            self.ctx.db_conn,
            self.user,
            chat_id=self.tg_id,
            message_id=9901,
            text="Хочу когда-нибудь сделать игру",
            llm_client_instance=bad_llm,
        )
        self.assertEqual(len(outcome.actions), 1)
        self.assertEqual(outcome.actions[0]["item_kind"], "idea")
        cur = await self.ctx.db_conn.execute("SELECT * FROM day_plan_items WHERE user_id = ?", (self.user_id,))
        self.assertEqual(len(await cur.fetchall()), 1)  # still only 1 from earlier voice!
        idea_rows = await repo.list_journal_entries(self.ctx.db_conn, self.user_id, entry_type="idea")
        self.assertEqual(len(idea_rows), 2)
        wish_entry = idea_rows[0]
        self.assertEqual(wish_entry["user_text"], "Хочу когда-нибудь сделать игру")
        self.assertEqual(wish_entry["model_note"], "можно начать с прототипа")

    async def test_2_timezone_tomorrow_and_ambiguous_dates(self):
        # 2026-11-18 22:30 UTC == 2026-11-19 01:30 Europe/Moscow -> "завтра" MUST be 2026-11-20!
        now_utc = datetime.datetime(2026, 11, 18, 22, 30, 0)
        sched = resolve_temporal_expression(
            date_hint="2026-11-19",  # simulate buggy LLM date arithmetic
            time_hint="12:00",
            raw_fragment="Напомни завтра в 12:00 поздравить друга",
            now_utc=now_utc,
            tz_str="Europe/Moscow",
        )
        self.assertFalse(sched.is_ambiguous)
        self.assertEqual(sched.resolved_date, "2026-11-20")
        self.assertEqual(sched.resolved_time, "12:00")

        with patch.dict(os.environ, {"LLM_ENABLED": "0"}, clear=False):
            outcome = await inbox_service.capture_and_process_text(
                self.ctx.db_conn,
                self.user,
                chat_id=self.tg_id,
                message_id=9902,
                text="Напомни завтра в 12:00 поздравить друга",
                now_utc=now_utc,
            )
        self.assertEqual(outcome.status, "processed")
        self.assertEqual(len(outcome.clarifications), 0)
        rems = [dict(r) for r in await repo.list_custom_reminders(self.ctx.db_conn, self.user_id)]
        self.assertEqual(len(rems), 1)
        self.assertEqual(rems[0]["reminder_time"], "12:00")
        self.assertEqual(rems[0]["frequency_days"], 9999)
        expected_last_sent = (datetime.date(2026, 11, 20) - datetime.timedelta(days=9999)).isoformat()
        self.assertEqual(rems[0]["last_sent_date"], expected_last_sent)

        # Ambiguous AM/PM via Telegram message: "Напомни завтра в 8 позвонить врачу"
        with patch.dict(os.environ, {"LLM_ENABLED": "0"}, clear=False):
            await self._send_text("Напомни завтра в 8 позвонить врачу")
        out_msg = self.proxy.sent_messages[-1]
        self.assertIn("Уточни", out_msg["text"])
        rems_before = await repo.list_custom_reminders(self.ctx.db_conn, self.user_id)
        self.assertEqual(len(rems_before), 1)  # no premature reminder created!
        cb_20 = _find_callback_data(out_msg["reply_markup"], "20:00")
        self.assertIsNotNone(cb_20)

        await self._click_callback(cb_20, out_msg["message_id"], out_msg["text"])
        rems_after = [dict(r) for r in await repo.list_custom_reminders(self.ctx.db_conn, self.user_id)]
        self.assertEqual(len(rems_after), 2)
        self.assertEqual(rems_after[0]["reminder_time"], "20:00")

        # Vague reminder via Telegram message: "Напомни потом оплатить счёт" -> custom reply
        with patch.dict(os.environ, {"LLM_ENABLED": "0"}, clear=False):
            await self._send_text("Напомни потом оплатить счёт")
        vague_msg = self.proxy.sent_messages[-1]
        cb_custom = _find_callback_data(vague_msg["reply_markup"], "Своё время")
        self.assertIsNotNone(cb_custom)
        await self._click_callback(cb_custom, vague_msg["message_id"], vague_msg["text"])
        await self._send_text("завтра в 14:30")
        rems_final = [dict(r) for r in await repo.list_custom_reminders(self.ctx.db_conn, self.user_id)]
        self.assertEqual(len(rems_final), 3)
        self.assertEqual(rems_final[0]["reminder_time"], "14:30")

    async def test_3_idempotent_redelivery(self):
        with patch.dict(os.environ, {"LLM_ENABLED": "0"}, clear=False):
            await self._send_text("Надо купить овсянку", message_id=4242)
            await self._send_text("Надо купить овсянку", message_id=4242)

        shop_rows = await repo.list_shopping_items(self.ctx.db_conn, self.user_id)
        self.assertEqual(len(shop_rows), 1)
        inbox_rows = await repo.list_inbox_messages(self.ctx.db_conn, self.user_id)
        self.assertEqual(len(inbox_rows), 1)
        self.assertIn("дубликат", self.proxy.sent_messages[-1]["text"].lower())

        # Voice redelivery should NOT call STT a second time
        stt_calls = 0

        async def _fake_stt(audio_bytes, **kwargs):
            nonlocal stt_calls
            stt_calls += 1
            return stt.STTResult(ok=True, text="Сегодня хорошо поработал", provider="groq")

        voice = Voice(file_id="v_dup", file_unique_id="u_dup", duration=5, mime_type="audio/ogg")
        o1 = await inbox_service.capture_and_process_voice(
            self.ctx.db_conn,
            self.proxy,
            self.user,
            chat_id=self.tg_id,
            message_id=4243,
            voice=voice,
            stt_fn=_fake_stt,
        )
        o2 = await inbox_service.capture_and_process_voice(
            self.ctx.db_conn,
            self.proxy,
            self.user,
            chat_id=self.tg_id,
            message_id=4243,
            voice=voice,
            stt_fn=_fake_stt,
        )
        self.assertFalse(o1.is_duplicate)
        self.assertTrue(o2.is_duplicate)
        self.assertEqual(stt_calls, 1)

    async def test_4_stt_and_llm_failures_preserve_raw_data(self):
        # 1. STT failure preserves raw voice record & supports retry
        with patch("services.stt.stt_enabled", return_value=True), patch(
            "services.stt.transcribe_voice",
            return_value=stt.STTResult(ok=False, provider="groq", error="http_503"),
        ):
            await self._send_voice(file_id="v_fail_1", message_id=5001)

        fail_msg = self.proxy.sent_messages[-1]
        self.assertIn("сохранила", fail_msg["text"].lower())
        retry_cb = _find_callback_data(fail_msg["reply_markup"], "Повторить")
        self.assertIsNotNone(retry_cb)

        inbox_rows = await repo.list_inbox_messages(self.ctx.db_conn, self.user_id)
        self.assertEqual(inbox_rows[0]["status"], "failed_stt")
        self.assertEqual(inbox_rows[0]["telegram_file_id"], "v_fail_1")

        # Retry succeeds when STT recovers
        with patch("services.stt.stt_enabled", return_value=True), patch(
            "services.stt.transcribe_voice",
            return_value=stt.STTResult(ok=True, text="Сегодня устал", provider="groq"),
        ), patch.dict(os.environ, {"LLM_ENABLED": "0"}, clear=False):
            await self._click_callback(retry_cb, fail_msg["message_id"], fail_msg["text"])

        recovered = await repo.get_inbox_message(self.ctx.db_conn, self.user_id, inbox_rows[0]["id"])
        self.assertEqual(recovered["status"], "processed")
        self.assertEqual(recovered["raw_text"], "Сегодня устал")

        # 2. LLM failure preserves raw_text and uses deterministic fallback
        broken_llm = StubLLMClient(
            LLMResult(ok=False, provider="openai", model="test-model", error="timeout"),
            configured=True,
        )
        outcome = await inbox_service.capture_and_process_text(
            self.ctx.db_conn,
            self.user,
            chat_id=self.tg_id,
            message_id=5002,
            text="Надо купить чай. Ещё придумал идею.",
            llm_client_instance=broken_llm,
        )
        self.assertEqual(outcome.llm_error, "timeout")
        self.assertFalse(outcome.used_llm)
        self.assertEqual(len(outcome.actions), 2)
        saved_row = await repo.get_inbox_message(self.ctx.db_conn, self.user_id, outcome.inbox_id)
        self.assertEqual(saved_row["raw_text"], "Надо купить чай. Ещё придумал идею.")
        self.assertEqual(saved_row["llm_error"], "timeout")
        self.assertIn("Модель не ответила", outcome.reply_text)
        self.assertIsNotNone(_find_callback_data(outcome.reply_markup, "Переразобрать"))

    async def test_5_correction_reclassify_rename_delete_and_undo(self):
        with patch.dict(os.environ, {"LLM_ENABLED": "0"}, clear=False):
            await self._send_text("Надо сделать прототип бота, купить чай", message_id=6001)
        summary_msg = self.proxy.sent_messages[-1]
        edit_cb = _find_callback_data(summary_msg["reply_markup"], "Исправить")
        self.assertIsNotNone(edit_cb)
        inbox_id = int(edit_cb.split(":")[-1])

        # Open edit list -> select task item -> reclassify task -> idea
        await self._click_callback(edit_cb, summary_msg["message_id"], summary_msg["text"])
        edit_view = self.proxy.last_output()
        item_cb = _find_callback_data(edit_view["reply_markup"], "Сделать прототип бота")
        self.assertIsNotNone(item_cb)
        await self._click_callback(item_cb, summary_msg["message_id"], edit_view["text"])

        item_view = self.proxy.last_output()
        reclass_cb = _find_callback_data(item_view["reply_markup"], "Идея")
        self.assertIsNotNone(reclass_cb)
        await self._click_callback(reclass_cb, summary_msg["message_id"], item_view["text"])

        # Old task row MUST be deleted, new idea row MUST exist with original source_quote
        cur = await self.ctx.db_conn.execute("SELECT * FROM day_plan_items WHERE user_id = ?", (self.user_id,))
        self.assertEqual(len(await cur.fetchall()), 0)
        ideas = await repo.list_journal_entries(self.ctx.db_conn, self.user_id, entry_type="idea")
        self.assertEqual(len(ideas), 1)
        self.assertIn("прототип бота", ideas[0]["user_text"].lower())

        # Rename item via FSM
        renamed_view = self.proxy.last_output()
        rename_cb = _find_callback_data(renamed_view["reply_markup"], "Изменить текст")
        self.assertIsNotNone(rename_cb)
        await self._click_callback(rename_cb, summary_msg["message_id"], renamed_view["text"])
        await self._send_text("Прототип нового бота")
        ideas_after_rename = await repo.list_journal_entries(self.ctx.db_conn, self.user_id, entry_type="idea")
        self.assertEqual(ideas_after_rename[0]["title"], "Прототип нового бота")

        # Undo everything for this inbox message
        await self._click_callback(f"inbox:undo:{inbox_id}", summary_msg["message_id"], "undo")
        self.assertEqual(len(await repo.list_journal_entries(self.ctx.db_conn, self.user_id)), 0)
        self.assertEqual(len(await repo.list_shopping_items(self.ctx.db_conn, self.user_id)), 0)
        inbox_row = await repo.get_inbox_message(self.ctx.db_conn, self.user_id, inbox_id)
        self.assertEqual(inbox_row["status"], "undone")

    async def test_6_diary_search_export_and_wipe(self):
        with patch.dict(os.environ, {"LLM_ENABLED": "0"}, clear=False):
            await self._send_text("Сегодня устал после трёх занятий. Ещё придумал идею для проекта.", message_id=7001)

        await self._send_text("/diary")
        diary_msg = self.proxy.sent_messages[-1]
        self.assertIn("Дневник", diary_msg["text"])
        self.assertIn("устал после трёх занятий", diary_msg["text"])

        await self._send_text("/diary занят")
        search_msg = self.proxy.sent_messages[-1]
        self.assertIn("Поиск в дневнике", search_msg["text"])
        self.assertIn("занятий", search_msg["text"])

        export_cb = _find_callback_data(diary_msg["reply_markup"], "Экспорт JSON")
        self.assertIsNotNone(export_cb)
        await self._click_callback(export_cb, diary_msg["message_id"], diary_msg["text"])
        self.assertEqual(len(self.proxy.documents), 1)
        exported = json.loads(self.proxy.documents[0]["data"].decode("utf-8"))
        self.assertIn("journal_entries", exported)
        self.assertIn("inbox_messages", exported)
        self.assertGreaterEqual(len(exported["journal_entries"]), 2)

        # Wipe personal data
        wipe_cb = _find_callback_data(diary_msg["reply_markup"], "Очистить дневник")
        self.assertIsNotNone(wipe_cb)
        await self._click_callback(wipe_cb, diary_msg["message_id"], diary_msg["text"])
        confirm_view = self.proxy.last_output()
        yes_cb = _find_callback_data(confirm_view["reply_markup"], "Да, удалить всё")
        self.assertIsNotNone(yes_cb)
        await self._click_callback(yes_cb, diary_msg["message_id"], confirm_view["text"])
        self.assertEqual(len(await repo.list_journal_entries(self.ctx.db_conn, self.user_id)), 0)
        self.assertEqual(len(await repo.list_inbox_messages(self.ctx.db_conn, self.user_id)), 0)

    async def test_7_llm_and_stt_transport_contracts(self):
        # 1. Config never invents a model ID when unset
        with patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "sk-test",
                "OPENAI_MODEL": "",
                "LLM_MODEL": "",
                "DEEPSEEK_API_KEY": "",
                "LLM_FAILOVER_PROVIDER": "",
            },
            clear=False,
        ):
            llm_cfg = get_llm_settings()
            self.assertFalse(llm_cfg.primary.is_ready)
            self.assertFalse(llm_cfg.enabled)

        # 2. OpenAI request body, proxy, retries on 500, no retry on 401 + failover
        recorded: list[dict] = []
        responses = [
            _FakeResponse(500, {"error": "temp"}),
            _FakeResponse(200, {"choices": [{"message": {"content": '{"items":[{"kind":"idea","title":"Тест"}]}'}}]}),
        ]

        def _handler_retry(url, **kwargs):
            return responses.pop(0)

        route = LLMRoute(
            provider="openai",
            base_url="https://api.example/v1",
            api_key="sk-primary",
            model="custom-model-id",
            proxy_url="http://127.0.0.1:8888",
            reasoning_effort="none",
        )
        client = LLMClient(route=route)
        with patch.object(llm_client, "RETRY_BACKOFF_SEC", (0.0, 0.0)), patch(
            "aiohttp.ClientSession", return_value=_FakeSession(_handler_retry, recorded)
        ):
            res = await client.complete_json("sys", "usr")

        self.assertTrue(res.ok)
        self.assertEqual(len(recorded), 2)
        req_json = recorded[0]["json"]
        self.assertEqual(req_json["max_completion_tokens"], 1200)
        self.assertNotIn("max_tokens", req_json)
        self.assertEqual(req_json["reasoning_effort"], "none")
        self.assertEqual(req_json["store"], False)
        self.assertEqual(recorded[0]["proxy"], "http://127.0.0.1:8888")

        # 401 should NOT retry primary, and should fall over immediately
        recorded_failover: list[dict] = []

        def _handler_failover(url, **kwargs):
            if "primary" in url:
                return _FakeResponse(401, {"error": "unauthorized"})
            return _FakeResponse(200, {"choices": [{"message": {"content": "ok failover"}}]})

        primary = LLMRoute(provider="openai", base_url="https://primary.test/v1", api_key="bad", model="m1")
        failover = LLMRoute(provider="deepseek", base_url="https://backup.test", api_key="good", model="m2")
        client2 = LLMClient(route=primary, failover_route=failover)
        with patch.object(llm_client, "RETRY_BACKOFF_SEC", (0.0, 0.0)), patch(
            "aiohttp.ClientSession", return_value=_FakeSession(_handler_failover, recorded_failover)
        ):
            res2 = await client2.complete_text("sys", "usr")
        self.assertTrue(res2.ok)
        self.assertEqual(res2.provider, "deepseek")
        self.assertEqual(len(recorded_failover), 2)
        self.assertEqual(recorded_failover[1]["json"]["thinking"], {"type": "disabled"})

        # 3. STT runtime fallback Groq (429) -> Qwen (200)
        stt_calls: list[dict] = []

        def _stt_handler(url, **kwargs):
            if "groq.com" in url:
                return _FakeResponse(429, {"error": "rate_limit"})
            return _FakeResponse(200, {"choices": [{"message": {"content": "расшифрованный текст"}}]})

        with patch.dict(
            os.environ,
            {
                "STT_PROVIDER": "auto",
                "GROQ_API_KEY": "gsk-test",
                "QWEN_API_KEY": "qsk-test",
                "OPENAI_API_KEY": "",
                "STT_API_KEY": "",
            },
            clear=False,
        ), patch("aiohttp.ClientSession", return_value=_FakeSession(_stt_handler, stt_calls)):
            stt_cfg = get_stt_settings()
            self.assertEqual([r.provider for r in stt_cfg.routes], ["groq", "qwen"])
            stt_res = await stt.transcribe_voice(b"fake_ogg")

        self.assertTrue(stt_res.ok)
        self.assertEqual(stt_res.provider, "qwen")
        self.assertEqual(stt_res.text, "расшифрованный текст")
        self.assertEqual(len(stt_calls), 2)

    async def test_8_audit_regression_fixes(self):
        # 1. Menu and expense matchers ignore ordinary free text
        self.assertFalse(match_menu_label("Сегодня устал после уроков", {"сегодня"}))
        self.assertTrue(match_menu_label("📅 Сегодня", {"сегодня"}))
        self.assertFalse(looks_like_expense("Сегодня было 3 урока"))
        self.assertTrue(looks_like_expense("потратила 200 рублей на такси"))
        self.assertFalse(is_single_clause_reminder("Напомни завтра в 8 позвонить врачу"))
        self.assertTrue(is_single_clause_reminder("напомни купить хлеб завтра в 10:00"))

        # 2. "Сегодня было 3 урока" is NOT logged as expense or hijacked by /today -> goes to inbox
        with patch.dict(os.environ, {"LLM_ENABLED": "0"}, clear=False):
            await self._send_text("Сегодня было 3 урока", message_id=8001)
        cur = await self.ctx.db_conn.execute("SELECT * FROM expenses WHERE user_id = ?", (self.user_id,))
        self.assertEqual(len(await cur.fetchall()), 0)
        diary = await repo.list_journal_entries(self.ctx.db_conn, self.user_id, entry_type="diary")
        self.assertEqual(len(diary), 1)
        self.assertEqual(diary[0]["user_text"], "Сегодня было 3 урока")

        # 3. /schedule renders cleanly without AttributeError
        await self._send_text("/schedule")
        sched_msg = self.proxy.sent_messages[-1]
        self.assertIn("Расписание", sched_msg["text"])

        # 4. PantryAddState.amount ("1 л") reaches kitchen.py instead of finance.py
        await self._click_callback("kitchen:fridge_add", sched_msg["message_id"], "fridge")
        await self._send_text("Овсяное молоко")
        await self._send_text("1 л")
        cat_msg = self.proxy.sent_messages[-1]
        self.assertIn("Выбери категорию", cat_msg["text"])


if __name__ == "__main__":
    unittest.main()
