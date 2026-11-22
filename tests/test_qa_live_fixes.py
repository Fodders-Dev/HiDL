import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery

from db import repositories as repo
from db.database import connect, init_db
from handlers import finance, home_supplies, home_tasks, kitchen, pause, pulse, schedule
from keyboards.common import food_menu_keyboard, money_menu_keyboard, more_menu_keyboard
from middlewares.error_log import ErrorLogMiddleware
from services import advice_service, kitchen_ai
from services.inbox_parser import (
    _extract_duration_minutes,
    _extract_relative_delta,
    resolve_temporal_expression,
)
from utils.today import render_today


def _all_callback_datas(markup) -> list[str]:
    return [
        btn.callback_data
        for row in (getattr(markup, "inline_keyboard", None) or [])
        for btn in row
        if getattr(btn, "callback_data", None)
    ]


def _all_button_texts(markup) -> list[str]:
    return [
        btn.text
        for row in (getattr(markup, "inline_keyboard", None) or [])
        for btn in row
    ]


class QALiveFixesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        user_id = await repo.create_user(
            self.db,
            telegram_id=777001,
            name="Tester",
            timezone="Europe/Moscow",
            wake_up_time="08:00",
            sleep_time="23:30",
        )
        await repo.ensure_user_routines(self.db, user_id)
        self.user_id = user_id
        self.user = dict(await repo.get_user(self.db, user_id))

    async def asyncTearDown(self) -> None:
        await self.db.close()

    def _make_callback(self, data: str, *, bot_id: int = 999999) -> tuple[SimpleNamespace, SimpleNamespace]:
        msg = SimpleNamespace(
            from_user=SimpleNamespace(id=bot_id, full_name="HiDLBot"),
            chat=SimpleNamespace(id=777001),
            message_id=101,
            answer=AsyncMock(),
            edit_text=AsyncMock(),
            edit_reply_markup=AsyncMock(),
        )
        cb = SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=777001, full_name="Tester"),
            message=msg,
            answer=AsyncMock(),
        )
        return cb, msg

    def _make_state(self) -> SimpleNamespace:
        return SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock(), update_data=AsyncMock())

    def test_calendar_duration_na_polchasa_does_not_override_day_after_tomorrow(self) -> None:
        now_utc = datetime.datetime(2026, 11, 22, 21, 8, 0)  # 2026-11-23 00:08 MSK
        raw = "Запиши в календарь послезавтра в 16:30 тест хайдл на полчаса"
        self.assertIsNone(_extract_relative_delta(raw))
        self.assertEqual(_extract_relative_delta("Напомни через полчаса выпить воду"), datetime.timedelta(minutes=30))
        self.assertEqual(_extract_duration_minutes(raw, 60), 30)

        sched = resolve_temporal_expression(
            date_hint="послезавтра",
            time_hint="16:30",
            raw_fragment=raw,
            now_utc=now_utc,
            tz_str="Europe/Moscow",
        )
        self.assertFalse(sched.is_ambiguous)
        self.assertEqual(sched.resolved_date, "2026-11-25")
        self.assertEqual(sched.resolved_time, "16:30")

    async def test_advice_llm_markdown_converted_to_safe_html(self) -> None:
        converted = advice_service._llm_text_to_safe_html(
            "## Ночной план\n* Выпей воды & отдохни <tag>\n**Микро-шаг прямо сейчас:** ляг в кровать с `таймером`."
        )
        self.assertNotIn("**", converted)
        self.assertNotIn("##", converted)
        self.assertIn("<b>Ночной план</b>", converted)
        self.assertIn("• Выпей воды &amp; отдохни &lt;tag&gt;", converted)
        self.assertIn("<b>Микро-шаг прямо сейчас:</b>", converted)
        self.assertIn("<code>таймером</code>", converted)

        fake_llm = SimpleNamespace(
            is_configured=lambda: True,
            complete_text=AsyncMock(
                return_value=SimpleNamespace(
                    ok=True,
                    text="Пора отдыхать.\n\n**Микро-шаг прямо сейчас:** поставь будильник и убери телефон.",
                )
            ),
        )
        reply, meta = await advice_service.build_situational_advice_reply(
            self.db,
            self.user,
            "Устал, что мне сейчас лучше сделать?",
            llm_client_instance=fake_llm,
            now_utc=datetime.datetime(2026, 11, 22, 21, 9, 0),
        )
        self.assertTrue(meta["used_llm"])
        self.assertNotIn("**", reply)
        self.assertIn("<b>Микро-шаг прямо сейчас:</b>", reply)

    async def test_finance_navigation_and_bills_user_identity(self) -> None:
        state = self._make_state()

        # money:bills must query/create under callback.from_user.id (777001), NOT bot_id (999999)
        cb_bills, msg_bills = self._make_callback("money:bills", bot_id=999999)
        await finance.money_callbacks(cb_bills, state, self.db)
        self.assertIsNone(await repo.get_user_by_telegram_id(self.db, 999999))
        msg_bills.edit_text.assert_awaited_once()
        self.assertIn("money:menu", _all_callback_datas(msg_bills.edit_text.call_args.kwargs["reply_markup"]))

        # money:tips has Back -> money:menu
        cb_tips, msg_tips = self._make_callback("money:tips")
        await finance.money_callbacks(cb_tips, state, self.db)
        self.assertIn("money:menu", _all_callback_datas(msg_tips.edit_text.call_args.kwargs["reply_markup"]))

        # money:menu returns money_menu_keyboard
        cb_menu, msg_menu = self._make_callback("money:menu")
        await finance.money_callbacks(cb_menu, state, self.db)
        menu_datas = _all_callback_datas(msg_menu.edit_text.call_args.kwargs["reply_markup"])
        self.assertEqual(menu_datas, _all_callback_datas(money_menu_keyboard()))

    async def test_kitchen_night_slot_and_early_callback_answer_and_stale_middleware(self) -> None:
        self.assertEqual(kitchen_ai.infer_meal_slot(0), "dinner")
        self.assertEqual(kitchen_ai.infer_meal_slot(4), "dinner")
        self.assertEqual(kitchen_ai.infer_meal_slot(5), "breakfast")
        self.assertEqual(kitchen_ai.infer_meal_slot(12), "lunch")
        self.assertEqual(kitchen_ai.infer_meal_slot(19), "dinner")

        # Unified kitchen menu labels
        food_texts = _all_button_texts(food_menu_keyboard())
        self.assertIn("📖 Рецепты", food_texts)
        self.assertIn("⬅️ В меню", food_texts)

        # kitchen:main answers callback
        cb_home, _ = self._make_callback("kitchen:main")
        await kitchen.kitchen_home(cb_home)
        cb_home.answer.assert_awaited_once()

        # mealplan_callbacks answers callback BEFORE get_meal_plan_view runs
        events: list[str] = []
        cb_plan, _ = self._make_callback("kitchen:meal_plan")

        async def _record_answer(*args, **kwargs):
            events.append("answer")

        async def _fake_plan_view(*args, **kwargs):
            events.append("plan")
            return {"days": [], "missing": [], "total_missing": 0, "num_days": 3, "meal_profile": "omnivore"}

        cb_plan.answer = AsyncMock(side_effect=_record_answer)
        with patch("services.kitchen_ai.get_meal_plan_view", side_effect=_fake_plan_view):
            await kitchen.mealplan_callbacks(cb_plan, self.db)
        self.assertEqual(events, ["answer", "plan"])

        # ErrorLogMiddleware does not call callback_query.answer() on stale query errors
        mw = ErrorLogMiddleware()
        stale_exc = TelegramBadRequest(
            method=AnswerCallbackQuery(callback_query_id="1"),
            message="Bad Request: query is too old and response timeout expired or query ID is invalid",
        )
        fake_cb = SimpleNamespace(answer=AsyncMock())
        fake_event = SimpleNamespace(message=None, callback_query=fake_cb)

        async def _raising_handler(event, data):
            raise stale_exc

        with self.assertRaises(TelegramBadRequest):
            await mw(_raising_handler, fake_event, {})
        fake_cb.answer.assert_not_awaited()

    async def test_ux_polish_schedule_today_pause_sick_tasks_and_supplies(self) -> None:
        # 1. /schedule suppresses duplicate location when already inside title
        await repo.upsert_calendar_connection(self.db, self.user_id, "ics", label="Test")
        await repo.upsert_single_calendar_event(
            self.db,
            self.user_id,
            "ics",
            {
                "external_uid": "evt-dup-loc",
                "event_date": "2026-11-23",
                "start_time": "16:00",
                "end_time": "17:00",
                "title": "Демид — информатика ОГЭ, онлайн",
                "location": "Онлайн",
                "is_busy": 1,
            },
        )
        await repo.upsert_single_calendar_event(
            self.db,
            self.user_id,
            "ics",
            {
                "external_uid": "evt-uniq-loc",
                "event_date": "2026-11-23",
                "start_time": "18:00",
                "end_time": "19:00",
                "title": "Созвон по проекту",
                "location": "Zoom",
                "is_busy": 1,
            },
        )
        sched_text, _, _ = await schedule._render_schedule_text(self.db, self.user, "2026-11-23")
        self.assertIn("Демид — информатика ОГЭ, онлайн", sched_text)
        self.assertNotIn("(Онлайн)", sched_text)
        self.assertIn("Созвон по проекту (Zoom)", sched_text)

        # 2. render_today uses distinct "⏭ Пропустить" label
        _, today_kb = await render_today(self.db, self.user)
        today_texts = _all_button_texts(today_kb)
        self.assertIn("⏭ Пропустить", today_texts)

        # 3. pause menu has Back -> main:menu
        self.assertIn("main:menu", _all_callback_datas(pause._pause_menu_keyboard()))

        # 4. more menu sick prompt returns to more:menu
        self.assertIn("pulse:sick:prompt:more", _all_callback_datas(more_menu_keyboard()))
        cb_sick, msg_sick = self._make_callback("pulse:sick:prompt:more")
        await pulse.cb_pulse_sick_prompt(cb_sick, self.db)
        sick_datas = _all_callback_datas(msg_sick.edit_text.call_args.kwargs["reply_markup"])
        self.assertIn("more:menu", sick_datas)

        # 5. home_tasks buttons include task titles
        tasks = [
            {"id": 1, "zone": "kitchen", "title": "Протереть плиту"},
            {"id": 2, "zone": "bathroom", "title": "Почистить раковину"},
        ]
        _, paged_kb = home_tasks._paginate_tasks(tasks, page=0)
        paged_texts = _all_button_texts(paged_kb)
        self.assertTrue(any("Протереть плиту" in t for t in paged_texts))
        all_texts = _all_button_texts(home_tasks._all_tasks_keyboard(tasks))
        self.assertTrue(any("Почистить раковину" in t for t in all_texts))

        # 6. home_supplies.supplies_menu uses explicit tg_user_id instead of bot_id
        bot_msg = SimpleNamespace(
            from_user=SimpleNamespace(id=999999, full_name="HiDLBot"),
            answer=AsyncMock(),
        )
        await home_supplies.supplies_menu(
            bot_msg,
            self.db,
            tg_user_id=777001,
            tg_full_name="Tester",
        )
        self.assertIsNone(await repo.get_user_by_telegram_id(self.db, 999999))
        bot_msg.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
