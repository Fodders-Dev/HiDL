import asyncio
import logging

from hidl.app import create_app
from scheduler.reminder import ReminderScheduler

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    """
    Точка входа «боевого» бота HiDL.

    Использует общую фабрику приложения, чтобы конфигурация совпадала
    с тестовым/симуляторным режимом.
    """
    ctx = await create_app(test_mode=False)

    scheduler = ReminderScheduler(ctx.bot, ctx.db_conn)
    scheduler.start()

    await ctx.dp.start_polling(ctx.bot)


if __name__ == "__main__":
    asyncio.run(main())
