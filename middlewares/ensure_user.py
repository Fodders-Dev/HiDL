from aiogram import BaseMiddleware

from db import repositories as repo


class EnsureUserMiddleware(BaseMiddleware):
    """Ensure user exists only when it's safe.

    Важно: НЕ создаём пользователя в middleware, иначе ломается FSM-регистрация (/start).
    Для большинства сценариев создание делается явно (в /start) или через utils.user.ensure_user в хендлерах.
    """

    def __init__(self, conn):
        super().__init__()
        self.conn = conn

    async def __call__(self, handler, event, data):
        from_user = getattr(event, "from_user", None) or getattr(getattr(event, "message", None), "from_user", None)
        message = getattr(event, "message", None)
        text = getattr(event, "text", None) or getattr(message, "text", None)
        if from_user:
            # /start должен запускать регистрацию как "нового" пользователя.
            if text and text.startswith("/start"):
                return await handler(event, data)
            # Если юзер уже существует — ничего не делаем (хендлеры сами дергают ensure_user при необходимости).
            # Если юзера нет — тоже ничего не делаем: пусть /start проведёт регистрацию.
            _ = await repo.get_user_by_telegram_id(self.conn, from_user.id)
        return await handler(event, data)
