from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.bot import DefaultBotProperties

from config import get_settings
from db.database import connect, init_db
from handlers import (
    ask_mom,
    affirmations,
    custom_reminders,
    donate,
    knowledge,
    menu,
    schedule,
    finance,
    guides,
    home_tasks,
    home_supplies,
    meds,
    movement,
    routines,
    day_plan,
    kitchen,
    settings as settings_handler,
    start,
    stats,
    natural,
    wellness,
    zones,
    routine_items,
    routine_steps,
    talk,
)
from middlewares.db import DbSessionMiddleware
from middlewares.ensure_user import EnsureUserMiddleware
from middlewares.error_log import ErrorLogMiddleware
from middlewares.debug_log import DebugLogMiddleware
from utils.logger import DEBUG_ENABLED, log_debug


logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    bot: Bot
    dp: Dispatcher
    db_conn: Any


async def create_app(test_mode: bool = False) -> AppContext:
    """
    Инициализировать приложение HiDL.

    test_mode=True — отдельная БД и фейковый токен для симулятора.
    """
    settings = get_settings()
    database_url = settings.database_url
    if test_mode:
        # отдельная БД для симуляций, чтобы не трогать боевые данные.
        # Можно переопределить через env, чтобы избежать проблем с lock на Windows.
        database_url = os.environ.get("HIDL_SIM_DB_URL", "sqlite:///hidl_simulator.db")
        logger.info("create_app in test mode, db=%s", database_url)

    conn = await connect(database_url)
    await init_db(conn)

    if test_mode:
        # aiogram валидирует формат токена, поэтому используем фиктивный,
        # но похожий на настоящий. В сеть он не ходит, потому что в
        # симуляторе все вызовы перехватывает TerminalBotProxy.
        bot_token = "123456:SIMULATOR_FAKE_TOKEN"
    else:
        bot_token = settings.bot_token
    bot = Bot(token=bot_token, default=DefaultBotProperties(parse_mode="HTML"))

    dp = Dispatcher()
    dp.update.middleware(DbSessionMiddleware(conn))
    dp.update.middleware(EnsureUserMiddleware(conn))
    dp.update.middleware(ErrorLogMiddleware())
    if settings.debug_log or DEBUG_ENABLED:
        dp.update.middleware(DebugLogMiddleware())
        log_debug("[setup] DebugLogMiddleware enabled")

    # роутеры
    dp.include_router(start.router)
    dp.include_router(menu.router)
    dp.include_router(schedule.router)
    dp.include_router(knowledge.router)
    dp.include_router(routines.router)
    dp.include_router(settings_handler.router)
    dp.include_router(affirmations.router)
    dp.include_router(day_plan.router)
    dp.include_router(custom_reminders.router)
    dp.include_router(ask_mom.router)
    dp.include_router(finance.router)
    dp.include_router(guides.router)
    dp.include_router(home_tasks.router)
    dp.include_router(home_supplies.router)
    dp.include_router(movement.router)
    dp.include_router(meds.router)
    dp.include_router(kitchen.router)
    dp.include_router(donate.router)
    dp.include_router(stats.router)
    dp.include_router(natural.router)
    dp.include_router(wellness.router)
    dp.include_router(zones.router)
    dp.include_router(routine_items.router)
    dp.include_router(routine_steps.router)
    dp.include_router(talk.router)

    return AppContext(bot=bot, dp=dp, db_conn=conn)
