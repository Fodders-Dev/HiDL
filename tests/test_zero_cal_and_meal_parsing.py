import datetime
import json
import unittest

from db import repositories as repo
from db.database import connect, init_db
from llm_client import LLMResult
from services import fitness_nutrition, inbox_service, nutrition_web
from utils.time import local_date_str


class _CapturingLLM:
    def __init__(self, payload: dict, *, configured: bool = True) -> None:
        self.payload = payload
        self._configured = configured
        self.calls: list[tuple[str, str]] = []

    def is_configured(self) -> bool:
        return self._configured

    async def complete_json(self, system_prompt: str, user_prompt: str, **kwargs) -> LLMResult:
        self.calls.append((system_prompt, user_prompt))
        return LLMResult(ok=True, text=json.dumps(self.payload), data=self.payload)


class ZeroCalAndMealParsingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.u_id = await repo.create_user(
            self.db,
            telegram_id=955001,
            name="Дима",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:00",
        )
        await repo.upsert_wellness(self.db, self.u_id, meal_profile="vegan")
        self.user = dict(await repo.get_user(self.db, self.u_id))

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def test_1_screenshot_repro_samokat_zero_cola(self) -> None:
        raw = (
            "Кола без сахара, без калорий, около 1 стакана (Самокат). "
            "запивал это колой без сахара, без калорий от самоката. Наверное, стакан выпил. Где-то так."
        )
        hint = "Кола без сахара, без калорий, около 1 стакана (Самокат)"
        res = await fitness_nutrition.parse_meal_input(
            raw_text=raw,
            title_hint=hint,
            db=self.db,
            user_id=self.u_id,
            allow_web_lookup=False,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["kcal"], 0)
        self.assertEqual(res["protein_g"], 0.0)
        self.assertEqual(res["fat_g"], 0.0)
        self.assertEqual(res["carbs_g"], 0.0)
        self.assertFalse(res.get("needs_clarification"))
        self.assertNotIn("запивал это к", res["title"])
        self.assertIn("Кола без сахара", res["title"])

    async def test_2_zero_cal_variants_vs_caloric_drinks(self) -> None:
        zero_cases = [
            "пепси зеро 330 мл",
            "чай без сахара",
            "чёрный кофе без сахара",
            "минеральная вода 500 мл",
            "кола лайт",
        ]
        for text in zero_cases:
            res = await fitness_nutrition.parse_meal_input(
                raw_text=text, allow_web_lookup=False, llm_client_instance=_CapturingLLM({}, configured=False)
            )
            self.assertTrue(res["ok"], text)
            self.assertEqual(res["kcal"], 0, f"expected 0 kcal for {text}, got {res}")

        caloric_cases = [
            ("кофе со сгущёнкой", 10),
            ("латте без сахара", 50),
            ("яблоко и чай без сахара", 50),
            ("кола 250 мл", 80),
        ]
        for text, min_kcal in caloric_cases:
            res = await fitness_nutrition.parse_meal_input(
                raw_text=text, allow_web_lookup=False, llm_client_instance=_CapturingLLM({}, configured=False)
            )
            self.assertTrue(res["ok"], text)
            self.assertGreaterEqual(res["kcal"], min_kcal, f"expected >= {min_kcal} for {text}, got {res}")
            self.assertLess(res["kcal"], 400, f"unexpected huge kcal for {text}: {res}")

    def test_3_negation_guard_and_normalize_allow_zero(self) -> None:
        self.assertNotEqual(fitness_nutrition._match_nutrition_key("кола без сахара"), "сахар")
        self.assertEqual(fitness_nutrition._match_nutrition_key("салат без масла"), "салат")
        self.assertEqual(fitness_nutrition._match_nutrition_key("творог без сахара"), "творог")

        self.assertIsNone(
            fitness_nutrition._normalize_nutrition_dict(
                {"title": "Кола зеро", "kcal": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0},
                allow_zero=False,
            )
        )
        zero_ok = fitness_nutrition._normalize_nutrition_dict(
            {"title": "Кола зеро", "kcal": 0, "protein_g": 0, "fat_g": 0, "carbs_g": 0},
            allow_zero=True,
        )
        self.assertIsNotNone(zero_ok)
        self.assertEqual(zero_ok["kcal"], 0)
        self.assertIsNone(fitness_nutrition._normalize_nutrition_dict({}, allow_zero=True))

        scaled = nutrition_web.scale_lookup_record(
            {
                "title": "Coca-Cola Zero",
                "source": "openfoodfacts",
                "per_100g": {"kcal": 0.0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0},
                "serving_g": 330.0,
                "kcal_serving": 0.0,
            },
            "330 мл",
        )
        self.assertIsNotNone(scaled)
        self.assertEqual(scaled["kcal"], 0)

    async def test_4_persist_meal_injects_vegan_and_recent_meals_and_cleans_title(self) -> None:
        now = datetime.datetime(2026, 11, 25, 13, 0, 0)
        yesterday = "2026-11-24"
        today = local_date_str(now, "Europe/Moscow")

        await repo.add_meal_log(
            self.db,
            self.u_id,
            yesterday,
            "Спагетти с фаршем по-флотски",
            meal_slot="dinner",
            kcal=525.0,
            protein_g=21.0,
            fat_g=19.0,
            carbs_g=66.0,
            source="voice",
        )
        inbox_row, _ = await repo.create_inbox_message_if_new(
            self.db,
            user_id=self.u_id,
            chat_id=955001,
            telegram_message_id=101,
            source="voice",
            local_date=today,
            raw_text="Я пообедал спагетти с фаршем Хай, то что я вчера ел на ужин",
        )

        fake_llm = _CapturingLLM(
            {
                "title": "Спагетти с растительным фаршем Hi",
                "meal_slot": "lunch",
                "kcal": 525,
                "protein_g": 21.0,
                "fat_g": 19.0,
                "carbs_g": 66.0,
                "is_nutrition_label": False,
                "per_100g": None,
                "serving_g": 350.0,
                "brand_or_place": "Hi!",
                "confidence": "high",
                "needs_clarification": False,
                "clarification_question": "",
            }
        )
        await inbox_service._persist_meal_item(
            self.db,
            self.user,
            inbox_row,
            source_quote="то что я вчера ел на ужин, порцией спагетти с фаршем хай",
            title_hint="Спагетти с фаршем Hi",
            local_date=today,
            now_utc=now,
            llm_client_instance=fake_llm,
            allow_clarification=True,
        )
        self.assertEqual(len(fake_llm.calls), 1)
        prompt_sent = fake_llm.calls[0][1]
        self.assertIn("Профиль питания пользователя: vegan", prompt_sent)
        self.assertIn("Спагетти с фаршем по-флотски", prompt_sent)

        meals_today = await repo.list_meal_logs(self.db, self.u_id, today)
        self.assertEqual(len(meals_today), 1)
        self.assertEqual(meals_today[0]["kcal"], 525.0)


if __name__ == "__main__":
    unittest.main()
