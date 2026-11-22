"""Telegram screens and deep-link handling for Fodder's Desk."""

from __future__ import annotations

import html
import datetime as dt

from aiogram import F, Router, types
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import get_desk_settings
from db import repositories as repo
from db.database import independent_connection
from keyboards.common import main_menu_keyboard
from services import desk_service
from utils.time import local_date_str, tzinfo_from_string

router = Router()


def _short_title(value: object, limit: int = 140) -> str:
    title = str(value or "")
    if len(title) > limit:
        title = title[: limit - 1].rstrip() + "…"
    return html.escape(title)


@router.message(CommandStart(magic=F.args.startswith("desk_")), F.chat.type == "private")
async def cmd_desk_start(message: types.Message, state: FSMContext, db, command: CommandObject | None = None) -> None:
    args = (command.args if command else "") or ""
    if not args:
        text = (message.text or "").split(maxsplit=1)
        args = text[1] if len(text) > 1 else ""
    if not args.startswith("desk_"):
        return
    token = args[5:].strip()
    user = await repo.get_user_by_telegram_id(db, message.from_user.id)
    async with independent_connection(db) as writer:
        result = await desk_service.consume_link_token(writer, token, int(message.from_user.id))
    if result.get("status") != "linked":
        if result.get("status") == "already_linked":
            text = "Этот аккаунт или профиль Desk уже связан с другим пользователем. Отключи текущую связь в Desk и создай новую ссылку."
        else:
            text = "Эта ссылка уже использована или срок её действия истёк. Создай новую ссылку в Desk."
        await message.answer(text)
        return
    user = await repo.get_user(db, int(result["user_id"]))
    await state.clear()
    if result.get("offer_student_mode"):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎓 Включить режим Ученика", callback_data="settings:profile_mode:student")],
            [InlineKeyboardButton(text="Оставить Взрослый", callback_data="desk:keep_adult")],
            [InlineKeyboardButton(text="📚 Открыть Desk", callback_data="desk:main")],
        ])
        await message.answer("Desk подключён. Включить сфокусированный режим Ученика?", reply_markup=kb)
    else:
        intro = "Desk подключён. Здесь появятся занятия и задания." if result.get("role") == "student" else "Desk подключён."
        await message.answer(intro, reply_markup=main_menu_keyboard(dict(user)))


@router.message(Command("desk"), F.chat.type == "private")
async def desk_command(message: types.Message, db) -> None:
    await _show_desk(message, db, message.from_user.id)


@router.message(F.text == "📚 Учёба / Desk", F.chat.type == "private")
async def desk_menu(message: types.Message, db) -> None:
    await _show_desk(message, db, message.from_user.id)


@router.callback_query(F.data == "desk:main", F.message.chat.type == "private")
async def desk_main(callback: types.CallbackQuery, db) -> None:
    await _show_desk(callback.message, db, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "desk:keep_adult", F.message.chat.type == "private")
async def desk_keep_adult(callback: types.CallbackQuery, db) -> None:
    user = await repo.get_user_by_telegram_id(db, callback.from_user.id)
    if not user:
        await callback.answer("Аккаунт не найден. Открой /start.", show_alert=True)
        return
    if not await desk_service.get_link_for_user(db, int(user["id"])):
        await callback.answer("Подключение Desk уже изменилось. Создай новую ссылку.", show_alert=True)
        return
    # No profile mutation: preserve the user's existing adult preferences.
    await callback.message.answer(
        "Подключение Desk сохранено. Режим можно изменить в ⚙ Настройки.",
        reply_markup=main_menu_keyboard(dict(user) if user else None),
    )
    await callback.answer()


@router.callback_query(F.data == "desk:unlink", F.message.chat.type == "private")
async def desk_unlink(callback: types.CallbackQuery, db) -> None:
    user = await repo.get_user_by_telegram_id(db, callback.from_user.id)
    if not user:
        await callback.answer("Аккаунт не найден. Открой /start.", show_alert=True)
        return
    async with independent_connection(db) as writer:
        ok = await desk_service.unlink_user(writer, int(user["id"]))
    await callback.message.edit_text("Desk отключён." if ok else "Desk уже отключён.")
    await callback.answer()


async def _show_desk(message, db, telegram_id: int) -> None:
    user = await repo.get_user_by_telegram_id(db, telegram_id)
    if not user:
        await message.answer("Сначала открой /start.")
        return
    uid = int(user["id"])
    link = await desk_service.get_link_for_user(db, uid)
    if not link:
        url = get_desk_settings().public_url
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔗 Открыть Desk", url=url)
        ]]) if url else None
        await message.answer("Desk пока не подключён. Создай ссылку подключения в Desk и открой её здесь.", reply_markup=markup)
        return
    cur = await db.execute(
        "SELECT s.title,s.event_date,s.start_time,s.end_time,p.external_url FROM schedule_events s "
        "LEFT JOIN day_plan_items p ON p.user_id=s.user_id AND p.external_key=s.external_key "
        "WHERE s.user_id=? AND s.source='desk' AND s.event_date>=? "
        "ORDER BY s.event_date,s.start_time LIMIT 5", (uid, local_date_str(dt.datetime.utcnow(), user["timezone"]))
    )
    lessons = await cur.fetchall()
    tz = tzinfo_from_string(user["timezone"])
    local_now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc).astimezone(tz)
    today = local_now.date().isoformat()
    lessons = [
        row for row in lessons
        if row["event_date"] != today or not row["end_time"]
        or str(row["end_time"]) > local_now.strftime("%H:%M")
    ]
    cur = await db.execute(
        "SELECT p.title,d.plan_date,p.external_url FROM day_plan_items p "
        "JOIN day_plans d ON d.id=p.plan_id "
        "WHERE p.user_id=? AND p.external_key LIKE 'desk:hw:%' AND p.done=0 "
        "ORDER BY d.plan_date LIMIT 10", (uid,)
    )
    homework = await cur.fetchall()
    lines = ["<b>📚 Учёба / Desk</b>", "Подключено ✅"]
    if lessons:
        lines.append("\n<b>Ближайшие занятия</b>")
        lines.extend(
            f"• {html.escape(str(x['event_date']))} {html.escape(str(x['start_time'] or ''))} — "
            + (f'<a href="{html.escape(str(x["external_url"]), quote=True)}">{_short_title(x["title"])}</a>' if x["external_url"] else _short_title(x["title"]))
            for x in lessons
        )
    if homework:
        lines.append("\n<b>Активные задания</b>")
        lines.extend(
            "• " + (f'<a href="{html.escape(str(x["external_url"]), quote=True)}">{_short_title(x["title"])}</a>' if x["external_url"] else _short_title(x["title"]))
            + (f" — до {html.escape(str(x['plan_date']))}" if x['plan_date'] else "")
            for x in homework
        )
    buttons = [[InlineKeyboardButton(text="Отключить Desk", callback_data="desk:unlink")]]
    public_url = get_desk_settings().public_url
    if public_url:
        buttons.insert(0, [InlineKeyboardButton(text="🔗 Открыть Desk", url=public_url)])
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
