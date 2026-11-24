import datetime
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from config import MCPSettings
from db import repositories as repo
from db.database import connect, init_db
from handlers import inbox, movement, natural, nutrition
from llm_client import LLMResult
from mcp_server.tools import _tool_get_fitness_nutrition
from scheduler.reminder import ReminderScheduler
from services import context_builder, fitness_nutrition, inbox_parser, inbox_service, nutrition_web
from utils.time import local_date_str


class _FakeLLM:
    def __init__(self, *, json_payload=None, text_reply: str = "", configured: bool = True) -> None:
        self._json = json_payload or {}
        self._text = text_reply
        self._configured = configured
        self.calls: list[tuple[str, str]] = []

    def is_configured(self) -> bool:
        return self._configured

    async def complete_json(self, system_prompt: str, user_prompt: str, **kwargs) -> LLMResult:
        self.calls.append((system_prompt, user_prompt))
        if isinstance(self._json, dict) and self._json:
            return LLMResult(ok=True, text=json.dumps(self._json), data=self._json)
        return LLMResult(ok=False, error="not_mocked")

    async def complete_text(self, system_prompt: str, user_prompt: str, **kwargs) -> LLMResult:
        self.calls.append((system_prompt, user_prompt))
        if self._text:
            return LLMResult(ok=True, text=self._text)
        return LLMResult(ok=False, error="not_mocked")


class VoiceKbjuAndCoachTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.u_id = await repo.create_user(
            self.db,
            telegram_id=940001,
            name="Дима",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:00",
        )
        await repo.upsert_wellness(self.db, self.u_id, meal_profile="vegan")
        await repo.update_user_body(
            self.db,
            self.u_id,
            height_cm=180,
            weight_goal="loss",
            weight_target=78.0,
            age_years=28,
            sex="male",
            activity_level="light",
            equipment=["dumbbells", "smart_scales"],
        )
        await repo.add_weight(
            self.db, self.u_id, 85.0, body_fat_pct=23.0, muscle_mass_kg=60.0, source="scale_text"
        )
        self.user = dict(await repo.get_user(self.db, self.u_id))

        self.stu_id = await repo.create_user(
            self.db,
            telegram_id=940002,
            name="Саша",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:00",
        )
        await repo.update_user_profile_mode(self.db, self.stu_id, "student")
        self.student = dict(await repo.get_user(self.db, self.stu_id))
        self.storage = MemoryStorage()

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        await self.db.close()

    def _fsm(self, tg_id: int = 940001) -> FSMContext:
        return FSMContext(storage=self.storage, key=StorageKey(bot_id=1, chat_id=tg_id, user_id=tg_id))

    async def test_1_goal_coach_modes_and_context_builder(self) -> None:
        await repo.update_user_coach_settings(
            self.db, self.u_id, weight_goal="recomp", coach_mode="strict", nutrition_checkin_enabled=True
        )
        u = dict(await repo.get_user(self.db, self.u_id))
        self.assertEqual(u["weight_goal"], "recomp")
        self.assertEqual(u["coach_mode"], "strict")

        # "maintain" alias normalizes to "keep"
        await repo.update_user_coach_settings(self.db, self.u_id, weight_goal="maintain")
        u_keep = dict(await repo.get_user(self.db, self.u_id))
        self.assertEqual(u_keep["weight_goal"], "keep")

        await repo.update_user_coach_settings(self.db, self.u_id, weight_goal="recomp")
        u = dict(await repo.get_user(self.db, self.u_id))

        tgt_recomp = fitness_nutrition.calculate_daily_targets(u, {"weight": 85.0})
        self.assertFalse(tgt_recomp["mindful_mode"])
        self.assertGreaterEqual(tgt_recomp["protein_g"], 150)

        tgt_mindful = fitness_nutrition.calculate_daily_targets({**u, "weight_goal": "mindful"}, {"weight": 85.0})
        self.assertTrue(tgt_mindful["mindful_mode"])

        now = datetime.datetime(2026, 11, 23, 15, 0)
        snap = await context_builder.build_user_day_snapshot(self.db, u, now_utc=now)
        llm_ctx = context_builder.format_context_for_llm(snap)
        self.assertIn("КБЖУ сегодня", llm_ctx)
        self.assertIn("Тренер / фокус", llm_ctx)
        self.assertIn("85.0 кг", llm_ctx)

        # Student profile MUST never expose nutrition/scale/strict coach context
        snap_s = await context_builder.build_user_day_snapshot(self.db, self.student, now_utc=now)
        llm_ctx_s = context_builder.format_context_for_llm(snap_s)
        self.assertNotIn("КБЖУ сегодня", llm_ctx_s)
        self.assertNotIn("Тренер / фокус", llm_ctx_s)
        self.assertIn("Профиль: ученик", llm_ctx_s)

    async def test_2_nutrition_web_lookup_tiers_and_label_memory(self) -> None:
        self.assertTrue(nutrition_web.needs_web_nutrition_lookup("съел в теремке блин с ветчиной"))
        self.assertTrue(nutrition_web.needs_web_nutrition_lookup("выпил капучино в Surf Coffee"))
        self.assertTrue(nutrition_web.needs_web_nutrition_lookup("4680019560922"))
        self.assertFalse(nutrition_web.needs_web_nutrition_lookup("гречка 150 г"))

        web_calls = 0

        async def fake_web(q: str):
            nonlocal web_calls
            web_calls += 1
            return {
                "title": "Блин с ветчиной и сыром (Теремок)",
                "source": "web_search",
                "source_url": "https://teremok.ru/menu",
                "per_100g": {"kcal": 240.0, "protein_g": 8.0, "fat_g": 13.0, "carbs_g": 22.0},
                "serving_g": 180.0,
                "kcal_serving": 432.0,
                "protein_g": 14.4,
                "fat_g": 23.4,
                "carbs_g": 39.6,
            }

        res1 = await fitness_nutrition.parse_meal_input(
            raw_text="съел в теремке блин с ветчиной и сыром",
            db=self.db,
            user_id=self.u_id,
            web_lookup_fn=fake_web,
        )
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["kcal"], 432)
        self.assertEqual(res1["lookup_source"], "web_search")
        self.assertFalse(res1["needs_clarification"])
        self.assertEqual(web_calls, 1)

        # Second call with 2 шт and allow_web_lookup=False MUST hit SQLite cache and double kcal!
        res2 = await fitness_nutrition.parse_meal_input(
            raw_text="в теремке блин с ветчиной и сыром 2 шт",
            db=self.db,
            user_id=self.u_id,
            allow_web_lookup=False,
        )
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["kcal"], 864)
        self.assertEqual(web_calls, 1)

        # Tier 2 mocked OpenFoodFacts packaged product -> scales 250 ml
        async def fake_off(q: str):
            return {
                "title": "Nemoloko Овсяное классическое",
                "source": "openfoodfacts",
                "source_url": "https://world.openfoodfacts.org/product/123",
                "per_100g": {"kcal": 45.0, "protein_g": 1.0, "fat_g": 1.5, "carbs_g": 6.5},
                "serving_g": 200.0,
                "kcal_serving": 90.0,
            }

        off_hit = await nutrition_web.lookup_food_nutrition(
            self.db, self.u_id, "выпил nemoloko овсяное 250 мл", allow_network=True, off_fn=fake_off
        )
        self.assertIsNotNone(off_hit)
        off_scaled = nutrition_web.scale_lookup_record(off_hit, "250 мл")
        self.assertIsNotNone(off_scaled)
        self.assertEqual(off_scaled["kcal"], 112)  # round(45 * 2.5)

        # Label math ("на 100 г ... съел 60 г") + remembers in user_food_products!
        label_parsed = await fitness_nutrition.parse_meal_input(
            raw_text="Батончик Bombbar фисташка на 100 г 320 ккал б25 ж10 у30, съел 60 г",
            db=self.db,
            user_id=self.u_id,
            allow_web_lookup=False,
        )
        self.assertTrue(label_parsed["ok"])
        self.assertEqual(label_parsed["kcal"], 192)
        self.assertAlmostEqual(label_parsed["protein_g"], 15.0, places=1)
        self.assertGreaterEqual(await repo.count_user_food_products(self.db, self.u_id), 1)

        # Future mention reuses saved personal label cleanly
        repeat_label = await fitness_nutrition.parse_meal_input(
            raw_text="съел батончик bombbar фисташка 120 г",
            db=self.db,
            user_id=self.u_id,
            allow_web_lookup=False,
        )
        self.assertTrue(repeat_label["ok"])
        self.assertEqual(repeat_label["lookup_source"], "my_label")
        self.assertEqual(repeat_label["kcal"], 384)

    async def test_3_voice_and_text_routing_backcompat_and_undo(self) -> None:
        now = datetime.datetime(2026, 11, 23, 12, 0)
        ld = local_date_str(now, "Europe/Moscow")

        # 1. Existing backcompat: "я поел" stays pulse_log
        o_pulse = await inbox_service.capture_and_process_text(
            self.db, self.user, chat_id=1, message_id=10, text="я поел", now_utc=now
        )
        self.assertEqual(o_pulse.actions[0]["item_kind"], "pulse_log")
        self.assertEqual(len(await repo.list_meal_logs(self.db, self.u_id, ld)), 0)

        # 2. Existing backcompat: "съел огурцы" when Огурцы in pantry -> removes from pantry
        await repo.create_pantry_item(self.db, self.u_id, "Огурцы", 2, "шт", None, "овощи")
        o_pan = await inbox_service.capture_and_process_text(
            self.db, self.user, chat_id=1, message_id=11, text="съел огурцы", now_utc=now
        )
        self.assertIn("Убрала из холодильника", o_pan.reply_text)

        # 3. Voice meal log -> logs КБЖУ, undo removes it
        async def fake_stt(audio_bytes: bytes, **kw) -> str:
            return "съел овсянку 80 г и банан"

        voice_stub = SimpleNamespace(file_id="f1", file_unique_id="u1", duration=4, mime_type="audio/ogg")
        orig_dl = inbox_service.stt.download_telegram_voice
        inbox_service.stt.download_telegram_voice = AsyncMock(return_value=(b"oggbytes", None))
        try:
            o_voice = await inbox_service.capture_and_process_voice(
                self.db, None, self.user, chat_id=1, message_id=12, voice=voice_stub, now_utc=now, stt_fn=fake_stt
            )
        finally:
            inbox_service.stt.download_telegram_voice = orig_dl

        self.assertIn("Расшифровала", o_voice.reply_text)
        self.assertEqual(o_voice.actions[0]["item_kind"], "meal_log")
        meals = await repo.list_meal_logs(self.db, self.u_id, ld)
        self.assertEqual(len(meals), 1)
        self.assertGreater(meals[0]["kcal"], 300)
        await inbox_service.undo_inbox_message(self.db, self.user, o_voice.inbox_id)
        self.assertEqual(len(await repo.list_meal_logs(self.db, self.u_id, ld)), 0)

        # 4. Weight & workout voice/text logs + undo
        o_w = await inbox_service.capture_and_process_text(
            self.db, self.user, chat_id=1, message_id=13, text="взвесился 84.3 жир 22.1% мышцы 60.8", now_utc=now
        )
        self.assertEqual(o_w.actions[0]["item_kind"], "weight_log")
        self.assertAlmostEqual((await repo.get_latest_weight(self.db, self.u_id))["weight"], 84.3, places=1)
        await inbox_service.undo_inbox_message(self.db, self.user, o_w.inbox_id)
        self.assertAlmostEqual((await repo.get_latest_weight(self.db, self.u_id))["weight"], 85.0, places=1)

        o_tr = await inbox_service.capture_and_process_text(
            self.db, self.user, chat_id=1, message_id=14, text="сделал 20 минут с гантелями", now_utc=now
        )
        self.assertEqual(o_tr.actions[0]["item_kind"], "workout_log")
        self.assertEqual((await repo.workout_stats_week(self.db, self.u_id, ld))["count"], 1)
        await inbox_service.undo_inbox_message(self.db, self.user, o_tr.inbox_id)
        self.assertEqual((await repo.workout_stats_week(self.db, self.u_id, ld))["count"], 0)

    async def test_4_lazy_human_clarification_flows_and_autocommit(self) -> None:
        now = datetime.datetime.utcnow()
        ld = local_date_str(now, "Europe/Moscow")

        # Flow A: Vague meal -> natural_handler passes lazy "похуй" to inbox -> approximate commit
        o_v1 = await inbox_service.capture_and_process_text(
            self.db, self.user, chat_id=940001, message_id=20, text="поел блины", now_utc=now
        )
        self.assertEqual(o_v1.status, "needs_clarification")
        self.assertEqual(o_v1.actions[0]["item_kind"], "pending_meal")
        self.assertEqual(len(await repo.list_meal_logs(self.db, self.u_id, ld)), 0)

        msg_lazy = SimpleNamespace(
            text="похуй",
            message_id=21,
            chat=SimpleNamespace(id=940001),
            from_user=SimpleNamespace(id=940001, full_name="Дима"),
            answer=AsyncMock(),
        )
        await natural.natural_handler(msg_lazy, self.db, self._fsm())
        self.assertIn("Без заморочек", msg_lazy.answer.call_args.args[0])
        self.assertIsNone(await repo.get_active_pending_meal(self.db, self.u_id))
        self.assertEqual(len(await repo.list_meal_logs(self.db, self.u_id, ld)), 1)

        # Flow B: Vague meal -> clarification ("300 мл с курицей") -> refined commit
        await inbox_service.capture_and_process_text(
            self.db, self.user, chat_id=940001, message_id=22, text="поел суп", now_utc=now
        )
        o_ref = await inbox_service.capture_and_process_text(
            self.db, self.user, chat_id=940001, message_id=23, text="300 мл с курицей", now_utc=now
        )
        self.assertIn("Уточнила и записала", o_ref.reply_text)
        self.assertEqual(len(await repo.list_meal_logs(self.db, self.u_id, ld)), 2)

        # Flow C: Unrelated message auto-commits pending meal + processes new intent
        await inbox_service.capture_and_process_text(
            self.db, self.user, chat_id=940001, message_id=24, text="съел торт", now_utc=now
        )
        o_switch = await inbox_service.capture_and_process_text(
            self.db, self.user, chat_id=940001, message_id=25, text="надо купить тофу", now_utc=now
        )
        self.assertIn("Прошлый приём записала примерно", o_switch.reply_text)
        self.assertEqual(o_switch.actions[0]["item_kind"], "shopping")
        self.assertEqual(len(await repo.list_meal_logs(self.db, self.u_id, ld)), 3)

        # Flow D: Scheduler silent auto-commit after auto_commit_after passes
        old_t = datetime.datetime.utcnow() - datetime.timedelta(minutes=25)
        await inbox_service.capture_and_process_text(
            self.db, self.user, chat_id=940001, message_id=26, text="выпил кофе с булочкой", now_utc=old_t
        )
        sched = ReminderScheduler(SimpleNamespace(send_message=AsyncMock()), self.db)
        auto_n = await sched._tick_pending_meals(now_utc=datetime.datetime.utcnow())
        self.assertEqual(auto_n, 1)
        self.assertIsNone(await repo.get_active_pending_meal(self.db, self.u_id))

    async def test_5_meal_check_unsolicited_label_photo_and_coach_handlers(self) -> None:
        now = datetime.datetime.utcnow()
        ld = local_date_str(now, "Europe/Moscow")

        # 1. Meal check must NOT log immediately
        o_chk = await inbox_service.capture_and_process_text(
            self.db,
            self.user,
            chat_id=940001,
            message_id=30,
            text="думаю съесть мороженое, можно?",
            now_utc=now,
        )
        self.assertEqual(o_chk.status, "answered")
        self.assertEqual(len(await repo.list_meal_logs(self.db, self.u_id, ld)), 0)
        cbs = [btn.callback_data for row in o_chk.reply_markup.inline_keyboard for btn in row]
        half_cb = next(c for c in cbs if c.startswith("kbju:pcommit:") and c.endswith(":0.5"))

        # Tapping half portion commits cleanly
        cb_half = SimpleNamespace(
            data=half_cb,
            from_user=SimpleNamespace(id=940001, full_name="Дима"),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await nutrition.kbju_pending_commit_cb(cb_half, self._fsm(), self.db)
        meals = await repo.list_meal_logs(self.db, self.u_id, ld)
        self.assertEqual(len(meals), 1)

        # 2. Unsolicited nutrition-label photo refines an active pending meal & saves product memory
        await inbox_service.capture_and_process_text(
            self.db, self.user, chat_id=940001, message_id=31, text="съел торт", now_utc=now
        )
        fake_label_llm = _FakeLLM(
            json_payload={
                "title": "Медовик ВкусВилл",
                "meal_slot": "snack",
                "kcal": 340,
                "protein_g": 5.5,
                "fat_g": 16.0,
                "carbs_g": 44.0,
                "is_nutrition_label": True,
                "per_100g": {"kcal": 340, "protein_g": 5.5, "fat_g": 16.0, "carbs_g": 44.0},
                "serving_g": 120.0,
                "brand_or_place": "ВкусВилл",
                "confidence": "high",
                "needs_clarification": False,
                "clarification_question": "",
            }
        )

        async def fake_dl(file_obj, destination=None):
            if isinstance(destination, io.BytesIO):
                destination.write(b"fakejpgbytes")
            return destination

        msg_photo = SimpleNamespace(
            photo=[SimpleNamespace(file_id="ph1")],
            caption="120 г",
            from_user=SimpleNamespace(id=940001, full_name="Дима"),
            bot=SimpleNamespace(download=fake_dl),
            answer=AsyncMock(),
        )
        with patch.object(fitness_nutrition, "default_llm_client", fake_label_llm):
            await inbox.handle_unsolicited_photo(msg_photo, self._fsm(), self.db)
        self.assertIn("408 ккал", msg_photo.answer.call_args.args[0])
        self.assertIsNotNone(await repo.find_user_food_product(self.db, self.u_id, "медовик вкусвилл"))
        self.assertIsNone(await repo.get_active_pending_meal(self.db, self.u_id))
        self.assertEqual(len(await repo.list_meal_logs(self.db, self.u_id, ld)), 2)

        # 3. Coach mode & mindful switch handlers
        cb_goal = SimpleNamespace(
            data="fit:set_goal:mindful",
            from_user=SimpleNamespace(id=940001, full_name="Дима"),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await movement.fit_set_goal_cb(cb_goal, self.db)
        fresh = dict(await repo.get_user(self.db, self.u_id))
        self.assertEqual(fresh["weight_goal"], "mindful")
        kb_text, _ = await nutrition.render_kbju_card(self.db, fresh, now_utc=now)
        self.assertIn("Осознанный режим", kb_text)

    async def test_6_nutrition_evening_scheduler_and_mcp(self) -> None:
        # Prepare user meal & label memory
        await repo.update_user_coach_settings(
            self.db, self.u_id, weight_goal="loss", coach_mode="balanced", nutrition_checkin_enabled=True
        )
        now_ev = datetime.datetime(2026, 11, 23, 18, 30)  # 21:30 Europe/Moscow (90m before 23:00)
        ld = local_date_str(now_ev, "Europe/Moscow")
        await repo.add_meal_log(
            self.db, self.u_id, ld, "Овсянка", kcal=350, protein_g=12, fat_g=8, carbs_g=56
        )
        await repo.upsert_user_food_product(
            self.db,
            self.u_id,
            "тофу копченый",
            "Тофу копчёный",
            per_100g={"kcal": 145, "protein_g": 16, "fat_g": 8, "carbs_g": 2},
        )

        bot_mock = SimpleNamespace(send_message=AsyncMock())
        sched = ReminderScheduler(bot_mock, self.db)
        sent1 = await sched._tick_nutrition_evening(now_utc=now_ev)
        self.assertEqual(sent1, 1)
        self.assertEqual(bot_mock.send_message.call_count, 1)

        # Second run same day does not spam
        sent2 = await sched._tick_nutrition_evening(now_utc=now_ev + datetime.timedelta(minutes=15))
        self.assertEqual(sent2, 0)

        # MCP response exposes coach fields and label count
        cfg = MCPSettings(
            host="127.0.0.1",
            port=0,
            public_base_url="http://127.0.0.1",
            owner_telegram_id=940001,
            access_password="test",
            bearer_token="tok_mcp",
            allow_diary=True,
            readonly=False,
            access_token_ttl_sec=3600,
            refresh_token_ttl_sec=86400,
        )
        u_fresh = dict(await repo.get_user(self.db, self.u_id))
        mcp_out = await _tool_get_fitness_nutrition(self.db, u_fresh, cfg, {})
        self.assertEqual(mcp_out["body_profile"]["coach_mode"], "balanced")
        self.assertEqual(mcp_out["remembered_products_count"], 1)


if __name__ == "__main__":
    unittest.main()
