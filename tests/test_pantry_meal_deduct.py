from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from db import repositories as repo
from db.database import connect, init_db
from handlers import nutrition
from services import inbox_parser, inbox_service, kitchen_ai
from utils.rows import rows_to_dicts


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


class PantryMealDeductTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.tg_id = 970001
        self.uid = await repo.create_user(self.db, self.tg_id, "Федор", "Europe/Moscow", "08:00", "23:30")
        self.user = dict(await repo.get_user(self.db, self.uid))

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def test_1_partial_pantry_remove_by_pieces_and_undo(self) -> None:
        pid = await repo.upsert_pantry_item_merged(self.db, self.uid, name="Яблоки Гала", amount=5.0, unit="шт")
        now = dt.datetime(2026, 11, 25, 9, 0, 0)
        with patch.object(inbox_parser.LLMClient, "is_configured", return_value=False):
            out = await inbox_service.capture_and_process_text(
                self.db,
                self.user,
                chat_id=self.tg_id,
                message_id=101,
                text="Убери одно яблоко из холодильника",
                now_utc=now,
            )
        pantry = rows_to_dicts(await repo.list_pantry_items(self.db, self.uid))
        self.assertEqual(len(pantry), 1)
        self.assertEqual(int(pantry[0]["id"]), pid)
        self.assertAlmostEqual(float(pantry[0]["amount"]), 4.0, places=2)
        self.assertIn("Списала из холодильника", out.reply_text)
        self.assertIn("осталось 4 шт", out.reply_text)

        # Undo restores back to 5 шт
        await inbox_service.undo_inbox_message(self.db, self.user, out.inbox_id)
        pantry_restored = rows_to_dicts(await repo.list_pantry_items(self.db, self.uid))
        self.assertAlmostEqual(float(pantry_restored[0]["amount"]), 5.0, places=2)

    async def test_2_partial_pantry_remove_pieces_from_kg_and_full_remove_without_count(self) -> None:
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Яблоки Гала", amount=1.0, unit="kg")
        now = dt.datetime(2026, 11, 25, 9, 5, 0)
        with patch.object(inbox_parser.LLMClient, "is_configured", return_value=False):
            out1 = await inbox_service.capture_and_process_text(
                self.db,
                self.user,
                chat_id=self.tg_id,
                message_id=102,
                text="Убери одно яблоко из холодильника",
                now_utc=now,
            )
        pantry1 = rows_to_dicts(await repo.list_pantry_items(self.db, self.uid))
        self.assertEqual(len(pantry1), 1)
        self.assertAlmostEqual(float(pantry1[0]["amount"]), 0.82, places=2)

        await inbox_service.undo_inbox_message(self.db, self.user, out1.inbox_id)
        pantry_undo = rows_to_dicts(await repo.list_pantry_items(self.db, self.uid))
        self.assertAlmostEqual(float(pantry_undo[0]["amount"]), 1.0, places=2)

        # Without count -> removes whole item, Undo brings it back
        with patch.object(inbox_parser.LLMClient, "is_configured", return_value=False):
            out2 = await inbox_service.capture_and_process_text(
                self.db,
                self.user,
                chat_id=self.tg_id,
                message_id=103,
                text="Убери яблоки из холодильника",
                now_utc=now + dt.timedelta(minutes=1),
            )
        self.assertEqual(len(await repo.list_pantry_items(self.db, self.uid)), 0)
        await inbox_service.undo_inbox_message(self.db, self.user, out2.inbox_id)
        self.assertEqual(len(await repo.list_pantry_items(self.db, self.uid)), 1)

    async def test_3_kbju_confirm_attaches_pantry_deduct_button_without_false_milk_match(self) -> None:
        apple_id = await repo.upsert_pantry_item_merged(self.db, self.uid, name="Яблоки Гала", amount=4.0, unit="шт")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Овсяное молоко", amount=1.0, unit="l")

        fsm = _MemFSM()
        await fsm.update_data(
            meal_preview={
                "ok": True,
                "title": "Яблоко и кофе со сгущённым молоком",
                "raw_input": "одно яблоко и кофе со сгущеным молоком (2 ложки)",
                "meal_slot": "snack",
                "kcal": 155,
                "protein_g": 3,
                "fat_g": 1,
                "carbs_g": 34,
                "used_llm": False,
            }
        )
        cb = _fake_cb("kbju:confirm", tg_id=self.tg_id)
        await nutrition.kbju_confirm_cb(cb, fsm, self.db)

        kb = cb.message.edit_text.await_args.kwargs["reply_markup"]
        deduct_btns = [
            btn
            for row in kb.inline_keyboard
            for btn in row
            if (btn.callback_data or "").startswith("kbju:pantry_deduct:")
        ]
        self.assertEqual(len(deduct_btns), 1)
        self.assertEqual(deduct_btns[0].callback_data, f"kbju:pantry_deduct:{apple_id}:1")
        self.assertIn("Яблоки Гала", deduct_btns[0].text)

        # Tap the deduct button -> subtracts 1 apple & removes button cleanly
        cb_ded = _fake_cb(deduct_btns[0].callback_data, tg_id=self.tg_id, reply_markup=kb)
        await nutrition.kbju_pantry_deduct_cb(cb_ded, self.db)
        pantry = rows_to_dicts(await repo.list_pantry_items(self.db, self.uid))
        apples = next(p for p in pantry if int(p["id"]) == apple_id)
        self.assertAlmostEqual(float(apples["amount"]), 3.0, places=2)
        self.assertIn("осталось 3 шт", cb_ded.answer.await_args.args[0])

        updated_kb = cb_ded.message.edit_reply_markup.await_args.kwargs["reply_markup"]
        self.assertFalse(
            any(
                (btn.callback_data or "").startswith(f"kbju:pantry_deduct:{apple_id}:")
                for row in updated_kb.inline_keyboard
                for btn in row
            )
        )

        # Second tap on stale copy doesn't crash
        cb_stale = _fake_cb(deduct_btns[0].callback_data, tg_id=self.tg_id)
        await nutrition.kbju_pantry_deduct_cb(cb_stale, self.db)
        self.assertIn("Списала", cb_stale.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
