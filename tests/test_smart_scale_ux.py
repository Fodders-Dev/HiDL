import base64
import datetime
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from db import repositories as repo
from db.database import connect, init_db
from handlers import inbox, movement
from llm_client import LLMResult
from scheduler.reminder import ReminderScheduler
from services import advice_service, coach_knowledge, fitness_nutrition


class _MemFSM:
    def __init__(self, initial=None) -> None:
        self._state = initial
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


class _QueuedLLM:
    def __init__(self, responses: list[dict], *, configured: bool = True) -> None:
        self._responses = list(responses)
        self._configured = configured
        self.calls: list[tuple[str, str, list[str] | None]] = []

    def is_configured(self) -> bool:
        return self._configured

    async def complete_json(self, system_prompt: str, user_prompt: str, **kwargs) -> LLMResult:
        self.calls.append((system_prompt, user_prompt, kwargs.get("image_data_urls")))
        payload = self._responses.pop(0) if self._responses else {}
        return LLMResult(ok=True, text=json.dumps(payload), data=payload)

    async def complete_text(self, system_prompt: str, user_prompt: str, **kwargs) -> LLMResult:
        self.calls.append((system_prompt, user_prompt, None))
        return LLMResult(ok=True, text="Держи умеренный дефицит и начни с 3-мин старта.", data=None)


def _make_png(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), color=(24, 32, 36))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class SmartScaleUXTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.u_id = await repo.create_user(
            self.db,
            telegram_id=950001,
            name="Фоддер",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:30",
        )
        await repo.upsert_wellness(self.db, self.u_id, meal_profile="vegan")
        await repo.update_user_body(
            self.db,
            self.u_id,
            height_cm=180,
            weight_goal="loss",
            weight_target=80.0,
            age_years=30,
            sex="male",
            activity_level="light",
            equipment=["smart_scales", "dumbbells", "mat"],
        )
        self.user = dict(await repo.get_user(self.db, self.u_id))

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def test_1_tall_image_tiling_and_low_res_warning(self) -> None:
        tall_hi = _make_png(1080, 5800)
        urls_hi, meta_hi = fitness_nutrition._inspect_and_tile_scale_image(tall_hi, "image/png")
        self.assertTrue(meta_hi["tiled"])
        self.assertTrue(3 <= len(urls_hi) <= 4)
        self.assertFalse(meta_hi["is_low_res_tall"])
        self.assertTrue(fitness_nutrition.is_tall_scale_like_image(tall_hi))

        tall_lo = _make_png(177, 1024)
        urls_lo, meta_lo = fitness_nutrition._inspect_and_tile_scale_image(tall_lo, "image/png")
        self.assertTrue(meta_lo["tiled"])
        self.assertTrue(meta_lo["is_low_res_tall"])
        self.assertTrue(all(u.startswith("data:image/png;base64,") for u in urls_lo))
        # Upscaled tile width should be 354px (2x of 177px)
        first_tile_bytes = base64.b64decode(urls_lo[0].split(",", 1)[1])
        with Image.open(io.BytesIO(first_tile_bytes)) as t_im:
            self.assertEqual(t_im.size[0], 354)

        llm_only_weight = _QueuedLLM([{"is_scale_screen": True, "weight": 99.2}])
        res_lo_warn = await fitness_nutrition.parse_smart_scale_input(
            image_bytes=tall_lo, image_mime="image/png", llm_client_instance=llm_only_weight
        )
        self.assertTrue(res_lo_warn["ok"])
        self.assertTrue(res_lo_warn["low_res_warning"])
        self.assertEqual(len(llm_only_weight.calls[0][2]), len(urls_lo))

        llm_rich = _QueuedLLM(
            [
                {
                    "is_scale_screen": True,
                    "weight": 99.2,
                    "body_fat_pct": 29.0,
                    "muscle_mass_kg": 66.6,
                    "water_pct": 50.3,
                    "visceral_fat": 15,
                    "bmi": 30.6,
                }
            ]
        )
        res_lo_rich = await fitness_nutrition.parse_smart_scale_input(
            image_bytes=tall_lo, image_mime="image/png", llm_client_instance=llm_rich
        )
        self.assertTrue(res_lo_rich["ok"])
        self.assertFalse(res_lo_rich["low_res_warning"])

        std_shot = _make_png(1080, 1920)
        urls_std, meta_std = fitness_nutrition._inspect_and_tile_scale_image(std_shot, "image/png")
        self.assertEqual(len(urls_std), 1)
        self.assertFalse(meta_std["tiled"])
        self.assertFalse(fitness_nutrition.is_tall_scale_like_image(std_shot))

        # Corrupt / fake bytes in tests fall back safely
        urls_fake, meta_fake = fitness_nutrition._inspect_and_tile_scale_image(b"not-an-image", "image/jpeg")
        self.assertEqual(len(urls_fake), 1)
        self.assertFalse(meta_fake["tiled"])

    async def test_2_katch_mcardle_lean_mass_and_partial_merge(self) -> None:
        targets = fitness_nutrition.calculate_daily_targets(
            self.user, {"weight": 99.2, "body_fat_pct": 29.0}
        )
        self.assertEqual(targets["bmr_formula"], "katch_mcardle")
        self.assertEqual(targets["lean_mass_kg"], 70.4)
        self.assertEqual(targets["bmr"], 1891)
        self.assertEqual(targets["protein_g"], 134)
        self.assertEqual(targets["fat_g"], 67)

        non_scale_llm = _QueuedLLM([{"is_scale_screen": False, "weight": 85.0}])
        rejected = await fitness_nutrition.parse_smart_scale_input(
            image_bytes=_make_png(800, 800),
            llm_client_instance=non_scale_llm,
            require_scale_screen=True,
        )
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"], "not_scale_screen")

        # Screen 1 then Screen 2 merge within 120m
        await repo.add_weight(
            self.db, self.u_id, 99.2, body_fat_pct=29.0, bmi=30.6, source="scale_photo"
        )
        merged = await repo.merge_recent_weight_metrics(
            self.db, self.u_id, muscle_mass_kg=66.6, water_pct=50.3, visceral_fat=15.0
        )
        self.assertIsNotNone(merged)
        self.assertEqual(merged["weight"], 99.2)
        self.assertEqual(merged["body_fat_pct"], 29.0)
        self.assertEqual(merged["bmi"], 30.6)
        self.assertEqual(merged["muscle_mass_kg"], 66.6)
        self.assertEqual(merged["water_pct"], 50.3)
        self.assertEqual(merged["visceral_fat"], 15.0)

        # Too old entry (>120 min) must NOT be merged
        old_now = datetime.datetime.utcnow() + datetime.timedelta(minutes=180)
        too_late = await repo.merge_recent_weight_metrics(
            self.db, self.u_id, muscle_mass_kg=60.0, now_utc=old_now
        )
        self.assertIsNone(too_late)

    async def test_3_inbox_document_and_two_screen_captionless_flow(self) -> None:
        tall_doc = _make_png(1080, 5600)
        doc_llm = _QueuedLLM(
            [
                {
                    "is_scale_screen": True,
                    "weight": 99.2,
                    "body_fat_pct": 29.0,
                    "muscle_mass_kg": 66.6,
                    "water_pct": 50.3,
                    "visceral_fat": 15,
                    "bmi": 30.6,
                }
            ]
        )

        async def _dl(file_obj, destination=None):
            data = getattr(file_obj, "_data", b"")
            if isinstance(destination, io.BytesIO):
                destination.write(data)
            return destination

        msg_doc = SimpleNamespace(
            document=SimpleNamespace(
                file_id="doc_xiaomi",
                mime_type="image/png",
                file_name="xiaomi_report.png",
                file_size=len(tall_doc),
                _data=tall_doc,
            ),
            caption="",
            from_user=SimpleNamespace(id=950001, full_name="Фоддер"),
            bot=SimpleNamespace(download=_dl),
            answer=AsyncMock(),
        )
        with patch.object(fitness_nutrition, "default_llm_client", doc_llm):
            await inbox.handle_unsolicited_image_document(msg_doc, _MemFSM(), self.db)

        # Tall image goes directly to scale parser (1 LLM call, NOT meal first)
        self.assertEqual(len(doc_llm.calls), 1)
        reply_doc = msg_doc.answer.call_args.args[0]
        self.assertIn("99.2 кг", reply_doc)
        self.assertIn("66.6 кг", reply_doc)
        self.assertIn("висц. жир", reply_doc)
        self.assertIn("+3 кармы", reply_doc)

        # Clean weights for 2-screen uncaptioned photo test on the SAME day (karma shouldn't double-count)
        await self.db.execute("DELETE FROM weights WHERE user_id = ?", (self.u_id,))
        await self.db.commit()

        screen1 = _make_png(1080, 1920)
        screen2 = _make_png(1080, 1920)
        two_screen_llm = _QueuedLLM(
            [
                # Screen 1: meal fails -> scale extracts top half
                {"title": ""},
                {"is_scale_screen": True, "weight": 99.2, "body_fat_pct": 29.0, "bmi": 30.6},
                # Screen 2: meal fails -> scale extracts lower half without weight
                {"title": ""},
                {"is_scale_screen": True, "weight": None, "muscle_mass_kg": 66.6, "water_pct": 50.3, "visceral_fat": 15},
            ]
        )
        msg_p1 = SimpleNamespace(
            photo=[SimpleNamespace(file_id="p1", _data=screen1)],
            caption="",
            from_user=SimpleNamespace(id=950001, full_name="Фоддер"),
            bot=SimpleNamespace(download=_dl),
            answer=AsyncMock(),
        )
        msg_p2 = SimpleNamespace(
            photo=[SimpleNamespace(file_id="p2", _data=screen2)],
            caption="",
            from_user=SimpleNamespace(id=950001, full_name="Фоддер"),
            bot=SimpleNamespace(download=_dl),
            answer=AsyncMock(),
        )
        with patch.object(fitness_nutrition, "default_llm_client", two_screen_llm):
            await inbox.handle_unsolicited_photo(msg_p1, _MemFSM(), self.db)
            await inbox.handle_unsolicited_photo(msg_p2, _MemFSM(), self.db)

        self.assertNotIn("+3 кармы", msg_p1.answer.call_args.args[0])
        self.assertIn("Дополнила свежий замер", msg_p2.answer.call_args.args[0])
        rows = await repo.list_weights(self.db, self.u_id, limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["muscle_mass_kg"], 66.6)
        self.assertEqual(rows[0]["visceral_fat"], 15.0)

        # Non-image document (PDF) silently ignored
        pdf_msg = SimpleNamespace(
            document=SimpleNamespace(file_id="pdf1", mime_type="application/pdf", file_name="notes.pdf", file_size=500),
            caption="",
            from_user=SimpleNamespace(id=950001, full_name="Фоддер"),
            bot=SimpleNamespace(download=AsyncMock()),
            answer=AsyncMock(),
        )
        await inbox.handle_unsolicited_image_document(pdf_msg, _MemFSM(), self.db)
        pdf_msg.answer.assert_not_called()
        pdf_msg.bot.download.assert_not_called()

    async def test_4_movement_scale_document_partial_merge_and_playbook(self) -> None:
        narrow_tall = _make_png(177, 1024)
        llm_seq = _QueuedLLM(
            [
                {"is_scale_screen": True, "weight": 99.2},
                {"is_scale_screen": True, "weight": None, "muscle_mass_kg": 66.6, "water_pct": 50.3, "visceral_fat": 15},
            ]
        )

        async def _dl(file_obj, destination=None):
            data = getattr(file_obj, "_data", b"")
            if isinstance(destination, io.BytesIO):
                destination.write(data)
            return destination

        fsm = _MemFSM(movement.ScaleImportState.waiting_input)
        doc_msg = SimpleNamespace(
            document=SimpleNamespace(
                file_id="narrow_doc",
                mime_type="image/png",
                file_name="strip.png",
                file_size=len(narrow_tall),
                _data=narrow_tall,
            ),
            caption="",
            from_user=SimpleNamespace(id=950001, full_name="Фоддер"),
            bot=SimpleNamespace(download=_dl),
            answer=AsyncMock(),
        )
        with patch.object(fitness_nutrition, "default_llm_client", llm_seq):
            await movement.fit_scale_document(doc_msg, fsm, self.db)
        self.assertIsNone(await fsm.get_state())
        self.assertIn("как файл (без сжатия)", doc_msg.answer.call_args.args[0])

        # Now send screen 2 photo in waiting_input
        await fsm.set_state(movement.ScaleImportState.waiting_input)
        photo_msg = SimpleNamespace(
            photo=[SimpleNamespace(file_id="scr2", _data=_make_png(1080, 1920))],
            caption="",
            from_user=SimpleNamespace(id=950001, full_name="Фоддер"),
            bot=SimpleNamespace(download=_dl),
            answer=AsyncMock(),
        )
        with patch.object(fitness_nutrition, "default_llm_client", llm_seq):
            await movement.fit_scale_photo(photo_msg, fsm, self.db)
        self.assertIsNone(await fsm.get_state())
        self.assertIn("66.6 кг", photo_msg.answer.call_args.args[0])

        # Playbook navigation callbacks
        cb_home = SimpleNamespace(
            data="fit:playbook",
            from_user=SimpleNamespace(id=950001, full_name="Фоддер"),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await movement.fit_playbook_home(cb_home, self.db)
        self.assertIn("База тренера", cb_home.message.edit_text.call_args.args[0])

        cb_card = SimpleNamespace(
            data="fit:playbook_card:never_miss_twice",
            from_user=SimpleNamespace(id=950001, full_name="Фоддер"),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await movement.fit_playbook_card(cb_card, self.db)
        self.assertIn("Atomic Habits", cb_card.message.edit_text.call_args.args[0])

    async def test_5_weigh_in_scheduler_reminders_and_karma_penalty(self) -> None:
        # 8 days old weight entry -> should trigger morning reminder AND -5 karma penalty
        old_ts = (datetime.datetime.utcnow() - datetime.timedelta(days=8)).isoformat()
        await self.db.execute(
            "INSERT INTO weights (user_id, weight, body_fat_pct, visceral_fat, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (self.u_id, 100.0, 30.0, 15.0, "scale_photo", old_ts),
        )
        await self.db.commit()

        # Student user should never receive weigh-in prompts or penalties
        stu_id = await repo.create_user(
            self.db,
            telegram_id=950099,
            name="Ученик",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:00",
        )
        await repo.update_user_profile_mode(self.db, stu_id, "student")
        await self.db.execute(
            "INSERT INTO weights (user_id, weight, source, created_at) VALUES (?, ?, ?, ?)",
            (stu_id, 60.0, "manual", old_ts),
        )
        await self.db.commit()

        # 06:00 UTC = 09:00 Europe/Moscow (within wake 08:00..10:30 window)
        now_morn = datetime.datetime(2026, 11, 25, 6, 0, 0)
        bot_mock = SimpleNamespace(send_message=AsyncMock())
        sched = ReminderScheduler(bot_mock, self.db)
        sent = await sched._tick_weight_prompt(now_utc=now_morn)
        self.assertEqual(sent, 1)
        self.assertEqual(bot_mock.send_message.call_count, 1)
        call = bot_mock.send_message.call_args
        msg_sent = call.kwargs.get("text") if call.kwargs and "text" in call.kwargs else call.args[1]
        self.assertIn("Неделя без весов", msg_sent)
        self.assertIn("-5", msg_sent)

        ks = await repo.get_or_create_karma_state(self.db, self.u_id)
        self.assertEqual(ks["score"], 65)

        # Idempotent same day
        sent_again = await sched._tick_weight_prompt(now_utc=now_morn + datetime.timedelta(minutes=30))
        self.assertEqual(sent_again, 0)

    async def test_6_coach_knowledge_selection_and_advice_integration(self) -> None:
        cards = coach_knowledge.select_coach_principles(
            {
                "latest": {"weight": 99.2, "body_fat_pct": 29.0, "visceral_fat": 15.0},
                "week_workouts": {"count": 0},
                "meal_profile": "vegan",
                "weight_goal": "loss",
            },
            limit=3,
        )
        topics = {c["topic"] for c in cards}
        self.assertTrue(bool(topics & {"visceral_neat", "training_med"}))

        # Daytime (10:00 Europe/Moscow) -> actionable today + 3-min start button
        day_utc = datetime.datetime(2026, 11, 24, 7, 0, 0)
        plan_day = coach_knowledge.build_body_action_plan(
            latest={"weight": 99.2, "visceral_fat": 15.0},
            trend={"latest": {"weight": 99.2, "visceral_fat": 15.0}, "count": 1},
            week_workouts={"count": 0},
            nutrition_advice={"targets": {"protein_g": 134}, "eaten": {"kcal": 0, "protein_g": 0}},
            user={**self.user, "coach_mode": "strict"},
            meal_profile="vegan",
            now_utc=day_utc,
        )
        self.assertTrue(plan_day["suggest_lazy_workout"])
        self.assertEqual(plan_day["action_label"], "Что делать сегодня")
        self.assertIn("15", plan_day["diagnosis"])

        summary_html = fitness_nutrition.format_scale_saved_summary(
            {"weight": 99.2, "body_fat_pct": 29.0, "visceral_fat": 15.0},
            {"latest": {"weight": 99.2, "visceral_fat": 15.0}, "count": 1},
            fitness_nutrition.calculate_daily_targets(self.user, {"weight": 99.2, "body_fat_pct": 29.0}),
            "loss",
            action_plan=plan_day,
        )
        self.assertNotIn("Сегодня: Сегодня:", summary_html)
        self.assertNotIn("Eric Helms", summary_html)
        self.assertNotIn("📚", summary_html)

        # Late evening (23:17 Europe/Moscow) -> sleep-aware "На завтра", no walk tonight suggestion
        night_utc = datetime.datetime(2026, 11, 24, 20, 17, 0)
        plan_night = coach_knowledge.build_body_action_plan(
            latest={"weight": 99.2, "visceral_fat": 15.0},
            trend={"latest": {"weight": 99.2, "visceral_fat": 15.0}, "count": 1},
            week_workouts={"count": 0},
            nutrition_advice={"targets": {"protein_g": 134}, "eaten": {"kcal": 0, "protein_g": 0}},
            user={**self.user, "coach_mode": "balanced"},
            meal_profile="vegan",
            now_utc=night_utc,
        )
        self.assertEqual(plan_night["action_label"], "На завтра")
        self.assertFalse(plan_night["suggest_lazy_workout"])
        self.assertIn("спать", plan_night["action_today"].lower())

    async def test_7_kbju_coaching_question_and_reply_context(self) -> None:
        from services import inbox_service

        await repo.add_weight(self.db, self.u_id, 99.2, body_fat_pct=29.0, source="scale_photo")

        class _CoachQuestionLLM:
            def __init__(self) -> None:
                self.json_calls: list[tuple[str, str]] = []
                self.text_calls: list[tuple[str, str]] = []

            def is_configured(self) -> bool:
                return True

            async def complete_json(self, system_prompt: str, user_prompt: str, **kwargs) -> LLMResult:
                self.json_calls.append((system_prompt, user_prompt))
                # Even if router tags a question as query_type="kbju", explanatory logic routes it to AI coach!
                payload = {"intent": "query", "query_type": "kbju", "query_term": "", "reply": "", "items": []}
                return LLMResult(ok=True, text=json.dumps(payload), data=payload)

            async def complete_text(self, system_prompt: str, user_prompt: str, **kwargs) -> LLMResult:
                self.text_calls.append((system_prompt, user_prompt))
                return LLMResult(
                    ok=True,
                    text="2210 ккал — это комфортный дефицит, а твоё поддержание около 2600 ккал.",
                    data=None,
                )

        llm = _CoachQuestionLLM()
        out_q = await inbox_service.capture_and_process_text(
            self.db,
            self.user,
            chat_id=101,
            message_id=501,
            text="2210 кл это надо есть или нельзя есть больше? Если будет больше, то буду толстеть?",
            reply_context="Дополнила свежий замер: вес 99.2 кг, жир 29.0%. Ориентир КБЖУ: 2210 ккал",
            llm_client_instance=llm,
        )
        self.assertEqual(len(llm.text_calls), 1)
        coach_prompt = llm.text_calls[0][1]
        self.assertIn("TDEE", coach_prompt)
        self.assertIn("Сообщение, на которое отвечает пользователь", coach_prompt)
        self.assertIn("комфортный дефицит", out_q.reply_text)
        self.assertNotIn("Твой КБЖУ на сегодня", out_q.reply_text)

        # Short status lookup STILL opens the KBJU card without calling complete_text
        out_status = await inbox_service.capture_and_process_text(
            self.db,
            self.user,
            chat_id=101,
            message_id=502,
            text="сколько калорий осталось",
            llm_client_instance=llm,
        )
        self.assertEqual(len(llm.text_calls), 1)
        self.assertIn("Твой КБЖУ на сегодня", out_status.reply_text)

        llm_adv = _QueuedLLM([])
        reply, meta = await advice_service.build_situational_advice_reply(
            self.db,
            self.user,
            "Вес стоит и мне лень тренироваться",
            llm_client_instance=llm_adv,
        )
        self.assertTrue(meta["used_llm"])
        self.assertIn("база тренера", llm_adv.calls[0][1].lower())
        self.assertTrue(len(reply) > 10)


if __name__ == "__main__":
    unittest.main()
