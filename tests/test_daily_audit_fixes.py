import datetime
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from db import repositories as repo
from db.database import connect, init_db
from handlers import inbox, nutrition
from llm_client import LLMResult
from middlewares.ensure_user import EnsureUserMiddleware
from scheduler.reminder import ReminderScheduler
from services import fitness_nutrition, inbox_parser, inbox_service, nutrition_web
from utils.time import local_date_str


class _FakeLLM:
    def __init__(self, payload: dict, *, configured: bool = True) -> None:
        self._payload = payload
        self._configured = configured
        self.calls: list[tuple[str, str]] = []

    def is_configured(self) -> bool:
        return self._configured

    async def complete_json(self, system_prompt: str, user_prompt: str, **kwargs) -> LLMResult:
        self.calls.append((system_prompt, user_prompt))
        return LLMResult(ok=True, text=json.dumps(self._payload), data=self._payload)


class DailyAuditFixesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.u_id = await repo.create_user(
            self.db,
            telegram_id=977001,
            name="Артем",
            timezone="Europe/Moscow",
            wake_up_time="07:00",
            sleep_time="23:30",
        )
        self.user = dict(await repo.get_user(self.db, self.u_id))
        self.storage = MemoryStorage()
        nutrition._MEAL_PREVIEW_CACHE.clear()

    async def asyncTearDown(self) -> None:
        nutrition._MEAL_PREVIEW_CACHE.clear()
        await self.storage.close()
        await self.db.close()

    def _fsm(self, tg_id: int = 977001) -> FSMContext:
        return FSMContext(
            storage=self.storage, key=StorageKey(bot_id=1, chat_id=tg_id, user_id=tg_id)
        )

    async def test_1_no_2x_doubling_on_spoons_and_russian_grams(self) -> None:
        self.assertEqual(nutrition_web.extract_requested_grams_and_mult("300 грамм"), (300.0, 1.0))
        self.assertEqual(nutrition_web.extract_requested_grams_and_mult("мл 300"), (300.0, 1.0))
        self.assertEqual(
            nutrition_web.extract_requested_grams_and_mult("Кофе, две ложки сгущёнки"), (None, 1.0)
        )
        self.assertEqual(nutrition_web.extract_requested_grams_and_mult("два блина"), (None, 2.0))
        self.assertEqual(
            nutrition_web.extract_requested_grams_and_mult(
                "Кофе мл 300, две ложки сгущенки. Порция супа была 300 грамм"
            ),
            (None, 1.0),
        )

        async def fake_web(_query: str):
            return {
                "title": "Обед: суп фо бо Самокат, 300 г + кофе со сгущёнкой, около 300 мл кофе и 2 ст. л. сгущёнки",
                "source": "web_search",
                "source_url": "https://kuper.ru/samokat/sup",
                "per_100g": {"kcal": 49.5, "protein_g": 2.1, "fat_g": 1.1, "carbs_g": 7.6},
                "serving_g": 630.0,
                "kcal_serving": 312.0,
                "protein_g": 13.5,
                "fat_g": 7.1,
                "carbs_g": 47.9,
            }

        res = await fitness_nutrition.parse_meal_input(
            raw_text=(
                "Суп фо бо из Самоката и кофе со сгущёнкой (обед). "
                "Пообедал супом фо бо из самоката и запил кофе со сгущенкой. "
                "(Кофе мл 300, две ложки сгущенки. Порция супа была 300 грамм)"
            ),
            db=self.db,
            user_id=self.u_id,
            allow_clarification=False,
            allow_web_lookup=True,
            web_lookup_fn=fake_web,
            meal_slot_hint="обед",
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["kcal"], 312)
        self.assertEqual(res["meal_slot"], "lunch")

        # Bloated multi-item combo must not pollute nutrition_lookup_cache
        cnt_row = await (await self.db.execute("SELECT COUNT(*) FROM nutrition_lookup_cache")).fetchone()
        self.assertEqual(int(cnt_row[0]), 0)

    async def test_2_consecutive_album_photos_preserve_both_previews(self) -> None:
        fsm = self._fsm()
        now_utc = datetime.datetime.utcnow()
        ld = local_date_str(now_utc, "Europe/Moscow")

        async def fake_dl(file_obj, destination=None):
            if isinstance(destination, io.BytesIO):
                destination.write(b"fakejpg")
            return destination

        sha_llm = _FakeLLM(
            {
                "title": "Шаурма с курицей по-домашнему",
                "meal_slot": "dinner",
                "kcal": 536,
                "protein_g": 21.0,
                "fat_g": 23.6,
                "carbs_g": 60.0,
                "is_nutrition_label": False,
                "per_100g": {"kcal": 268, "protein_g": 10.5, "fat_g": 11.8, "carbs_g": 30.0},
                "serving_g": 200.0,
                "needs_clarification": False,
                "clarification_question": "",
            }
        )
        teos_llm = _FakeLLM(
            {
                "title": "TEOS PRO питьевой кисломолочный продукт",
                "meal_slot": "snack",
                "kcal": 188,
                "protein_g": 30.0,
                "fat_g": 0.5,
                "carbs_g": 16.0,
                "is_nutrition_label": True,
                "per_100g": {"kcal": 56.5, "protein_g": 9.0, "fat_g": 0.1, "carbs_g": 4.8},
                "serving_g": 330.0,
                "needs_clarification": False,
                "clarification_question": "",
            }
        )

        msg1 = SimpleNamespace(
            photo=[SimpleNamespace(file_id="ph_sha")],
            caption="",
            chat=SimpleNamespace(id=977001),
            from_user=SimpleNamespace(id=977001, full_name="Артем"),
            bot=SimpleNamespace(download=fake_dl),
            answer=AsyncMock(),
        )
        msg2 = SimpleNamespace(
            photo=[SimpleNamespace(file_id="ph_teos")],
            caption="",
            chat=SimpleNamespace(id=977001),
            from_user=SimpleNamespace(id=977001, full_name="Артем"),
            bot=SimpleNamespace(download=fake_dl),
            answer=AsyncMock(),
        )

        with patch.object(fitness_nutrition, "default_llm_client", sha_llm):
            await inbox.handle_unsolicited_photo(msg1, fsm, self.db)
        with patch.object(fitness_nutrition, "default_llm_client", teos_llm):
            await nutrition.kbju_confirm_photo(msg2, fsm, self.db)

        cb1_data = msg1.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        cb2_data = msg2.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        self.assertTrue(cb1_data.startswith("kbju:confirm:"))
        self.assertTrue(cb2_data.startswith("kbju:confirm:"))
        self.assertNotEqual(cb1_data, cb2_data)

        # Confirm #2 first, then #1 -> BOTH must be logged cleanly!
        cb2 = SimpleNamespace(
            data=cb2_data,
            from_user=SimpleNamespace(id=977001, full_name="Артем"),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await nutrition.kbju_confirm_cb(cb2, fsm, self.db)

        cb1 = SimpleNamespace(
            data=cb1_data,
            from_user=SimpleNamespace(id=977001, full_name="Артем"),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        await nutrition.kbju_confirm_cb(cb1, fsm, self.db)

        meals = await repo.list_meal_logs(self.db, self.u_id, ld)
        self.assertEqual(len(meals), 2)
        titles = {m["title"] for m in meals}
        self.assertIn("TEOS PRO питьевой кисломолочный продукт", titles)
        self.assertIn("Шаурма с курицей по-домашнему", titles)

    async def test_3_meal_slot_inference_and_pulse_close(self) -> None:
        now = datetime.datetime(2026, 11, 26, 13, 20, 0)
        ld = local_date_str(now, "Europe/Moscow")
        await repo.upsert_pulse_checkin(
            self.db, self.u_id, ld, "lunch", "food", now.isoformat(), now.isoformat()
        )
        inbox_row, _ = await repo.create_inbox_message_if_new(
            self.db,
            user_id=self.u_id,
            chat_id=977001,
            telegram_message_id=201,
            source="text",
            local_date=ld,
            raw_text="Пообедал борщом 300 г",
        )
        parsed_res = await inbox_service.fitness_nutrition.parse_meal_input(
            raw_text="Пообедал борщом 300 г",
            db=self.db,
            user_id=self.u_id,
            allow_clarification=False,
            allow_web_lookup=False,
            meal_slot_hint="обед",
        )
        self.assertEqual(parsed_res["meal_slot"], "lunch")

        await inbox_service._persist_meal_item(
            self.db,
            self.user,
            dict(inbox_row),
            source_quote="Пообедал борщом 300 г",
            title_hint="Борщ",
            local_date=ld,
            now_utc=now,
            allow_clarification=False,
            meal_slot_hint="обед",
        )
        meals = await repo.list_meal_logs(self.db, self.u_id, ld)
        self.assertEqual(meals[0]["meal_slot"], "lunch")
        chk = await repo.get_pulse_checkin(self.db, self.u_id, ld, "lunch")
        self.assertEqual(chk["status"], "done")

    def test_4_status_update_task_guardrail_and_exhaustion_empathy(self) -> None:
        now = datetime.datetime(2026, 11, 26, 13, 20, 0)
        for status_quote in ("Сейчас на занятия победу.", "Над проектами работаю параллельно за компом!"):
            it = inbox_parser._apply_guardrails(
                inbox_parser.ParsedInboxItem(
                    kind="task",
                    title=status_quote,
                    source_quote=status_quote,
                ),
                now_utc=now,
                tz_str="Europe/Moscow",
            )
            self.assertEqual(it.kind, "diary", status_quote)

        real_task = inbox_parser._apply_guardrails(
            inbox_parser.ParsedInboxItem(
                kind="task",
                title="Починить кран",
                source_quote="Надо починить кран сегодня",
            ),
            now_utc=now,
            tz_str="Europe/Moscow",
        )
        self.assertEqual(real_task.kind, "task")

        summary, _ = inbox_service.render_inbox_summary(
            {"id": 1, "source": "text", "raw_text": "Устал, не до слежки"},
            [
                {
                    "id": 1,
                    "item_kind": "diary",
                    "status": "active",
                    "title": "Устал",
                    "source_quote": "Да я вчера устал пиздец.",
                    "payload_json": "{}",
                }
            ],
            intent="capture",
            support_reply="",
        )
        self.assertIn("вымотался", summary)
        self.assertIn("Записала:", summary)

    async def test_5_infra_db_scheduler_and_bot_middleware(self) -> None:
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmpd:
            fconn = await connect(os.path.join(tmpd, "test_pragmas.db"))
            try:
                bt = await (await fconn.execute("PRAGMA busy_timeout;")).fetchone()
                jm = await (await fconn.execute("PRAGMA journal_mode;")).fetchone()
                self.assertGreaterEqual(int(bt[0]), 30000)
                self.assertEqual(str(jm[0]).lower(), "wal")
            finally:
                await fconn.close()

        sched = ReminderScheduler(SimpleNamespace(), self.db)
        sched.start()
        try:
            interval_times = [
                job.next_run_time
                for job in sched.scheduler.get_jobs()
                if job.next_run_time is not None
            ]
            # Staggered interval jobs should have multiple distinct start times
            self.assertGreater(len(set(interval_times)), 10)
        finally:
            sched.scheduler.shutdown(wait=False)

        mw = EnsureUserMiddleware(self.db)
        handler = AsyncMock()
        bot_msg = SimpleNamespace(
            from_user=SimpleNamespace(id=8275063077, full_name="HiDL", is_bot=True),
            text="/start",
        )
        await mw(handler, bot_msg, {})
        handler.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
