import traceback

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from utils.logger import log_error


class ErrorLogMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        try:
            return await handler(event, data)
        except Exception as exc:  # noqa: BLE001
            log_error("Unhandled error", exc)
            try:
                message = getattr(event, "message", None) or getattr(event, "callback_query", None)
                if message:
                    await message.answer("Ой, что-то пошло не так. Давай попробуем ещё раз.")
            except Exception:
                traceback.print_exc()
            raise
