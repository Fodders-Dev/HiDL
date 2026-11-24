"""Tests for smart after-wake breakfast, 3-meal Pulse slots, and 1-tap KBJU buttons."""

from __future__ import annotations

import datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from db import repositories as repo
from db.database import connect, init_db
from handlers import nutrition
from services import kitchen_ai, pulse_service


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


class BreakfastPulseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.tg_id = 771001
        self.uid = await repo.create_user(
            self.db,
            telegram_id=self.tg_id,
            name="Артем",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:30",
        )
        await repo.upsert_wellness(self.db, self.uid, meal_profile="vegan")
        self.user = dict(await repo.get_user(self.db, self.uid))

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def test_1_compute_daily_slots_2x_vs_3x_and_student_mode(self) -> None:
        # 1. Default 2-time meal_times -> 6 slots, no breakfast
        slots_2x = pulse_service.compute_daily_slots(
            self.user, {"meal_times": "13:00,19:00"}, "2026-11-24"
        )
        keys_2x = [s["slot_key"] for s in slots_2x]
        self.assertEqual(keys_2x, ["wake", "lunch", "afternoon", "dinner", "winddown", "bedtime"])

        # 2. 3-time meal_times -> 7 slots including breakfast
        slots_3x = pulse_service.compute_daily_slots(
            self.user, {"meal_times": "08:30,13:00,19:30"}, "2026-11-24"
        )
        keys_3x = [s["slot_key"] for s in slots_3x]
        self.assertEqual(
            keys_3x,
            ["wake", "breakfast", "lunch", "afternoon", "dinner", "winddown", "bedtime"],
        )
        by_key = {s["slot_key"]: s for s in slots_3x}
        self.assertEqual(by_key["breakfast"]["local_hhmm"], "08:30")
        self.assertEqual(by_key["lunch"]["local_hhmm"], "13:00")
        self.assertEqual(by_key["dinner"]["local_hhmm"], "19:30")

        # 3. Student mode skips breakfast/lunch/dinner even with 3-time meal_times
        await repo.update_user_profile_mode(self.db, self.uid, "student")
        stu_user = dict(await repo.get_user(self.db, self.uid))
        slots_stu = pulse_service.compute_daily_slots(
            stu_user, {"meal_times": "08:30,13:00,19:30"}, "2026-11-24"
        )
        keys_stu = [s["slot_key"] for s in slots_stu]
        self.assertEqual(keys_stu, ["wake", "afternoon", "winddown", "bedtime"])

    async def test_2_after_wake_schedules_gentle_breakfast_and_respects_lunch_cutoff(self) -> None:
        # Wake at 08:00 MSK (05:00 UTC) -> gentle breakfast scheduled at 08:25 MSK
        wake_utc = datetime.datetime(2026, 11, 24, 5, 0)
        chk = await repo.upsert_pulse_checkin(
            self.db,
            self.uid,
            "2026-11-24",
            "wake",
            "sleep",
            wake_utc.isoformat(),
            wake_utc.isoformat(),
        )
        res = await pulse_service.complete_checkin(self.db, self.user, int(chk["id"]), now_utc=wake_utc)
        self.assertTrue(res["ok"])
        self.assertEqual(res.get("breakfast_scheduled_hhmm"), "08:25")

        bf_chk = await repo.get_pulse_checkin(self.db, self.uid, "2026-11-24", "breakfast")
        self.assertIsNotNone(bf_chk)
        self.assertEqual(bf_chk["status"], "pending")
        bf_meta = json.loads(bf_chk["meta_json"])
        self.assertTrue(bf_meta.get("gentle"))
        self.assertTrue(bf_meta.get("after_wake"))

        # Late wake at 12:45 MSK (09:45 UTC) on another day -> too close to 13:00 lunch, no breakfast
        late_utc = datetime.datetime(2026, 11, 25, 9, 45)
        chk_late = await repo.upsert_pulse_checkin(
            self.db,
            self.uid,
            "2026-11-25",
            "wake",
            "sleep",
            late_utc.isoformat(),
            late_utc.isoformat(),
        )
        res_late = await pulse_service.complete_checkin(
            self.db, self.user, int(chk_late["id"]), now_utc=late_utc
        )
        self.assertIsNone(res_late.get("breakfast_scheduled_hhmm"))
        self.assertIsNone(await repo.get_pulse_checkin(self.db, self.uid, "2026-11-25", "breakfast"))

        # Breakfast already logged before wake on another day -> no duplicate breakfast ping
        early_utc = datetime.datetime(2026, 11, 26, 5, 0)
        await repo.add_meal_log(
            self.db,
            self.uid,
            "2026-11-26",
            "Овсянка на завтрак",
            meal_slot="breakfast",
            kcal=320,
        )
        chk_logged = await repo.upsert_pulse_checkin(
            self.db,
            self.uid,
            "2026-11-26",
            "wake",
            "sleep",
            early_utc.isoformat(),
            early_utc.isoformat(),
        )
        res_logged = await pulse_service.complete_checkin(
            self.db, self.user, int(chk_logged["id"]), now_utc=early_utc
        )
        self.assertIsNone(res_logged.get("breakfast_scheduled_hhmm"))
        self.assertIsNone(await repo.get_pulse_checkin(self.db, self.uid, "2026-11-26", "breakfast"))

    async def test_3_gentle_breakfast_no_karma_penalty_and_kbju_buttons_on_food_cards(self) -> None:
        # Verify food cards & wake checklist have kbju:add
        ks = await repo.get_or_create_karma_state(self.db, self.uid)
        start_score = int(ks["score"])
        for slot_key, dom in (("breakfast", "food"), ("lunch", "food"), ("dinner", "food")):
            row = await repo.upsert_pulse_checkin(
                self.db,
                self.uid,
                "2026-11-24",
                slot_key,
                dom,
                "2026-11-24T05:25:00",
                "2026-11-24T05:25:00",
                meta_json=json.dumps({"gentle": True}) if slot_key == "breakfast" else "{}",
            )
            _, kb = pulse_service.build_pulse_message(row, ks, level=1, user=self.user)
            self.assertIn("kbju:add", _button_datas(kb), f"Missing kbju:add on {slot_key}")

        adult_wake_kb = pulse_service.build_wake_checklist_keyboard(1, {}, is_student=False)
        self.assertIn("kbju:add", _button_datas(adult_wake_kb))
        stu_wake_kb = pulse_service.build_wake_checklist_keyboard(1, {}, is_student=True)
        self.assertNotIn("kbju:add", _button_datas(stu_wake_kb))

        # Mark wake done so tick isolates breakfast lifecycle cleanly
        await repo.upsert_pulse_checkin(
            self.db,
            self.uid,
            "2026-11-24",
            "wake",
            "sleep",
            "2026-11-24T05:00:00",
            "2026-11-24T05:00:00",
            status="done",
        )

        # Test gentle breakfast ping lifecycle -> 2 pings then skipped with ZERO karma loss
        sent_texts: list[str] = []

        async def _fake_send(chat_id, text, **kwargs):
            sent_texts.append(text)
            return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=len(sent_texts))

        bot = SimpleNamespace(send_message=AsyncMock(side_effect=_fake_send))
        t1 = datetime.datetime(2026, 11, 24, 5, 25)
        await pulse_service.process_pulse_tick(self.db, bot, now_utc=t1)
        self.assertTrue(any("Время завтрака" in t for t in sent_texts))

        # Trigger ping 2 (after 20m)
        t2 = t1 + datetime.timedelta(minutes=21)
        await pulse_service.process_pulse_tick(self.db, bot, now_utc=t2)
        self.assertTrue(any("Мягко напоминаю про завтрак" in t for t in sent_texts))

        # Trigger 3rd tick -> gentle breakfast becomes skipped with NO karma penalty
        t3 = t2 + datetime.timedelta(minutes=21)
        await pulse_service.process_pulse_tick(self.db, bot, now_utc=t3)
        bf_after = await repo.get_pulse_checkin(self.db, self.uid, "2026-11-24", "breakfast")
        self.assertEqual(bf_after["status"], "skipped")
        ks_after = await repo.get_or_create_karma_state(self.db, self.uid)
        self.assertEqual(int(ks_after["score"]), start_score)

    async def test_4_logging_breakfast_in_kbju_or_cooking_auto_closes_pulse(self) -> None:
        # Case A: logging breakfast via nutrition.kbju_confirm_cb closes open breakfast check-in for today
        from utils.time import local_date_str

        now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        today_local = local_date_str(now_utc, "Europe/Moscow")
        await repo.upsert_pulse_checkin(
            self.db,
            self.uid,
            today_local,
            "breakfast",
            "food",
            now_utc.isoformat(),
            now_utc.isoformat(),
            meta_json=json.dumps({"after_wake": True, "gentle": True}),
        )
        fsm = _InMemoryFSM()
        await fsm.update_data(
            meal_preview={
                "ok": True,
                "title": "Овсянка с бананом",
                "meal_slot": "breakfast",
                "kcal": 340,
                "protein_g": 11.0,
                "fat_g": 6.0,
                "carbs_g": 60.0,
                "used_llm": False,
            }
        )
        cb_msg = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
        cb = SimpleNamespace(
            data="kbju:confirm",
            from_user=SimpleNamespace(id=self.tg_id, full_name="Артем"),
            message=cb_msg,
            answer=AsyncMock(),
        )
        await nutrition.kbju_confirm_cb(cb, fsm, self.db)
        bf_row = await repo.get_pulse_checkin(self.db, self.uid, today_local, "breakfast")
        self.assertIsNotNone(bf_row)
        self.assertEqual(bf_row["status"], "done")

        # Case B: cooking a breakfast recipe via kitchen_ai.commit_cooked_recipe closes breakfast Pulse
        next_day = datetime.date.fromisoformat(today_local) + datetime.timedelta(days=1)
        next_day_str = next_day.isoformat()
        bf2_utc = datetime.datetime(next_day.year, next_day.month, next_day.day, 5, 25)
        await repo.upsert_pulse_checkin(
            self.db,
            self.uid,
            next_day_str,
            "breakfast",
            "food",
            bf2_utc.isoformat(),
            bf2_utc.isoformat(),
        )
        saved_rec = await repo.upsert_user_recipe(
            self.db,
            self.uid,
            title="Быстрая овсянка к завтраку",
            tags=["vegan", "breakfast"],
            base_servings=1,
            time_minutes=10,
            ingredients=[{"name": "Овсянка", "qty": 60, "unit": "g"}],
            steps=["Залей кипятком и дай постоять 5 минут."],
        )
        cook_res = await kitchen_ai.commit_cooked_recipe(
            self.db,
            self.user,
            saved_rec["id"],
            servings=1,
            meal_slot="breakfast",
            now_utc=bf2_utc,
        )
        self.assertTrue(cook_res["ok"])
        self.assertEqual(cook_res["pulse_closed"], "breakfast")
        bf2_row = await repo.get_pulse_checkin(self.db, self.uid, next_day_str, "breakfast")
        self.assertEqual(bf2_row["status"], "done")


if __name__ == "__main__":
    unittest.main()
