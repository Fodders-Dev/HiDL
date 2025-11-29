from aiogram import BaseMiddleware

from utils.user import ensure_user


class EnsureUserMiddleware(BaseMiddleware):
    """Auto-create user if missing (except fresh /start)."""

    def __init__(self, conn):
        super().__init__()
        self.conn = conn

    async def __call__(self, handler, event, data):
        from_user = getattr(event, "from_user", None) or getattr(getattr(event, "message", None), "from_user", None)
        text = getattr(event, "text", None)
        if from_user:
            if not (text and text.startswith("/start")):
                await ensure_user(self.conn, from_user.id, from_user.full_name)
        return await handler(event, data)
