import datetime
import logging
from typing import List, Dict

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.time import local_date_str
from utils.user import ensure_user
from utils.rows import rows_to_dicts


router = Router()
logger = logging.getLogger(__name__)


class DayPlanState(StatesGroup):
    important = State()
    extra = State()
    morning_add = State()


def _split_items(text: str) -> List[str]:
    raw = text.replace(";", "\n")
    parts: List[str] = []
    for line in raw.splitlines():
        for piece in line.split(","):
            piece = piece.strip()
            if piece:
                parts.append(piece)
    return parts


async def _save_plan(db, user_id: int, plan_date: str, important: List[str], extra: List[str]) -> None:
    items: List[Dict] = []
    for title in important:
        items.append({"title": title, "category": "work", "is_important": True})
    for title in extra:
        items.append({"title": title, "category": "misc", "is_important": False})
    await repo.upsert_day_plan(db, user_id, plan_date, items)
    logger.info(
        "day_plan.saved",
        extra={"user_id": user_id, "plan_date": plan_date, "important_cnt": len(important), "extra_cnt": len(extra)},
    )


@router.message(Command("plan_tomorrow"))
async def plan_tomorrow(message: types.Message, state: FSMContext, db) -> None:
    """Вечернее планирование завтрашнего дня."""
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    now_utc = datetime.datetime.utcnow()
    local_today = local_date_str(now_utc, user["timezone"])
    today_date = datetime.date.fromisoformat(local_today)
    tomorrow = (today_date + datetime.timedelta(days=1)).isoformat()
    await state.update_data(plan_date=tomorrow)
    await state.set_state(DayPlanState.important)
    await message.answer(
        "Давай придумаем завтрашний день.\n"
        "Напиши 1–3 самых важных дела, которые точно хочешь успеть. "
        "Можно через запятую или с новой строки. Если ничего не приходит в голову — напиши «нет».",
        reply_markup=main_menu_keyboard(),
    )


@router.message(DayPlanState.important)
async def plan_tomorrow_important(message: types.Message, state: FSMContext) -> None:
    text = (message.text or "").strip().lower()
    important: List[str] = []
    if text not in ("нет", "ничего", "no"):
        important = _split_items(message.text or "")
    await state.update_data(important=important)
    await state.set_state(DayPlanState.extra)
    await message.answer(
        "Теперь давай чуть по жизни.\n"
        "Есть ли что-то по дому, здоровью или для себя? "
        "Напиши 1–3 дела или «нет», если ничего добавлять не хочешь.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(DayPlanState.extra)
async def plan_tomorrow_extra(message: types.Message, state: FSMContext, db) -> None:
    data = await state.get_data()
    plan_date = data.get("plan_date")
    important: List[str] = data.get("important", [])
    text = (message.text or "").strip().lower()
    extra: List[str] = []
    if text not in ("нет", "ничего", "no"):
        extra = _split_items(message.text or "")
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    await _save_plan(db, user["id"], plan_date, important, extra)
    await state.clear()
    lines = ["Завтра для тебя главное:"]
    if important:
        for title in important:
            lines.append(f"• {title}")
    else:
        lines.append("• без жёстких обязательных дел.")
    if extra:
        lines.append("\nДополнительно по жизни:")
        for title in extra:
            lines.append(f"• {title}")
    lines.append("\nОстальное — бонус. Утром я напомню про этот план в разделе Сегодня.")
    await message.answer("\n".join(lines), reply_markup=main_menu_keyboard())


@router.callback_query(lambda c: c.data and c.data.startswith("dplan:"))
async def day_plan_callbacks(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    """Обработка утреннего пинга плана дня: всё ок / удалить / добавить."""
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    now_utc = datetime.datetime.utcnow()
    local_date = local_date_str(now_utc, user["timezone"])
    items_rows = await repo.list_day_plan_items(db, user["id"], local_date)
    items = rows_to_dicts(items_rows)
    if action == "ok":
        # помечаем важные пункты как «синхронизированные» с сегодняшним днём
        important_ids = [it["id"] for it in items if it.get("is_important")]
        if important_ids:
            await repo.mark_day_plan_items_synced(db, important_ids)
            logger.info(
                "day_plan.synced_to_today",
                extra={"user_id": user["id"], "date": local_date, "count": len(important_ids)},
            )
        await callback.answer("Ок, держу твой план в голове.")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if action == "delmenu":
        if not items:
            await callback.answer("План на сегодня пуст.", show_alert=True)
            return
        kb_rows = [
            [types.InlineKeyboardButton(text=it["title"][:32], callback_data=f"dplan:del:{it['id']}")]
            for it in items
        ]
        kb = types.InlineKeyboardMarkup(inline_keyboard=kb_rows)
        await callback.message.answer("Что убрать из плана?", reply_markup=kb)
        await callback.answer()
        return
    if action == "add":
        await state.set_state(DayPlanState.morning_add)
        await callback.message.answer(
            "Напиши одно дело, которое хочешь добавить к сегодняшнему плану.",
            reply_markup=main_menu_keyboard(),
        )
        await callback.answer()
        return
    if action == "done" and len(parts) > 2:
        # отметка пункта плана как выполненного из /today
        try:
            item_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        item = next((it for it in items if it.get("id") == item_id), None)
        await repo.mark_day_plan_item_done(db, item_id, True)
        # важные дела дают больше очков
        base_points = 3 if item and item.get("is_important") else 1
        await repo.add_points(db, user["id"], base_points, local_date=local_date)
        logger.info(
            "day_plan.done",
            extra={"user_id": user["id"], "date": local_date, "item_id": item_id, "points": base_points},
        )
        await callback.answer("Отметила дело из плана.")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if action == "del" and len(parts) > 2:
        try:
            item_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        await repo.delete_day_plan_item(db, user["id"], item_id)
        await callback.answer("Убрала из плана.")
        return
    await callback.answer()


@router.message(DayPlanState.morning_add)
async def day_plan_morning_add(message: types.Message, state: FSMContext, db) -> None:
    """Добавить одно дело в план текущего дня утром."""
    text = (message.text or "").strip()
    if not text:
        await message.answer("Если дела нет — можно ничего не добавлять.")
        await state.clear()
        return
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    now_utc = datetime.datetime.utcnow()
    local_date = local_date_str(now_utc, user["timezone"])
    await repo.add_day_plan_item(db, user["id"], local_date, text, category="misc", is_important=False)
    await state.clear()
    await message.answer(
        f"Добавила в план на сегодня: {text}.",
        reply_markup=main_menu_keyboard(),
    )
