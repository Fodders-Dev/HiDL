import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from db import repositories as repo
from db.database import connect, init_db
from handlers import finance, settings, start
from services import household_service, kitchen_ai


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


class HouseholdStage1Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.u1_id = await repo.create_user(
            self.db,
            telegram_id=910001,
            name="Дима",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:30",
        )
        self.u2_id = await repo.create_user(
            self.db,
            telegram_id=910002,
            name="Алиса",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:30",
        )
        await repo.upsert_wellness(self.db, self.u1_id, meal_profile="vegan")
        await repo.upsert_wellness(self.db, self.u2_id, meal_profile="vegan")
        self.u1 = dict(await repo.get_user(self.db, self.u1_id))
        self.u2 = dict(await repo.get_user(self.db, self.u2_id))

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def test_1_random_invite_deeplink_members_regen_and_leave(self) -> None:
        hid1 = await repo.get_or_create_household(self.db, self.u1_id)
        house = await repo.get_household(self.db, hid1)
        code = house["invite_code"]
        self.assertTrue(code.startswith("H-") and len(code) == 10, f"Unexpected code: {code}")

        # Deep-link /start house_<code> for existing user u2
        fake_bot = SimpleNamespace(send_message=AsyncMock())
        msg = SimpleNamespace(
            text=f"/start house_{code}",
            from_user=SimpleNamespace(id=910002, first_name="Алиса"),
            bot=fake_bot,
            answer=AsyncMock(),
        )
        await start.cmd_start(msg, _MemFSM(), self.db)
        u2_after = dict(await repo.get_user(self.db, self.u2_id))
        self.assertEqual(int(u2_after["household_id"]), hid1)
        members = await repo.list_household_members(self.db, hid1)
        self.assertEqual([m["id"] for m in members], [self.u1_id, self.u2_id])
        fake_bot.send_message.assert_awaited_once()
        self.assertEqual(fake_bot.send_message.call_args.args[0], 910001)

        # Regenerate code invalidates old code
        new_code = await repo.regenerate_household_invite_code(self.db, hid1)
        self.assertNotEqual(new_code, code)
        self.assertIsNone(await repo.get_household_by_code(self.db, code))
        self.assertIsNotNone(await repo.get_household_by_code(self.db, new_code))

        # Brand new user onboarding with /start house_<new_code> auto-joins after reg_sleep
        st_new = _MemFSM()
        msg_new = SimpleNamespace(
            text=f"/start house_{new_code}",
            from_user=SimpleNamespace(id=910003, first_name="Гость"),
            answer=AsyncMock(),
        )
        await start.cmd_start(msg_new, st_new, self.db)
        await st_new.update_data(name="Гость", gender="neutral", timezone="Europe/Moscow", wake_up="08:00")
        msg_sleep = SimpleNamespace(
            text="23:30",
            from_user=SimpleNamespace(id=910003, full_name="Гость"),
            bot=fake_bot,
            answer=AsyncMock(),
        )
        await start.reg_sleep(msg_sleep, st_new, self.db)
        u3 = dict(await repo.get_user_by_telegram_id(self.db, 910003))
        self.assertEqual(int(u3["household_id"]), hid1)

        # Leave household creates a separate private household
        new_hid3 = await repo.leave_household(self.db, u3["id"])
        self.assertNotEqual(new_hid3, hid1)
        self.assertEqual([m["id"] for m in await repo.list_household_members(self.db, hid1)], [self.u1_id, self.u2_id])

    async def test_2_shared_user_recipes_and_meal_plans_respect_toggles(self) -> None:
        hid1 = await repo.get_or_create_household(self.db, self.u1_id)
        await repo.set_user_household(self.db, self.u2_id, hid1)
        self.u2 = dict(await repo.get_user(self.db, self.u2_id))

        saved = await repo.upsert_user_recipe(
            self.db,
            self.u1_id,
            title="Общий тофу-скрэмбл",
            tags=["vegan", "breakfast"],
            ingredients=[{"name": "Тофу", "qty": 150, "unit": "g"}],
            steps=["Обжарь тофу."],
            source="pdf_book",
        )
        rid = saved["id"]

        # Partner 2 immediately sees Partner 1's user_recipe
        recs_u2 = await kitchen_ai.load_recipes_for_user(self.db, self.u2_id)
        self.assertTrue(any(r["id"] == rid for r in recs_u2))
        self.assertIsNotNone(await kitchen_ai.get_recipe_for_user(self.db, self.u2_id, rid))

        # Partner 1 generates a 3-day meal plan -> Partner 2 sees the exact same plan
        plan_u1 = await kitchen_ai.generate_meal_plan(
            self.db, self.u1, days=3, start_date="2026-11-23"
        )
        plan_u2 = await kitchen_ai.get_meal_plan_view(
            self.db, self.u2, days=3, start_date="2026-11-23", auto_generate=False
        )
        self.assertEqual(
            [(r["plan_date"], r["meal_slot"], r["title"]) for r in plan_u1["items"]],
            [(r["plan_date"], r["meal_slot"], r["title"]) for r in plan_u2["items"]],
        )

        # Toggle share_recipes & share_meal_plan off -> Partner 2 no longer sees u1's recipes or plan
        await repo.update_household_settings(self.db, hid1, share_recipes=False, share_meal_plan=False)
        recs_u2_priv = await kitchen_ai.load_recipes_for_user(self.db, self.u2_id)
        self.assertFalse(any(r["id"] == rid for r in recs_u2_priv))
        plan_u2_priv = await repo.list_meal_plans_range(self.db, self.u2_id, "2026-11-23", "2026-11-25")
        self.assertEqual(plan_u2_priv, [])

    async def test_3_shared_bills_and_household_expenses_privacy(self) -> None:
        hid1 = await repo.get_or_create_household(self.db, self.u1_id)
        await repo.set_user_household(self.db, self.u2_id, hid1)

        # Personal vs Household bills
        b_priv = await repo.upsert_bill(self.db, self.u1_id, "Личный телефон", 600, 10, scope="personal")
        b_house = await repo.upsert_bill(self.db, self.u1_id, "ЖКХ", 5000, 15, scope="personal")
        new_scope = await repo.toggle_bill_scope(self.db, self.u1_id, b_house)
        self.assertEqual(new_scope, "household")

        bills_u2_titles = [b["title"] for b in await repo.list_bills(self.db, self.u2_id)]
        self.assertIn("ЖКХ", bills_u2_titles)
        self.assertNotIn("Личный телефон", bills_u2_titles)

        # Partner 2 marks household bill paid -> notifies Partner 1 & shows paid_by_name
        fake_bot = SimpleNamespace(send_message=AsyncMock())
        cb_msg = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
        cb_pay = SimpleNamespace(
            data=f"bill:pay:{b_house}",
            from_user=SimpleNamespace(id=910002, full_name="Алиса"),
            bot=fake_bot,
            message=cb_msg,
            answer=AsyncMock(),
        )
        await finance.bill_pay(cb_pay, self.db)
        edited_text = cb_msg.edit_text.call_args.args[0]
        self.assertIn("🏠 ЖКХ", edited_text)
        self.assertIn("оплатил(а) Алиса", edited_text)
        fake_bot.send_message.assert_awaited_once()
        self.assertEqual(fake_bot.send_message.call_args.args[0], 910001)
        # Paid household bill is excluded from due_soon for both partners
        cur_date = datetime.date.today().replace(day=13).isoformat()
        self.assertEqual(await repo.bills_due_soon(self.db, self.u1_id, cur_date, days_ahead=3), [])

        # Expenses: personal stays out of household monthly summary
        await repo.add_expense(self.db, self.u1_id, 1200, "личное", scope="personal")
        await repo.add_expense(self.db, self.u1_id, 3000, "продукты", scope="household")
        await repo.add_expense(self.db, self.u2_id, 1500, "химия", scope="household")
        summary = await repo.household_monthly_expenses_summary(self.db, hid1)
        self.assertEqual(summary["total"], 4500.0)
        self.assertEqual(
            {m["name"]: m["total"] for m in summary["by_member"]},
            {"Дима": 3000.0, "Алиса": 1500.0},
        )
        self.assertEqual(summary["by_category"], {"продукты": 3000.0, "химия": 1500.0})

    async def test_4_notify_household_partners_respects_quiet_and_toggles(self) -> None:
        hid1 = await repo.get_or_create_household(self.db, self.u1_id)
        await repo.set_user_household(self.db, self.u2_id, hid1)
        self.u1 = dict(await repo.get_user(self.db, self.u1_id))

        bot = SimpleNamespace(send_message=AsyncMock())
        # Daytime 14:00 MSK (11:00 UTC) -> sends to u2 only
        sent_day = await household_service.notify_household_partners(
            bot, self.db, self.u1, "Привет дом!", now_utc=datetime.datetime(2026, 11, 23, 11, 0)
        )
        self.assertEqual(sent_day, 1)
        bot.send_message.assert_awaited_once_with(910002, "Привет дом!", parse_mode="HTML")

        # Night quiet hours 02:00 MSK (23:00 UTC) inside u2 sleep_time 23:30..08:00 -> skipped!
        bot.send_message.reset_mock()
        sent_night = await household_service.notify_household_partners(
            bot, self.db, self.u1, "Ночной тест", now_utc=datetime.datetime(2026, 11, 23, 23, 0)
        )
        self.assertEqual(sent_night, 0)
        bot.send_message.assert_not_awaited()

        # Disabled notify_partner toggle -> skipped even during daytime
        await repo.update_household_settings(self.db, hid1, notify_partner=False)
        sent_off = await household_service.notify_household_partners(
            bot, self.db, self.u1, "Выключено", now_utc=datetime.datetime(2026, 11, 23, 11, 0)
        )
        self.assertEqual(sent_off, 0)


if __name__ == "__main__":
    unittest.main()
