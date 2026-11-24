import time
import unittest

from db import repositories as repo
from db.database import connect, init_db
from handlers import kitchen
from services import kitchen_ai


class SmartMealPlanKBJUTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.u_id = await repo.create_user(
            self.db,
            telegram_id=960001,
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
            equipment=["smart_scales", "dumbbells"],
        )
        await repo.add_weight(self.db, self.u_id, 99.2, body_fat_pct=29.0, source="scale_photo")
        pantry_items = [
            ("Картофель", 650, "г", "овощи"),
            ("Масло подсолнечное", 1, "л", "соусы/масла"),
            ("Чеснок", 3, "шт", "овощи"),
            ("Котлеты растительные GREEN MEAT", 1120, "г", "белок"),
            ("Фрикадельки HI Hiballs", 500, "г", "белок"),
            ("Тофу классический", 600, "г", "белок"),
            ("Чечевица красная", 500, "г", "белок"),
            ("Фасоль красная консервированная", 400, "г", "белок"),
            ("Нут консервированный", 800, "г", "белок"),
            ("Арахисовая паста без сахара", 340, "г", "соусы/масла"),
            ("Овсяные хлопья", 500, "г", "крупы"),
            ("Бананы", 1000, "г", "фрукты"),
            ("Хлеб цельнозерновой", 330, "г", "крупы"),
            ("Авокадо Хасс", 700, "г", "фрукты"),
            ("Томаты красные", 600, "г", "овощи"),
            ("Лук репчатый", 1000, "г", "овощи"),
            ("Гречка", 1000, "г", "крупы"),
            ("Рис круглозёрный", 900, "г", "крупы"),
            ("Шампиньоны", 250, "г", "овощи"),
            ("Овсяное молоко", 1, "л", "молочка"),
        ]
        for name, amt, unit, cat in pantry_items:
            await repo.create_pantry_item(self.db, self.u_id, name, amt, unit, None, cat)
        self.user = dict(await repo.get_user(self.db, self.u_id))

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def test_1_meal_plan_prefers_protein_and_boosts_satiety(self) -> None:
        plan = await kitchen_ai.generate_meal_plan(self.db, self.user, days=3)
        self.assertIsNotNone(plan["nutrition_targets"])
        self.assertEqual(plan["nutrition_targets"]["kcal"], 2210)
        self.assertEqual(plan["nutrition_targets"]["protein_g"], 134)

        items = plan["items"]
        self.assertEqual(len(items), 9)

        # Bare low-protein potato sides must NOT appear unboosted as standalone dinners
        for it in items:
            t_low = str(it.get("title") or "").lower()
            if it["meal_slot"] == "dinner":
                self.assertNotIn("жареная картошка", t_low)
                self.assertNotEqual(t_low.strip(), "картофельные дольки с чесноком на сковороде")
            if it["meal_slot"] == "breakfast" and any(k in t_low for k in ("оладь", "блин", "овсян", "смузи")):
                self.assertNotIn("котлет", t_low)
                self.assertNotIn("фрикадел", t_low)

        daily = plan["daily_summary"]
        self.assertEqual(len(daily), 3)
        for ds in daily.values():
            self.assertGreaterEqual(ds["kcal"], 1500)
            self.assertGreaterEqual(ds["protein_g"], 65)

    async def test_2_get_meal_plan_view_render_and_student_isolation(self) -> None:
        await kitchen_ai.generate_meal_plan(self.db, self.user, days=3)
        view = await kitchen_ai.get_meal_plan_view(self.db, self.user, days=3, auto_generate=False)
        self.assertIn("nutrition_targets", view)
        self.assertIn("daily_summary", view)
        text, _ = kitchen._render_meal_plan_payload(view)
        self.assertIn("цель ~<b>2210 ккал", text)
        self.assertIn("Итого за день:", text)
        self.assertIn("ккал, Б", text)

        # Student mode must hide KBJU targets/totals and NOT mutate dish titles with boosters/multipliers
        stu_id = await repo.create_user(
            self.db,
            telegram_id=960099,
            name="Ученик",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:00",
        )
        await repo.update_user_profile_mode(self.db, stu_id, "student")
        stu_user = dict(await repo.get_user(self.db, stu_id))
        stu_plan = await kitchen_ai.generate_meal_plan(self.db, stu_user, days=3)
        self.assertIsNone(stu_plan["nutrition_targets"])
        stu_text, _ = kitchen._render_meal_plan_payload(stu_plan)
        self.assertNotIn("Итого за день:", stu_text)
        self.assertNotIn("ккал", stu_text)

    async def test_3_meal_plan_generation_stays_fast(self) -> None:
        t0 = time.perf_counter()
        await kitchen_ai.generate_meal_plan(self.db, self.user, days=3)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
