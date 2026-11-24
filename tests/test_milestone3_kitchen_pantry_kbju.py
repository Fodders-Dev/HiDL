import datetime
import json
import sqlite3
import types
import unittest
from types import SimpleNamespace

from db.database import connect, init_db
from db import repositories as repo
from handlers import kitchen as kitchen_handler
from handlers import nutrition as nutrition_handler
from services import kitchen_ai, pulse_service


class _FakeMessage:
    def __init__(self, user_id: int = 999111):
        self.from_user = SimpleNamespace(id=user_id, full_name="Tester", username="tester")
        self.chat = SimpleNamespace(id=user_id)
        self.answers: list[tuple[str, dict]] = []
        self.edits: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs):
        self.answers.append((text, kwargs))
        return SimpleNamespace(message_id=101, chat=self.chat)

    async def edit_text(self, text: str, **kwargs):
        self.edits.append((text, kwargs))
        return SimpleNamespace(message_id=101, chat=self.chat)


class _FakeCallback:
    def __init__(self, data: str, user_id: int = 999111):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, full_name="Tester", username="tester")
        self.message = _FakeMessage(user_id=user_id)
        self.bot = None
        self.alerts: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.alerts.append((text, show_alert))


class _FakeFSM:
    def __init__(self, initial: dict | None = None):
        self.data = dict(initial or {})
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, st):
        self.state = st

    async def clear(self):
        self.data.clear()
        self.state = None


async def _init_db():
    conn = await connect(":memory:")
    await init_db(conn)
    return conn


class Milestone3KitchenPantryKBJUTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await _init_db()
        self.user_id = await repo.create_user(self.db, 555003, "Тест Кухня", "Europe/Moscow", "07:00", "23:30")
        self.user = dict(await repo.get_user(self.db, self.user_id))
        await repo.upsert_wellness(self.db, self.user_id, meal_profile="vegan")

    async def asyncTearDown(self):
        await self.db.close()

    def test_russian_case_stemming_and_canonical_matching(self):
        self.assertTrue(kitchen_ai.ingredients_match("Консервированной белой фасоли", "Белая фасоль"))
        self.assertTrue(kitchen_ai.ingredients_match("Овсяного молока", "Овсяное молоко"))
        self.assertTrue(kitchen_ai.ingredients_match("Оливкового масла", "Масло растительное"))
        self.assertTrue(kitchen_ai.ingredients_match("Миндаля", "Миндаль"))
        self.assertFalse(kitchen_ai.ingredients_match("Овсяное молоко", "Коровье молоко"))
        self.assertFalse(kitchen_ai.ingredients_match("Арахисовая паста", "Макароны"))

    def test_merge_missing_preserves_matching_other_units_and_parse_amount(self):
        merged = kitchen_ai._merge_missing_items([
            {"name": "корица молотая", "qty": 1, "unit": "ч.л."},
            {"name": "корица", "qty": 2, "unit": "ч.л."},
        ], preserve_staples=True)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["qty"], 3.0)
        self.assertEqual(kitchen_handler._parse_amount_unit("300 г"), (300.0, "g"))
        self.assertEqual(kitchen_handler._parse_amount_unit("300г"), (300.0, "g"))
        self.assertEqual(kitchen_handler._parse_amount_unit("1.5 л"), (1.5, "l"))

    async def test_quantity_shortfall_in_recipe_and_meal_plan_and_add_shop(self):
        await repo.create_pantry_item(self.db, self.user_id, "фасоль белая", 50, "g", None, "белок")
        recipe = {
            "id": "r_test_short",
            "title": "Рагу из фасоли",
            "base_servings": 2,
            "ingredients": [{"name": "Консервированной белой фасоли", "qty": 200, "unit": "г"}],
        }
        match = kitchen_ai.match_recipe_to_pantry(recipe, [dict(r) for r in await repo.list_pantry_items(self.db, self.user_id)], servings=2)
        self.assertEqual(len(match["missing"]), 1)
        self.assertAlmostEqual(match["missing"][0]["qty"], 150.0, places=1)
        self.assertLess(match["coverage"], 1.0)

        plan_rows = [{"status": "planned", "ingredients": [{"name": "фасоль", "qty": 200, "unit": "г"}]}]
        plan_missing = await kitchen_ai.compute_meal_plan_missing_shopping(self.db, self.user, plan_rows)
        self.assertEqual(len(plan_missing), 1)
        self.assertAlmostEqual(plan_missing[0]["qty"], 150.0, places=1)

        shop_res = await kitchen_ai.add_missing_ingredients_to_shopping(self.db, self.user_id, plan_missing)
        self.assertEqual(len(shop_res["added"]), 1)
        self.assertAlmostEqual(shop_res["added"][0]["qty"], 150.0, places=1)

    async def test_commit_cook_partial_oil_bottle_deduction(self):
        oil_id = await repo.create_pantry_item(self.db, self.user_id, "Оливковое масло", 1.0, "шт", None, "масла")
        recipe_id = "user_oil_test"
        await repo.upsert_user_recipe(
            self.db,
            self.user_id,
            recipe_key=recipe_id,
            title="Салат с маслом",
            tags=["vegan"],
            base_servings=1,
            time_minutes=5,
            ingredients=[{"name": "Оливкового масла", "qty": 15, "unit": "мл"}],
            steps=["Смешать"],
        )
        res = await kitchen_ai.commit_cooked_recipe(self.db, self.user, recipe_id, servings=1)
        self.assertTrue(res["ok"])
        pantry = [dict(r) for r in await repo.list_pantry_items(self.db, self.user_id)]
        oil = next((p for p in pantry if int(p["id"]) == oil_id), None)
        self.assertIsNotNone(oil)
        self.assertAlmostEqual(float(oil["amount"]), 0.85, places=2)

    async def test_pantry_merge_units_shopping_scope_and_supplies_on_finish(self):
        # 1) Scope isolation between personal & household
        await repo.create_shopping_item(self.db, self.user_id, "Бананы", 1, "kg", scope="household")
        await repo.create_shopping_item(self.db, self.user_id, "Протеин личный", 1, "шт", scope="personal")
        hh_items = [dict(r) for r in await repo.list_shopping_items(self.db, self.user_id, scope="household")]
        pers_items = [dict(r) for r in await repo.list_shopping_items(self.db, self.user_id, scope="personal")]
        self.assertEqual([i["item_name"] for i in hh_items], ["Бананы"])
        self.assertEqual([i["item_name"] for i in pers_items], ["Протеин личный"])

        # 2) Duplicate bulk add merges g <-> kg cleanly
        await kitchen_ai.bulk_add_pantry_items(self.db, self.user_id, "гречка 1 кг")
        await kitchen_ai.bulk_add_pantry_items(self.db, self.user_id, "Гречка 500 г")
        pantry = [dict(r) for r in await repo.list_pantry_items(self.db, self.user_id)]
        buck = [p for p in pantry if "гречк" in p["name"].lower()]
        self.assertEqual(len(buck), 1)
        self.assertEqual(buck[0]["unit"], "kg")
        self.assertAlmostEqual(float(buck[0]["amount"]), 1.5, places=2)

        # 3) Supply item in shopping list restocks supplies without entering pantry_items
        await repo.ensure_supplies(self.db, self.user_id)
        sup_id = await repo.add_supply_item(self.db, self.user_id, "Мыло жидкое")
        await repo.update_supply_status(self.db, self.user_id, sup_id, "empty")
        shop_sup_id = await repo.create_shopping_item(
            self.db, self.user_id, "Мыло жидкое", 1, "шт", category="бытовое", scope="household"
        )
        await repo.mark_shopping_bought(self.db, self.user_id, shop_sup_id, True, scope="household")
        await repo.mark_shopping_bought(self.db, self.user_id, hh_items[0]["id"], True, scope="household")
        moved = await repo.complete_shopping_trip(self.db, self.user_id, scope="household")
        self.assertEqual(moved, 2)

        sup_row = await repo.get_supply_by_id(self.db, self.user_id, sup_id)
        self.assertEqual(sup_row["status"], "full")
        pantry_after = [dict(r) for r in await repo.list_pantry_items(self.db, self.user_id)]
        self.assertFalse(any("мыло" in p["name"].lower() for p in pantry_after))
        self.assertTrue(any("банан" in p["name"].lower() for p in pantry_after))

    async def test_meal_plan_virtual_pantry_and_kbju_pulse_closure_and_dedupe(self):
        # Test generate_meal_plan stores scaled ingredients cleanly
        await self.db.execute(
            "UPDATE users SET weight_goal = 'loss', height_cm = 178 WHERE id = ?",
            (self.user_id,),
        )
        await self.db.commit()
        await repo.add_weight(self.db, self.user_id, 98.0)
        self.user = dict(await repo.get_user(self.db, self.user_id))
        await repo.create_pantry_item(self.db, self.user_id, "тофу", 200, "g", None, "белок")
        await repo.create_pantry_item(self.db, self.user_id, "овсянка", 500, "g", None, "крупы")
        plan = await kitchen_ai.generate_meal_plan(
            self.db,
            self.user,
            days=3,
            start_date="2026-11-25",
            now_utc=datetime.datetime(2026, 11, 25, 8, 0),
        )
        self.assertGreaterEqual(len(plan["items"]), 6)

        # Open breakfast pulse slot -> kbju:log_recipe closes pulse + dedupes double-tap
        now_utc = datetime.datetime.utcnow()
        today = pulse_service.local_date_str(now_utc, self.user["timezone"])
        await repo.upsert_pulse_checkin(
            self.db,
            self.user_id,
            today,
            "breakfast",
            "food",
            now_utc.isoformat(),
            now_utc.isoformat(),
            status="pending",
        )
        cb1 = _FakeCallback("kbju:log_recipe:rice_veg:1", user_id=555003)
        await nutrition_handler.kbju_log_recipe_cb(cb1, self.db)
        logs_after_1 = await repo.list_meal_logs(self.db, self.user_id, today)
        self.assertEqual(len(logs_after_1), 1)
        chk = await repo.get_pulse_checkin(self.db, self.user_id, today, "breakfast")
        self.assertEqual(chk["status"], "done")

        # Rapid double tap must dedupe
        cb2 = _FakeCallback("kbju:log_recipe:rice_veg:1", user_id=555003)
        await nutrition_handler.kbju_log_recipe_cb(cb2, self.db)
        logs_after_2 = await repo.list_meal_logs(self.db, self.user_id, today)
        self.assertEqual(len(logs_after_2), 1)

    async def test_cook_and_log_one_tap_and_shopping_pagination(self):
        # Pagination stays under Telegram limits on large shopping lists
        big_items = [
            {"id": idx, "item_name": f"Товар {idx}", "quantity": 1, "unit": "шт", "is_bought": 0}
            for idx in range(1, 35)
        ]
        kb_p0 = kitchen_handler.shopping_list_keyboard(big_items, scope="household", page=0)
        kb_p2 = kitchen_handler.shopping_list_keyboard(big_items, scope="household", page=2)
        self.assertLessEqual(len(kb_p0.inline_keyboard), 16)
        self.assertTrue(any("➡️" in btn.text for row in kb_p0.inline_keyboard for btn in row))
        self.assertTrue(any("⬅️" in btn.text for row in kb_p2.inline_keyboard for btn in row))

        # 1-tap cook_and_log updates pantry + meal_logs + meal_plan
        await repo.create_pantry_item(self.db, self.user_id, "рис", 300, "g", None, "крупы")
        cb = _FakeCallback("kitchen:cook_and_log:rice_veg:1:1", user_id=555003)
        await kitchen_handler.cook_and_log_commit(cb, self.db)
        today = pulse_service.local_date_str(datetime.datetime.utcnow(), self.user["timezone"])
        logs = await repo.list_meal_logs(self.db, self.user_id, today)
        self.assertGreaterEqual(len(logs), 1)
        self.assertGreater(len(cb.message.edits), 0)
