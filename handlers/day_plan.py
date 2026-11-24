import datetime
import html
import logging
from typing import List, Dict

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from db import repositories as repo
from keyboards.common import main_menu_keyboard
from utils.time import local_date_str
from utils.sender import safe_edit
from utils.user import ensure_user
from utils.rows import rows_to_dicts
from utils.profile import is_student_mode


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


def _plan_item_line(item: dict) -> str:
    icon = "✅" if item.get("done") else "⬜️"
    title = html.escape(str(item.get("title") or ""))
    kind = " (важное)" if item.get("is_important") else ""
    if str(item.get("external_key") or "").startswith("desk:"):
        url = str(item.get("external_url") or "")
        title = f"🎓 Desk · {title}"
        if url.startswith(("https://", "http://")):
            title = f'<a href="{html.escape(url, quote=True)}">{title}</a>'
    return f"{icon} {title}{kind}"


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
        "Это не жёсткий список, а ориентир. Утром мы сможем что‑то убрать или добавить.\n"
        "Можно через запятую или с новой строки. Если ничего не приходит в голову — напиши «нет».",
        reply_markup=main_menu_keyboard(user),
    )


@router.message(DayPlanState.important)
async def plan_tomorrow_important(message: types.Message, state: FSMContext, db) -> None:
    text = (message.text or "").strip().lower()
    important: List[str] = []
    if text not in ("нет", "ничего", "no"):
        important = _split_items(message.text or "")
    await state.update_data(important=important)
    await state.set_state(DayPlanState.extra)
    user = await ensure_user(db, message.from_user.id, message.from_user.full_name)
    prompt = ("Есть ли ещё учебное или личное дело? Напиши 1–3 пункта или «нет»." if is_student_mode(user)
              else "Теперь давай чуть по жизни.\nЕсть ли что-то по дому, здоровью или для себя? Напиши 1–3 дела или «нет», если ничего добавлять не хочешь.")
    await message.answer(prompt, reply_markup=main_menu_keyboard(user))


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
    
    # Award points for planning
    await repo.add_points(db, user["id"], 1, local_date=local_date_str(datetime.datetime.utcnow(), user["timezone"]))
    
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
    lines.append("\nОстальное — бонус. Утром я напомню про этот план (+1 💎 за планирование).")
    if is_student_mode(user):
        lines = [line.replace("Дополнительно по жизни:", "Дополнительно:") for line in lines]
        lines = [line.replace(" (+1 💎 за планирование)", "") for line in lines]
    await message.answer("\n".join(lines), reply_markup=main_menu_keyboard(user))


def _render_day_plan_list_view(items: List[Dict], page: int = 0) -> tuple[str, types.InlineKeyboardMarkup]:
    items_per_page = 15
    total_items = len(items)
    total_pages = (total_items + items_per_page - 1) // items_per_page if total_items > 0 else 1
    page = max(0, min(page, total_pages - 1))
    start_idx = page * items_per_page
    page_items = items[start_idx : start_idx + items_per_page]

    lines = ["<b>🎯 План на день — детали:</b>"]
    if not items:
        lines.append("Пока пусто.")
    else:
        for item in items:
            lines.append(_plan_item_line(item))

    kb_rows = []
    for item in page_items:
        if not item.get("done"):
            title = (item.get("title") or "")[:30]
            kb_rows.append([
                types.InlineKeyboardButton(
                    text=f"✅ {title}",
                    callback_data=f"dplan:done:{item.get('id')}:list:{page}",
                )
            ])

    if total_pages > 1:
        pagination_row = []
        if page > 0:
            pagination_row.append(
                types.InlineKeyboardButton(text="⬅️ Пред", callback_data=f"dplan:list:{page - 1}")
            )
        pagination_row.append(
            types.InlineKeyboardButton(text=f"📄 {page + 1}/{total_pages}", callback_data="dplan:noop")
        )
        if page < total_pages - 1:
            pagination_row.append(
                types.InlineKeyboardButton(text="След ➡️", callback_data=f"dplan:list:{page + 1}")
            )
        kb_rows.append(pagination_row)

    kb_rows.append([
        types.InlineKeyboardButton(text="➕ Добавить", callback_data="dplan:add"),
        types.InlineKeyboardButton(text="🗑 Удалить", callback_data="dplan:delmenu"),
    ])
    kb_rows.append([types.InlineKeyboardButton(text="⬅️ Назад", callback_data="today:menu")])
    return "\n".join(lines), types.InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.callback_query(lambda c: c.data and c.data.startswith("dplan:"))
async def day_plan_callbacks(callback: types.CallbackQuery, state: FSMContext, db) -> None:
    """Обработка утреннего пинга плана дня: всё ок / удалить / добавить."""
    parts = callback.data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    user = await ensure_user(db, callback.from_user.id, callback.from_user.full_name)
    now_utc = datetime.datetime.utcnow()
    local_date = local_date_str(now_utc, user["timezone"])
    await repo.sync_overdue_day_plan_items(db, user["id"], local_date)
    items_rows = await repo.list_day_plan_items(db, user["id"], local_date)
    items = rows_to_dicts(items_rows)
    
    if action == "list":
        page = 0
        if len(parts) > 2:
            try:
                page = int(parts[2])
            except ValueError:
                page = 0
        text, kb = _render_day_plan_list_view(items, page=page)
        await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()
        return
    
    if action == "noop":
        # Игнорируем клик на индикатор страниц
        await callback.answer()
        return

    if action == "hide":
        await callback.message.delete()
        await callback.answer()
        return

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
        # button to return to list
        kb_rows.append([types.InlineKeyboardButton(text="⬅️ Отмена", callback_data="dplan:list")])
        kb = types.InlineKeyboardMarkup(inline_keyboard=kb_rows)
        # Edit text
        await callback.message.edit_text("Что убираем? Это нормально, планы меняются.", reply_markup=kb)
        await callback.answer()
        return
    if action == "add":
        await state.set_state(DayPlanState.morning_add)
        # For 'add', we usually need user input, so sending a new message is safer/easier
        # Or we can edit the text to prompt, but then we need to handle the message response to delete/update it.
        # Let's keep sending new message for input to avoid FSM confusion with old messages.
        await callback.message.answer(
            "Напиши одно дело, которое хочешь добавить к сегодняшнему плану.",
            reply_markup=main_menu_keyboard(user),
        )
        await callback.answer()
        return
    if action == "done" and len(parts) > 2:
        # отметка пункта плана как выполненного
        # Формат: dplan:done:ID или dplan:done:ID:list:PAGE
        try:
            item_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        
        # Определяем откуда вызвано (list/today) и текущую страницу
        from_list = len(parts) > 3 and parts[3] == "list"
        page = 0
        if from_list and len(parts) > 4:
            try:
                page = int(parts[4])
            except ValueError:
                page = 0
        
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
        
        # SMART REFRESH
        if from_list:
            items = rows_to_dicts(await repo.list_day_plan_items(db, user["id"], local_date))
            text, kb = _render_day_plan_list_view(items, page=page)
            await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
        else:
            # Refresh /today dashboard
            from utils.today import render_today
            text, kb = await render_today(db, user)
            await safe_edit(callback.message, text, reply_markup=kb or main_menu_keyboard(user), parse_mode="HTML")
        return
    if action == "del" and len(parts) > 2:
        try:
            item_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        await repo.delete_day_plan_item(db, user["id"], item_id)
        items = rows_to_dicts(await repo.list_day_plan_items(db, user["id"], local_date))
        text, kb = _render_day_plan_list_view(items, page=0)
        await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
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
        reply_markup=main_menu_keyboard(user),
    )
