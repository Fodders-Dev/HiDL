from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from db import repositories as repo
from db.database import connect, init_db
from handlers import day_plan, inbox, kitchen, menu
from services import inbox_parser


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


def _fake_cb(data: str, *, tg_id: int, full_name: str = "Федор"):
    msg = SimpleNamespace(
        text="",
        chat=SimpleNamespace(id=tg_id),
        from_user=SimpleNamespace(id=tg_id, full_name=full_name, first_name=full_name),
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


class UXClarityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.tg_id = 960001
        self.uid = await repo.create_user(self.db, self.tg_id, "Федор", "UTC", "08:00", "23:30")
        self.user = dict(await repo.get_user(self.db, self.uid))

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def test_1_kitchen_home_shows_planned_meal_and_empty_guidance(self) -> None:
        # Without meal plan -> clear guidance
        text0, kb0 = await kitchen.build_kitchen_home_payload(self.db, self.user)
        self.assertIn("Меню на 3 дня", text0)
        self.assertIn("выбираешь один", text0)
        self.assertFalse(any("kitchen:cook_view:" in (btn.callback_data or "") for row in kb0.inline_keyboard for btn in row))

        # With planned breakfast today -> top direct button
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        await repo.upsert_meal_plan_slot(
            self.db,
            self.uid,
            today,
            "breakfast",
            title="Тосты с тофу и авокадо",
            recipe_id="rec_tofu_toast",
            time_minutes=10,
            ingredients=[],
            status="planned",
        )
        msg = SimpleNamespace(
            text="🍽 Еда и покупки",
            from_user=SimpleNamespace(id=self.tg_id, full_name="Федор"),
            answer=AsyncMock(),
        )
        await menu.food_menu(msg, _MemFSM(), self.db)
        sent_text = msg.answer.await_args.args[0]
        sent_kb = msg.answer.await_args.kwargs["reply_markup"]
        self.assertIn("Тосты с тофу и авокадо", sent_text)
        top_btn = sent_kb.inline_keyboard[0][0]
        self.assertIn("Мой", top_btn.text)
        self.assertTrue(top_btn.callback_data.startswith("kitchen:cook_view:rec_tofu_toast:"))

    async def test_2_quick_pick_and_ai_chef_clarify_pick_one_of_three(self) -> None:
        cb = _fake_cb("kitchen:quick_pick", tg_id=self.tg_id)
        await kitchen.quick_pick_recipes(cb, self.db)
        qp_text = cb.message.edit_text.await_args.args[0]
        self.assertIn("Выбери 1 из", qp_text)

        ai_text, _ = kitchen._render_ai_recipes_list(
            [{"title": "Тофу-скрэмбл", "time_minutes": 15, "desc": "Сытно", "nutrition": {"kcal": 380, "protein_g": 22, "fat_g": 18, "carbs_g": 28}}],
            profile="vegan",
            slot_label="завтрак",
            pantry_count=5,
            expiring_names=[],
        )
        self.assertIn("Выбери 1 из 1 вариантов", ai_text)

    async def test_3_day_plan_defer_to_backlog_and_restore_from_idea(self) -> None:
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        await repo.add_day_plan_item(self.db, self.uid, today, "Починить ванну")
        items = await repo.list_day_plan_items(self.db, self.uid, today)
        self.assertEqual(len(items), 1)
        item_id = int(items[0]["id"])

        cb_menu = _fake_cb("dplan:defermenu", tg_id=self.tg_id)
        await day_plan.day_plan_callbacks(cb_menu, _MemFSM(), self.db)
        menu_kb = cb_menu.message.edit_text.await_args.kwargs["reply_markup"]
        self.assertEqual(menu_kb.inline_keyboard[0][0].callback_data, f"dplan:defer:{item_id}")

        cb_defer = _fake_cb(f"dplan:defer:{item_id}", tg_id=self.tg_id)
        await day_plan.day_plan_callbacks(cb_defer, _MemFSM(), self.db)
        self.assertEqual(len(await repo.list_day_plan_items(self.db, self.uid, today)), 0)

        ideas = await repo.list_journal_entries(self.db, self.uid, entry_type="idea", limit=10)
        self.assertEqual(len(ideas), 1)
        self.assertEqual(ideas[0]["title"], "Починить ванну")

        # One tap back from 💡 На потом -> Day Plan
        cb_restore = _fake_cb(f"diary:to_task:{ideas[0]['id']}", tg_id=self.tg_id)
        await inbox.diary_callbacks(cb_restore, _MemFSM(), self.db)
        restored = await repo.list_day_plan_items(self.db, self.uid, today)
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["title"], "Починить ванну")

    async def test_4_long_horizon_phrase_routes_to_idea_and_tomorrow_stays_task(self) -> None:
        now = dt.datetime(2026, 11, 25, 6, 30, 0)
        long_res = inbox_parser.parse_inbox_offline(
            "Также нужно до конца года ванну починить",
            now_utc=now,
            tz_str="Europe/Moscow",
        )
        short_res = inbox_parser.parse_inbox_offline(
            "завтра надо начать работать над поздравлением папе",
            now_utc=now,
            tz_str="Europe/Moscow",
        )
        self.assertTrue(any(it.kind == "idea" for it in long_res.items))
        self.assertFalse(any(it.kind == "task" for it in long_res.items))
        self.assertTrue(any(it.kind == "task" for it in short_res.items))


if __name__ == "__main__":
    unittest.main()
