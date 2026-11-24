"""Telegram self-service hub (/mcp) for connecting AI clients to personal & household HiDL MCP."""

from __future__ import annotations

import datetime
import html
from typing import Any, Tuple

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import get_mcp_settings
from db import repositories as repo
from utils.profile import is_student_mode
from utils.sender import safe_edit
from utils.time import format_date_display, format_time_local, local_date_str
from utils.user import ensure_user

router = Router()


def _mcp_endpoint_url() -> str:
    cfg = get_mcp_settings()
    base = (cfg.public_base_url or "https://hidl.fodders.ru").rstrip("/")
    return f"{base}/mcp"


def _format_expiry_label(expires_iso: str, tz: str) -> str:
    try:
        dt = datetime.datetime.fromisoformat(str(expires_iso or "").strip())
        if dt.tzinfo is not None:
            dt_naive_utc = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        else:
            dt_naive_utc = dt
        d_str = format_date_display(local_date_str(dt_naive_utc, tz))
        t_str = format_time_local(dt_naive_utc, tz)
        return f"{d_str} {t_str}"
    except Exception:
        return str(expires_iso or "")[:16]


async def _safe_cb_answer(callback: types.CallbackQuery, text: str | None = None, *, show_alert: bool = False) -> None:
    try:
        if text is None and not show_alert:
            await callback.answer()
        else:
            await callback.answer(text, show_alert=show_alert)
    except Exception:
        pass


async def _render_mcp_hub(db, user: dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    uid = int(user["id"])
    tz = str(user.get("timezone") or "UTC")
    mcp_url = _mcp_endpoint_url()
    conns = await repo.list_user_mcp_connections(db, uid)

    lines = [
        "🤖 <b>Подключение ИИ-клиентов (MCP)</b>",
        f"Адрес сервера:\n<code>{html.escape(mcp_url)}</code>",
        "",
        "<b>Как подключить за 3 шага:</b>",
        "1. Открой ChatGPT (Connectors / Apps), Claude, Cursor или Antigravity и добавь MCP URL выше (OAuth 2.1).",
        "2. Нажми ниже «🔑 Код подключения», скопируй одноразовый код.",
        "3. Вставь этот код в окне авторизации — он привяжет клиента <b>лично к твоему аккаунту HiDL</b>.",
        "",
        "🔐 <b>Приватность:</b> твой дневник, личные задачи, сон, карма, вес и КБЖУ строго видны только тебе; "
        "холодильник, рецепты, список покупок, дела по дому и общие просьбы синхронизируются с домом.",
    ]

    if conns:
        lines.append(f"\n🔗 <b>Активные подключения ({len(conns)}):</b>")
        for c in conns[:6]:
            cname = html.escape(str(c.get("client_name") or "MCP-клиент"))
            exp_txt = _format_expiry_label(str(c.get("expires_at") or ""), tz)
            lines.append(f"• {cname} (до {html.escape(exp_txt)})")
    else:
        lines.append("\n⚪ Пока нет активных подключений под твоим аккаунтом.")

    rows = [
        [InlineKeyboardButton(text="🔑 Код подключения (OAuth, 15м)", callback_data="mcp:code:new")],
        [InlineKeyboardButton(text="🪪 Личный Bearer-токен", callback_data="mcp:pat:new")],
    ]
    if conns:
        rows.append([InlineKeyboardButton(text=f"🚫 Отозвать все доступы ({len(conns)})", callback_data="mcp:revoke:ask")])
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data="mcp:hub"),
        InlineKeyboardButton(text="⚙ Настройки", callback_data="settings:main"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("mcp"))
async def mcp_cmd(message: types.Message, db):
    user = dict(await ensure_user(db, message.from_user.id, message.from_user.full_name))
    if is_student_mode(user):
        await message.answer("Подключение MCP доступно во взрослом режиме HiDL.")
        return
    text, kb = await _render_mcp_hub(db, user)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data == "mcp:hub")
async def mcp_hub_callback(callback: types.CallbackQuery, db):
    await _safe_cb_answer(callback)
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if is_student_mode(user):
        await callback.message.answer("Подключение MCP доступно во взрослом режиме HiDL.")
        return
    text, kb = await _render_mcp_hub(db, user)
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data == "mcp:code:new")
async def mcp_new_connect_code(callback: types.CallbackQuery, db):
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if is_student_mode(user):
        await _safe_cb_answer(callback, "Подключение MCP доступно во взрослом режиме HiDL.", show_alert=True)
        return
    await _safe_cb_answer(callback, "Одноразовый код готов 🔑")

    tz = str(user.get("timezone") or "UTC")
    mcp_url = _mcp_endpoint_url()
    res = await repo.create_mcp_connect_code(db, int(user["id"]), ttl_minutes=15)
    exp_local = _format_expiry_label(res["expires_at"], tz)
    code = str(res["code"])

    text = (
        "🔑 <b>Одноразовый код подключения OAuth</b>\n\n"
        f"Код (нажми, чтобы скопировать):\n<code>{html.escape(code)}</code>\n\n"
        f"URL сервера MCP:\n<code>{html.escape(mcp_url)}</code>\n\n"
        f"⏳ Действует до <b>{html.escape(exp_local)}</b> (15 мин) и сработает ровно один раз.\n"
        "Вставь этот код в поле авторизации на странице входа — после этого все запросы клиента пойдут от имени "
        f"<b>{html.escape(str(user.get('name') or 'твоего аккаунта'))}</b>."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Новый код", callback_data="mcp:code:new")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="mcp:hub")],
    ])
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data == "mcp:pat:new")
async def mcp_new_personal_token(callback: types.CallbackQuery, db):
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if is_student_mode(user):
        await _safe_cb_answer(callback, "Подключение MCP доступно во взрослом режиме HiDL.", show_alert=True)
        return
    await _safe_cb_answer(callback, "Личный токен создан 🪪")

    tz = str(user.get("timezone") or "UTC")
    mcp_url = _mcp_endpoint_url()
    res = await repo.create_mcp_personal_token(db, int(user["id"]), ttl_days=90)
    exp_local = _format_expiry_label(res["expires_at"], tz)

    text = (
        "🪪 <b>Личный Bearer-токен MCP</b>\n\n"
        "Используй его для клиентов без OAuth (в заголовке <code>Authorization: Bearer ...</code>):\n"
        f"<code>{html.escape(res['token'])}</code>\n\n"
        f"URL сервера:\n<code>{html.escape(mcp_url)}</code>\n\n"
        f"⚠️ <b>Показывается один раз</b>, никому не пересылай. Действует 90 дней (до {html.escape(exp_local)})."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Отозвать все доступы", callback_data="mcp:revoke:ask")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="mcp:hub")],
    ])
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data == "mcp:revoke:ask")
async def mcp_revoke_ask(callback: types.CallbackQuery, db):
    await _safe_cb_answer(callback)
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if is_student_mode(user):
        return
    text = (
        "🚫 <b>Отозвать все твои MCP-доступы?</b>\n\n"
        "Все выпущенные для твоего аккаунта OAuth-токены, refresh-токены, личные Bearer-токены "
        "и неиспользованные коды перестанут работать (доступы партнёра не затронутся)."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, отозвать всё", callback_data="mcp:revoke:confirm")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="mcp:hub")],
    ])
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(lambda c: c.data == "mcp:revoke:confirm")
async def mcp_revoke_confirm(callback: types.CallbackQuery, db):
    user = dict(await ensure_user(db, callback.from_user.id, callback.from_user.full_name))
    if is_student_mode(user):
        await _safe_cb_answer(callback)
        return
    revoked = await repo.revoke_user_mcp_tokens(db, int(user["id"]))
    await _safe_cb_answer(callback, f"Отозвано: {revoked}", show_alert=True)
    text = (
        f"✅ <b>Отозвано токенов: {revoked}.</b>\n"
        "Все твои ИИ-подключения к HiDL отключены. При желании всегда можно выпустить новый код."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Новый код", callback_data="mcp:code:new")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="mcp:hub")],
    ])
    await safe_edit(callback.message, text, reply_markup=kb, parse_mode="HTML")
