from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from db import repositories as repo
from db.database import connect, init_db
from handlers import kitchen
from services import kitchen_ai
from utils.rows import rows_to_dicts


def _fake_cb(data: str, *, tg_id: int, full_name: str = "Федор", reply_markup=None):
    msg = SimpleNamespace(
        text="",
        chat=SimpleNamespace(id=tg_id),
        from_user=SimpleNamespace(id=tg_id, full_name=full_name, first_name=full_name),
        reply_markup=reply_markup,
        answer=AsyncMock(),
        edit_text=AsyncMock(),
        edit_reply_markup=AsyncMock(),
    )
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=tg_id, full_name=full_name, first_name=full_name),
        message=msg,
        answer=AsyncMock(),
    )


def _flat_callbacks(markup) -> list[str]:
    if not markup or not getattr(markup, "inline_keyboard", None):
        return []
    out: list[str] = []
    for row in markup.inline_keyboard:
        for btn in row:
            cb = getattr(btn, "callback_data", None)
            if cb:
                out.append(str(cb))
    return out


class RecipePantryAdaptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        kitchen._ADAPTED_RECIPES.clear()
        self.db = await connect(":memory:")
        self.addAsyncCleanup(self.db.close)
        await init_db(self.db)
        self.tg_id = 980011
        self.uid = await repo.create_user(self.db, self.tg_id, "Федор", "Europe/Moscow", "08:00", "23:30")
        await repo.upsert_wellness(self.db, self.uid, meal_profile="vegan")
        self.user = dict(await repo.get_user(self.db, self.uid))

    async def asyncTearDown(self) -> None:
        kitchen._ADAPTED_RECIPES.clear()

    async def _seed_toast_recipe(self) -> None:
        await repo.upsert_user_recipe(
            self.db,
            self.uid,
            recipe_key="pdf_d2cb059e6c",
            title="Белая фасоль и тост с хумусом и авокадо",
            desc="Сытный растительный завтрак",
            tags=["breakfast"],
            base_servings=1,
            time_minutes=15,
            ingredients=[
                {"name": "Фасоль белая", "qty": 150, "unit": "г"},
                {"name": "Хлеб цельнозерновой", "qty": 60, "unit": "г"},
                {"name": "Хумус", "qty": 50, "unit": "г"},
                {"name": "Авокадо", "qty": 70, "unit": "г"},
                {"name": "Миндаль", "qty": 15, "unit": "г"},
                {"name": "Базилик", "qty": 10, "unit": "г"},
                {"name": "Вяленые томаты", "qty": 10, "unit": "г"},
            ],
            steps=["Разогрейте фасоль.", "Соберите тост с хумусом, авокадо и зеленью."],
            source="pdf",
            source_label="PDF",
        )

    async def _seed_toast_pantry(self) -> None:
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Фасоль белая", amount=400.0, unit="г")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Хлеб цельнозерновой", amount=300.0, unit="г")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Хумус", amount=200.0, unit="г")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Авокадо", amount=200.0, unit="г")
        await repo.upsert_pantry_item_merged(
            self.db, self.uid, name="Шпинат мини", amount=100.0, unit="г", expires_at="2026-12-02"
        )
        await repo.upsert_pantry_item_merged(
            self.db, self.uid, name="Укроп", amount=50.0, unit="г", expires_at="2026-11-27"
        )
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Кунжут белый", amount=80.0, unit="г")

    async def test_1_offline_adapt_prioritizes_expiring_pantry_and_preserves_original(self) -> None:
        await self._seed_toast_recipe()
        await self._seed_toast_pantry()
        now = dt.datetime(2026, 11, 27, 7, 0, 0)

        with patch.object(kitchen_ai.LLMClient, "is_configured", return_value=False):
            res = await kitchen_ai.adapt_recipe_to_pantry(
                self.db, self.user, "pdf_d2cb059e6c", servings=1, now_utc=now
            )

        self.assertTrue(res["ok"])
        self.assertFalse(res["already_complete"])
        self.assertFalse(res["used_llm"])
        self.assertEqual(res["remaining_missing"], [])

        subs_by_orig = {s["missing_name"]: s for s in res["substitutions"]}
        self.assertEqual(subs_by_orig["Базилик"]["action"], "replace")
        self.assertEqual(subs_by_orig["Базилик"]["replacement"], "Укроп")
        self.assertEqual(subs_by_orig["Миндаль"]["action"], "replace")
        self.assertEqual(subs_by_orig["Миндаль"]["replacement"], "Кунжут белый")
        self.assertEqual(subs_by_orig["Вяленые томаты"]["action"], "skip")

        adapted_names = [i["name"] for i in res["adapted_recipe"]["ingredients"]]
        self.assertIn("Укроп", adapted_names)
        self.assertIn("Кунжут белый", adapted_names)
        self.assertNotIn("Миндаль", adapted_names)
        self.assertNotIn("Базилик", adapted_names)
        self.assertNotIn("Вяленые томаты", adapted_names)
        self.assertGreater(int(res["adapted_nutrition"]["kcal"]), 0)

        # Оригинал в user_recipes не изменён
        orig_db = await kitchen_ai.get_recipe_for_user(self.db, self.uid, "pdf_d2cb059e6c")
        orig_names = [i["name"] for i in orig_db["ingredients"]]
        self.assertEqual(
            orig_names,
            ["Фасоль белая", "Хлеб цельнозерновой", "Хумус", "Авокадо", "Миндаль", "Базилик", "Вяленые томаты"],
        )

    async def test_2_llm_hallucination_and_non_vegan_guardrail(self) -> None:
        await repo.upsert_user_recipe(
            self.db,
            self.uid,
            recipe_key="cream_pasta",
            title="Паста с томатами и сливками",
            tags=["lunch"],
            base_servings=1,
            time_minutes=20,
            ingredients=[
                {"name": "Спагетти", "qty": 100, "unit": "г"},
                {"name": "Томаты", "qty": 150, "unit": "г"},
                {"name": "Овсяные сливки", "qty": 150, "unit": "мл"},
                {"name": "Базилик", "qty": 10, "unit": "г"},
            ],
            steps=["Сварите пасту и добавьте соус."],
        )
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Спагетти", amount=500.0, unit="г")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Томаты", amount=400.0, unit="г")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Кокосовое молоко", amount=400.0, unit="мл")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Сливки 20%", amount=200.0, unit="мл")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Укроп", amount=40.0, unit="г")

        mock_client = SimpleNamespace(
            is_configured=lambda: True,
            complete_json=AsyncMock(
                return_value=SimpleNamespace(
                    ok=True,
                    data={
                        "substitutions": [
                            {
                                "missing_name": "Овсяные сливки",
                                "action": "replace",
                                "replacement": "Трюфельное масло",
                                "reason": "фантазия",
                            },
                            {
                                "missing_name": "Базилик",
                                "action": "replace",
                                "replacement": "Сливки 20%",
                                "reason": "не веган",
                            },
                        ],
                        "note": "тест",
                    },
                )
            ),
        )

        res = await kitchen_ai.adapt_recipe_to_pantry(
            self.db,
            self.user,
            "cream_pasta",
            servings=1,
            llm_client_instance=mock_client,
            now_utc=dt.datetime(2026, 11, 27, 8, 0, 0),
        )
        self.assertTrue(res["ok"])
        self.assertTrue(res["used_llm"])
        subs_by_orig = {s["missing_name"]: s for s in res["substitutions"]}
        self.assertEqual(subs_by_orig["Овсяные сливки"]["replacement"], "Кокосовое молоко")
        self.assertEqual(subs_by_orig["Базилик"]["replacement"], "Укроп")

    async def test_3_ui_callback_flow_adapt_apply_and_cook_deducts_substitutes(self) -> None:
        await self._seed_toast_recipe()
        await self._seed_toast_pantry()
        today_str = kitchen_ai.local_date_str(dt.datetime.utcnow(), self.user.get("timezone") or "UTC")
        await repo.upsert_meal_plan_slot(
            self.db,
            self.uid,
            today_str,
            "breakfast",
            recipe_id="pdf_d2cb059e6c",
            title="Белая фасоль и тост с хумусом и авокадо",
            status="planned",
        )

        # 1. Открываем карточку рецепта
        cb1 = _fake_cb("kitchen:cook_view:pdf_d2cb059e6c:1:all:0", tg_id=self.tg_id)
        await kitchen.view_recipe(cb1, self.db)
        text1 = cb1.message.edit_text.call_args[0][0]
        kb1 = cb1.message.edit_text.call_args[1]["reply_markup"]
        self.assertIn("Есть дома (4/7)", text1)
        self.assertIn("Не хватает (3)", text1)
        self.assertIn("kitchen:adapt:pdf_d2cb059e6c:1:all:0", _flat_callbacks(kb1))

        # 2. Жмём «✨ Заменить по холодильнику»
        cb2 = _fake_cb("kitchen:adapt:pdf_d2cb059e6c:1:all:0", tg_id=self.tg_id)
        with patch.object(kitchen_ai.LLMClient, "is_configured", return_value=False):
            await kitchen.adapt_recipe_cb(cb2, self.db)
        text2 = cb2.message.edit_text.call_args[0][0]
        kb2 = cb2.message.edit_text.call_args[1]["reply_markup"]
        self.assertIn("Укроп", text2)
        apply_cb = next(c for c in _flat_callbacks(kb2) if c.startswith("kitchen:adapt_apply:"))

        # 3. Жмём «✅ Норм, готовлю так»
        cb3 = _fake_cb(apply_cb, tg_id=self.tg_id)
        await kitchen.adapt_apply_cb(cb3, self.db)
        text3 = cb3.message.edit_text.call_args[0][0]
        kb3 = cb3.message.edit_text.call_args[1]["reply_markup"]
        self.assertIn("под холодильник", text3)
        self.assertIn("Есть дома (6/6)", text3)
        cook_ad_cb = next(c for c in _flat_callbacks(kb3) if c.startswith("kitchen:cook_and_log_ad:") and c.endswith(":1"))

        # 4. Готовим и логируем адаптированный рецепт -> списываются Укроп и Кунжут белый, закрывается слот плана
        cb4 = _fake_cb(cook_ad_cb, tg_id=self.tg_id)
        await kitchen.cook_and_log_adapted_commit(cb4, self.db)

        pantry_after = {p["name"]: float(p["amount"]) for p in rows_to_dicts(await repo.list_pantry_items(self.db, self.uid))}
        self.assertAlmostEqual(pantry_after["Укроп"], 40.0, places=2)
        self.assertAlmostEqual(pantry_after["Кунжут белый"], 65.0, places=2)
        self.assertAlmostEqual(pantry_after["Фасоль белая"], 250.0, places=2)

        plans = rows_to_dicts(await repo.list_meal_plans_range(self.db, self.uid, today_str, today_str))
        self.assertEqual(plans[0]["status"], "cooked")
        meals = rows_to_dicts(await repo.list_meal_logs(self.db, self.uid, today_str))
        self.assertEqual(len(meals), 1)
        self.assertIn("под холодильник", meals[0]["title"])

    async def test_4_boundary_already_complete_and_too_many_missing(self) -> None:
        await self._seed_toast_recipe()

        # Пустой холодильник -> too_many_missing
        res_empty = await kitchen_ai.adapt_recipe_to_pantry(self.db, self.user, "pdf_d2cb059e6c", servings=1)
        self.assertFalse(res_empty["ok"])
        self.assertEqual(res_empty["error"], "too_many_missing")

        # Все 7 ингредиентов есть дома -> already_complete, кнопки адаптации нет
        await self._seed_toast_pantry()
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Миндаль", amount=100.0, unit="г")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Базилик", amount=50.0, unit="г")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Вяленые томаты", amount=100.0, unit="г")

        res_full = await kitchen_ai.adapt_recipe_to_pantry(self.db, self.user, "pdf_d2cb059e6c", servings=1)
        self.assertTrue(res_full["ok"])
        self.assertTrue(res_full["already_complete"])

        cb = _fake_cb("kitchen:cook_view:pdf_d2cb059e6c:1:all:0", tg_id=self.tg_id)
        await kitchen.view_recipe(cb, self.db)
        kb = cb.message.edit_text.call_args[1]["reply_markup"]
        self.assertFalse(any(c.startswith("kitchen:adapt:") for c in _flat_callbacks(kb)))

    async def test_5_meal_plan_day0_pantry_priority_and_stale_banner(self) -> None:
        # Рецепт A (полный, с истекающим яблоком) vs Рецепт B (не хватает 3 продуктов)
        await repo.upsert_user_recipe(
            self.db,
            self.uid,
            recipe_key="full_oatmeal",
            title="Овсянка с яблоком",
            tags=["breakfast", "vegan"],
            base_servings=1,
            time_minutes=10,
            ingredients=[
                {"name": "Овсяные хлопья", "qty": 60, "unit": "г"},
                {"name": "Яблоко", "qty": 120, "unit": "г"},
            ],
            steps=["Сварите овсянку с яблоком."],
            nutrition={"kcal": 480, "protein_g": 28, "fat_g": 10, "carbs_g": 68},
        )
        await repo.upsert_user_recipe(
            self.db,
            self.uid,
            recipe_key="missing_toast",
            title="Тост без 3 продуктов",
            tags=["breakfast", "vegan"],
            base_servings=1,
            time_minutes=10,
            ingredients=[
                {"name": "Хлеб цельнозерновой", "qty": 60, "unit": "г"},
                {"name": "Миндаль", "qty": 15, "unit": "г"},
                {"name": "Базилик", "qty": 10, "unit": "г"},
                {"name": "Вяленые томаты", "qty": 10, "unit": "г"},
            ],
            steps=["Соберите тост."],
            nutrition={"kcal": 430, "protein_g": 14, "fat_g": 16, "carbs_g": 54},
        )
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Овсяные хлопья", amount=500.0, unit="г")
        await repo.upsert_pantry_item_merged(
            self.db, self.uid, name="Яблоко", amount=300.0, unit="г", expires_at="2026-11-27"
        )
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Хлеб цельнозерновой", amount=200.0, unit="г")

        now = dt.datetime(2026, 11, 27, 7, 0, 0)
        plan = await kitchen_ai.generate_meal_plan(self.db, self.user, days=3, start_date="2026-11-27", now_utc=now)
        day0_breakfast = next(
            it for it in plan["items"] if it["plan_date"] == "2026-11-27" and it["meal_slot"] == "breakfast"
        )
        self.assertEqual(day0_breakfast["title"], "Овсянка с яблоком")
        self.assertNotEqual(day0_breakfast["recipe_id"], "missing_toast")
        fresh_view = await kitchen_ai.get_meal_plan_view(
            self.db, self.user, days=3, start_date="2026-11-27", auto_generate=False, now_utc=now
        )
        fresh_day0_b = next(
            it for it in fresh_view["items"] if it["plan_date"] == "2026-11-27" and it["meal_slot"] == "breakfast"
        )
        self.assertEqual(fresh_day0_b["missing_count"], 0)
        self.assertFalse(fresh_view["is_stale"])

        # Имитируем старый неполный план от 24.11 на 27.11 с missing_toast
        await self.db.execute("DELETE FROM meal_plans WHERE user_id = ?", (self.uid,))
        await self.db.execute(
            """
            INSERT INTO meal_plans (user_id, plan_date, meal_slot, recipe_id, title, time_minutes, ingredients_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)
            """,
            (self.uid, "2026-11-27", "breakfast", "missing_toast", "Тост без 3 продуктов", 10, "[]", "2026-11-24T11:41:00", "2026-11-24T11:41:00"),
        )
        await self.db.commit()

        stale_view = await kitchen_ai.get_meal_plan_view(
            self.db, self.user, days=3, start_date="2026-11-27", auto_generate=False, now_utc=now
        )
        self.assertTrue(stale_view["is_stale"])
        self.assertEqual(stale_view["items"][0]["missing_count"], 3)

        rendered_text, _ = kitchen._render_meal_plan_payload(stale_view)
        self.assertIn("Меню собрано раньше или неполное", rendered_text)
        self.assertIn("⚠️ нет 3 прод.", rendered_text)


if __name__ == "__main__":
    unittest.main()
