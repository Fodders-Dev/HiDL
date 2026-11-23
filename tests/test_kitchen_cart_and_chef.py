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


class KitchenCartAndChefTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.tg_id = 888001
        self.user_id = await repo.create_user(
            self.db,
            telegram_id=self.tg_id,
            name="ChefTester",
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

    async def test_1_user_recipes_crud_suggest_and_cook(self) -> None:
        saved = await repo.upsert_user_recipe(
            self.db,
            self.user_id,
            title="Авторский тофу-боул с авокадо",
            desc="Личный быстрый рецепт",
            tags=["vegan", "lunch", "fast"],
            base_servings=2,
            time_minutes=12,
            ingredients=[
                {"name": "Тофу", "qty": 200, "unit": "g"},
                {"name": "Авокадо", "qty": 1, "unit": "шт"},
            ],
            steps=["Нарежь тофу и обжарь 5 минут.", "Добавь авокадо и подавай."],
            source="ai_chef",
            source_label="Тест",
        )
        rid = saved["id"]
        self.assertTrue(rid.startswith("ur_"))
        self.assertTrue(saved["is_user_recipe"])

        all_recs = await kitchen_ai.load_recipes_for_user(self.db, self.user_id)
        self.assertTrue(any(r["id"] == rid for r in all_recs))
        fetched = await kitchen_ai.get_recipe_for_user(self.db, self.user_id, rid)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["title"], "Авторский тофу-боул с авокадо")

        await repo.create_pantry_item(self.db, self.user_id, "Тофу", 400, "g", None, "белок")
        await repo.create_pantry_item(self.db, self.user_id, "Авокадо", 2, "шт", None, "фрукты")

        sug = await kitchen_ai.suggest_meals_now(
            self.db,
            self.user,
            meal_slot="lunch",
            limit=5,
            now_utc=datetime.datetime(2026, 11, 23, 10, 0),
        )
        matched_ids = [s["recipe"]["id"] for s in sug["suggestions"]]
        self.assertIn(rid, matched_ids)

        cooked = await kitchen_ai.commit_cooked_recipe(
            self.db,
            self.user,
            rid,
            servings=2,
            meal_slot="lunch",
            now_utc=datetime.datetime(2026, 11, 23, 10, 5),
        )
        self.assertTrue(cooked["ok"])
        pantry_after = {p["name"]: p["amount"] for p in await repo.list_pantry_items(self.db, self.user_id)}
        self.assertEqual(pantry_after["Тофу"], 200.0)
        self.assertEqual(pantry_after["Авокадо"], 1.0)

        deleted = await repo.delete_user_recipe_by_key(self.db, self.user_id, rid)
        self.assertTrue(deleted)
        self.assertIsNone(await repo.get_user_recipe_by_key(self.db, self.user_id, rid))

    async def test_2_import_cart_items_and_parse_cart_text_and_vision(self) -> None:
        # Offline Перекрёсток cart text
        raw_cart = (
            "Перекрёсток\n"
            "Молоко овсяное Nemoloko 1 л — 139,90 ₽\n"
            "Тофу классический 300 г x 2 — 320 руб\n"
            "Бананы 1.2 кг — 189 ₽\n"
            "Итого: 648,90 ₽\n"
        )
        no_llm = SimpleNamespace(is_configured=lambda: False)
        parsed = await kitchen_ai.parse_cart_input(raw_cart, llm_client_instance=no_llm)
        self.assertTrue(parsed["ok"])
        self.assertFalse(parsed["used_llm"])
        self.assertEqual(parsed["store"], "Перекрёсток")
        self.assertEqual(parsed["total_amount"], 648.9)
        self.assertEqual(len(parsed["items"]), 3)
        tofu = next(i for i in parsed["items"] if "Тофу" in i["name"])
        self.assertEqual(tofu["amount"], 600.0)
        self.assertEqual(tofu["unit"], "g")

        # Put one matching item in shopping_list to verify sync_shopping
        await repo.create_shopping_item(self.db, self.user_id, "Тофу", qty=1, unit="шт", scope="household")
        imported = await kitchen_ai.import_cart_items(
            self.db,
            self.user,
            parsed["items"],
            total_amount=parsed["total_amount"],
            sync_shopping=True,
        )
        self.assertEqual(imported["added_count"], 3)
        self.assertIn("Тофу", imported["synced_shopping"])
        self.assertIsNotNone(imported["expense_id"])
        self.assertEqual(imported["total_amount"], 648.9)

        shop_left = [s for s in await repo.list_shopping_items(self.db, self.user_id) if not s.get("is_bought")]
        self.assertEqual(shop_left, [])
        expenses = await repo.expenses_last_days(self.db, self.user_id, days=1)
        self.assertEqual(len(expenses), 1)
        self.assertEqual(float(expenses[0]["amount"]), 648.9)

        # Vision LLM mock for cart screenshot
        vision_payload = {
            "store": "Перекрёсток",
            "total_amount": 420.0,
            "items": [
                {"name": "Нут консервированный", "amount": 400, "unit": "g", "category": "белок"},
                {"name": "Помидоры черри", "amount": 250, "unit": "g", "category": "овощи"},
            ],
        }
        vision_llm = SimpleNamespace(
            is_configured=lambda: True,
            complete_json=AsyncMock(
                return_value=SimpleNamespace(ok=True, data=vision_payload, error=None)
            ),
        )
        v_res = await kitchen_ai.parse_cart_input(
            raw_text="скрин корзины",
            image_bytes=b"\xff\xd8\xff\xe0fakejpeg",
            llm_client_instance=vision_llm,
        )
        self.assertTrue(v_res["ok"])
        self.assertTrue(v_res["used_llm"])
        self.assertEqual(len(v_res["items"]), 2)
        kwargs = vision_llm.complete_json.call_args.kwargs
        self.assertTrue(kwargs["image_data_urls"][0].startswith("data:image/jpeg;base64,"))

    async def test_3_generate_ai_recipes_from_pantry_respects_vegan_and_expiring(self) -> None:
        today = "2026-11-23"
        soon_date = "2026-11-24"
        await repo.create_pantry_item(self.db, self.user_id, "Шпинат", 150, "g", soon_date, "овощи")
        await repo.create_pantry_item(self.db, self.user_id, "Нут", 400, "g", None, "белок")

        chef_payload = {
            "recipes": [
                {
                    "title": "Тёплый салат со шпинатом и нутом",
                    "desc": "Быстро спасаем свежий шпинат.",
                    "time_minutes": 15,
                    "base_servings": 2,
                    "tags": ["healthy"],
                    "ingredients": [
                        {"name": "Шпинат", "qty": 150, "unit": "г"},
                        {"name": "Нут", "qty": 200, "unit": "г"},
                    ],
                    "steps": ["Прогрей нут на сковороде.", "Смешай со шпинатом."],
                }
            ]
        }
        fake_llm = SimpleNamespace(
            is_configured=lambda: True,
            complete_json=AsyncMock(
                return_value=SimpleNamespace(ok=True, data=chef_payload, error=None)
            ),
        )
        res = await kitchen_ai.generate_ai_recipes_from_pantry(
            self.db,
            self.user,
            meal_slot="dinner",
            max_minutes=20,
            count=2,
            llm_client_instance=fake_llm,
            now_utc=datetime.datetime.fromisoformat(f"{today}T15:00:00"),
        )
        self.assertTrue(res["ok"])
        self.assertIn("Шпинат", res["expiring_names"])
        self.assertEqual(res["meal_profile"], "vegan")
        self.assertEqual(len(res["recipes"]), 1)
        rec = res["recipes"][0]
        self.assertIn("vegan", rec["tags"])
        self.assertIn("dinner", rec["tags"])
        prompt_sent = fake_llm.complete_json.call_args.args[1]
        self.assertIn("Шпинат", prompt_sent)
        self.assertIn("vegan", prompt_sent)

    async def test_4_mcp_hidl_import_cart_and_add_recipe(self) -> None:
        add_shop = await repo.create_shopping_item(self.db, self.user_id, "Авокадо", qty=2, unit="шт", scope="household")
        self.assertGreater(add_shop, 0)

        cart_call = await call_mcp_tool(
            self.db,
            self.mcp_settings,
            "hidl_import_cart",
            {
                "store": "Перекрёсток",
                "total_amount": 950,
                "items": [
                    {"name": "Авокадо", "amount": 2, "unit": "шт"},
                    {"name": "Хлеб цельнозерновой", "amount": 1, "unit": "шт"},
                ],
            },
        )
        self.assertFalse(cart_call["isError"])
        cart_json = json.loads(cart_call["content"][0]["text"])
        self.assertTrue(cart_json["ok"])
        self.assertEqual(cart_json["added_count"], 2)
        self.assertIn("Авокадо", cart_json["synced_shopping"])
        self.assertEqual(cart_json["total_amount"], 950.0)
        self.assertIsInstance(cart_json["quick_suggestions"], list)

        rec_call = await call_mcp_tool(
            self.db,
            self.mcp_settings,
            "hidl_add_recipe",
            {
                "title": "Тост с авокадо из Перекрёстка",
                "tags": ["vegan", "breakfast"],
                "time_minutes": 10,
                "ingredients": [
                    {"name": "Хлеб", "qty": 2, "unit": "шт"},
                    {"name": "Авокадо", "qty": 1, "unit": "шт"},
                ],
                "steps": ["Подсуши хлеб.", "Разомни авокадо сверху."],
                "source_label": "ChatGPT",
            },
        )
        self.assertFalse(rec_call["isError"])
        rec_json = json.loads(rec_call["content"][0]["text"])
        self.assertTrue(rec_json["ok"])
        rid = rec_json["recipe_id"]

        kitchen_call = await call_mcp_tool(self.db, self.mcp_settings, "hidl_get_kitchen", {"servings": 2})
        self.assertFalse(kitchen_call["isError"])
        kitchen_json = json.loads(kitchen_call["content"][0]["text"])
        self.assertTrue(any(r["id"] == rid for r in kitchen_json["recipes"]))

    async def test_5_telegram_handlers_ai_chef_and_cart_import(self) -> None:
        state = _InMemoryFSM()
        fake_ai_res = {
            "ok": True,
            "recipes": [
                {
                    "title": "Паста с тофу и шпинатом",
                    "desc": "Сытный ужин из холодильника.",
                    "time_minutes": 20,
                    "base_servings": 2,
                    "tags": ["vegan", "dinner"],
                    "ingredients": [{"name": "Паста", "qty": 200, "unit": "g"}],
                    "steps": ["Свари пасту.", "Перемешай с тофу."],
                }
            ],
            "pantry_count": 3,
            "expiring_names": ["Шпинат"],
            "meal_profile": "vegan",
            "meal_slot": "dinner",
            "meal_slot_label": "ужин",
        }
        msg = SimpleNamespace(
            chat=SimpleNamespace(id=self.tg_id),
            message_id=501,
            answer=AsyncMock(),
            edit_text=AsyncMock(),
        )
        cb_chef = SimpleNamespace(
            data="kitchen:ai_chef",
            from_user=SimpleNamespace(id=self.tg_id, full_name="ChefTester"),
            message=msg,
            answer=AsyncMock(),
        )
        with patch("services.kitchen_ai.generate_ai_recipes_from_pantry", AsyncMock(return_value=fake_ai_res)):
            await kitchen.ai_chef_suggest(cb_chef, state, self.db)
        msg.edit_text.assert_awaited()
        self.assertIn("Паста с тофу", msg.edit_text.call_args.args[0])

        cb_view = SimpleNamespace(
            data="kitchen:ai_view:0",
            from_user=SimpleNamespace(id=self.tg_id, full_name="ChefTester"),
            message=msg,
            answer=AsyncMock(),
        )
        await kitchen.ai_chef_view_recipe(cb_view, state)
        self.assertIn("Шаги", msg.edit_text.call_args.args[0])
        self.assertIn("kitchen:ai_save:0", _button_datas(msg.edit_text.call_args.kwargs["reply_markup"]))

        cb_save = SimpleNamespace(
            data="kitchen:ai_save:0",
            from_user=SimpleNamespace(id=self.tg_id, full_name="ChefTester"),
            message=msg,
            answer=AsyncMock(),
        )
        await kitchen.ai_chef_save_recipe(cb_save, state, self.db)
        saved_recs = await repo.list_user_recipes(self.db, self.user_id)
        self.assertEqual(len(saved_recs), 1)
        self.assertEqual(saved_recs[0]["title"], "Паста с тофу и шпинатом")

        # Cart import text -> preview -> confirm:both
        state_cart = _InMemoryFSM()
        cb_start = SimpleNamespace(
            data="kitchen:cart_import",
            from_user=SimpleNamespace(id=self.tg_id, full_name="ChefTester"),
            message=SimpleNamespace(answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await kitchen.cart_import_start(cb_start, state_cart)
        self.assertEqual(await state_cart.get_state(), kitchen.CartImportState.waiting_input)

        text_msg = SimpleNamespace(
            text="Нут 400 г — 95 ₽\nИтого: 95 ₽",
            from_user=SimpleNamespace(id=self.tg_id, full_name="ChefTester"),
            answer=AsyncMock(),
        )
        with patch("services.kitchen_ai.default_llm_client.is_configured", return_value=False):
            await kitchen.cart_import_text(text_msg, state_cart)
        self.assertEqual(await state_cart.get_state(), kitchen.CartImportState.confirm)
        preview_kb = text_msg.answer.call_args.kwargs["reply_markup"]
        self.assertIn("kitchen:cart_confirm:both", _button_datas(preview_kb))

        confirm_msg = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
        cb_confirm = SimpleNamespace(
            data="kitchen:cart_confirm:both",
            from_user=SimpleNamespace(id=self.tg_id, full_name="ChefTester"),
            message=confirm_msg,
            answer=AsyncMock(),
        )
        await kitchen.cart_import_confirm(cb_confirm, state_cart, self.db)
        self.assertIsNone(await state_cart.get_state())
        self.assertIn("Перенесла в холодильник", confirm_msg.edit_text.call_args.args[0])
        pantry_names = [p["name"] for p in await repo.list_pantry_items(self.db, self.user_id)]
        self.assertIn("Нут", pantry_names)


if __name__ == "__main__":
    unittest.main()
