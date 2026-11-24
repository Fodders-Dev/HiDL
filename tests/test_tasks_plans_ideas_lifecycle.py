import datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from db import repositories as repo
from db.database import connect, init_db
from handlers import inbox, pulse
from llm_client import LLMResult
from scheduler.reminder import ReminderScheduler
from services import context_builder, inbox_service, pulse_service
from utils.today import render_today


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


class _FixedTaskLLM:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def is_configured(self) -> bool:
        return True

    async def complete_json(self, system_prompt: str, user_prompt: str, **kwargs) -> LLMResult:
        return LLMResult(ok=True, text=json.dumps(self._payload), data=self._payload)


class TasksPlansIdeasLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.u_id = await repo.create_user(
            self.db,
            telegram_id=970001,
            name="Фоддер",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:30",
        )
        # Ensure modern Pulse mode (legacy_pings_enabled=0)
        await self.db.execute(
            "UPDATE users SET pulse_enabled = 1, legacy_pings_enabled = 0 WHERE id = ?",
            (self.u_id,),
        )
        await self.db.commit()
        self.user = dict(await repo.get_user(self.db, self.u_id))

    async def asyncTearDown(self) -> None:
        await self.db.close()

    async def test_1_task_tomorrow_date_hint_and_overdue_carryover(self) -> None:
        # User writes at 23:12 MSK on 2026-11-23 (20:12 UTC) about "завтра" -> must save on 2026-11-24!
        now_nov23_night = datetime.datetime(2026, 11, 23, 20, 12, 0)
        llm = _FixedTaskLLM(
            {
                "intent": "capture",
                "query_type": "none",
                "query_term": "",
                "reply": "",
                "items": [
                    {
                        "kind": "task",
                        "title": "Купить билеты до Новороссийска",
                        "source_quote": "завтра надо будет купить билеты до Новороссийска",
                        "date_hint": "завтра",
                    }
                ],
            }
        )
        out = await inbox_service.capture_and_process_text(
            self.db,
            self.user,
            chat_id=10,
            message_id=1,
            text="завтра надо будет купить билеты до Новороссийска",
            now_utc=now_nov23_night,
            llm_client_instance=llm,
        )
        self.assertIn("на 24.11", out.reply_text)
        nov24_items = await repo.list_day_plan_items(self.db, self.u_id, "2026-11-24")
        self.assertEqual([i["title"] for i in nov24_items], ["Купить билеты до Новороссийска"])
        nov23_items = await repo.list_day_plan_items(self.db, self.u_id, "2026-11-23")
        self.assertEqual(nov23_items, [])

        # Add 1 undone and 1 done task on 2026-11-23 -> sync to 2026-11-25 carries over BOTH undone tasks cleanly
        undone_id = await repo.add_day_plan_item(self.db, self.u_id, "2026-11-23", "Починить ванну")
        done_id = await repo.add_day_plan_item(self.db, self.u_id, "2026-11-23", "Уже сделано вчера")
        await repo.mark_day_plan_item_done(self.db, done_id, True)

        moved = await repo.sync_overdue_day_plan_items(self.db, self.u_id, "2026-11-25")
        self.assertEqual(moved, 2)
        nov25_titles = [i["title"] for i in await repo.list_day_plan_items(self.db, self.u_id, "2026-11-25")]
        self.assertIn("Купить билеты до Новороссийска", nov25_titles)
        self.assertIn("Починить ванну", nov25_titles)
        self.assertNotIn("Уже сделано вчера", nov25_titles)
        # Re-running sync is idempotent and undone item kept its id
        self.assertEqual(await repo.sync_overdue_day_plan_items(self.db, self.u_id, "2026-11-25"), 0)
        self.assertEqual(
            [i["id"] for i in await repo.list_day_plan_items(self.db, self.u_id, "2026-11-25") if i["title"] == "Починить ванну"],
            [undone_id],
        )

    async def test_2_morning_day_plan_tick_runs_on_pulse_users_and_is_idempotent(self) -> None:
        await repo.add_day_plan_item(self.db, self.u_id, "2026-11-24", "Заказать продукты")
        await repo.add_day_plan_item(self.db, self.u_id, "2026-11-24", "Поздравление папе", is_important=True)

        # 06:00 UTC = 09:00 MSK on 2026-11-25 (within wake 08:00..11:00 window)
        now_morn = datetime.datetime(2026, 11, 25, 6, 0, 0)
        bot_mock = SimpleNamespace(send_message=AsyncMock())
        sched = ReminderScheduler(bot_mock, self.db)

        sent = await sched._tick_day_plan(now_utc=now_morn)
        self.assertEqual(sent, 1)
        call = bot_mock.send_message.call_args
        msg_txt = call.kwargs.get("text") if call.kwargs and "text" in call.kwargs else call.args[1]
        self.assertIn("Твои дела на сегодня", msg_txt)
        self.assertIn("Заказать продукты", msg_txt)
        self.assertIn("Поздравление папе", msg_txt)
        kb = call.kwargs["reply_markup"]
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertTrue(any(cb.startswith("dplan:done:") for cb in cbs))

        # Second run same day does not spam
        sent2 = await sched._tick_day_plan(now_utc=now_morn + datetime.timedelta(minutes=15))
        self.assertEqual(sent2, 0)

    async def test_3_ideas_in_context_and_one_tap_promote_to_task(self) -> None:
        idea_id = await repo.create_journal_entry(
            self.db,
            user_id=self.u_id,
            entry_type="idea",
            entry_date="2026-11-24",
            source="text",
            user_text="Цель - похудеть к лету",
            title="Похудеть к лету",
        )
        snap = await context_builder.build_user_day_snapshot(self.db, self.user)
        ctx_text = context_builder.format_context_for_llm(snap)
        self.assertIn("Активные идеи и цели пользователя: Похудеть к лету", ctx_text)

        view_txt, view_kb = await inbox._render_diary_view(self.db, self.user, tab="idea")
        self.assertIn("похудеть к лету", view_txt.lower())
        cbs = [b.callback_data for row in view_kb.inline_keyboard for b in row]
        self.assertIn(f"diary:to_task:{idea_id}", cbs)

        cb_promote = SimpleNamespace(
            data=f"diary:to_task:{idea_id}",
            from_user=SimpleNamespace(id=970001, full_name="Фоддер"),
            message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        await inbox.diary_callbacks(cb_promote, _MemFSM(), self.db)
        today_local = datetime.datetime.now(datetime.timezone.utc).astimezone(
            datetime.timezone(datetime.timedelta(hours=3))
        ).date().isoformat()
        today_tasks = [i["title"] for i in await repo.list_day_plan_items(self.db, self.u_id, today_local)]
        self.assertIn("Похудеть к лету", today_tasks)

    async def test_4_wake_confirm_and_afternoon_pulse_surface_tasks(self) -> None:
        await repo.add_day_plan_item(self.db, self.u_id, "2026-11-24", "Убрать бардак дома")
        await repo.add_day_plan_item(self.db, self.u_id, "2026-11-24", "Заказать продукты")

        now_nov25 = datetime.datetime(2026, 11, 25, 6, 30, 0)  # 09:30 MSK
        chk = await repo.upsert_pulse_checkin(
            self.db, self.u_id, "2026-11-25", "wake", "sleep", now_nov25.isoformat(), now_nov25.isoformat()
        )
        res = await pulse_service.complete_checkin(self.db, self.user, int(chk["id"]), now_utc=now_nov25)
        self.assertEqual(res["pending_tasks"], ["Убрать бардак дома", "Заказать продукты"])

        cb_wake = SimpleNamespace(
            data=f"pulse:done:{chk['id']}",
            from_user=SimpleNamespace(id=970001, full_name="Фоддер"),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock(), chat=SimpleNamespace(id=1), message_id=9),
            bot=SimpleNamespace(edit_message_text=AsyncMock()),
            answer=AsyncMock(),
        )
        # Re-open a fresh pending wake to verify handler message renders pending_tasks too
        chk2 = await repo.upsert_pulse_checkin(
            self.db, self.u_id, "2026-11-26", "wake", "sleep", now_nov25.isoformat(), now_nov25.isoformat()
        )
        cb_wake.data = f"pulse:done:{chk2['id']}"
        await pulse.cb_pulse_done(cb_wake, self.db)
        wake_text = cb_wake.message.edit_text.call_args.args[0]
        self.assertIn("Задачи на сегодня", wake_text)
        self.assertIn("Убрать бардак дома", wake_text)

        # Afternoon pulse card lists up to 3 open tasks + dplan:list button
        ctx = await pulse_service._gather_slot_context(self.db, self.user, "2026-11-26", "afternoon")
        aft_chk = await repo.upsert_pulse_checkin(
            self.db, self.u_id, "2026-11-26", "afternoon", "tasks", now_nov25.isoformat(), now_nov25.isoformat()
        )
        karma_state = await repo.get_or_create_karma_state(self.db, self.u_id)
        aft_txt, aft_kb = pulse_service.build_pulse_message(
            aft_chk, karma_state, level=1, context=ctx, user=self.user
        )
        self.assertIn("Убрать бардак дома", aft_txt)
        self.assertIn("Заказать продукты", aft_txt)
        aft_cbs = [b.callback_data for row in aft_kb.inline_keyboard for b in row]
        self.assertIn("dplan:list", aft_cbs)

        # /today also reflects auto-synced overdue tasks
        with patch("utils.today.local_date_str", return_value="2026-11-26"):
            today_txt, _ = await render_today(self.db, self.user)
        self.assertIn("Убрать бардак дома", today_txt)


if __name__ == "__main__":
    unittest.main()
