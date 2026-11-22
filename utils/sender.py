"""Утилиты безопасной отправки/редактирования сообщений.

Чтобы не падать на часто встречающихся ошибках Telegram (message is not modified,
can't edit), используем единый хелпер и fallback на новое сообщение.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from aiogram import types


logger = logging.getLogger(__name__)


async def safe_edit(
    message: types.Message,
    text: str,
    reply_markup: Optional[types.InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = None,
) -> None:
    """Попробовать отредактировать сообщение, если не получилось — отправить новое."""
    kwargs: dict[str, object] = {"reply_markup": reply_markup}
    if parse_mode is not None:
        kwargs["parse_mode"] = parse_mode
    try:
        await message.edit_text(text, **kwargs)
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.debug("safe_edit failed; sending fallback message (%s)", type(exc).__name__)
        await message.answer(text, **kwargs)


async def safe_edit_markup(
    message: types.Message, reply_markup: types.InlineKeyboardMarkup
) -> None:
    """Редактирует только клавиатуру, при ошибке — дублирует сообщение."""
    try:
        await message.edit_reply_markup(reply_markup=reply_markup)
    except Exception:
        await message.answer("Обновила список.", reply_markup=reply_markup)


async def safe_delete_or_clear_markup(
    bot,
    chat_id: int | None,
    message_id: int | None,
) -> bool:
    """Best-effort removal of a Telegram card, falling back to clearing its buttons."""
    if not chat_id or not message_id:
        return False
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as delete_error:
        logger.debug(
            "Pulse card delete failed; trying markup clear (%s)",
            type(delete_error).__name__,
        )
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=None,
        )
        return True
    except Exception as clear_error:
        logger.debug("Pulse card markup clear failed (%s)", type(clear_error).__name__)
        return False
