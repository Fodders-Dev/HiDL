"""Tests for Sprint 12: live HTML/done bugfixes + Smart Kitchen 2.0 (Combo)."""

from __future__ import annotations

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
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from db import repositories as repo
from hidl.app import create_app
from llm_client import LLMResult
from services import context_builder, inbox_service, kitchen_ai, pulse_service
from utils.rows import rows_to_dicts
from utils.sender import safe_edit
from utils.time import local_date_str
from utils.today import render_today


class FakeDoneLLM:
    """Fake LLM returning scripted JSON for done/capture tests."""

    def __init__(self, payload: dict):
        self._payload = payload

    def is_configured(self) -> bool:
        return True

    async def complete_json(self, system_prompt: str, user_prompt: str, **kwargs) -> LLMResult:
        return LLMResult(
            ok=True,
            text=json.dumps(self._payload, ensure_ascii=False),
            data=self._payload,
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
        self._msg_seq = 13000

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


class Sprint12KitchenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "sprint12_test.db"
        self.db_url = f"sqlite:///{self.db_path}"
        self._orig_sim_db = os.environ.get("HIDL_SIM_DB_URL")
        os.environ["HIDL_SIM_DB_URL"] = self.db_url
        self.ctx = await create_app(test_mode=True)
        self.db = self.ctx.db_conn
        self.dp = self.ctx.dp
        self.bot = LocalBotProxy(self.ctx.bot)
        self.tg_id = 777002233
        self.uid = await repo.create_user(
            self.db,
            self.tg_id,
            "KitchenTester",
            "Europe/Moscow",
            "07:00",
            "23:30",
        )
        await repo.upsert_wellness(self.db, self.uid, meal_profile="vegan")
        inbox_service.clear_dialog_history()
        self._seq = 900

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
            from_user=User(id=self.tg_id, is_bot=False, first_name="KitchenTester"),
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
            id=f"cq_{self._seq}",
            from_user=User(id=self.tg_id, is_bot=False, first_name="KitchenTester"),
            chat_instance="ci12",
            message=msg,
            data=cb_data,
        )
        upd = Update(update_id=self._seq, callback_query=cq)
        await self.dp.feed_update(self.bot, upd, db=self.db)
        return self.bot._last_output

    async def test_1_safe_edit_omits_none_parse_mode(self):
        recorded: list[dict] = []

        class StubMsg:
            async def edit_text(self, text, **kwargs):
                recorded.append({"method": "edit", "text": text, "kwargs": kwargs})

            async def answer(self, text, **kwargs):
                recorded.append({"method": "answer", "text": text, "kwargs": kwargs})

        msg = StubMsg()
        await safe_edit(msg, "<b>Ок</b>")
        self.assertEqual(len(recorded), 1)
        self.assertNotIn("parse_mode", recorded[0]["kwargs"])

        await safe_edit(msg, "<b>Явно</b>", parse_mode="HTML")
        self.assertEqual(recorded[1]["kwargs"].get("parse_mode"), "HTML")

    async def test_2_inbox_done_closes_day_plan_task_and_undo_reopens(self):
        now = datetime.datetime(2026, 11, 19, 20, 0)
        local_date = local_date_str(now, "Europe/Moscow")
        task_id = await repo.add_day_plan_item(
            self.db, self.uid, local_date, "Искупаться", category="inbox", is_important=False
        )
        karma_before = await repo.get_or_create_karma_state(self.db, self.uid)
        score_before = int(karma_before["score"])

        user = await self._user()
        fake_llm = FakeDoneLLM(
            {
                "intent": "capture",
                "reply": "",
                "query_type": "none",
                "query_term": "",
                "items": [
                    {
                        "kind": "done",
                        "title": "Искупаться",
                        "source_quote": "только что искупался, задача выполнена",
                        "model_note": "",
                        "tags": [],
                    }
                ],
            }
        )
        outcome = await inbox_service.capture_and_process_text(
            self.db,
            user,
            chat_id=self.tg_id,
            message_id=9101,
            text="Если что, только что искупался, задача выполнена",
            now_utc=now,
            llm_client_instance=fake_llm,
        )
        self.assertEqual(outcome.status, "processed")
        self.assertIn("задача закрыта", outcome.reply_text)
        self.assertIn("+2 кармы", outcome.reply_text)

        items = rows_to_dicts(await repo.list_day_plan_items(self.db, self.uid, local_date))
        target = next(i for i in items if int(i["id"]) == task_id)
        self.assertEqual(int(target["done"]), 1)

        karma_after = await repo.get_or_create_karma_state(self.db, self.uid)
        self.assertEqual(int(karma_after["score"]), score_before + 2)

        undone = await inbox_service.undo_inbox_message(self.db, user, outcome.inbox_id)
        self.assertGreaterEqual(undone, 1)
        items_reopened = rows_to_dicts(await repo.list_day_plan_items(self.db, self.uid, local_date))
        target_reopened = next(i for i in items_reopened if int(i["id"]) == task_id)
        self.assertEqual(int(target_reopened["done"]), 0)

    async def test_3_ingredient_matching_stems_aliases_and_staples(self):
        self.assertTrue(kitchen_ai.ingredients_match("Помидоры", "томатная пассата/помидоры"))
        self.assertTrue(kitchen_ai.ingredients_match("Овсянка", "хлопья овсяные"))
        self.assertTrue(kitchen_ai.ingredients_match("масло оливковое", "масло растительное"))
        self.assertFalse(kitchen_ai.ingredients_match("масло оливковое", "масло сливочное"))
        self.assertFalse(kitchen_ai.ingredients_match("арахисовая паста", "макароны"))
        self.assertFalse(kitchen_ai.ingredients_match("кокосовое молоко", "молоко"))
        self.assertTrue(kitchen_ai.is_staple_ingredient("вода"))
        self.assertTrue(kitchen_ai.is_staple_ingredient("соль"))

        recipe = kitchen_ai.get_recipe("vegan_buckwheat_tofu")
        self.assertIsNotNone(recipe)
        pantry = [
            {"id": 1, "name": "Гречка", "amount": 500, "unit": "g"},
            {"id": 2, "name": "Тофу", "amount": 300, "unit": "g"},
            {"id": 3, "name": "Оливковое масло", "amount": 200, "unit": "ml"},
        ]
        match = kitchen_ai.match_recipe_to_pantry(recipe, pantry, servings=2)
        have_names = {h["name"].lower() for h in match["have"]}
        miss_names = {m["name"].lower() for m in match["missing"]}
        self.assertIn("гречка", have_names)
        self.assertIn("тофу", have_names)
        self.assertNotIn("соль", miss_names)
        self.assertNotIn("вода", miss_names)
        self.assertGreater(match["coverage"], 0.4)

    async def test_4_suggest_meals_now_diet_pantry_and_energy(self):
        user = await self._user()
        await kitchen_ai.bulk_add_pantry_items(
            self.db, self.uid, "гречка 1 кг, тофу 300 г, соевый соус 1 шт, оливковое масло 1 шт, огурцы 3 шт"
        )
        now = datetime.datetime(2026, 11, 20, 15, 30)  # 18:30 MSK -> dinner
        res = await kitchen_ai.suggest_meals_now(self.db, user, energy="normal", now_utc=now)
        self.assertEqual(res["meal_profile"], "vegan")
        self.assertEqual(res["meal_slot"], "dinner")
        titles = [s["recipe"]["title"] for s in res["suggestions"]]
        self.assertEqual(len(titles), 3)
        for s in res["suggestions"]:
            self.assertIn("vegan", s["recipe"].get("tags", []))

        low_res = await kitchen_ai.suggest_meals_now(self.db, user, energy="low", now_utc=now)
        self.assertLessEqual(int(low_res["suggestions"][0]["recipe"]["time_minutes"]), 15)

        first_id = res["suggestions"][0]["recipe"]["id"]
        next_res = await kitchen_ai.suggest_meals_now(
            self.db, user, energy="normal", exclude_ids=[first_id], now_utc=now
        )
        self.assertNotIn(first_id, [s["recipe"]["id"] for s in next_res["suggestions"]])

    async def test_5_add_missing_deduplicates_pantry_and_shopping(self):
        await repo.create_pantry_item(self.db, self.uid, "Гречка", 500, "g", None, "крупы")
        await repo.create_shopping_item(self.db, self.uid, "Морковь", 200, "g", scope="household")

        res = await kitchen_ai.add_missing_ingredients_to_shopping(
            self.db,
            self.uid,
            [
                {"name": "гречка", "qty": 160, "unit": "g"},
                {"name": "морковь", "qty": 100, "unit": "g"},
                {"name": "соль", "qty": 1, "unit": "щепотка"},
                {"name": "чеснок", "qty": 2, "unit": "шт"},
                {"name": "чеснок", "qty": 1, "unit": "шт"},
            ],
        )
        added_names = [x["name"].lower() for x in res["added"]]
        self.assertEqual(added_names, ["чеснок"])
        self.assertAlmostEqual(float(res["added"][0]["qty"]), 3.0)

    async def test_6_commit_cooked_deducts_pantry_closes_pulse_and_awards_karma(self):
        user = await self._user()
        now = datetime.datetime(2026, 11, 20, 16, 0)  # 19:00 MSK -> dinner
        local_date = local_date_str(now, "Europe/Moscow")

        await repo.create_pantry_item(self.db, self.uid, "гречка", 160, "g", None, "крупы")
        await repo.create_pantry_item(self.db, self.uid, "тофу", 400, "g", None, "белок")
        await repo.upsert_meal_plan_slot(
            self.db,
            self.uid,
            local_date,
            "dinner",
            title="Гречка с жареным тофу и овощами",
            recipe_id="vegan_buckwheat_tofu",
            time_minutes=25,
        )
        await repo.upsert_pulse_checkin(
            self.db,
            self.uid,
            local_date,
            "dinner",
            "food",
            now.isoformat(),
            now.isoformat(),
        )
        karma_before = int((await repo.get_or_create_karma_state(self.db, self.uid))["score"])

        res = await kitchen_ai.commit_cooked_recipe(
            self.db, user, "vegan_buckwheat_tofu", servings=2, meal_slot="dinner", now_utc=now
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["marked_slot"], "dinner")
        self.assertEqual(res["pulse_closed"], "dinner")
        self.assertGreater(int(res["karma_delta"]), 0)

        karma_after = int((await repo.get_or_create_karma_state(self.db, self.uid))["score"])
        # No double karma: Pulse dinner completion (+3) only
        self.assertEqual(karma_after, karma_before + int(res["karma_delta"]))

        pantry_after = rows_to_dicts(await repo.list_pantry_items(self.db, self.uid))
        names_left = {p["name"]: float(p["amount"]) for p in pantry_after}
        # Buckwheat (160g - 2*80g = 0) should be soft-deleted, tofu reduced!
        self.assertNotIn("гречка", names_left)
        self.assertLess(names_left.get("тофу", 999), 400.0)

        plan_rows = await repo.get_meal_plan_for_date(self.db, self.uid, local_date)
        dinner_row = next(r for r in plan_rows if r["meal_slot"] == "dinner")
        self.assertEqual(dinner_row["status"], "cooked")

    async def test_7_generate_meal_plan_3_and_4_days_unique_vegan(self):
        user = await self._user()
        await kitchen_ai.bulk_add_pantry_items(self.db, self.uid, "овсянка 500 г, нут 500 г")
        now = datetime.datetime(2026, 11, 20, 8, 0)

        plan3 = await kitchen_ai.generate_meal_plan(self.db, user, days=3, now_utc=now)
        self.assertEqual(len(plan3["items"]), 9)
        ids3 = [r["recipe_id"] for r in plan3["items"]]
        self.assertEqual(len(set(ids3)), 9)
        for rid in ids3:
            rec = kitchen_ai.get_recipe(rid)
            self.assertIsNotNone(rec)
            self.assertIn("vegan", rec.get("tags", []))
        miss_names = {m["name"].lower() for m in plan3["missing_shopping"]}
        self.assertNotIn("овсянка", miss_names)
        self.assertNotIn("овсяные хлопья", miss_names)
        self.assertNotIn("нут", miss_names)

        plan4 = await kitchen_ai.generate_meal_plan(self.db, user, days=4, now_utc=now)
        self.assertEqual(len(plan4["items"]), 12)
        self.assertEqual(len({r["recipe_id"] for r in plan4["items"]}), 12)

    async def test_8_bulk_pantry_parse_syncs_shopping_list_and_ui_handlers(self):
        await repo.create_shopping_item(self.db, self.uid, "Тофу", 300, "g", scope="household")
        await repo.create_shopping_item(self.db, self.uid, "Бананы", 1, "kg", scope="household")

        out = await self._send_text("/mealplan")
        self.assertIn("Меню на 3 дн.", out["text"])
        btns = _all_buttons(out["reply_markup"])
        self.assertTrue(any(cb.startswith("kitchen:meal_plan:shop:") for _, cb in btns))

        shop_cb = next(cb for _, cb in btns if cb.startswith("kitchen:meal_plan:shop:"))
        await self._click_callback(shop_cb, out["message_id"], out["text"])
        shop_after = rows_to_dicts(await repo.list_shopping_items(self.db, self.uid, scope="household"))
        self.assertGreater(len(shop_after), 2)

        # Test single-step comma pantry input via "➕ Добавить продукт"
        await self._click_callback("kitchen:fridge", out["message_id"], out["text"])
        fridge_out = self.bot._last_output
        self.assertIn("kitchen:fridge_bulk", [cb for _, cb in _all_buttons(fridge_out["reply_markup"])])

        await self._click_callback("kitchen:fridge_add", fridge_out["message_id"], fridge_out["text"])
        bulk_resp = await self._send_text("Тофу 300 г, гречка 1 кг, огурцы 3 шт")
        self.assertIn("Добавила в холодильник (3)", bulk_resp["text"])
        self.assertIn("Вычеркнула из покупок", bulk_resp["text"])

    async def test_9_pulse_context_and_today_include_planned_meals(self):
        user = await self._user()
        now = datetime.datetime(2026, 11, 20, 15, 30)  # 18:30 MSK
        local_date = local_date_str(now, "Europe/Moscow")

        await repo.upsert_meal_plan_slot(
            self.db,
            self.uid,
            local_date,
            "breakfast",
            title="Овсянка с бананом",
            recipe_id="oatmeal_berry",
            time_minutes=10,
        )
        await repo.upsert_meal_plan_slot(
            self.db,
            self.uid,
            local_date,
            "dinner",
            title="Гречка с тофу",
            recipe_id="buckwheat_tofu",
            time_minutes=25,
        )

        # Pulse dinner ping includes planned dinner
        dinner_chk = await repo.upsert_pulse_checkin(
            self.db, self.uid, local_date, "dinner", "food", now.isoformat(), now.isoformat()
        )
        ctx = await pulse_service._gather_slot_context(self.db, user, local_date, "dinner")
        karma = await repo.get_or_create_karma_state(self.db, self.uid)
        pulse_text, _ = pulse_service.build_pulse_message(dinner_chk, karma, level=1, context=ctx)
        self.assertIn("По плану на ужин", pulse_text)
        self.assertIn("Гречка с тофу", pulse_text)

        # Context builder includes meal_plan_today
        snap = await context_builder.build_user_day_snapshot(self.db, user, now_utc=now)
        llm_ctx = context_builder.format_context_for_llm(snap)
        self.assertIn("Меню на сегодня:", llm_ctx)
        self.assertIn("Гречка с тофу", llm_ctx)

        # /today dashboard mentions Меню дня
        with patch("utils.today.local_date_str", return_value=local_date):
            today_text, _ = await render_today(self.db, user)
        self.assertIn("Меню дня:", today_text)
        self.assertIn("Гречка с тофу", today_text)


if __name__ == "__main__":
    unittest.main()
