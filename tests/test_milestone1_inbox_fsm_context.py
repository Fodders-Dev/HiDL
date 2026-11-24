import datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from db import repositories as repo
from db.database import connect, init_db
from handlers import finance, inbox, menu, nutrition
from llm_client import LLMResult
from services import advice_service, context_builder, fitness_nutrition, inbox_parser, inbox_service
from utils.menu_match import is_single_clause_reminder, looks_like_expense
from utils.nl_parser import parse_command


class _MemFSM:
    def __init__(self, initial_state: str | None = None, initial_data: dict | None = None):
        self._state = initial_state
        self._data: dict = dict(initial_data or {})

    async def get_state(self):
        return self._state

    async def set_state(self, s):
        self._state = getattr(s, "state", s)

    async def clear(self):
        self._state = None
        self._data.clear()

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)


class Milestone1InboxFsmContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db = await connect(":memory:")
        await init_db(self.db)
        self.tg_id = 981001
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
            coach_mode="balanced",
        )
        self.user = dict(await repo.get_user(self.db, self.u_id))

    async def asyncTearDown(self) -> None:
        inbox_service._DIALOG_HISTORY.clear()
        await self.db.close()

    async def test_1_evaluate_meal_check_with_configured_llm(self) -> None:
        fake_llm = SimpleNamespace(
            is_configured=lambda: True,
            complete_json=AsyncMock(
                return_value=LLMResult(
                    ok=True,
                    data={
                        "title": "Пицца Маргарита 2 куска",
                        "kcal": 520,
                        "protein_g": 20,
                        "fat_g": 18,
                        "carbs_g": 68,
                        "meal_slot": "dinner",
                    },
                )
            ),
            complete_text=AsyncMock(
                return_value=LLMResult(
                    ok=True,
                    text="Вписывается в дневной лимит: ~520 ккал оставят запас.",
                )
            ),
        )
        res = await fitness_nutrition.evaluate_meal_check(
            self.db,
            self.user,
            "2 куска пиццы",
            now_utc=datetime.datetime(2026, 11, 25, 16, 0, 0),
            llm_client_instance=fake_llm,
        )
        self.assertTrue(res["ok"])
        self.assertIn("Вписывается в дневной лимит", res["advice_html"])

    async def test_2_legacy_hijackers_and_reminder_ampm(self) -> None:
        for txt in (
            "150 ккал",
            "2 куска пиццы",
            "3 яйца",
            "25 отжиманий",
            "запиши вес 84.5",
            "запиши в календарь 25 ноября встреча",
        ):
            self.assertFalse(looks_like_expense(txt), f"Should not treat as expense: {txt}")
        self.assertTrue(looks_like_expense("потратила 200 рублей на такси"))

        self.assertFalse(is_single_clause_reminder("Удали напоминание про воду"))
        self.assertFalse(is_single_clause_reminder("Напомни, сколько калорий?"))

        parsed_rem = parse_command("напомни в 8 вечера позвонить")
        self.assertIsNotNone(parsed_rem)
        self.assertEqual(parsed_rem.type, "reminder")
        self.assertEqual(parsed_rem.payload.get("time"), "20:00")

        parsed_day = parse_command("напомни в 3 дня выпить чай")
        self.assertIsNotNone(parsed_day)
        self.assertEqual(parsed_day.payload.get("time"), "15:00")

    async def test_3_meal_add_confirm_text_and_pending_meal_cancel(self) -> None:
        # MealAddState.confirm accepts "да", cancels on "нет", exits to inbox on free text
        fsm_yes = _MemFSM(
            nutrition.MealAddState.confirm.state,
            {
                "meal_preview": {
                    "ok": True,
                    "title": "Тофу 200 г",
                    "meal_slot": "lunch",
                    "kcal": 240,
                    "protein_g": 24,
                    "fat_g": 12,
                    "carbs_g": 6,
                    "used_llm": False,
                }
            },
        )
        msg_yes = SimpleNamespace(
            text="да",
            from_user=SimpleNamespace(id=self.tg_id, full_name="Артем"),
            chat=SimpleNamespace(id=self.tg_id),
            message_id=101,
            answer=AsyncMock(),
        )
        await nutrition.kbju_confirm_text(msg_yes, fsm_yes, self.db)
        self.assertIsNone(await fsm_yes.get_state())
        self.assertIn("Записала в КБЖУ", msg_yes.answer.await_args.args[0])

        fsm_no = _MemFSM(nutrition.MealAddState.confirm.state, {"meal_preview": {"ok": True}})
        msg_no = SimpleNamespace(
            text="нет",
            from_user=SimpleNamespace(id=self.tg_id, full_name="Артем"),
            chat=SimpleNamespace(id=self.tg_id),
            message_id=102,
            answer=AsyncMock(),
        )
        await nutrition.kbju_confirm_text(msg_no, fsm_no, self.db)
        self.assertIsNone(await fsm_no.get_state())
        self.assertIn("не записываю", msg_no.answer.await_args.args[0].lower())

        fsm_other = _MemFSM(nutrition.MealAddState.confirm.state, {"meal_preview": {"ok": True}})
        msg_other = SimpleNamespace(
            text="привет",
            from_user=SimpleNamespace(id=self.tg_id, full_name="Артем"),
            chat=SimpleNamespace(id=self.tg_id),
            message_id=103,
            reply_to_message=None,
            answer=AsyncMock(),
        )
        await nutrition.kbju_confirm_text(msg_other, fsm_other, self.db)
        self.assertIsNone(await fsm_other.get_state())
        self.assertTrue(msg_other.answer.await_count >= 1)

        # pending_meal cancel & non-swallow checks
        for cancel_txt in ("нет", "пропусти", "отмена"):
            now = datetime.datetime(2026, 11, 25, 11, 0, 0)
            pm_id = await repo.create_pending_meal_clarification(
                self.db,
                self.u_id,
                self.tg_id,
                "2026-11-25",
                "сырники",
                "Сырники",
                "lunch",
                {"title": "Сырники", "kcal": 350, "protein_g": 15, "fat_g": 12, "carbs_g": 35},
                "Сколько штук?",
                now_utc=now,
            )
            out = await inbox_service.capture_and_process_text(
                self.db,
                self.user,
                chat_id=self.tg_id,
                message_id=200 + abs(hash(cancel_txt)) % 1000,
                text=cancel_txt,
                now_utc=now,
            )
            self.assertIn("не записываю", out.reply_text.lower())
            self.assertIsNone(await repo.get_active_pending_meal(self.db, self.u_id, now_utc=now))
            self.assertEqual(len(await repo.list_meal_logs(self.db, self.u_id, "2026-11-25")), 1)
            self.assertTrue(pm_id > 0)

        self.assertFalse(fitness_nutrition.looks_like_meal_clarification("я проснулся"))
        self.assertFalse(fitness_nutrition.looks_like_meal_clarification("привет"))

        # Explicit scale caption wins over active pending_meal
        await repo.create_pending_meal_clarification(
            self.db,
            self.u_id,
            self.tg_id,
            "2026-11-25",
            "борщ",
            "Борщ",
            "lunch",
            {"title": "Борщ", "kcal": 300, "protein_g": 10, "fat_g": 8, "carbs_g": 40},
            "Какая порция?",
        )
        msg_scale = SimpleNamespace(
            caption="вес 98.4",
            from_user=SimpleNamespace(id=self.tg_id, full_name="Артем"),
            answer=AsyncMock(),
        )
        with patch(
            "services.fitness_nutrition.parse_smart_scale_input",
            new=AsyncMock(return_value={"ok": True, "weight": 98.4}),
        ):
            await inbox._process_unsolicited_image(
                msg_scale, _MemFSM(), self.db, self.user, b"fakeimg", "image/jpeg"
            )
        w = await repo.get_latest_weight(self.db, self.u_id)
        self.assertIsNotNone(w)
        self.assertAlmostEqual(float(w["weight"]), 98.4)

    async def test_4_reply_context_voice_advice_memory_and_context_builder(self) -> None:
        # Reply-to-bot preserves _latest_assistant_query_type for "а ещё?"
        recent_ctx = [
            {"role": "user", "text": "что приготовить?"},
            {"role": "assistant", "text": "Вот варианты...", "meta": {"query_type": "food"}},
            {"role": "assistant", "text": "Вот варианты...", "is_reply_context": True},
        ]
        self.assertEqual(inbox_parser._latest_assistant_query_type(recent_ctx), "food")
        parsed_more = await inbox_parser.analyze_inbox_text(
            "а ещё?",
            datetime.datetime(2026, 11, 25, 12, 0, 0),
            "Europe/Moscow",
            recent_context=recent_ctx,
        )
        self.assertEqual(parsed_more.intent, "query")
        self.assertEqual(parsed_more.query_type, "food")
        self.assertEqual(parsed_more.query_term, "more")

        # handle_voice_inbox passes reply_context
        voice_msg = SimpleNamespace(
            from_user=SimpleNamespace(id=self.tg_id, full_name="Артем"),
            chat=SimpleNamespace(id=self.tg_id),
            message_id=305,
            bot=SimpleNamespace(),
            voice=SimpleNamespace(file_id="fid1", file_unique_id="fuid1", duration=3),
            reply_to_message=SimpleNamespace(text="Пропущен подъём", caption=None),
            answer=AsyncMock(),
        )
        with patch(
            "services.inbox_service.capture_and_process_voice",
            new=AsyncMock(
                return_value=inbox_service.InboxProcessOutcome(
                    inbox_id=1, status="processed", is_duplicate=False, source="voice", reply_text="Ок"
                )
            ),
        ) as mock_voice:
            await inbox.handle_voice_inbox(voice_msg, _MemFSM(), self.db)
        self.assertEqual(mock_voice.await_args.kwargs.get("reply_context"), "Пропущен подъём")

        # advice_service uses recent_context for short "Почему?" follow-up
        captured_prompt = {}

        async def _fake_complete_text(sys_p, usr_p, **kw):
            captured_prompt["usr"] = usr_p
            return LLMResult(ok=True, text="Потому что при дефиците сохраняется мышечная масса.")

        fake_adv_llm = SimpleNamespace(is_configured=lambda: True, complete_text=_fake_complete_text)
        adv_html, adv_meta = await advice_service.build_situational_advice_reply(
            self.db,
            self.user,
            "Почему?",
            recent_context=[{"role": "assistant", "text": "Нужно набирать 134 г белка в день."}],
            llm_client_instance=fake_adv_llm,
            now_utc=datetime.datetime(2026, 11, 25, 12, 0, 0),
        )
        self.assertTrue(adv_meta["used_llm"])
        self.assertIn("134 г белка", captured_prompt["usr"])
        self.assertIn("Потому что", adv_html)

        # context_builder renders meals, reminders, full tomorrow calendar, finance, today's diary
        await repo.add_meal_log(
            self.db, self.u_id, "2026-11-25", "Тофу-скрэмбл", meal_slot="breakfast", kcal=420
        )
        await repo.create_custom_reminder(self.db, self.u_id, "Выпить витамин D", "09:30", 1)
        await repo.add_expense(self.db, self.u_id, 1250, "продукты")
        await repo.create_journal_entry(
            self.db,
            user_id=self.u_id,
            entry_type="diary",
            entry_date="2026-11-25",
            source="text",
            user_text="Спал крепко 7 часов",
            title="Спал крепко 7 часов",
        )
        with patch(
            "services.calendar_service.get_unified_events_for_date",
            new=AsyncMock(
                side_effect=[
                    [],
                    [
                        {
                            "is_all_day": False,
                            "start_time": "17:00",
                            "end_time": "18:00",
                            "title": "Врач",
                        }
                    ],
                ]
            ),
        ):
            snap = await context_builder.build_user_day_snapshot(
                self.db, self.user, now_utc=datetime.datetime(2026, 11, 25, 9, 0, 0)
            )
        ctx_txt = context_builder.format_context_for_llm(snap)
        self.assertIn("Съедено сегодня: Тофу-скрэмбл (~420 ккал)", ctx_txt)
        self.assertIn("Напоминания: 09:30 Выпить витамин D", ctx_txt)
        self.assertIn("Календарь завтра: 17:00–18:00 Врач", ctx_txt)
        self.assertIn("Финансы за месяц: потрачено 1250 ₽", ctx_txt)
        self.assertIn("Заметки дня: Спал крепко 7 часов", ctx_txt)

    async def test_5_wake_shift_delete_intents_and_undo_karma_rollback(self) -> None:
        now = datetime.datetime(2026, 11, 25, 6, 30, 0)
        # 1) "вот только сейчас проснулся! завтра подъемы будут в 6:30!"
        out_wake = await inbox_service.capture_and_process_text(
            self.db,
            self.user,
            chat_id=self.tg_id,
            message_id=401,
            text="вот только сейчас проснулся! завтра подъемы будут в 6:30!",
            now_utc=now,
        )
        self.assertEqual(len(out_wake.actions), 2)
        kinds = {a["item_kind"] for a in out_wake.actions}
        self.assertEqual(kinds, {"pulse_log", "shift_wake"})
        updated_user = dict(await repo.get_user(self.db, self.u_id))
        self.assertEqual(updated_user["wake_up_time"], "06:30")

        # Undo pulse_log reverts check-in to pending and karma back to initial 70
        pulse_act = next(a for a in out_wake.actions if a["item_kind"] == "pulse_log")
        await inbox_service.delete_inbox_action(self.db, updated_user, int(pulse_act["id"]))
        chk = await repo.get_pulse_checkin(self.db, self.u_id, "2026-11-25", "wake")
        self.assertEqual(chk["status"], "pending")
        k_state = await repo.get_or_create_karma_state(self.db, self.u_id)
        self.assertEqual(int(k_state["score"]), 70)

        # 2) Delete task & reminder by NL + Undo restores them
        await repo.add_day_plan_item(self.db, self.u_id, "2026-11-25", "Починить ванну")
        out_tdel = await inbox_service.capture_and_process_text(
            self.db,
            updated_user,
            chat_id=self.tg_id,
            message_id=402,
            text="удали задачу починить ванну",
            now_utc=now,
        )
        self.assertEqual(out_tdel.actions[0]["item_kind"], "task_delete")
        self.assertEqual(len(await repo.list_day_plan_items(self.db, self.u_id, "2026-11-25")), 0)
        await inbox_service.undo_inbox_message(self.db, updated_user, out_tdel.inbox_id)
        restored_tasks = await repo.list_day_plan_items(self.db, self.u_id, "2026-11-25")
        self.assertEqual(len(restored_tasks), 1)
        self.assertEqual(restored_tasks[0]["title"], "Починить ванну")

        await repo.create_custom_reminder(self.db, self.u_id, "Пить магний", "21:00", 1)
        out_rdel = await inbox_service.capture_and_process_text(
            self.db,
            updated_user,
            chat_id=self.tg_id,
            message_id=403,
            text="удали напоминание про магний",
            now_utc=now,
        )
        self.assertEqual(out_rdel.actions[0]["item_kind"], "reminder_delete")
        self.assertEqual(len(await repo.list_custom_reminders(self.db, self.u_id)), 0)
        await inbox_service.undo_inbox_message(self.db, updated_user, out_rdel.inbox_id)
        restored_rems = await repo.list_custom_reminders(self.db, self.u_id)
        self.assertEqual(len(restored_rems), 1)
        self.assertEqual(restored_rems[0]["title"], "Пить магний")


if __name__ == "__main__":
    unittest.main()
