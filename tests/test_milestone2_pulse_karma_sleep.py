import datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from db import repositories as repo
from db.database import connect, init_db
from handlers import movement, pulse
from scheduler.reminder import ReminderScheduler
from services import inbox_service, pulse_service


class Milestone2PulseKarmaSleepTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.tg_id = 982001
        self.u_id = await repo.create_user(
            self.db,
            self.tg_id,
            "Артем",
            "Europe/Moscow",
            "07:00",
            "23:30",
        )
        await repo.update_user_coach_settings(
            self.db,
            self.u_id,
            weight_goal="loss",
            coach_mode="strict",
        )
        self.user = dict(await repo.get_user(self.db, self.u_id))
        self.bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=self.tg_id), message_id=777)),
            edit_message_reply_markup=AsyncMock(),
            delete_message=AsyncMock(),
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def test_1_post_midnight_bedtime_fires_and_confirms_on_prev_cycle(self) -> None:
        await repo.update_user_sleep(self.db, self.u_id, "00:30")
        user = dict(await repo.get_user(self.db, self.u_id))

        # 00:20 MSK on Nov 26 = 21:20 UTC on Nov 25
        now_0020 = datetime.datetime(2026, 11, 25, 21, 20, 0)
        sent = await pulse_service.process_pulse_tick(self.db, self.bot, now_utc=now_0020)
        self.assertGreaterEqual(sent, 1)

        bed_prev = await repo.get_pulse_checkin(self.db, self.u_id, "2026-11-25", "bedtime")
        self.assertIsNotNone(bed_prev)
        self.assertIn(bed_prev["status"], {"pending", "done"})

        # Manual confirm_bedtime at 00:25 MSK attaches to 2026-11-25 cycle, not 2026-11-26
        res = await pulse_service.confirm_bedtime(
            self.db, user, now_utc=datetime.datetime(2026, 11, 25, 21, 25, 0)
        )
        self.assertTrue(res["ok"])
        bed_prev2 = await repo.get_pulse_checkin(self.db, self.u_id, "2026-11-25", "bedtime")
        self.assertEqual(bed_prev2["status"], "done")
        self.assertIsNone(await repo.get_pulse_checkin(self.db, self.u_id, "2026-11-26", "bedtime"))

    async def test_2_late_wake_on_ignored_rollover_closes_winddown_and_stale_expiry(self) -> None:
        # Late wake works on status="ignored"
        now_morn = datetime.datetime(2026, 11, 25, 8, 30, 0)  # 11:30 MSK
        chk_wake = await repo.upsert_pulse_checkin(
            self.db,
            self.u_id,
            "2026-11-25",
            "wake",
            "sleep",
            "2026-11-25T04:00:00",
            "2026-11-25T04:00:00",
            status="ignored",
        )
        res = await pulse_service.complete_checkin(self.db, self.user, int(chk_wake["id"]), now_utc=now_morn)
        self.assertTrue(res["late_wake_recorded"])
        self.assertEqual(res["local_time"], "11:30")

        # cb_pulse_rollover closes winddown checkin
        chk_wind = await repo.upsert_pulse_checkin(
            self.db,
            self.u_id,
            "2026-11-25",
            "winddown",
            "tasks",
            "2026-11-25T19:30:00",
            "2026-11-25T19:30:00",
            status="pending",
        )
        cb_roll = SimpleNamespace(
            data=f"pulse:rollover:{chk_wind['id']}",
            from_user=SimpleNamespace(id=self.tg_id, full_name="Артем"),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await pulse.cb_pulse_rollover(cb_roll, self.db)
        chk_wind_after = await repo.get_pulse_checkin_by_id(self.db, int(chk_wind["id"]))
        self.assertEqual(chk_wind_after["status"], "done")

        # Stale pending checkin from 2 days ago expires silently without firing
        stale = await repo.upsert_pulse_checkin(
            self.db,
            self.u_id,
            "2026-11-23",
            "dinner",
            "food",
            "2026-11-23T16:30:00",
            "2026-11-23T16:30:00",
            status="pending",
        )
        self.bot.send_message.reset_mock()
        await pulse_service.process_pulse_tick(
            self.db, self.bot, now_utc=datetime.datetime(2026, 11, 25, 11, 0, 0)
        )
        stale_after = await repo.get_pulse_checkin_by_id(self.db, int(stale["id"]))
        self.assertEqual(stale_after["status"], "skipped")
        sent_texts = [c.kwargs.get("text", "") for c in self.bot.send_message.await_args_list]
        self.assertFalse(any("Ужин" in t for t in sent_texts))

    async def test_3_one_night_shift_and_day_off_mode(self) -> None:
        # One-night shift_bedtime does NOT mutate permanent sleep_time
        now = datetime.datetime(2026, 11, 25, 15, 0, 0)
        out_sb = await inbox_service.capture_and_process_text(
            self.db,
            self.user,
            chat_id=self.tg_id,
            message_id=501,
            text="сегодня ложусь в 00:10",
            now_utc=now,
        )
        sb_act = next(a for a in out_sb.actions if a["item_kind"] == "shift_bedtime")
        ok, _ = await inbox_service.confirm_inbox_action(self.db, self.user, int(sb_act["id"]), now_utc=now)
        self.assertTrue(ok)
        u_after = dict(await repo.get_user(self.db, self.u_id))
        self.assertEqual(u_after["sleep_time"], "23:30")
        _, eff_today, _ = pulse_service.get_effective_schedule(u_after, "2026-11-25")
        _, eff_tom, _ = pulse_service.get_effective_schedule(u_after, "2026-11-26")
        self.assertEqual(eff_today, "00:10")
        self.assertEqual(eff_tom, "23:30")

        # "завтра без будильника" skips wake ping on 2026-11-26
        out_off = await inbox_service.capture_and_process_text(
            self.db,
            u_after,
            chat_id=self.tg_id,
            message_id=502,
            text="завтра без будильника",
            now_utc=now,
        )
        self.assertEqual(out_off.actions[0]["item_kind"], "shift_wake")
        u_off = dict(await repo.get_user(self.db, self.u_id))
        self.assertTrue(pulse_service.is_day_off(u_off, "2026-11-26"))

        self.bot.send_message.reset_mock()
        # 07:05 MSK on Nov 26 = 04:05 UTC
        await pulse_service.process_pulse_tick(
            self.db, self.bot, now_utc=datetime.datetime(2026, 11, 26, 4, 5, 0)
        )
        self.assertIsNone(await repo.get_pulse_checkin(self.db, self.u_id, "2026-11-26", "wake"))

    async def test_4_fair_karma_gentle_skip_skip_buttons_and_night_catch_cap(self) -> None:
        # Gentle breakfast honest skip -> 0 karma penalty
        bf = await repo.upsert_pulse_checkin(
            self.db,
            self.u_id,
            "2026-11-25",
            "breakfast",
            "food",
            "2026-11-25T05:00:00",
            "2026-11-25T05:00:00",
            meta_json=json.dumps({"gentle": True}),
        )
        skip_res = await pulse_service.skip_checkin(
            self.db, self.user, int(bf["id"]), now_utc=datetime.datetime(2026, 11, 25, 5, 30, 0)
        )
        self.assertEqual(skip_res["karma"]["delta"], 0)
        self.assertEqual(skip_res["karma"]["score_after"], 70)

        # afternoon & winddown keyboards include pulse:skip:
        ks = await repo.get_or_create_karma_state(self.db, self.u_id)
        for s_key in ("afternoon", "winddown"):
            chk = {"id": 900, "slot_key": s_key, "domain": "tasks", "meta_json": "{}"}
            _, kb = pulse_service.build_pulse_message(chk, ks, level=1, user=self.user)
            cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
            self.assertIn("pulse:skip:900", cbs)

        # check_night_violation ignores early wake text & caps -5 penalty once per night
        t_conf = datetime.datetime(2026, 11, 25, 20, 15, 0)  # 23:15 MSK
        await pulse_service.confirm_bedtime(self.db, self.user, now_utc=t_conf)
        u_conf = dict(await repo.get_user(self.db, self.u_id))
        self.assertEqual((await repo.get_or_create_karma_state(self.db, self.u_id))["score"], 75)

        self.assertIsNone(
            await pulse_service.check_night_violation(
                self.db, u_conf, now_utc=t_conf + datetime.timedelta(hours=1), text="я проснулся"
            )
        )
        n1 = await pulse_service.check_night_violation(
            self.db, u_conf, now_utc=datetime.datetime(2026, 11, 25, 22, 0, 0), text="не спится"
        )
        self.assertIn("-5", n1)
        u_c1 = dict(await repo.get_user(self.db, self.u_id))
        n2 = await pulse_service.check_night_violation(
            self.db, u_c1, now_utc=datetime.datetime(2026, 11, 25, 22, 45, 0), text="ещё сижу"
        )
        self.assertIsNotNone(n2)
        self.assertIn("Повторный штраф не ставлю", n2)
        self.assertEqual((await repo.get_or_create_karma_state(self.db, self.u_id))["score"], 70)

    async def test_5_weight_and_evening_nutrition_scheduler_fairness(self) -> None:
        # Seed smart_scales + 8-day-old weight so strict mode triggers weigh_in_missed
        await repo.update_user_body(
            self.db, self.u_id, height_cm=180, age_years=24, sex="male", equipment=["smart_scales"]
        )
        await repo.add_weight(self.db, self.u_id, 99.5, source="manual")
        await self.db.execute(
            "UPDATE weights SET created_at = '2026-11-16T06:00:00' WHERE user_id = ?",
            (self.u_id,),
        )
        await self.db.commit()

        sched = ReminderScheduler(self.bot, self.db)
        now_morn = datetime.datetime(2026, 11, 25, 4, 30, 0)  # 07:30 MSK
        # If send fails, karma must NOT be docked!
        self.bot.send_message = AsyncMock(side_effect=RuntimeError("network error"))
        sent_fail = await sched._tick_weight_prompt(now_utc=now_morn)
        self.assertEqual(sent_fail, 0)
        self.assertEqual((await repo.get_or_create_karma_state(self.db, self.u_id))["score"], 70)

        # Next day: succeeds -> -5 once
        self.bot.send_message = AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=self.tg_id), message_id=10))
        sent1 = await sched._tick_weight_prompt(now_utc=now_morn)
        self.assertEqual(sent1, 1)
        self.assertEqual((await repo.get_or_create_karma_state(self.db, self.u_id))["score"], 65)

        # Day after: no 2nd -5 penalty within 7 days!
        sent2 = await sched._tick_weight_prompt(now_utc=now_morn + datetime.timedelta(days=1))
        self.assertEqual(sent2, 1)
        self.assertEqual((await repo.get_or_create_karma_state(self.db, self.u_id))["score"], 65)

        # fit:weigh_skip snoozes prompts for 3 days
        cb_skip = SimpleNamespace(
            data="fit:weigh_skip",
            from_user=SimpleNamespace(id=self.tg_id, full_name="Артем"),
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        await movement.fit_weigh_skip_cb(cb_skip, self.db)
        sent3 = await sched._tick_weight_prompt(now_utc=now_morn + datetime.timedelta(days=2))
        self.assertEqual(sent3, 0)

        # Evening nutrition respects quiet_mode AND does not push 3-min start after 21:00
        await repo.add_meal_log(self.db, self.u_id, "2026-11-25", "Гречка", kcal=500, protein_g=20)
        await repo.set_quiet_mode(self.db, self.u_id, True)
        now_ev = datetime.datetime(2026, 11, 25, 19, 0, 0)  # 22:00 MSK (90 min to 23:30 sleep)
        self.assertEqual(await sched._tick_nutrition_evening(now_utc=now_ev), 0)

        await repo.set_quiet_mode(self.db, self.u_id, False)
        self.bot.send_message.reset_mock()
        self.assertEqual(await sched._tick_nutrition_evening(now_utc=now_ev), 1)
        call_ev = self.bot.send_message.await_args
        self.assertIn("фокус на сон", call_ev.kwargs["text"])
        ev_cbs = [b.callback_data for row in call_ev.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertNotIn("fit:lazy", ev_cbs)
        self.assertIn("pulse:hub", ev_cbs)


if __name__ == "__main__":
    unittest.main()
