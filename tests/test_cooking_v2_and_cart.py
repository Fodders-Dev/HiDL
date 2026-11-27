from __future__ import annotations

import asyncio
import datetime as dt
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from db import repositories as repo
from db.database import connect, init_db
from handlers import inbox as inbox_handler
from handlers import kitchen
from services import inbox_parser, inbox_service, kitchen_ai
from utils.pantry import format_quantity
from utils.rows import rows_to_dicts


class _MemFSM:
    def __init__(self, initial_state: str | None = None, initial_data: dict | None = None) -> None:
        self._state = initial_state
        self._data: dict = dict(initial_data or {})

    async def set_state(self, st) -> None:
        self._state = st.state if hasattr(st, "state") else st

    async def get_state(self):
        return self._state

    async def update_data(self, **kw) -> None:
        self._data.update(kw)

    async def get_data(self) -> dict:
        return dict(self._data)

    async def clear(self) -> None:
        self._state = None
        self._data.clear()


def _fake_cb(data: str, *, tg_id: int, full_name: str = "Федор", reply_markup=None, bot=None):
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
        bot=bot,
        from_user=SimpleNamespace(id=tg_id, full_name=full_name, first_name=full_name),
        message=msg,
        answer=AsyncMock(),
    )


def _fake_photo_msg(
    *,
    tg_id: int,
    full_name: str = "Девушка",
    caption: str = "",
    media_group_id: str | None = None,
    payload_bytes: bytes = b"fake-image",
    bot_download_side_effect=None,
):
    async def _dl(photo_obj, destination=None):
        if bot_download_side_effect:
            return bot_download_side_effect(photo_obj, destination)
        if isinstance(destination, io.BytesIO):
            destination.write(payload_bytes)
        return destination

    bot = SimpleNamespace(download=AsyncMock(side_effect=_dl))
    return SimpleNamespace(
        chat=SimpleNamespace(id=tg_id),
        from_user=SimpleNamespace(id=tg_id, full_name=full_name, first_name=full_name),
        caption=caption,
        media_group_id=media_group_id,
        photo=[SimpleNamespace(file_id=f"file-{id(payload_bytes)}")],
        bot=bot,
        answer=AsyncMock(),
    )


def _flat_buttons(markup) -> list[tuple[str, str]]:
    if not markup or not getattr(markup, "inline_keyboard", None):
        return []
    out: list[tuple[str, str]] = []
    for row in markup.inline_keyboard:
        for btn in row:
            out.append((str(getattr(btn, "text", "")), str(getattr(btn, "callback_data", ""))))
    return out


class CookingV2AndCartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        kitchen._ADAPTED_RECIPES.clear()
        kitchen._SHARED_COOKED_MEALS.clear()
        kitchen._COOK_LOG_BASE.clear()
        kitchen._CART_ALBUM_BUFFERS.clear()
        inbox_handler._INBOX_ALBUM_BUFFERS.clear()
        self.db = await connect(":memory:")
        self.addAsyncCleanup(self.db.close)
        await init_db(self.db)
        self.tg_id = 990001
        self.uid = await repo.create_user(self.db, self.tg_id, "Федор", "Europe/Moscow", "06:50", "23:30")
        await repo.upsert_wellness(self.db, self.uid, meal_profile="vegan")
        self.user = dict(await repo.get_user(self.db, self.uid))

        self.partner_tg_id = 990002
        self.partner_uid = await repo.create_user(self.db, self.partner_tg_id, "Таня", "Europe/Moscow", "08:00", "23:30")
        await repo.upsert_wellness(self.db, self.partner_uid, meal_profile="vegan")
        hid = await repo.get_or_create_household(self.db, self.uid)
        await repo.set_user_household(self.db, self.partner_uid, hid)

    async def test_1_delete_first_recipe_card_prep_badge_and_dry_beans_guard(self) -> None:
        # 1. Быстрое тако НЕ должно матчить сухую фасоль, а консервированная должна матчиться с подсказкой "(дома: ...)"
        dry_id = await repo.upsert_pantry_item_merged(self.db, self.uid, name="Фасоль белая сухая", amount=500.0, unit="г")
        pantry_dry = rows_to_dicts(await repo.list_pantry_items(self.db, self.uid))
        taco = await kitchen_ai.get_recipe_for_user(self.db, self.uid, "bean_corn_tacos")
        self.assertIsNotNone(taco)
        m_dry = kitchen_ai.match_recipe_to_pantry(taco, pantry_dry, servings=2)
        self.assertTrue(any("фасол" in m["name"].lower() for m in m_dry["missing"]))

        await repo.delete_pantry_item(self.db, self.uid, dry_id)
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Фасоль красная консервированная", amount=400.0, unit="г")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Лаваш тонкий", amount=4.0, unit="шт")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Кукуруза консервированная", amount=200.0, unit="г")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Помидор", amount=2.0, unit="шт")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Авокадо", amount=2.0, unit="шт")

        cb_taco = _fake_cb("kitchen:cook_view:bean_corn_tacos:2:all:0", tg_id=self.tg_id)
        await kitchen.view_recipe(cb_taco, self.db)
        text_taco = cb_taco.message.edit_text.call_args[0][0]
        kb_taco = cb_taco.message.edit_text.call_args[1]["reply_markup"]
        btns = _flat_buttons(kb_taco)
        cbs = [c for _, c in btns]
        labels = [lbl for lbl, _ in btns]

        # Delete-first: старые дубли не рендерятся
        self.assertFalse(any(c.startswith("kitchen:cook_fast:") for c in cbs))
        self.assertFalse(any(c.startswith("kbju:log_recipe:") for c in cbs))
        self.assertFalse(any(c.startswith("kitchen:cook_check:") for c in cbs))
        self.assertIn("kitchen:cook_and_log:bean_corn_tacos:2:1", cbs)
        self.assertIn("kitchen:cook_deduct:bean_corn_tacos:2", cbs)
        self.assertTrue(any("Приготовил (2 порц.) + мне 1 порц. в КБЖУ" in l for l in labels))
        self.assertIn("дома: Фасоль красная консервированная", text_taco)

        # 2. Рецепт с замачиванием на ночь -> бейдж ⏳ Подготовка заранее
        chia = await kitchen_ai.get_recipe_for_user(self.db, self.uid, "overnight_chia_oats")
        self.assertIsNotNone(chia)
        self.assertIsNotNone(kitchen_ai.detect_recipe_prep_warning(chia))
        cb_chia = _fake_cb("kitchen:cook_view:overnight_chia_oats:1:all:0", tg_id=self.tg_id)
        await kitchen.view_recipe(cb_chia, self.db)
        text_chia = cb_chia.message.edit_text.call_args[0][0]
        self.assertIn("⏳ <b>Подготовка заранее:</b>", text_chia)

    async def test_2_cook_2_servings_edit_portion_tail_cleanup_and_partner_push(self) -> None:
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Фасоль красная консервированная", amount=400.0, unit="г")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Лаваш тонкий", amount=4.0, unit="шт")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Кукуруза консервированная", amount=200.0, unit="г")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Помидор", amount=2.0, unit="шт")
        await repo.upsert_pantry_item_merged(self.db, self.uid, name="Авокадо", amount=2.0, unit="шт")

        bot_mock = SimpleNamespace(send_message=AsyncMock())
        cb_cook = _fake_cb("kitchen:cook_and_log:bean_corn_tacos:2:1", tg_id=self.tg_id, bot=bot_mock)
        await kitchen.cook_and_log_commit(cb_cook, self.db)

        today_str = kitchen_ai.local_date_str(dt.datetime.utcnow(), "Europe/Moscow")
        # Склад списан на 2 порц. (остаток фасоли: 400 - 250 = 150 г, кукурузы: 200 - 120 = 80 г)
        pantry_after = {p["name"]: (int(p["id"]), float(p["amount"])) for p in rows_to_dicts(await repo.list_pantry_items(self.db, self.uid))}
        self.assertAlmostEqual(pantry_after["Фасоль красная консервированная"][1], 150.0, places=2)
        self.assertAlmostEqual(pantry_after["Кукуруза консервированная"][1], 80.0, places=2)

        # Личный КБЖУ содержит ровно 1 запись на 1 порцию
        my_meals = await repo.list_meal_logs(self.db, self.uid, today_str)
        self.assertEqual(len(my_meals), 1)
        log_id = int(my_meals[0]["id"])
        full_kcal = float(my_meals[0]["kcal"])
        self.assertEqual(float(my_meals[0]["servings"]), 1.0)

        # Экран «Приготовлено» имеет кнопки правки порции + компактные кнопки добивки хвостов
        kb_after = cb_cook.message.edit_text.call_args[1]["reply_markup"]
        btns_after = _flat_buttons(kb_after)
        self.assertIn(f"kitchen:cook_edit_log:{log_id}:0.5", [c for _, c in btns_after])
        tail_btn = next((l, c) for l, c in btns_after if c.startswith("kitchen:tail_del:"))
        self.assertTrue(tail_btn[0].startswith("🗑 Кончилось: "))

        # Правка порции на ½ обновляет ту же запись meal_logs (дубля нет!)
        cb_half = _fake_cb(f"kitchen:cook_edit_log:{log_id}:0.5", tg_id=self.tg_id)
        await kitchen.cook_edit_logged_portion_cb(cb_half, self.db)
        my_meals_edited = await repo.list_meal_logs(self.db, self.uid, today_str)
        self.assertEqual(len(my_meals_edited), 1)
        self.assertAlmostEqual(float(my_meals_edited[0]["servings"]), 0.5, places=2)
        self.assertAlmostEqual(float(my_meals_edited[0]["kcal"]), round(full_kcal * 0.5), places=0)

        # Добивка хвоста 1 тапом убирает остаток полностью и повторно безопасна
        bean_pid = pantry_after["Фасоль красная консервированная"][0]
        cb_tail = _fake_cb(f"kitchen:tail_del:{bean_pid}", tg_id=self.tg_id, reply_markup=kb_after)
        await kitchen.tail_delete_pantry_cb(cb_tail, self.db)
        cb_tail2 = _fake_cb(f"kitchen:tail_del:{bean_pid}", tg_id=self.tg_id, reply_markup=kb_after)
        await kitchen.tail_delete_pantry_cb(cb_tail2, self.db)
        pantry_after_tail = {p["name"] for p in rows_to_dicts(await repo.list_pantry_items(self.db, self.uid))}
        self.assertNotIn("Фасоль красная консервированная", pantry_after_tail)

        # Партнёр получил пуш с 1-тап кнопками -> нажимает 1 порц. -> пишется в её КБЖУ и идемпотентно на 2-й клик
        self.assertEqual(bot_mock.send_message.call_count, 1)
        p_call = bot_mock.send_message.call_args
        self.assertEqual(p_call.args[0], self.partner_tg_id)
        p_markup = p_call.kwargs["reply_markup"]
        partner_eat_cb = next(c for _, c in _flat_buttons(p_markup) if c.endswith(":1"))

        cb_partner = _fake_cb(partner_eat_cb, tg_id=self.partner_tg_id, full_name="Таня")
        await kitchen.partner_eat_shared_cb(cb_partner, self.db)
        cb_partner_dup = _fake_cb(partner_eat_cb, tg_id=self.partner_tg_id, full_name="Таня")
        await kitchen.partner_eat_shared_cb(cb_partner_dup, self.db)
        partner_meals = await repo.list_meal_logs(self.db, self.partner_uid, today_str)
        self.assertEqual(len(partner_meals), 1)
        self.assertEqual(float(partner_meals[0]["kcal"]), full_kcal)

    async def test_3_cooking_question_routes_to_advice_with_active_recipe_context(self) -> None:
        taco = await kitchen_ai.get_recipe_for_user(self.db, self.uid, "bean_corn_tacos")
        inbox_service.record_active_recipe_context(self.uid, taco)
        ctx = inbox_service._get_recent_context(self.uid)  # noqa: SLF001
        self.assertEqual(ctx[-1]["meta"]["query_type"], "recipe_view")

        q = "• фасоль чёрная или красная: 125 г\nА че за фасоль, консервированная или замачивать надо было?"
        now = dt.datetime(2026, 11, 27, 8, 30, 0)
        with patch.object(inbox_parser.LLMClient, "is_configured", return_value=False):
            parsed = await inbox_parser.analyze_inbox_text(q, now, "Europe/Moscow", recent_context=ctx)
        self.assertEqual(parsed.intent, "query")
        self.assertEqual(parsed.query_type, "advice")

        # Даже если LLM ошибочно вернул food -> guardrail обязан переключить на advice
        mock_llm = SimpleNamespace(
            is_configured=lambda: True,
            complete_json=AsyncMock(
                return_value=SimpleNamespace(
                    ok=True,
                    provider="mock",
                    model="mock",
                    text="{}",
                    data={"intent": "query", "query_type": "food", "query_term": "", "reply": "", "items": []},
                )
            ),
        )
        parsed_llm = await inbox_parser.analyze_inbox_text(
            q, now, "Europe/Moscow", llm_client_instance=mock_llm, recent_context=ctx
        )
        self.assertEqual(parsed_llm.query_type, "advice")

    async def test_4_multi_screenshot_cart_merge_and_captionless_album_routing(self) -> None:
        m1 = _merge = kitchen._merge_cart_previews(  # noqa: SLF001
            None,
            {
                "ok": True,
                "store": "ВкусВилл",
                "total_amount": 1850.0,
                "items": [
                    {"name": "Тофу классический", "amount": 300, "unit": "g", "category": "белок"},
                    {"name": "Напиток овсяный Nemoloko лайт 1.5%", "amount": 1.0, "unit": "l", "category": "молочка"},
                ],
            },
        )
        m2 = kitchen._merge_cart_previews(  # noqa: SLF001
            m1,
            {
                "ok": True,
                "store": "ВкусВилл",
                "total_amount": 1850.0,  # Повторно тот же итог с 2-го скрина
                "items": [
                    {"name": "Напиток овсяный Nemoloko лайт 1.5%", "amount": 1.0, "unit": "l", "category": "молочка"},
                    {"name": "Авокадо Хасс", "amount": 2, "unit": "шт", "category": "овощи"},
                ],
            },
        )
        m3 = kitchen._merge_cart_previews(  # noqa: SLF001
            m2,
            {"ok": False, "items": [], "error": "vision_parse_failed"},
        )
        self.assertTrue(m3["ok"])
        self.assertEqual(len(m3["items"]), 3)
        self.assertEqual(m3["total_amount"], 1850.0)
        self.assertEqual(m3["photos_count"], 3)
        self.assertEqual(m3["failed_photos"], 1)

        # Роутинг альбома 3+ фото без подписи -> мультискрин-корзина (а <=2 фото -> обычный фото-пайплайн)
        batch3 = [
            _fake_photo_msg(tg_id=self.partner_tg_id, media_group_id="grp_vv", payload_bytes=b"img1"),
            _fake_photo_msg(tg_id=self.partner_tg_id, media_group_id="grp_vv", payload_bytes=b"img2"),
            _fake_photo_msg(tg_id=self.partner_tg_id, media_group_id="grp_vv", payload_bytes=b"img3"),
        ]
        fsm = _MemFSM()
        with patch.object(
            kitchen_ai,
            "parse_cart_input",
            AsyncMock(
                side_effect=[
                    {"ok": True, "store": "ВкусВилл", "total_amount": 1200.0, "items": [{"name": "Хумус", "amount": 200, "unit": "g", "category": "белок"}]},
                    {"ok": True, "store": "ВкусВилл", "total_amount": 1200.0, "items": [{"name": "Томаты черри", "amount": 250, "unit": "g", "category": "овощи"}]},
                    {"ok": True, "store": "ВкусВилл", "total_amount": 1200.0, "items": [{"name": "Укроп", "amount": 50, "unit": "g", "category": "овощи"}]},
                ]
            ),
        ):
            await inbox_handler._flush_inbox_photo_album(batch3, fsm, self.db, dict(await repo.get_user(self.db, self.partner_uid)))  # noqa: SLF001

        self.assertEqual(await fsm.get_state(), kitchen.CartImportState.confirm.state)
        preview = (await fsm.get_data())["cart_preview"]
        self.assertEqual(len(preview["items"]), 3)
        self.assertEqual(preview["photos_count"], 3)
        ans_text = batch3[-1].answer.call_args[0][0]
        self.assertIn("ВкусВилл", ans_text)
        self.assertIn("3 скрин.", ans_text)

    async def test_5_pantry_format_nemoloko_merge_breakfast_pulse_and_russian_month_date(self) -> None:
        # 1. Нет научной нотации 3e+02 / 3.3e+02
        self.assertEqual(format_quantity(299.999, "g"), "300 г")
        self.assertEqual(format_quantity(330.0001, "g"), "330 г")
        self.assertEqual(format_quantity(0.9, "l"), "0.9 л")

        # 2. Напиток овсяный Nemoloko + Овсяное молоко сливаются, Йогурт Nemoloko НЕ сливается
        id_drink = await repo.upsert_pantry_item_merged(self.db, self.uid, name="Напиток овсяный Nemoloko лайт 1.5%", amount=2.0, unit="л")
        id_milk = await repo.upsert_pantry_item_merged(self.db, self.uid, name="Овсяное молоко", amount=900.0, unit="мл")
        id_yog = await repo.upsert_pantry_item_merged(self.db, self.uid, name="Йогурт овсяный Nemoloko Груша", amount=260.0, unit="г")
        self.assertEqual(id_drink, id_milk)
        self.assertNotEqual(id_yog, id_drink)
        pantry_map = {p["name"]: (float(p["amount"]), p["unit"]) for p in rows_to_dicts(await repo.list_pantry_items(self.db, self.uid))}
        self.assertAlmostEqual(pantry_map["Напиток овсяный Nemoloko лайт 1.5%"][0], 2.9, places=2)
        self.assertIn("Йогурт овсяный Nemoloko Груша", pantry_map)

        # 3. Завтрак не трогает missed wake и не пишет "встал в 11:30"
        now_1130 = dt.datetime(2026, 11, 27, 8, 30, 0)  # 11:30 Europe/Moscow
        chk_wake = await repo.upsert_pulse_checkin(self.db, self.uid, "2026-11-27", "wake", "sleep", now_1130.isoformat(), now_1130.isoformat())
        await repo.transition_pulse_checkin(self.db, int(chk_wake["id"]), user_id=self.uid, allowed_statuses=("pending", "snoozed"), status="missed", resolved_at_utc=now_1130.isoformat())

        mock_llm_bf = SimpleNamespace(
            is_configured=lambda: True,
            complete_json=AsyncMock(
                return_value=SimpleNamespace(
                    ok=True,
                    provider="mock",
                    model="mock",
                    text="{}",
                    data={
                        "intent": "capture",
                        "query_type": "none",
                        "reply": "",
                        "items": [
                            {"kind": "meal_log", "title": "Белая фасоль и тост с хумусом и авокадо, 443 ккал", "source_quote": "завтрак: белая фасоль 443 ккал"},
                            {"kind": "pulse_log", "category": "wake", "title": "Завтрак", "source_quote": "завтрак"},
                        ],
                    },
                )
            ),
        )
        res_bf = await inbox_parser.analyze_inbox_text(
            "завтрак: белая фасоль и тост с хумусом и авокадо, 443 ккал Б21.4 Ж18.5 У48.2",
            now_1130,
            "Europe/Moscow",
            llm_client_instance=mock_llm_bf,
        )
        self.assertTrue(all(it.kind != "pulse_log" for it in res_bf.items))

        # 4. Словесные даты "до 5 декабря" -> plan_date=2026-12-05 (и отображается в ответе)
        now_task = dt.datetime(2026, 11, 27, 8, 57, 0)
        with patch.object(inbox_parser.LLMClient, "is_configured", return_value=False):
            out_task = await inbox_service.capture_and_process_text(
                self.db,
                self.user,
                chat_id=self.tg_id,
                message_id=501,
                text="Запиши в планах до 5 декабря собрать посылку папе",
                now_utc=now_task,
            )
        dec5_items = rows_to_dicts(await repo.list_day_plan_items(self.db, self.uid, "2026-12-05"))
        self.assertEqual(len(dec5_items), 1)
        self.assertIn("05.12", out_task.reply_text)


if __name__ == "__main__":
    unittest.main()
