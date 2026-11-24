from __future__ import annotations

import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from db import repositories as repo
from db.database import connect, init_db
from handlers import home_supplies, home_tasks, household
from services import household_service
from utils.rows import rows_to_dicts
from utils.time import local_date_str


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


class HouseholdStage2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.u1_id = await repo.create_user(
            self.db,
            telegram_id=920001,
            name="Дима",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:30",
        )
        self.u2_id = await repo.create_user(
            self.db,
            telegram_id=920002,
            name="Алиса",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:30",
        )
        self.stu_id = await repo.create_user(
            self.db,
            telegram_id=920003,
            name="Ученик",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:30",
        )
        await repo.update_user_profile_mode(self.db, self.stu_id, "student")
        await repo.upsert_wellness(self.db, self.u1_id, meal_profile="vegan")
        await repo.upsert_wellness(self.db, self.u2_id, meal_profile="vegan")
        self.u1 = dict(await repo.get_user(self.db, self.u1_id))
        self.u2 = dict(await repo.get_user(self.db, self.u2_id))
        self.stu = dict(await repo.get_user(self.db, self.stu_id))

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def _join_couple(self) -> int:
        hid1 = await repo.get_or_create_household(self.db, self.u1_id)
        await repo.set_user_household(self.db, self.u2_id, hid1)
        self.u1 = dict(await repo.get_user(self.db, self.u1_id))
        self.u2 = dict(await repo.get_user(self.db, self.u2_id))
        return hid1

    async def test_1_shared_home_tasks_dedup_attribution_notify_and_toggle(self) -> None:
        today = local_date_str(datetime.datetime.utcnow(), "Europe/Moscow")
        await repo.ensure_regular_tasks(self.db, self.u1_id, today)
        await repo.ensure_regular_tasks(self.db, self.u2_id, today)
        hid1 = await self._join_couple()

        t_u1 = await repo.list_regular_tasks(self.db, self.u1_id)
        t_u2 = await repo.list_regular_tasks(self.db, self.u2_id)
        self.assertEqual(len(t_u1), 13)
        self.assertEqual([t["id"] for t in t_u1], [t["id"] for t in t_u2])

        target = t_u1[0]
        fake_bot = SimpleNamespace(send_message=AsyncMock())
        cb_done = SimpleNamespace(
            data=f"hweek:done:{target['id']}",
            from_user=SimpleNamespace(id=920002, full_name="Алиса"),
            bot=fake_bot,
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await home_tasks.plan_mark_done(cb_done, self.db)

        refreshed = next(t for t in await repo.list_regular_tasks(self.db, self.u1_id) if t["id"] == target["id"])
        self.assertEqual(refreshed["last_done_by_name"], "Алиса")
        self.assertIn("закрыл(а): Алиса", home_tasks._format_task_line(refreshed))
        fake_bot.send_message.assert_awaited_once()
        self.assertEqual(fake_bot.send_message.call_args.args[0], 920001)
        self.assertIn("Алиса", fake_bot.send_message.call_args.args[1])

        # Turn off share_home_tasks -> each partner sees only their own rows; leaving preserves u1's tasks
        await repo.update_household_settings(self.db, hid1, share_home_tasks=False)
        await repo.leave_household(self.db, self.u2_id)
        self.assertGreaterEqual(len(await repo.list_regular_tasks(self.db, self.u1_id)), 1)

    async def test_2_shared_supplies_attribution_shopping_and_add(self) -> None:
        await repo.ensure_supplies(self.db, self.u1_id)
        await repo.ensure_supplies(self.db, self.u2_id)
        hid1 = await self._join_couple()

        s_u1 = await repo.list_supplies(self.db, self.u1_id)
        s_u2 = await repo.list_supplies(self.db, self.u2_id)
        self.assertEqual(len(s_u1), 5)
        self.assertEqual([s["id"] for s in s_u1], [s["id"] for s in s_u2])

        sid = s_u1[0]["id"]
        sname = s_u1[0]["name"]
        fake_bot = SimpleNamespace(send_message=AsyncMock())
        cb_empty = SimpleNamespace(
            data=f"supply:set:{sid}:empty",
            from_user=SimpleNamespace(id=920002, full_name="Алиса"),
            bot=fake_bot,
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await home_supplies.supply_set(cb_empty, self.db)

        updated = await repo.get_supply_by_id(self.db, self.u1_id, sid)
        self.assertEqual(updated["status"], "empty")
        self.assertEqual(updated["updated_by_name"], "Алиса")
        fake_bot.send_message.assert_awaited_once()
        self.assertEqual(fake_bot.send_message.call_args.args[0], 920001)

        # 1-tap add to shared household shopping_list (idempotent on double tap)
        cb_shop = SimpleNamespace(
            data=f"supply:toshop:{sid}",
            from_user=SimpleNamespace(id=920002, full_name="Алиса"),
            answer=AsyncMock(),
        )
        await home_supplies.supply_to_shopping(cb_shop, self.db)
        await home_supplies.supply_to_shopping(cb_shop, self.db)
        shop_items = rows_to_dicts(await repo.list_shopping_items(self.db, self.u1_id, scope="household"))
        matching = [i for i in shop_items if i["item_name"] == sname]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["category"], "бытовое")

        # Add custom household supply -> visible to partner
        fsm = _MemFSM()
        await fsm.set_state(home_supplies.SupplyAddState.waiting_name)
        msg_add = SimpleNamespace(
            text="Таблетки для посудомойки",
            from_user=SimpleNamespace(id=920002, full_name="Алиса"),
            answer=AsyncMock(),
        )
        await home_supplies.supply_add_name(msg_add, fsm, self.db)
        s_u1_after = await repo.list_supplies(self.db, self.u1_id)
        self.assertTrue(any(s["name"] == "Таблетки для посудомойки" for s in s_u1_after))

        # Privacy toggle separates supplies
        await repo.update_household_settings(self.db, hid1, share_supplies=False)
        self.assertFalse(any(s["name"] == "Таблетки для посудомойки" for s in await repo.list_supplies(self.db, self.u1_id)))

    async def test_3_dinner_poll_voting_unanimous_match_and_reroll(self) -> None:
        hid1 = await self._join_couple()
        await repo.create_pantry_item(self.db, self.u1_id, "Гречка", 500, "g", None, "крупы")
        await repo.create_pantry_item(self.db, self.u1_id, "Нут", 400, "g", None, "бобовые")

        fake_bot = SimpleNamespace(send_message=AsyncMock())
        cb_start = SimpleNamespace(
            data="hh:vote:start",
            from_user=SimpleNamespace(id=920001, full_name="Дима"),
            bot=fake_bot,
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await household.hh_vote_start_cb(cb_start, self.db)
        today = local_date_str(datetime.datetime.utcnow(), "Europe/Moscow")
        poll = await repo.get_latest_open_dinner_poll(self.db, hid1, today)
        self.assertIsNotNone(poll)
        self.assertEqual(len(poll["options"]), 3)
        pid = poll["id"]
        orig_ids = {o["id"] for o in poll["options"]}

        # Partner 1 votes option 0 -> still open, Partner 2 notified
        cb_v1 = SimpleNamespace(
            data=f"hh:vote:{pid}:0",
            from_user=SimpleNamespace(id=920001, full_name="Дима"),
            bot=fake_bot,
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await household.hh_vote_cast_cb(cb_v1, self.db)
        p_mid = await repo.get_dinner_poll(self.db, pid)
        self.assertEqual(p_mid["status"], "open")
        self.assertEqual(fake_bot.send_message.call_args.args[0], 920002)

        # Partner 2 also votes option 0 -> unanimous match decides winner!
        fake_bot.send_message.reset_mock()
        cb_v2 = SimpleNamespace(
            data=f"hh:vote:{pid}:0",
            from_user=SimpleNamespace(id=920002, full_name="Алиса"),
            bot=fake_bot,
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await household.hh_vote_cast_cb(cb_v2, self.db)
        p_done = await repo.get_dinner_poll(self.db, pid)
        self.assertEqual(p_done["status"], "decided")
        self.assertEqual(p_done["winner_recipe_id"], poll["options"][0]["id"])
        self.assertEqual(fake_bot.send_message.call_args.args[0], 920001)
        self.assertIn("Совпадение", fake_bot.send_message.call_args.args[1])

        # Reroll picks a fresh poll avoiding previous option IDs
        cb_reroll = SimpleNamespace(
            data=f"hh:vote:reroll:{pid}",
            from_user=SimpleNamespace(id=920001, full_name="Дима"),
            bot=fake_bot,
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await household.hh_vote_reroll_cb(cb_reroll, self.db)
        new_poll = await repo.get_latest_open_dinner_poll(self.db, hid1, today)
        self.assertNotEqual(new_poll["id"], pid)
        new_ids = {o["id"] for o in new_poll["options"]}
        self.assertEqual(orig_ids & new_ids, set())

    async def test_4_partner_requests_quiet_hours_and_completion(self) -> None:
        hid1 = await self._join_couple()
        fake_bot = SimpleNamespace(send_message=AsyncMock())

        # Partner 2 has quiet_mode=1 -> saved in DB, NO ping sent
        await repo.set_quiet_mode(self.db, self.u2_id, True)
        self.u1 = dict(await repo.get_user(self.db, self.u1_id))
        req_q, notify_q = await household._send_household_request(fake_bot, self.db, self.u1, "Тихая просьба")
        self.assertEqual(req_q["status"], "open")
        self.assertEqual(notify_q["sent"], 0)
        self.assertEqual(notify_q["skipped_quiet"], ["Алиса"])
        fake_bot.send_message.assert_not_awaited()

        # Disable quiet_mode -> preset sends cleanly to Partner 2
        await repo.set_quiet_mode(self.db, self.u2_id, False)
        cb_pre = SimpleNamespace(
            data="hh:req:preset:trash",
            from_user=SimpleNamespace(id=920001, full_name="Дима"),
            bot=fake_bot,
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await household.hh_req_preset_cb(cb_pre, _MemFSM(), self.db)
        fake_bot.send_message.assert_awaited_once()
        self.assertEqual(fake_bot.send_message.call_args.args[0], 920002)

        active_reqs = await repo.list_household_requests(self.db, hid1, kind="request")
        trash_req = next(r for r in active_reqs if "мусор" in r["text"])

        # Partner 2 taps "Принято", then "Готово" -> +3 points + notifies Partner 1
        fake_bot.send_message.reset_mock()
        cb_acc = SimpleNamespace(
            data=f"hh:req:act:{trash_req['id']}:accepted",
            from_user=SimpleNamespace(id=920002, full_name="Алиса"),
            bot=fake_bot,
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await household.hh_req_action_cb(cb_acc, self.db)
        self.assertEqual((await repo.get_household_request(self.db, trash_req["id"]))["status"], "accepted")

        today = local_date_str(datetime.datetime.utcnow(), "Europe/Moscow")
        pts_before = await repo.points_today(self.db, self.u2_id, today)
        cb_done = SimpleNamespace(
            data=f"hh:req:act:{trash_req['id']}:done",
            from_user=SimpleNamespace(id=920002, full_name="Алиса"),
            bot=fake_bot,
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await household.hh_req_action_cb(cb_done, self.db)
        self.assertEqual((await repo.get_household_request(self.db, trash_req["id"]))["status"], "done")
        self.assertEqual(await repo.points_today(self.db, self.u2_id, today), pts_before + 3)
        self.assertEqual(fake_bot.send_message.call_args.args[0], 920001)

    async def test_5_care_nudges_thanks_karma_cooldown_and_weekly_summary(self) -> None:
        hid1 = await self._join_couple()
        fake_bot = SimpleNamespace(send_message=AsyncMock())
        now_day = datetime.datetime(2026, 11, 24, 11, 0)  # 14:00 MSK

        karma_before = int((await repo.get_or_create_karma_state(self.db, self.u2_id)).get("score") or 70)
        res_thx = await household_service.send_care_nudge(fake_bot, self.db, self.u1, "thanks", now_utc=now_day)
        self.assertTrue(res_thx["ok"])
        self.assertEqual(res_thx["sent"], 1)
        karma_after = int((await repo.get_or_create_karma_state(self.db, self.u2_id)).get("score") or 70)
        self.assertEqual(karma_after, karma_before + 5)

        # Duplicate within 2 hours hits cooldown
        res_dup = await household_service.send_care_nudge(
            fake_bot, self.db, self.u1, "thanks", now_utc=now_day + datetime.timedelta(minutes=30)
        )
        self.assertFalse(res_dup["ok"])
        self.assertTrue(res_dup["cooldown"])
        self.assertEqual(int((await repo.get_or_create_karma_state(self.db, self.u2_id)).get("score") or 70), karma_after)

        # Quiet hours block "water" nudge delivery cleanly
        await repo.set_quiet_mode(self.db, self.u2_id, True)
        res_q = await household_service.send_care_nudge(fake_bot, self.db, self.u1, "water", now_utc=now_day)
        self.assertTrue(res_q["ok"])
        self.assertEqual(res_q["sent"], 0)
        self.assertEqual(res_q["skipped_quiet"], ["Алиса"])

        # Weekly summary aggregates tasks, expenses, supplies, and nudges
        today = local_date_str(now_day, "Europe/Moscow")
        await repo.ensure_regular_tasks(self.db, self.u1_id, today)
        tasks = await repo.list_regular_tasks(self.db, self.u1_id)
        await repo.mark_regular_done(self.db, self.u2_id, tasks[0]["id"], today, actor_name="Алиса")
        await repo.add_expense(self.db, self.u1_id, 1500, "продукты", scope="household")
        await repo.ensure_supplies(self.db, self.u1_id)
        sups = await repo.list_supplies(self.db, self.u1_id)
        await repo.update_supply_status(self.db, self.u2_id, sups[0]["id"], "low", actor_name="Алиса")

        summary = await household_service.build_household_weekly_summary(self.db, self.u1, now_utc=now_day)
        self.assertIn("Закрыто домашних дел за 7 дней: <b>1</b>", summary)
        self.assertIn("Алиса: 1", summary)
        self.assertIn("1 500", summary.replace("\xa0", " "))
        self.assertIn(sups[0]["name"], summary)
        self.assertIn("благодарностей: 1", summary)

        # Student mode guard rejects /house & hh:hub
        msg_stu = SimpleNamespace(
            text="/house",
            from_user=SimpleNamespace(id=920003, full_name="Ученик"),
            answer=AsyncMock(),
        )
        await household.house_command(msg_stu, _MemFSM(), self.db)
        self.assertIn("В режиме ученика", msg_stu.answer.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
