"""Regression tests for UX playtest fixes, FSM escapes, AI agent routing, and slash commands."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
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
    GetFile,
    SendDocument,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from db import repositories as repo
from hidl.app import create_app
from llm_client import LLMClient, LLMResult
from services import inbox_service


class CaptureBotProxy:
    """Fake Bot proxy capturing Telegram API calls made by Dispatcher."""

    def __init__(self, real_bot):
        self._real_bot = real_bot
        self.id = real_bot.id
        self.sent_messages: list[dict] = []
        self.edited_messages: list[dict] = []
        self.edited_markups: list[dict] = []
        self.documents: list[dict] = []
        self.callbacks: list[dict] = []
        self._last_output: dict = {"message_id": 0, "text": "", "reply_markup": None}
        self._msg_seq = 6000

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

    async def send_message(self, chat_id: int, text: str, reply_markup=None, **kwargs):
        return await self(SendMessage(chat_id=chat_id, text=text, reply_markup=reply_markup))

    async def edit_message_text(self, text: str, chat_id: int | None = None, message_id: int | None = None, reply_markup=None, **kwargs):
        return await self(
            EditMessageText(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup)
        )

    async def edit_message_reply_markup(self, chat_id: int | None = None, message_id: int | None = None, reply_markup=None, **kwargs):
        return await self(
            EditMessageReplyMarkup(chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)
        )

    def last_output(self) -> dict:
        return self._last_output


def _iter_inline_buttons(reply_markup):
    if not reply_markup or not getattr(reply_markup, "inline_keyboard", None):
        return []
    return [btn for row in reply_markup.inline_keyboard for btn in row]


def _find_callback_data(reply_markup, label_part: str) -> str | None:
    for btn in _iter_inline_buttons(reply_markup):
        if label_part in (btn.text or ""):
            return btn.callback_data
    return None


def _all_callback_datas(reply_markup) -> list[str]:
    return [btn.callback_data for btn in _iter_inline_buttons(reply_markup) if btn.callback_data]


@contextmanager
def _offline_llm():
    """Force deterministic offline parsing without touching external LLM/STT endpoints."""
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"LLM_ENABLED": "0", "STT_PROVIDER": "off"}, clear=False))
        stack.enter_context(patch.object(LLMClient, "is_configured", return_value=False))
        yield


@contextmanager
def _mock_llm_response(payload: dict, *, model: str = "gpt-6-luna"):
    """Patch LLMClient so all agent JSON calls return payload deterministically without network."""

    async def _fake_complete_json(self, system_prompt: str, user_prompt: str, **kwargs) -> LLMResult:
        return LLMResult(
            ok=True,
            text=json.dumps(payload, ensure_ascii=False),
            data=payload,
            provider="openai",
            model=model,
        )

    async def _fake_complete_text(self, system_prompt: str, user_prompt: str, **kwargs) -> LLMResult:
        reply = str(payload.get("reply") or "Я рядом.")
        return LLMResult(
            ok=True,
            text=reply,
            provider="openai",
            model=model,
        )

    with ExitStack() as stack:
        stack.enter_context(patch.object(LLMClient, "is_configured", return_value=True))
        stack.enter_context(patch.object(LLMClient, "complete_json", new=_fake_complete_json))
        stack.enter_context(patch.object(LLMClient, "complete_text", new=_fake_complete_text))
        yield


class UXFixesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._prev_sim_db = os.environ.get("HIDL_SIM_DB_URL")
        self._tmpdir = tempfile.TemporaryDirectory(prefix="hidl_ux_test_")
        db_file = Path(self._tmpdir.name) / "ux_test.db"
        os.environ["HIDL_SIM_DB_URL"] = f"sqlite:///{db_file.as_posix()}"

        self.ctx = await create_app(test_mode=True)
        self.proxy = CaptureBotProxy(self.ctx.bot)
        self.tg_id = 778001
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
        self._msg_id = 200
        inbox_service.clear_dialog_history()

    async def asyncTearDown(self):
        inbox_service.clear_dialog_history()
        try:
            await self.ctx.db_conn.close()
        finally:
            await self.ctx.bot.session.close()
            if self._prev_sim_db is None:
                os.environ.pop("HIDL_SIM_DB_URL", None)
            else:
                os.environ["HIDL_SIM_DB_URL"] = self._prev_sim_db
            self._tmpdir.cleanup()

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _send_text(
        self,
        text: str,
        *,
        tg_id: int | None = None,
        first_name: str = "Tester",
        message_id: int | None = None,
    ) -> Message:
        uid = tg_id if tg_id is not None else self.tg_id
        mid = message_id if message_id is not None else self._next_id()
        msg = Message(
            message_id=mid,
            date=datetime.datetime.now(datetime.timezone.utc),
            chat=Chat(id=uid, type="private"),
            from_user=User(id=uid, is_bot=False, first_name=first_name),
            text=text,
        )
        await self.ctx.dp.feed_update(self.proxy, Update(update_id=mid, message=msg))
        return msg

    async def _click_callback(
        self,
        callback_data: str,
        message_id: int,
        text: str = "msg",
        *,
        tg_id: int | None = None,
        first_name: str = "Tester",
    ) -> None:
        uid = tg_id if tg_id is not None else self.tg_id
        cid = self._next_id()
        cb_msg = Message(
            message_id=message_id,
            date=datetime.datetime.now(datetime.timezone.utc),
            chat=Chat(id=uid, type="private"),
            from_user=User(id=1, is_bot=True, first_name="HiDL"),
            text=text,
        )
        cb = CallbackQuery(
            id=f"cb_{cid}",
            chat_instance="test",
            from_user=User(id=uid, is_bot=False, first_name=first_name),
            message=cb_msg,
            data=callback_data,
        )
        await self.ctx.dp.feed_update(self.proxy, Update(update_id=cid, callback_query=cb))

    async def test_reminder_parsed_confirm_does_not_swallow_subsequent_messages(self):
        with _offline_llm():
            await self._send_text("Напомни позвонить врачу")
            first_out = self.proxy.last_output()
            self.assertIn("позвонить врачу", first_out["text"].lower())

            fsm = self.ctx.dp.fsm.get_context(self.proxy, self.tg_id, self.tg_id)
            self.assertEqual(await fsm.get_state(), "CustomReminderState:parsed_confirm")

            # Follow-up time/date inside parsed_confirm updates the pending summary
            await self._send_text("завтра в 10:30")
            updated_out = self.proxy.last_output()
            self.assertIn("10:30", updated_out["text"])
            self.assertIn("позвонить врачу", updated_out["text"].lower())

            # Unrelated message escapes parsed_confirm cleanly and saves as idea instead of silence
            before_cnt = len(self.proxy.sent_messages)
            await self._send_text("Хочу когда-нибудь выучить японский")
            self.assertGreater(len(self.proxy.sent_messages), before_cnt)
            escape_out = self.proxy.sent_messages[-1]
            self.assertIn("Идея", escape_out["text"])
            self.assertIsNone(await fsm.get_state())

        ideas = await repo.list_journal_entries(self.ctx.db_conn, self.user_id, entry_type="idea")
        self.assertEqual(len(ideas), 1)
        self.assertIn("японский", ideas[0]["user_text"].lower())

    async def test_customdel_requires_confirmation_and_shows_real_title(self):
        rem_id = await repo.create_custom_reminder(
            self.ctx.db_conn,
            user_id=self.user_id,
            title="Тест врача",
            reminder_time="15:00",
            frequency_days=1,
        )
        await self._send_text("/reminders")
        rem_msg = self.proxy.sent_messages[-1]
        del_cb = f"customdel:{rem_id}"
        self.assertIn(del_cb, _all_callback_datas(rem_msg["reply_markup"]))

        await self._click_callback(del_cb, rem_msg["message_id"], rem_msg["text"])
        # Must NOT be deleted yet; prompt must ask confirmation with real title
        still_there = await repo.list_custom_reminders(self.ctx.db_conn, self.user_id)
        self.assertEqual(len(still_there), 1)

        confirm_out = self.proxy.last_output()
        self.assertIn("Тест врача", confirm_out["text"])
        yes_cb = f"customdel_yes:{rem_id}"
        self.assertIn(yes_cb, _all_callback_datas(confirm_out["reply_markup"]))

        await self._click_callback(yes_cb, confirm_out["message_id"], confirm_out["text"])
        after_del = await repo.list_custom_reminders(self.ctx.db_conn, self.user_id)
        self.assertEqual(len(after_del), 0)
        last_toast = self.proxy.callbacks[-1]["text"] or ""
        self.assertIn("Тест врача", last_toast)

    async def test_settings_callbacks_and_no_phantom_user(self):
        await repo.update_user_wake(self.ctx.db_conn, self.user_id, "07:15")
        await self._send_text("/settings")
        s_msg = self.proxy.sent_messages[-1]

        # 1. No AttributeError on CallbackQuery.replace replacements
        await self._click_callback("settings:mealprof:set:vegan", s_msg["message_id"], s_msg["text"])
        w1 = dict(await repo.get_wellness(self.ctx.db_conn, self.user_id))
        self.assertEqual(w1.get("meal_profile"), "vegan")

        await self._click_callback("settings:affirm:set_sched:morning", s_msg["message_id"], s_msg["text"])
        w2 = dict(await repo.get_wellness(self.ctx.db_conn, self.user_id))
        self.assertEqual(json.loads(w2.get("affirm_hours") or "[]"), [9])

        # 2. Water toggle flips water_enabled 0 <-> 1
        await self._click_callback("settings:notify:water", s_msg["message_id"], s_msg["text"])
        w3 = dict(await repo.get_wellness(self.ctx.db_conn, self.user_id))
        self.assertEqual(int(w3.get("water_enabled") or 0), 1)

        await self._click_callback("settings:notify:water", s_msg["message_id"], s_msg["text"])
        w4 = dict(await repo.get_wellness(self.ctx.db_conn, self.user_id))
        self.assertEqual(int(w4.get("water_enabled") or 0), 0)

        # 3. affirm_cat_menu callback opens categories screen
        await self._click_callback("settings:notify:affirm_menu", s_msg["message_id"], s_msg["text"])
        affirm_menu_out = self.proxy.last_output()
        self.assertIn("settings:notify:affirm_cat_menu", _all_callback_datas(affirm_menu_out["reply_markup"]))

        await self._click_callback(
            "settings:notify:affirm_cat_menu",
            affirm_menu_out["message_id"],
            affirm_menu_out["text"],
        )
        cat_out = self.proxy.last_output()
        self.assertIn("Категории аффирмаций", cat_out["text"])

        # 4. settings:sleep_mode does NOT create a phantom user row and shows real times
        cur = await self.ctx.db_conn.execute("SELECT COUNT(*) FROM users")
        users_before = (await cur.fetchone())[0]
        await self._click_callback("settings:sleep_mode", s_msg["message_id"], s_msg["text"])
        cur = await self.ctx.db_conn.execute("SELECT COUNT(*) FROM users")
        users_after = (await cur.fetchone())[0]
        self.assertEqual(users_after, users_before)

        sleep_out = self.proxy.last_output()
        self.assertIn("23:30", sleep_out["text"])
        self.assertIn("07:15", sleep_out["text"])

    async def test_help_food_respects_vegan_profile(self):
        await repo.upsert_wellness(self.ctx.db_conn, self.user_id, meal_profile="vegan")
        await self._send_text("/help_now")
        help_msg = self.proxy.sent_messages[-1]

        await self._click_callback("help:food", help_msg["message_id"], help_msg["text"])
        food_out = self.proxy.last_output()
        self.assertNotIn("куриц", food_out["text"].lower())

        buttons = _iter_inline_buttons(food_out["reply_markup"])
        for btn in buttons:
            self.assertNotIn("куриц", (btn.text or "").lower())

        cook_cbs = [cb for cb in _all_callback_datas(food_out["reply_markup"]) if cb.startswith("kitchen:cook_view:")]
        self.assertGreater(len(cook_cbs), 0)
        for cb in cook_cbs:
            parts = cb.split(":")
            self.assertGreaterEqual(len(parts), 4)
            self.assertEqual(parts[3], "2", f"Expected 2-portion default in {cb}")

        # Clicking kitchen:quick_pick also respects vegan profile
        await self._click_callback("kitchen:quick_pick", food_out["message_id"], food_out["text"])
        quick_out = self.proxy.last_output()
        quick_lower = quick_out["text"].lower()
        self.assertTrue(quick_lower.strip())
        self.assertNotIn("куриц", quick_lower)
        for non_vegan_marker in ("омлет", "шакшук", "говядин", "тунец", "тунц", "лосос", "индейк"):
            self.assertNotIn(non_vegan_marker, quick_lower)

    async def test_ask_mom_bare_and_free_question_fsm(self):
        with _offline_llm():
            await self._send_text("/ask_mom")
            mom_msg = self.proxy.sent_messages[-1]
            self.assertIn("Мама на связи", mom_msg["text"])
            self.assertIn("ask:start:free", _all_callback_datas(mom_msg["reply_markup"]))

            await self._click_callback("ask:start:free", mom_msg["message_id"], mom_msg["text"])
            fsm = self.ctx.dp.fsm.get_context(self.proxy, self.tg_id, self.tg_id)
            self.assertEqual(await fsm.get_state(), "AskMomFree:question")

            await self._send_text("Как отмыть сковороду?")
            answer_out = self.proxy.sent_messages[-1]
            self.assertTrue(answer_out["text"].strip())
            self.assertNotIn("Записала:", answer_out["text"])
            self.assertNotIn("Дневник", answer_out["text"])

        self.assertEqual(len(await repo.list_journal_entries(self.ctx.db_conn, self.user_id)), 0)

    async def test_ai_agent_chat_query_shopping_remove_expense_and_diary_stem(self):
        # 1. intent="chat" -> no journal_entries & no "Модель не ответила"
        with _mock_llm_response(
            {"intent": "chat", "reply": "Привет! Я тут.", "query_type": "none", "items": []}
        ):
            await self._send_text("Привет!")
            chat_out = self.proxy.sent_messages[-1]
            self.assertIn("Привет!", chat_out["text"])
            self.assertNotIn("Модель не ответила", chat_out["text"])

            await self._send_text("Как дела, помощница?")
            llm_chat_out = self.proxy.sent_messages[-1]
            self.assertIn("Привет! Я тут.", llm_chat_out["text"])
            self.assertNotIn("Модель не ответила", llm_chat_out["text"])
        self.assertEqual(len(await repo.list_journal_entries(self.ctx.db_conn, self.user_id)), 0)

        # 2. intent="query", query_type="today" -> Today dashboard & zero junk journal_entries
        with _mock_llm_response(
            {"intent": "query", "query_type": "today", "reply": "", "items": []}
        ):
            await self._send_text("Что у меня на сегодня?")
        today_out = self.proxy.sent_messages[-1]
        self.assertIn("План на", today_out["text"])
        self.assertEqual(len(await repo.list_journal_entries(self.ctx.db_conn, self.user_id)), 0)

        # 3. shopping_remove (mocked LLM + undo restore + deterministic fallback)
        await repo.create_shopping_item(
            self.ctx.db_conn, self.user_id, "Гречка", qty=1, unit="уп", category="food", scope="household"
        )
        self.assertEqual(len(await repo.list_shopping_items(self.ctx.db_conn, self.user_id)), 1)

        with _mock_llm_response(
            {
                "intent": "capture",
                "reply": "",
                "query_type": "none",
                "items": [{"kind": "shopping_remove", "title": "Гречка", "source_quote": "Удали гречку"}],
            }
        ):
            await self._send_text("Удали гречку")
        self.assertEqual(len(await repo.list_shopping_items(self.ctx.db_conn, self.user_id)), 0)

        rm_msg = self.proxy.sent_messages[-1]
        undo_shop_cb = _find_callback_data(rm_msg["reply_markup"], "Отменить")
        self.assertIsNotNone(undo_shop_cb)
        await self._click_callback(undo_shop_cb, rm_msg["message_id"], rm_msg["text"])
        restored = [dict(r) for r in await repo.list_shopping_items(self.ctx.db_conn, self.user_id)]
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["item_name"], "Гречка")

        with _offline_llm():
            await self._send_text("убери гречку из покупок")
        self.assertEqual(len(await repo.list_shopping_items(self.ctx.db_conn, self.user_id)), 0)

        # 4. expense inside inbox + undo deletes expense
        with _mock_llm_response(
            {
                "intent": "capture",
                "reply": "",
                "query_type": "none",
                "items": [
                    {
                        "kind": "expense",
                        "title": "Кофе",
                        "amount": 450,
                        "category": "кофе",
                        "source_quote": "Отдала в кофейне 450 за капучино",
                    }
                ],
            }
        ):
            await self._send_text("Отдала в кофейне 450 за капучино")
        cur = await self.ctx.db_conn.execute("SELECT * FROM expenses WHERE user_id = ?", (self.user_id,))
        exp_rows = [dict(r) for r in await cur.fetchall()]
        self.assertEqual(len(exp_rows), 1)
        self.assertEqual(float(exp_rows[0]["amount"]), 450.0)
        self.assertEqual(exp_rows[0]["category"], "кофе")

        exp_msg = self.proxy.sent_messages[-1]
        undo_exp_cb = _find_callback_data(exp_msg["reply_markup"], "Отменить")
        self.assertIsNotNone(undo_exp_cb)
        await self._click_callback(undo_exp_cb, exp_msg["message_id"], exp_msg["text"])
        cur = await self.ctx.db_conn.execute("SELECT * FROM expenses WHERE user_id = ?", (self.user_id,))
        self.assertEqual(len(await cur.fetchall()), 0)

        # 5. Mixed message with "поздравлением папе" -> /diary папа finds it via stem matching
        with _offline_llm():
            await self._send_text("Надо заняться поздравлением папе, дома закончилась гречка.")
            await self._send_text("/diary папа")
        diary_search_out = self.proxy.sent_messages[-1]
        self.assertIn("папе", diary_search_out["text"].lower())

    async def test_fsm_escapes_and_registration_guard(self):
        # 1. FocusCafeState.task ignores /diary command and routes to diary view
        await self._send_text("/cafe")
        fsm = self.ctx.dp.fsm.get_context(self.proxy, self.tg_id, self.tg_id)
        self.assertEqual(await fsm.get_state(), "FocusCafeState:task")

        await self._send_text("/diary")
        diary_out = self.proxy.sent_messages[-1]
        self.assertIn("Дневник", diary_out["text"])
        self.assertNotIn("Сколько минут", diary_out["text"])
        self.assertIsNone(await fsm.get_state())

        # 2. ShoppingAddState.name: "отмена" cancels without asking "Сколько?"
        await self._click_callback("kitchen:shop_add:household", diary_out["message_id"], diary_out["text"])
        self.assertEqual(await fsm.get_state(), "ShoppingAddState:name")
        await self._send_text("отмена")
        cancel_out = self.proxy.sent_messages[-1]
        self.assertNotIn("Сколько", cancel_out["text"])
        self.assertIsNone(await fsm.get_state())
        self.assertEqual(len(await repo.list_shopping_items(self.ctx.db_conn, self.user_id)), 0)

        # 3. Brand-new user ignores /start name buttons -> phrase is NOT saved as display name
        new_tg_id = 888999
        with _offline_llm():
            await self._send_text("/start", tg_id=new_tg_id, first_name="Алекс")
            await self._send_text("Хочу записать дело", tg_id=new_tg_id, first_name="Алекс")

        new_user = await repo.get_user_by_telegram_id(self.ctx.db_conn, new_tg_id)
        if new_user is not None:
            self.assertNotIn("Хочу записать дело", new_user["name"])
        new_fsm = self.ctx.dp.fsm.get_context(self.proxy, new_tg_id, new_tg_id)
        new_data = await new_fsm.get_data()
        self.assertNotEqual(new_data.get("name"), "Хочу записать дело")
        self.assertNotIn("Как мне к тебе обращаться?", self.proxy.sent_messages[-1]["text"])

    async def test_slash_commands_respond_and_have_no_dead_silence(self):
        commands = (
            "/pantry",
            "/home",
            "/finance",
            "/movement",
            "/settings",
            "/sleep",
            "/help_now",
            "/diary",
        )
        for cmd in commands:
            with self.subTest(cmd=cmd):
                before = len(self.proxy.sent_messages)
                await self._send_text(cmd)
                self.assertGreater(len(self.proxy.sent_messages), before, f"{cmd} stayed silent")
                out = self.proxy.sent_messages[-1]
                self.assertTrue((out["text"] or "").strip(), f"{cmd} returned empty text")
                self.assertNotIn("что-то пошло не так", out["text"].lower())
                self.assertNotIn("traceback", out["text"].lower())
                if cmd == "/settings":
                    self.assertIsNotNone(getattr(out["reply_markup"], "inline_keyboard", None))

    async def test_food_followup_more_uses_dialog_context_and_does_not_hit_diary(self):
        await repo.upsert_wellness(self.ctx.db_conn, self.user_id, meal_profile="vegan")

        with _offline_llm():
            # 1. Ask what to cook for dinner
            await self._send_text("Что приготовить на ужин?")
            first_out = self.proxy.last_output()
            first_text = first_out["text"]
            self.assertNotIn("дневник", first_text.lower())
            self.assertNotIn("куриц", first_text.lower())
            first_cbs = _all_callback_datas(first_out["reply_markup"])
            self.assertIn("kitchen:more_food", first_cbs)
            first_recipes = {cb.split(":")[2] for cb in first_cbs if cb.startswith("kitchen:cook_view:")}
            self.assertEqual(len(first_recipes), 3)

            # 2. Follow up with "А еще?" -> returns next unseen vegan recipes, NOT diary!
            await self._send_text("А еще?")
            second_out = self.proxy.last_output()
            second_text = second_out["text"]
            self.assertNotIn("Последние записи дневника", second_text)
            self.assertNotIn("куриц", second_text.lower())
            second_cbs = _all_callback_datas(second_out["reply_markup"])
            second_recipes = {cb.split(":")[2] for cb in second_cbs if cb.startswith("kitchen:cook_view:")}
            self.assertEqual(len(second_recipes), 3)
            self.assertEqual(first_recipes & second_recipes, set())

            # 3. Click "🔄 Ещё варианты" -> shows remaining unseen vegan recipes (including 35m lentil_soup)
            await self._click_callback("kitchen:more_food", second_out["message_id"], second_text)
            third_out = self.proxy.last_output()
            third_text = third_out["text"]
            self.assertNotIn("куриц", third_text.lower())
            third_cbs = _all_callback_datas(third_out["reply_markup"])
            third_recipes = {cb.split(":")[2] for cb in third_cbs if cb.startswith("kitchen:cook_view:")}
            self.assertGreaterEqual(len(third_recipes), 2)
            self.assertEqual((first_recipes | second_recipes) & third_recipes, set())

        self.assertEqual(len(await repo.list_journal_entries(self.ctx.db_conn, self.user_id)), 0)


if __name__ == "__main__":
    unittest.main()
