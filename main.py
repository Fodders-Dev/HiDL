import asyncio
import logging

from aiogram import Bot, Dispatcher

from config import get_settings
from db.database import connect, init_db
from handlers import (
    ask_mom,
    custom_reminders,
    donate,
    knowledge,
    menu,
    finance,
    guides,
    home_tasks,
    movement,
    routines,
    settings as settings_handler,
    start,
    stats,
    natural,
    wellness,
    zones,
    routine_items,
    talk,
)
from middlewares.db import DbSessionMiddleware
from middlewares.ensure_user import EnsureUserMiddleware
from scheduler.reminder import ReminderScheduler

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    settings = get_settings()
    conn = await connect(settings.database_url)
    await init_db(conn)

    bot = Bot(token=settings.bot_token, parse_mode="HTML")

    dp = Dispatcher()
    dp.update.middleware(DbSessionMiddleware(conn))
    dp.update.middleware(EnsureUserMiddleware(conn))
    dp.include_router(start.router)
    dp.include_router(menu.router)
    dp.include_router(knowledge.router)
    dp.include_router(routines.router)
    dp.include_router(settings_handler.router)
    dp.include_router(custom_reminders.router)
    dp.include_router(ask_mom.router)
    dp.include_router(finance.router)
    dp.include_router(guides.router)
    dp.include_router(home_tasks.router)
    dp.include_router(movement.router)
    dp.include_router(donate.router)
    dp.include_router(stats.router)
    dp.include_router(natural.router)
    dp.include_router(wellness.router)
    dp.include_router(zones.router)
    dp.include_router(routine_items.router)
    dp.include_router(talk.router)

    scheduler = ReminderScheduler(bot, conn)
    scheduler.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
