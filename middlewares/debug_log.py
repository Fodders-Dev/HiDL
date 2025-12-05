from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, Update

from utils.logger import log_debug


class DebugLogMiddleware(BaseMiddleware):
    """Логирование действий пользователя для отладки (включается DEBUG_LOG=1)."""

    async def __call__(self, handler, event, data):
        try:
            if isinstance(event, Update):
                if event.message and isinstance(event.message, Message):
                    m = event.message
                    log_debug(
                        f"[update] message chat={m.chat.id} user={m.from_user.id} "
                        f"text={repr(m.text)} caption={repr(m.caption)} "
                        f"reply_to={m.reply_to_message.message_id if m.reply_to_message else None}"
                    )
                elif event.callback_query and isinstance(event.callback_query, CallbackQuery):
                    c = event.callback_query
                    log_debug(
                        f"[update] callback chat={c.message.chat.id if c.message else None} "
                        f"user={c.from_user.id} data={repr(c.data)} "
                        f"message_id={c.message.message_id if c.message else None}"
                    )
                else:
                    log_debug(f"[update] type={event.update_type}")
            elif isinstance(event, Message):
                m = event
                log_debug(
                    f"[update] message chat={m.chat.id} user={m.from_user.id} "
                    f"text={repr(m.text)} caption={repr(m.caption)} "
                    f"reply_to={m.reply_to_message.message_id if m.reply_to_message else None}"
                )
            elif isinstance(event, CallbackQuery):
                c = event
                log_debug(
                    f"[update] callback chat={c.message.chat.id if c.message else None} "
                    f"user={c.from_user.id} data={repr(c.data)} "
                    f"message_id={c.message.message_id if c.message else None}"
                )
        except Exception:
            # Не роняем обработку при логировании
            pass
        return await handler(event, data)
