from aiogram import BaseMiddleware


class DbSessionMiddleware(BaseMiddleware):
    """Inject shared DB connection into handler data."""

    def __init__(self, conn):
        super().__init__()
        self.conn = conn

    async def __call__(self, handler, event, data):
        data["db"] = self.conn
        return await handler(event, data)
