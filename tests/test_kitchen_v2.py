import datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from config import MCPSettings
from db import repositories as repo
from db.database import connect, init_db
from handlers import kitchen
from mcp_server.tools import call_mcp_tool
from services import kitchen_ai


class _InMemoryFSM:
    def __init__(self) -> None:
        self._state = None
        self._data: dict = {}

    async def set_state(self, state) -> None:
        self._state = state

    async def get_state(self):
        return self._state

    async def update_data(self, **kwargs) -> None:
        self._data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self._data)

    async def clear(self) -> None:
        self._state = None
        self._data.clear()


def _button_datas(markup) -> list[str]:
    return [
        btn.callback_data
        for row in (getattr(markup, "inline_keyboard", None) or [])
        for btn in row
        if getattr(btn, "callback_data", None)
    ]


class KitchenV2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.tg_id = 889101
        self.user_id = await repo.create_user(
            self.db,
            telegram_id=self.tg_id,
            name="Артем",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:30",
        )
        await repo.upsert_wellness(self.db, self.user_id, meal_profile="vegan")
        self.user = dict(await repo.get_user(self.db, self.user_id))
        self.mcp_settings = MCPSettings(
            host="127.0.0.1",
            port=8000,
            public_base_url="https://hidl.fodders.ru",
            owner_telegram_id=self.tg_id,
            access_password="test",
            bearer_token="test-token",
            allow_diary=True,
            readonly=False,
            access_token_ttl_sec=3600,
            refresh_token_ttl_sec=86400,
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def test_1_almost_ready_strict_filter_and_expiring_priority(self) -> None:
        now = datetime.datetime(2026, 11, 24, 10, 0)
        exp_date = "2026-11-25"
        await repo.create_pantry_item(self.db, self.user_id, "Тофу", 300, "g", exp_date, "белок")
        await repo.create_pantry_item(self.db, self.user_id, "Рис", 900, "g", None, "крупы")

        # Recipe with 0 missing (must NOT appear in almost_ready)
        r_ready = await repo.upsert_user_recipe(
            self.db,
            self.user_id,
            title="Тест: Тофу с рисом дома",
            tags=["vegan", "lunch"],
            base_servings=2,
            time_minutes=15,
            ingredients=[
                {"name": "Тофу", "qty": 200, "unit": "g"},
                {"name": "Рис", "qty": 150, "unit": "g"},
            ],
            steps=["Свари рис.", "Обжарь тофу."],
        )
        # Recipe with 1 missing + expiring Тофу (MUST appear & include uses_expiring)
        r_almost = await repo.upsert_user_recipe(
            self.db,
            self.user_id,
            title="Тест: Тофу с рисом и брокколи",
            tags=["vegan", "lunch"],
            base_servings=2,
            time_minutes=20,
            ingredients=[
                {"name": "Тофу", "qty": 200, "unit": "g"},
                {"name": "Рис", "qty": 150, "unit": "g"},
                {"name": "Брокколи", "qty": 200, "unit": "g"},
            ],
            steps=["Свари рис и брокколи.", "Обжарь тофу."],
        )
        # Recipe with 3 missing (must NOT appear)
        r_far = await repo.upsert_user_recipe(
            self.db,
            self.user_id,
            title="Тест: Сложное рагу",
            tags=["vegan", "lunch"],
            base_servings=2,
            time_minutes=30,
            ingredients=[
                {"name": "Тофу", "qty": 200, "unit": "g"},
                {"name": "Авокадо", "qty": 1, "unit": "шт"},
                {"name": "Кинза", "qty": 30, "unit": "g"},
                {"name": "Кукуруза", "qty": 200, "unit": "g"},
            ],
            steps=["Всё смешай."],
        )

        res = await kitchen_ai.suggest_almost_ready_recipes(
            self.db, self.user, max_missing=2, meal_slot="lunch", limit=40, now_utc=now
        )
        suggestions = res["suggestions"]
        self.assertGreater(len(suggestions), 0)
        for s in suggestions:
            self.assertTrue(1 <= len(s["missing"]) <= 2, f"Expected 1-2 missing, got {len(s['missing'])}")
            self.assertIn("vegan", s["recipe"].get("tags") or [])

        ids = [s["recipe"]["id"] for s in suggestions]
        self.assertNotIn(r_ready["id"], ids)
        self.assertIn(r_almost["id"], ids)
        self.assertNotIn(r_far["id"], ids)

        almost_entry = next(s for s in suggestions if s["recipe"]["id"] == r_almost["id"])
        self.assertTrue(almost_entry["uses_expiring"])

        # Telegram handler + back button preservation for "almost"
        msg = SimpleNamespace(
            chat=SimpleNamespace(id=self.tg_id),
            message_id=701,
            edit_text=AsyncMock(),
            answer=AsyncMock(),
        )
        cb_list = SimpleNamespace(
            data="kitchen:almost_ready:0",
            from_user=SimpleNamespace(id=self.tg_id, full_name="Артем"),
            message=msg,
            answer=AsyncMock(),
        )
        await kitchen.almost_ready_recipes(cb_list, self.db)
        msg.edit_text.assert_awaited()
        txt = msg.edit_text.call_args.args[0]
        self.assertIn("Не хватает 1–2 продуктов", txt)
        datas = _button_datas(msg.edit_text.call_args.kwargs["reply_markup"])
        self.assertTrue(any(d.endswith(":2:almost:0") for d in datas))
        self.assertTrue(any(d.startswith("kitchen:add_miss:") for d in datas))

        # Opening cook_view from almost_ready sets back callback to kitchen:almost_ready:0
        cb_view = SimpleNamespace(
            data=f"kitchen:cook_view:{r_almost['id']}:2:almost:0",
            from_user=SimpleNamespace(id=self.tg_id, full_name="Артем"),
            message=msg,
            answer=AsyncMock(),
        )
        await kitchen.view_recipe(cb_view, self.db)
        view_datas = _button_datas(msg.edit_text.call_args.kwargs["reply_markup"])
        self.assertIn("kitchen:almost_ready:0", view_datas)

    async def test_2_meal_plan_shopping_export_grouped_and_mcp(self) -> None:
        now = datetime.datetime(2026, 11, 24, 10, 0)
        await repo.create_pantry_item(self.db, self.user_id, "Гречка", 900, "g", None, "крупы")

        plan = await kitchen_ai.generate_meal_plan(self.db, self.user, days=3, now_utc=now)
        self.assertIn("missing_shopping_grouped", plan)
        self.assertIn("shopping_export_text", plan)
        self.assertTrue(plan["missing_shopping"])
        self.assertTrue(plan["shopping_export_text"])

        # Add the first missing item + an explicit household shopping item
        deducted_name = plan["missing_shopping"][0]["name"]
        await repo.create_shopping_item(self.db, self.user_id, deducted_name, 1, "шт", scope="household")
        await repo.create_shopping_item(self.db, self.user_id, "Кофе молотый", 250, "g", scope="household")

        plan_rows = plan["items"]
        exp_plan_only = await kitchen_ai.build_meal_plan_shopping_export(
            self.db, self.user, plan_rows, include_active_shopping=False
        )
        self.assertNotIn(deducted_name.lower(), exp_plan_only["plain_text"].lower())

        exp_with_shop = await kitchen_ai.build_meal_plan_shopping_export(
            self.db, self.user, plan_rows, include_active_shopping=True
        )
        self.assertIn("Кофе молотый", exp_with_shop["plain_text"])
        self.assertIn(deducted_name, exp_with_shop["plain_text"])

        # Telegram meal plan export + active shopping list export
        msg = SimpleNamespace(
            chat=SimpleNamespace(id=self.tg_id),
            message_id=702,
            edit_text=AsyncMock(),
            answer=AsyncMock(),
        )
        cb_export = SimpleNamespace(
            data="kitchen:meal_plan:export:3",
            from_user=SimpleNamespace(id=self.tg_id, full_name="Артем"),
            message=msg,
            answer=AsyncMock(),
        )
        await kitchen.mealplan_callbacks(cb_export, self.db)
        export_html = msg.edit_text.call_args.args[0]
        self.assertIn("<pre>", export_html)
        self.assertIn("kitchen:meal_plan:export_all:3", _button_datas(msg.edit_text.call_args.kwargs["reply_markup"]))

        cb_shop_exp = SimpleNamespace(
            data="kitchen:shop_export:household",
            from_user=SimpleNamespace(id=self.tg_id, full_name="Артем"),
            message=msg,
            answer=AsyncMock(),
        )
        await kitchen.export_shoplist(cb_shop_exp, self.db)
        shop_exp_html = msg.edit_text.call_args.args[0]
        self.assertIn("<pre>", shop_exp_html)
        self.assertIn("Кофе молотый", shop_exp_html)

        # MCP hidl_get_meal_plan exposes grouped export additively
        mcp_res = await call_mcp_tool(self.db, self.mcp_settings, "hidl_get_meal_plan", {"days": 3})
        self.assertFalse(mcp_res["isError"])
        mcp_data = json.loads(mcp_res["content"][0]["text"])
        self.assertIn("missing_shopping_grouped", mcp_data)
        self.assertIn("shopping_export_text", mcp_data)

    async def test_3_recipe_import_offline_text_and_jsonld_url(self) -> None:
        no_llm = SimpleNamespace(is_configured=lambda: False)
        raw_recipe = (
            "Тофу-скрэмбл со шпинатом\n"
            "Время: 15 мин, 2 порции\n"
            "Ингредиенты:\n"
            "- Тофу твёрдый — 200 г\n"
            "- Шпинат — 100 г\n"
            "- Оливковое масло — 1 ст. л.\n"
            "- Куркума — 0.5 ч. л.\n"
            "Шаги:\n"
            "1. Раскроши тофу руками.\n"
            "2. Обжарь тофу с куркумой 5 минут и добавь шпинат.\n"
        )
        res_txt = await kitchen_ai.parse_recipe_from_input(
            raw_recipe,
            profile="vegan",
            llm_client_instance=no_llm,
        )
        self.assertTrue(res_txt["ok"])
        self.assertFalse(res_txt["used_llm"])
        self.assertEqual(res_txt["source"], "text_import")
        rec_txt = res_txt["recipe"]
        self.assertEqual(rec_txt["title"], "Тофу-скрэмбл со шпинатом")
        self.assertEqual(rec_txt["time_minutes"], 15)
        self.assertEqual(rec_txt["base_servings"], 2)
        self.assertEqual(len(rec_txt["ingredients"]), 4)
        self.assertEqual(len(rec_txt["steps"]), 2)
        self.assertIn("vegan", rec_txt["tags"])
        self.assertGreater(rec_txt["nutrition"]["kcal"], 0)

        # JSON-LD HTML recipe from URL
        html_page = """
        <html><head>
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "Recipe",
          "name": "Тёплый боул с нутом и бататом",
          "description": "Сытный растительный ужин за полчаса",
          "totalTime": "PT30M",
          "recipeYield": "2 servings",
          "recipeIngredient": ["Нут варёный — 250 г", "Батат — 300 г", "Тахини — 2 ст. л."],
          "recipeInstructions": [
            {"@type": "HowToStep", "text": "Запеки батат кубиками 20 минут."},
            {"@type": "HowToStep", "text": "Выложи с нутом и полей тахини."}
          ],
          "nutrition": {"@type": "NutritionInformation", "calories": "480 kcal", "proteinContent": "18 g"}
        }
        </script>
        </head><body></body></html>
        """
        res_url = await kitchen_ai.parse_recipe_from_input(
            "https://example.com/recipes/chickpea-bowl",
            profile="vegan",
            llm_client_instance=no_llm,
            fetch_url_fn=lambda u: (html_page, u),
        )
        self.assertTrue(res_url["ok"])
        self.assertFalse(res_url["used_llm"])
        self.assertEqual(res_url["source"], "url_import")
        rec_url = res_url["recipe"]
        self.assertEqual(rec_url["title"], "Тёплый боул с нутом и бататом")
        self.assertEqual(rec_url["time_minutes"], 30)
        self.assertEqual(len(rec_url["ingredients"]), 3)
        self.assertEqual(len(rec_url["steps"]), 2)

    async def test_4_recipe_import_ssrf_guard(self) -> None:
        unsafe_targets = [
            "http://127.0.0.1:8765/internal/status",
            "http://localhost/admin",
            "http://10.0.0.1/secret",
            "http://192.168.1.5/router",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/local",
            "file:///etc/passwd",
            "ftp://example.com/recipe.txt",
        ]
        for bad_url in unsafe_targets:
            with self.assertRaises(ValueError, msg=f"Should reject {bad_url}"):
                await kitchen_ai._validate_public_url(bad_url)

        # Host resolving to loopback or private IP must also fail
        with self.assertRaises(ValueError):
            await kitchen_ai._validate_public_url(
                "https://rebind.example/r",
                resolve_fn=lambda _h: ["127.0.0.1"],
            )
        with self.assertRaises(ValueError):
            await kitchen_ai._validate_public_url(
                "https://rebind6.example/r",
                resolve_fn=lambda _h: ["::1"],
            )

        ok_literal = await kitchen_ai._validate_public_url("http://93.184.216.34/recipe")
        self.assertEqual(ok_literal, "http://93.184.216.34/recipe")
        ok_domain = await kitchen_ai._validate_public_url(
            "https://example.org/recipe#ingredients",
            resolve_fn=lambda _h: ["93.184.216.34"],
        )
        self.assertEqual(ok_domain, "https://example.org/recipe")

    async def test_5_recipe_import_photo_vision_and_telegram_save_household_sharing(self) -> None:
        partner_tg = 889102
        partner_id = await repo.create_user(
            self.db,
            telegram_id=partner_tg,
            name="Саша",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:00",
        )
        hid = await repo.get_or_create_household(self.db, self.user_id)
        await repo.set_user_household(self.db, partner_id, hid)

        # Student mode guard on /addrecipe
        student_tg = 889103
        stu_id = await repo.create_user(
            self.db,
            telegram_id=student_tg,
            name="Студент",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:00",
        )
        await repo.update_user_profile_mode(self.db, stu_id, "student")
        student_msg = SimpleNamespace(
            from_user=SimpleNamespace(id=student_tg, full_name="Студент"),
            answer=AsyncMock(),
        )
        student_fsm = _InMemoryFSM()
        await kitchen.addrecipe_cmd(student_msg, student_fsm, self.db)
        self.assertIsNone(await student_fsm.get_state())
        self.assertIn("взрослом режиме", student_msg.answer.call_args.args[0])

        # Partner A imports a recipe from photo
        fsm = _InMemoryFSM()
        cb_start = SimpleNamespace(
            data="kitchen:recipe_import",
            from_user=SimpleNamespace(id=self.tg_id, full_name="Артем"),
            message=SimpleNamespace(answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await kitchen.recipe_import_start(cb_start, fsm, self.db)
        self.assertEqual(await fsm.get_state(), kitchen.RecipeImportState.waiting_input)

        vision_recipe = {
            "title": "Чечевичный крем-суп по фото",
            "desc": "Из домашней книги рецептов",
            "time_minutes": 25,
            "base_servings": 3,
            "tags": ["lunch", "healthy"],
            "ingredients": [
                {"name": "Чечевица красная", "qty": 200, "unit": "g"},
                {"name": "Морковь", "qty": 150, "unit": "g"},
                {"name": "Кокосовое молоко", "qty": 200, "unit": "ml"},
            ],
            "steps": [
                "Отвари чечевицу с морковью 18 минут.",
                "Взбей блендером с кокосовым молоком.",
            ],
        }
        fake_bot = SimpleNamespace(
            download=AsyncMock(return_value=b"\xff\xd8\xff\xe0recipe-photo"),
            send_message=AsyncMock(),
        )
        photo_msg = SimpleNamespace(
            photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")],
            caption="",
            from_user=SimpleNamespace(id=self.tg_id, full_name="Артем"),
            bot=fake_bot,
            answer=AsyncMock(),
        )
        with (
            patch("services.kitchen_ai.default_llm_client.is_configured", return_value=True),
            patch(
                "services.kitchen_ai.default_llm_client.complete_json",
                AsyncMock(return_value=SimpleNamespace(ok=True, data=vision_recipe, error=None)),
            ),
        ):
            await kitchen.recipe_import_photo(photo_msg, fsm, self.db)

        self.assertEqual(await fsm.get_state(), kitchen.RecipeImportState.confirm)
        preview_txt = photo_msg.answer.call_args.args[0]
        self.assertIn("Чечевичный крем-суп по фото", preview_txt)
        self.assertIn("kitchen:recipe_import_save", _button_datas(photo_msg.answer.call_args.kwargs["reply_markup"]))

        save_msg = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
        cb_save = SimpleNamespace(
            data="kitchen:recipe_import_save",
            from_user=SimpleNamespace(id=self.tg_id, full_name="Артем"),
            message=save_msg,
            bot=fake_bot,
            answer=AsyncMock(),
        )
        with patch("services.household_service.is_partner_quiet_or_paused", return_value=False):
            await kitchen.recipe_import_save(cb_save, fsm, self.db)
        self.assertIsNone(await fsm.get_state())
        self.assertIn("Мои рецепты", save_msg.edit_text.call_args.args[0])

        # Partner B received notification & shares access to saved recipe
        fake_bot.send_message.assert_awaited()
        notify_args = fake_bot.send_message.call_args.args
        self.assertEqual(notify_args[0], partner_tg)
        self.assertIn("Чечевичный крем-суп по фото", notify_args[1])

        partner_recipes = await kitchen_ai.load_recipes_for_user(self.db, partner_id)
        saved_row = next((r for r in partner_recipes if r["title"] == "Чечевичный крем-суп по фото"), None)
        self.assertIsNotNone(saved_row)
        fetched = await kitchen_ai.get_recipe_for_user(self.db, partner_id, saved_row["id"])
        self.assertIsNotNone(fetched)


if __name__ == "__main__":
    unittest.main()
