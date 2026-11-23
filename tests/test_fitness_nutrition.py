import datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from config import MCPSettings
from db import repositories as repo
from db.database import connect, init_db
from handlers import kitchen, movement, nutrition
from llm_client import LLMResult
from mcp_server.app import create_mcp_app
from services import fitness_nutrition, kitchen_ai


class _MemFSM:
    def __init__(self) -> None:
        self._state = None
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


class _FakeLLM:
    def __init__(self, payload: dict, *, configured: bool = True) -> None:
        self._payload = payload
        self._configured = configured
        self.prompts: list[tuple[str, str, list[str] | None]] = []

    def is_configured(self) -> bool:
        return self._configured

    async def complete_json(
        self, system_prompt: str, user_prompt: str, **kwargs
    ) -> LLMResult:
        self.prompts.append((system_prompt, user_prompt, kwargs.get("image_data_urls")))
        return LLMResult(ok=True, text=json.dumps(self._payload), data=self._payload)


class FitnessNutritionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.u1_id = await repo.create_user(
            self.db,
            telegram_id=930001,
            name="Дима",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:30",
        )
        self.u2_id = await repo.create_user(
            self.db,
            telegram_id=930002,
            name="Алиса",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:30",
        )
        hid = await repo.get_or_create_household(self.db, self.u1_id)
        await repo.set_user_household(self.db, self.u2_id, hid)
        await repo.upsert_wellness(self.db, self.u1_id, meal_profile="vegan")
        await repo.upsert_wellness(self.db, self.u2_id, meal_profile="vegan")
        await repo.update_user_body(
            self.db,
            self.u1_id,
            height_cm=180,
            weight_goal="loss",
            weight_target=78.0,
            age_years=28,
            sex="male",
            activity_level="light",
            equipment=["smart_scales", "dumbbells", "pullup_bar", "bands", "mat"],
        )
        await repo.add_weight(self.db, self.u1_id, 86.0)
        self.u1 = dict(await repo.get_user(self.db, self.u1_id))
        self.u2 = dict(await repo.get_user(self.db, self.u2_id))

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def test_1_schema_targets_and_workout_bonus(self) -> None:
        latest = await repo.get_latest_weight(self.db, self.u1_id)
        self.assertIsNotNone(latest)
        self.assertEqual(latest["weight"], 86.0)

        # Legacy negative weight row from set_body_profile must be ignored
        await repo.set_body_profile(self.db, self.u1_id, 180.0, "loss")
        latest_after = await repo.get_latest_weight(self.db, self.u1_id)
        self.assertEqual(latest_after["weight"], 86.0)

        base_targets = fitness_nutrition.calculate_daily_targets(self.u1, latest_after)
        self.assertFalse(base_targets["is_estimated"])
        self.assertGreater(base_targets["kcal"], 1600)
        self.assertGreaterEqual(base_targets["protein_g"], 120)

        active_targets = fitness_nutrition.calculate_daily_targets(
            self.u1, latest_after, workout_kcal_today=200
        )
        self.assertEqual(active_targets["kcal"], base_targets["kcal"] + 100)
        self.assertEqual(active_targets["protein_g"], base_targets["protein_g"] + 10)
        self.assertEqual(active_targets["workout_bonus_kcal"], 100)

        # Incomplete profile falls back gracefully with is_estimated=True
        est_targets = fitness_nutrition.calculate_daily_targets(self.u2, None)
        self.assertTrue(est_targets["is_estimated"])
        self.assertGreaterEqual(est_targets["kcal"], 1350)

    async def test_2_recipe_macros_advice_and_ai_chef_prompt(self) -> None:
        tofu_rec = {
            "id": "tofu_bowl",
            "title": "Тофу с чечевицей",
            "base_servings": 1,
            "time_minutes": 15,
            "tags": ["vegan", "dinner"],
            "ingredients": [
                {"name": "тофу", "qty": 200, "unit": "г"},
                {"name": "чечевица", "qty": 80, "unit": "г"},
                {"name": "брокколи", "qty": 150, "unit": "г"},
            ],
        }
        nutr = fitness_nutrition.estimate_recipe_nutrition(tofu_rec)
        self.assertGreater(nutr["kcal"], 350)
        self.assertGreater(nutr["protein_g"], 35.0)

        # Add pantry and heavy fat meal log so advice tells user to boost protein & avoid fats
        await repo.create_pantry_item(self.db, self.u1_id, "тофу", 400, "г", None, "белок")
        await repo.create_pantry_item(self.db, self.u1_id, "чечевица", 300, "г", None, "крупы")
        now = datetime.datetime(2026, 11, 23, 15, 0, 0)
        today = "2026-11-23"
        await repo.add_meal_log(
            self.db,
            self.u1_id,
            today,
            "Орехи с маслом",
            kcal=950,
            protein_g=18,
            fat_g=68,
            carbs_g=65,
        )

        # Partner u2 logs stay strictly separate (0 eaten for u2!)
        u2_totals = await repo.daily_nutrition_totals(self.db, self.u2_id, today)
        self.assertEqual(u2_totals["kcal"], 0)

        advice = await fitness_nutrition.get_daily_nutrition_advice(self.db, self.u1, now_utc=now)
        self.assertTrue(advice["eat_more"])
        self.assertTrue(any("жир" in g.lower() or "масла" in g.lower() for g in advice["go_easy_on"]))

        # Save high-protein and high-fat recipes into user_recipes and ensure macro ranking runs cleanly
        await repo.upsert_user_recipe(
            self.db,
            self.u1_id,
            recipe_key="ur_hp_test",
            title="Белковый скрембл из тофу",
            tags=["vegan", "dinner", "fast"],
            base_servings=1,
            time_minutes=12,
            ingredients=tofu_rec["ingredients"],
            steps=["Обжарить тофу с чечевицей."],
            nutrition={"kcal": 480, "protein_g": 42, "fat_g": 12, "carbs_g": 44},
        )
        await repo.upsert_user_recipe(
            self.db,
            self.u1_id,
            recipe_key="ur_fat_test",
            title="Супер-жирная паста с арахисом",
            tags=["vegan", "dinner", "fast"],
            base_servings=1,
            time_minutes=12,
            ingredients=[{"name": "арахисовая паста", "qty": 100, "unit": "г"}],
            steps=["Смешать."],
            nutrition={"kcal": 980, "protein_g": 20, "fat_g": 65, "carbs_g": 70},
        )
        sug = await kitchen_ai.suggest_meals_now(self.db, self.u1, energy="normal", limit=100, now_utc=now)
        self.assertIn("daily_advice", sug)
        ids_order = [s["recipe"]["id"] for s in sug["suggestions"]]
        self.assertLess(ids_order.index("ur_hp_test"), ids_order.index("ur_fat_test"))

        # AI Chef injects КБЖУ advice and preserves nutrition on recipe
        fake_llm = _FakeLLM(
            {
                "recipes": [
                    {
                        "title": "Боул с тофу и брокколи",
                        "desc": "Лёгкий белковый ужин",
                        "time_minutes": 15,
                        "base_servings": 2,
                        "tags": ["vegan", "dinner"],
                        "nutrition": {"kcal": 420, "protein_g": 28, "fat_g": 11, "carbs_g": 46},
                        "ingredients": [{"name": "тофу", "qty": 200, "unit": "г"}],
                        "steps": ["Приготовить тофу."],
                    }
                ]
            }
        )
        ai_res = await kitchen_ai.generate_ai_recipes_from_pantry(
            self.db, self.u1, llm_client_instance=fake_llm, now_utc=now
        )
        self.assertTrue(ai_res["ok"])
        self.assertIn("Ориентир по КБЖУ", fake_llm.prompts[0][1])
        self.assertEqual(ai_res["recipes"][0]["nutrition"]["kcal"], 420)

    async def test_3_smart_scales_parsing_trend_and_privacy(self) -> None:
        # Backdate an earlier scale reading so 30d trend captures recomposition
        await self.db.execute(
            """
            INSERT INTO weights (user_id, weight, body_fat_pct, muscle_mass_kg, water_pct, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (self.u1_id, 86.2, 24.5, 60.0, 54.0, "scale_text", "2026-11-10T08:00:00"),
        )
        await self.db.commit()

        parsed = await fitness_nutrition.parse_smart_scale_input(
            raw_text="86.1 жир 23.2% мышцы 61.1 вода 55.4% имт 26.6 висц 8"
        )
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["weight"], 86.1)
        self.assertEqual(parsed["body_fat_pct"], 23.2)
        self.assertEqual(parsed["muscle_mass_kg"], 61.1)
        self.assertEqual(parsed["water_pct"], 55.4)
        self.assertEqual(parsed["bmi"], 26.6)
        self.assertEqual(parsed["visceral_fat"], 8.0)

        await repo.add_weight(
            self.db,
            self.u1_id,
            parsed["weight"],
            body_fat_pct=parsed["body_fat_pct"],
            muscle_mass_kg=parsed["muscle_mass_kg"],
            water_pct=parsed["water_pct"],
            visceral_fat=parsed["visceral_fat"],
            bmi=parsed["bmi"],
            source=parsed["source"],
        )
        trend = await repo.body_metrics_trend(self.db, self.u1_id, days=30)
        self.assertAlmostEqual(trend["fat_delta"], -1.3, places=1)
        self.assertAlmostEqual(trend["muscle_delta"], 1.1, places=1)
        fb = fitness_nutrition.format_body_trend_feedback(trend, "loss")
        self.assertIn("рекомпозиция", fb.lower())

        # Privacy check: u2 (partner in same household) has 0 scale metrics
        u2_trend = await repo.body_metrics_trend(self.db, self.u2_id, days=30)
        self.assertIsNone(u2_trend["latest"])
        self.assertEqual(u2_trend["count"], 0)

    async def test_4_anti_laziness_workouts_and_handlers(self) -> None:
        eq = repo.get_user_equipment(self.u1)
        self.assertIn("dumbbells", eq)

        lazy = fitness_nutrition.build_lazy_start_workout(eq, variation=0)
        self.assertLessEqual(lazy["duration_min"], 5)
        self.assertIn("гантел", lazy["equipment_used"].lower())

        # Complete lazy workout -> logs workout, awards karma, boosts daily kcal target
        now = datetime.datetime(2026, 11, 23, 18, 0, 0)
        res = await fitness_nutrition.complete_workout(
            self.db, self.u1, lazy, is_lazy_start=True, now_utc=now
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["week_stats"]["count"], 1)
        self.assertEqual(res["week_stats"]["lazy_starts"], 1)
        self.assertGreater(res["daily_advice"]["targets"]["workout_bonus_kcal"], 0)

        # Next lazy start prioritizes another focus_area
        lazy2 = fitness_nutrition.build_lazy_start_workout(
            eq, recent_focus_areas=res["week_stats"]["recent_focus_areas"], variation=0
        )
        self.assertNotEqual(lazy2["focus_area"], lazy["focus_area"])

        # Personal workout generator works offline respecting dumbbells/pullup_bar
        w_gen = await fitness_nutrition.generate_personal_workout(
            self.db, self.u1, duration_min=20, focus_area="upper", now_utc=now
        )
        self.assertTrue(w_gen["ok"])
        self.assertEqual(w_gen["duration_min"], 20)
        self.assertTrue(len(w_gen["steps"]) >= 4)

        # Test Telegram handlers: kbju:log_recipe & fit:gear_toggle
        fsm = _MemFSM()
        cb_log = SimpleNamespace(
            data="kbju:log_recipe:omelet:1",
            from_user=SimpleNamespace(id=930001, full_name="Дима"),
            message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        await nutrition.kbju_log_recipe_cb(cb_log, self.db)
        today_str = datetime.date.today().isoformat()
        # Check at least 1 meal logged for u1 in user's local date
        from utils.time import local_date_str
        ld = local_date_str(datetime.datetime.utcnow(), "Europe/Moscow")
        u1_meals = await repo.list_meal_logs(self.db, self.u1_id, ld)
        self.assertEqual(len(u1_meals), 1)
        self.assertGreater(u1_meals[0]["kcal"], 100)

        cb_gear = SimpleNamespace(
            data="fit:gear_toggle:cardio",
            from_user=SimpleNamespace(id=930001, full_name="Дима"),
            message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        await movement.fit_gear_toggle(cb_gear, self.db)
        u1_after = dict(await repo.get_user(self.db, self.u1_id))
        self.assertIn("cardio", repo.get_user_equipment(u1_after))
        await fsm.clear()

    async def test_5_mcp_fitness_nutrition_and_readonly_guard(self) -> None:
        cfg = MCPSettings(
            host="127.0.0.1",
            port=0,
            public_base_url="http://127.0.0.1",
            owner_telegram_id=930001,
            access_password="test",
            bearer_token="tok_fit_mcp_123456",
            allow_diary=True,
            readonly=False,
            access_token_ttl_sec=3600,
            refresh_token_ttl_sec=86400,
        )
        app = create_mcp_app(self.db, settings=cfg)
        server = TestServer(app)
        await server.start_server()
        try:
            async with ClientSession() as client:
                hdrs = {"Authorization": f"Bearer {cfg.bearer_token}"}
                async with client.post(
                    server.make_url("/mcp"),
                    headers=hdrs,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "hidl_log_meal", "arguments": {"text": "Гречка 150 г и тофу 150 г", "meal_slot": "lunch"}},
                    },
                ) as resp:
                    log_res = await resp.json()
                    self.assertFalse(log_res["result"]["isError"])
                    payload = json.loads(log_res["result"]["content"][0]["text"])
                    self.assertTrue(payload["ok"])
                    self.assertGreater(payload["logged"]["kcal"], 300)

                async with client.post(
                    server.make_url("/mcp"),
                    headers=hdrs,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "hidl_get_fitness_nutrition", "arguments": {}},
                    },
                ) as resp:
                    fit_res = await resp.json()
                    fit_data = json.loads(fit_res["result"]["content"][0]["text"])
                    self.assertGreater(fit_data["daily_nutrition"]["eaten"]["kcal"], 300)
                    self.assertIn("dumbbells", fit_data["equipment"])

                async with client.post(
                    server.make_url("/mcp"),
                    headers=hdrs,
                    json={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "hidl_get_kitchen", "arguments": {"servings": 2}},
                    },
                ) as resp:
                    kit_res = await resp.json()
                    kit_data = json.loads(kit_res["result"]["content"][0]["text"])
                    self.assertIn("daily_nutrition", kit_data)
        finally:
            await server.close()

        # Readonly mode hides hidl_log_meal
        ro_cfg = MCPSettings(**{**cfg.__dict__, "readonly": True})
        ro_app = create_mcp_app(self.db, settings=ro_cfg)
        ro_server = TestServer(ro_app)
        await ro_server.start_server()
        try:
            async with ClientSession() as client:
                async with client.post(
                    ro_server.make_url("/mcp"),
                    headers={"Authorization": f"Bearer {ro_cfg.bearer_token}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                ) as resp:
                    ro_list = await resp.json()
                    names = {t["name"] for t in ro_list["result"]["tools"]}
                    self.assertNotIn("hidl_log_meal", names)
                    self.assertIn("hidl_get_fitness_nutrition", names)
        finally:
            await ro_server.close()


if __name__ == "__main__":
    unittest.main()
