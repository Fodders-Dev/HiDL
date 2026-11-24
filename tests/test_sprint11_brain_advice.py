"""Tests for Sprint 11: context-aware brain, habit portrait, situational advice, voice household commands, and expanded knowledge base."""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path

from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from db import repositories as repo
from hidl.app import create_app
from llm_client import LLMClient, LLMResult
from services import advice_service, context_builder, fitness_nutrition, inbox_service, karma_service, pulse_service
from services.inbox_parser import analyze_inbox_text
from services.knowledge import KnowledgeService
from utils.time import local_date_str


class FakeLLMRecorder:
    """Fake LLM client recording prompts and returning scripted JSON/text."""

    def __init__(self, json_payload=None, text_reply: str = ""):
        self.calls: list[dict] = []
        self._json_payload = json_payload or {
            "intent": "chat",
            "reply": "Держим курс!",
            "query_type": "none",
            "query_term": "",
            "items": [],
        }
        self._text_reply = text_reply

    def is_configured(self) -> bool:
        return True

    async def complete_json(self, system_prompt: str, user_prompt: str, **kwargs) -> LLMResult:
        self.calls.append({"kind": "json", "system": system_prompt, "user": user_prompt})
        return LLMResult(
            ok=True,
            text=json.dumps(self._json_payload, ensure_ascii=False),
            data=self._json_payload,
            provider="mock",
            model="mock-gpt",
        )

    async def complete_text(self, system_prompt: str, user_prompt: str, **kwargs) -> LLMResult:
        self.calls.append({"kind": "text", "system": system_prompt, "user": user_prompt})
        return LLMResult(
            ok=True,
            text=self._text_reply or "Закрой сейчас одну главную задачу на 15 минут.",
            provider="mock",
            model="mock-gpt",
        )


class LocalBotProxy:
    """Minimal Telegram bot proxy capturing Dispatcher responses."""

    def __init__(self, real_bot=None):
        self.id = real_bot.id if real_bot else 1
        self.sent_messages: list[dict] = []
        self.edited_messages: list[dict] = []
        self.callbacks: list[dict] = []
        self._last_output: dict = {"message_id": 0, "text": "", "reply_markup": None}
        self._msg_seq = 12000

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
        return None


def _all_buttons(markup) -> list[tuple[str, str]]:
    if not markup or not getattr(markup, "inline_keyboard", None):
        return []
    return [
        (b.text, b.callback_data)
        for row in markup.inline_keyboard
        for b in row
        if getattr(b, "callback_data", None)
    ]


class Sprint11BrainAdviceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "sprint11_test.db"
        self.db_url = f"sqlite:///{self.db_path}"
        self._orig_sim_db = os.environ.get("HIDL_SIM_DB_URL")
        os.environ["HIDL_SIM_DB_URL"] = self.db_url
        self.ctx = await create_app(test_mode=True)
        self.db = self.ctx.db_conn
        self.dp = self.ctx.dp
        self.bot = LocalBotProxy(self.ctx.bot)
        self.tg_id = 888001122
        self.uid = await repo.create_user(
            self.db,
            self.tg_id,
            "Tester",
            "Europe/Moscow",
            "07:30",
            "23:30",
        )
        await repo.upsert_wellness(self.db, self.uid, meal_profile="vegan")
        inbox_service.clear_dialog_history()
        self._seq = 500

    async def asyncTearDown(self) -> None:
        inbox_service.clear_dialog_history()
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

    def test_1_expanded_knowledge_base_schemas_and_counts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        recipes = json.loads((root / "data" / "knowledge" / "recipes_core.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(recipes), 85)
        ids = [r["id"] for r in recipes]
        self.assertEqual(len(ids), len(set(ids)))
        for orig_id in ("omelet", "tofu_scramble", "chickpea_curry", "lentil_soup", "hummus_wrap", "eggs_toast"):
            self.assertIn(orig_id, ids)
        vegan_cnt = sum(1 for r in recipes if "vegan" in (r.get("tags") or []))
        self.assertGreaterEqual(vegan_cnt, 65)
        for r in recipes:
            self.assertTrue(r.get("title") and r.get("ingredients") and r.get("steps"))
            self.assertGreater(int(r.get("time_minutes") or 0), 0)
            est = fitness_nutrition.estimate_recipe_nutrition(r)
            self.assertGreater(est["kcal"], 0, r.get("id"))
            if "nutrition" in r:
                nutr = r["nutrition"]
                self.assertIsInstance(nutr, dict, r.get("id"))
                self.assertGreater(nutr.get("kcal") or 0, 0, r.get("id"))
                for macro in ("protein_g", "fat_g", "carbs_g"):
                    self.assertIn(macro, nutr, r.get("id"))
                    self.assertGreaterEqual(float(nutr[macro]), 0.0, f"{r.get('id')}.{macro}")

        aff_full = json.loads((root / "data" / "knowledge" / "affirmations_full.json").read_text(encoding="utf-8"))
        for cat in ("motivation", "calm", "confidence", "quotes"):
            self.assertGreaterEqual(len(aff_full.get(cat) or []), 55, cat)
        self.assertGreaterEqual(len(aff_full.get("discipline") or []), 30)

        aff_simple = json.loads((root / "data" / "affirmations.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(aff_simple), 55)

        advice_cards = json.loads((root / "data" / "knowledge" / "situational_advice.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(advice_cards), 30)
        for card in advice_cards:
            self.assertTrue(card.get("id") and card.get("situation") and card.get("micro_step"))
            tv = card.get("tone_variants") or {}
            self.assertTrue(tv.get("warm") and tv.get("firm"))

        ks = KnowledgeService()
        self.assertIsNotNone(ks.get_random_affirmation(category="discipline"))
        self.assertIsNotNone(ks.get_random_affirmation(category="quotes"))

    async def test_2_context_snapshot_and_habit_portrait(self) -> None:
        user = await self._user()
        now = datetime.datetime(2026, 11, 19, 18, 0)  # 21:00 MSK
        await karma_service.record_karma_event(
            self.db, user, domain="sleep", event_type="night_catch", delta=-5, reason="Ночной онлайн", now_utc=now
        )
        await karma_service.record_karma_event(
            self.db, user, domain="sleep", event_type="checkin_ignored", delta=-10, reason="Игнор отбоя", now_utc=now
        )
        await karma_service.record_karma_event(
            self.db, user, domain="food", event_type="checkin_ignored", delta=-10, reason="Пропуск обеда 1", now_utc=now
        )
        await karma_service.record_karma_event(
            self.db, user, domain="food", event_type="checkin_skip", delta=-4, reason="Пропуск ужина", now_utc=now
        )
        await repo.add_day_plan_item(self.db, self.uid, "2026-11-19", "Доделать отчёт", is_important=True)
        await repo.create_pantry_item(self.db, self.uid, "Тофу", 1, "шт", None, "продукты")

        memory = await context_builder.refresh_user_portrait_if_needed(self.db, user, now_utc=now, force=True)
        patterns = json.loads(memory["patterns_json"])
        self.assertIn("late_night_drift", patterns)
        self.assertIn("skips_meals", patterns)
        self.assertIn("веган", memory["portrait_text"].lower())

        snapshot = await context_builder.build_user_day_snapshot(self.db, user, now_utc=now)
        self.assertEqual(snapshot["karma_score"], 41)
        self.assertEqual(snapshot["karma_mode"]["code"], "strict")
        self.assertEqual(snapshot["meal_profile"], "vegan")
        ctx_str = context_builder.format_context_for_llm(snapshot)
        self.assertIn("Строгая мама", ctx_str)
        self.assertIn("Доделать отчёт", ctx_str)
        self.assertIn("Тофу", ctx_str)

    async def test_3_situational_advice_selection_and_tone_adaptation(self) -> None:
        user = await self._user()
        now_late = datetime.datetime(2026, 11, 19, 20, 45)  # 23:45 MSK, past 23:30 sleep
        snap_high = await context_builder.build_user_day_snapshot(self.db, user, now_utc=now_late)
        snap_high["karma_score"] = 85
        snap_high["karma_mode"] = karma_service.get_karma_mode(85)
        card_warm = advice_service.pick_situational_advice(snap_high, "не могу уснуть")
        self.assertEqual(card_warm["situation"], "late_night")
        self.assertEqual(card_warm["tone_variant_key"], "warm")

        snap_low = dict(snap_high)
        snap_low["karma_score"] = 20
        snap_low["karma_mode"] = karma_service.get_karma_mode(20)
        card_firm = advice_service.pick_situational_advice(snap_low, "не могу уснуть")
        self.assertEqual(card_firm["situation"], "late_night")
        self.assertEqual(card_firm["tone_variant_key"], "firm")
        self.assertNotEqual(card_warm["selected_tone_text"], card_firm["selected_tone_text"])

        # Task overwhelm
        for i in range(5):
            await repo.add_day_plan_item(self.db, self.uid, "2026-11-19", f"Задача {i}")
        now_day = datetime.datetime(2026, 11, 19, 9, 0)  # 12:00 MSK
        snap_day = await context_builder.build_user_day_snapshot(self.db, user, now_utc=now_day)
        card_tasks = advice_service.pick_situational_advice(snap_day, "много дел, не успеваю")
        self.assertEqual(card_tasks["situation"], "overwhelmed_tasks")

    async def test_4_household_voice_pantry_pulse_and_undo(self) -> None:
        user = await self._user()
        await repo.create_shopping_item(self.db, self.uid, "Тофу", qty=1, unit="шт", scope="household")
        await repo.create_shopping_item(self.db, self.uid, "Огурцы", qty=2, unit="шт", scope="household")

        # 1. pantry_add adds to pantry AND deletes matching unbought items from shopping_list
        o_add = await inbox_service.capture_and_process_text(
            self.db,
            user,
            chat_id=self.tg_id,
            message_id=101,
            text="купил тофу, гречку и огурцы",
            now_utc=datetime.datetime(2026, 11, 19, 10, 0),
        )
        pantry_names = [dict(r)["name"].lower() for r in await repo.list_pantry_items(self.db, self.uid)]
        self.assertIn("тофу", pantry_names)
        self.assertIn("гречка", pantry_names)
        self.assertIn("огурцы", pantry_names)
        shop_after = [dict(r)["item_name"].lower() for r in await repo.list_shopping_items(self.db, self.uid, scope="household")]
        self.assertEqual(shop_after, [])

        # Undo reverses BOTH pantry items AND restores synced shopping items!
        undone = await inbox_service.undo_inbox_message(self.db, user, o_add.inbox_id)
        self.assertEqual(undone, 3)
        pantry_reverted = await repo.list_pantry_items(self.db, self.uid)
        self.assertEqual(len(pantry_reverted), 0)
        shop_restored = [dict(r)["item_name"].lower() for r in await repo.list_shopping_items(self.db, self.uid, scope="household")]
        self.assertIn("тофу", shop_restored)
        self.assertIn("огурцы", shop_restored)

        # 2. pantry_remove deletes pantry item and Undo restores it
        await repo.create_pantry_item(self.db, self.uid, "Огурцы", 2, "шт", None, "продукты")
        o_rem = await inbox_service.capture_and_process_text(
            self.db,
            user,
            chat_id=self.tg_id,
            message_id=102,
            text="съел огурцы",
            now_utc=datetime.datetime(2026, 11, 19, 11, 0),
        )
        self.assertIn("Убрала из холодильника", o_rem.reply_text)
        self.assertEqual(len(await repo.list_pantry_items(self.db, self.uid)), 0)
        await inbox_service.undo_inbox_message(self.db, user, o_rem.inbox_id)
        self.assertEqual(len(await repo.list_pantry_items(self.db, self.uid)), 1)

        # 3. Existing guardrail: "закончилось овсяное молоко" stays shopping (NOT pantry_remove)
        o_shop = await inbox_service.capture_and_process_text(
            self.db,
            user,
            chat_id=self.tg_id,
            message_id=103,
            text="закончилось овсяное молоко",
            now_utc=datetime.datetime(2026, 11, 19, 11, 5),
        )
        self.assertEqual(o_shop.actions[0]["item_kind"], "shopping")

        # 4. pulse_log updates real pulse checkins and awards karma
        o_pulse = await inbox_service.capture_and_process_text(
            self.db,
            user,
            chat_id=self.tg_id,
            message_id=104,
            text="я поел",
            now_utc=datetime.datetime(2026, 11, 19, 11, 10),
        )
        self.assertIn("Режим:", o_pulse.reply_text)
        lunch_chk = await repo.get_pulse_checkin(self.db, self.uid, "2026-11-19", "lunch")
        self.assertIsNotNone(lunch_chk)
        self.assertEqual(lunch_chk["status"], "done")
        st = await repo.get_or_create_karma_state(self.db, self.uid)
        self.assertEqual(st["score"], 73)

    async def test_5_one_tap_confirmations_for_rollover_and_shift_bedtime(self) -> None:
        today = local_date_str(datetime.datetime.utcnow(), "Europe/Moscow")
        tomorrow = (datetime.date.fromisoformat(today) + datetime.timedelta(days=1)).isoformat()
        await repo.add_day_plan_item(self.db, self.uid, today, "Несделанный хвост 1", is_important=True)

        # Send rollover request via Dispatcher -> must NOT move yet!
        out_ro = await self._send_text("перенеси хвосты на завтра")
        self.assertEqual(len(await repo.list_day_plan_items(self.db, self.uid, today)), 1)
        self.assertEqual(len(await repo.list_day_plan_items(self.db, self.uid, tomorrow)), 0)
        btns = _all_buttons(out_ro["reply_markup"])
        confirm_cb = next((cb for _, cb in btns if cb.startswith("inbox:confirm:")), None)
        self.assertIsNotNone(confirm_cb)

        await self._click(confirm_cb)
        self.assertEqual(len(await repo.list_day_plan_items(self.db, self.uid, today)), 0)
        self.assertEqual(len(await repo.list_day_plan_items(self.db, self.uid, tomorrow)), 1)

        # Shift bedtime request -> must NOT mutate sleep_time until confirmed
        out_sb = await self._send_text("сегодня ложусь в 00:30")
        user_before = await self._user()
        self.assertEqual(user_before["sleep_time"], "23:30")
        btns2 = _all_buttons(out_sb["reply_markup"])
        confirm_sb = next((cb for _, cb in btns2 if cb.startswith("inbox:confirm:")), None)
        self.assertIsNotNone(confirm_sb)

        await self._click(confirm_sb)
        user_after = await self._user()
        # Note: if today is Sat/Sun in UTC/Moscow test runtime, checks effective schedule accordingly
        _, eff_sleep, _ = pulse_service.get_effective_schedule(user_after, today)
        self.assertEqual(eff_sleep, "00:30")

    async def test_6_queries_advice_portrait_and_llm_context_injection(self) -> None:
        out_karma = await self._send_text("какая у меня карма?")
        self.assertIn("Сон, режим и карма", out_karma["text"])

        await repo.create_pantry_item(self.db, self.uid, "Банан", 2, "шт", None, "продукты")
        out_pantry = await self._send_text("что у нас в холодильнике?")
        self.assertIn("Банан", out_pantry["text"])

        out_adv = await self._send_text("/advice")
        self.assertIn("Микро-шаг", out_adv["text"])
        await self._click("advice:more")
        self.assertIn("Микро-шаг", self.bot._last_output["text"])

        await self._send_text("🌙 Сон и карма")
        await self._click("pulse:portrait")
        self.assertIn("Портрет привычек", self.bot._last_output["text"])

        # Verify context block is passed directly into LLM prompt
        recorder = FakeLLMRecorder()
        await repo.add_day_plan_item(self.db, self.uid, "2026-11-19", "Уникальная задача из снимка", is_important=True)
        await inbox_service.capture_and_process_text(
            self.db,
            await self._user(),
            chat_id=self.tg_id,
            message_id=205,
            text="Я запутался, помоги",
            now_utc=datetime.datetime(2026, 11, 19, 18, 0),
            llm_client_instance=recorder,
        )
        self.assertEqual(len(recorder.calls), 1)
        prompt_sent = recorder.calls[0]["user"]
        self.assertIn("Снимок дня пользователя", prompt_sent)
        self.assertIn("Уникальная задача из снимка", prompt_sent)
        self.assertIn("Банан", prompt_sent)
