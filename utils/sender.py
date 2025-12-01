"""Утилиты безопасной отправки/редактирования сообщений.

Чтобы не падать на часто встречающихся ошибках Telegram (message is not modified,
can't edit), используем единый хелпер и fallback на новое сообщение.
"""

from __future__ import annotations

from typing import Optional

from aiogram import types


async def safe_edit(
    message: types.Message,
    text: str,
    reply_markup: Optional[types.InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = None,
) -> None:
    """Попробовать отредактировать сообщение, если не получилось — отправить новое."""
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)


async def safe_edit_markup(
    message: types.Message, reply_markup: types.InlineKeyboardMarkup
) -> None:
    """Редактирует только клавиатуру, при ошибке — дублирует сообщение."""
    try:
        await message.edit_reply_markup(reply_markup=reply_markup)
    except Exception:
        await message.answer("Обновила список.", reply_markup=reply_markup)
