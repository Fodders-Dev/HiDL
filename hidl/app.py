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
    desk,
    donate,
    knowledge,
    menu,
    pause,
    schedule,
    finance,
    focus_cafe,
    guides,
    home_tasks,
    home_supplies,
    household,
    meds,
    movement,
    nutrition,
    routines,
    day_plan,
    kitchen,
    pulse,
    settings as settings_handler,
    start,
    stats,
    natural,
    wellness,
    sleep_mode,
    help_now,
    zones,
    routine_items,
    routine_steps,
    inbox,
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
    settings = get_settings(require_bot_token=not test_mode)
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
    routers = (
        desk.router,
        pause.router,
        start.router,
        menu.router,
        pulse.router,
        schedule.router,
        knowledge.router,
        routines.router,
        settings_handler.router,
        affirmations.router,
        day_plan.router,
        custom_reminders.router,
        ask_mom.router,
        finance.router,
        focus_cafe.router,
        guides.router,
        home_tasks.router,
        home_supplies.router,
        household.router,
        movement.router,
        meds.router,
        kitchen.router,
        nutrition.router,
        donate.router,
        stats.router,
        inbox.router,
        natural.router,
        wellness.router,
        sleep_mode.router,
        help_now.router,
        zones.router,
        routine_items.router,
        routine_steps.router,
        talk.router,
    )
    try:
        for router in routers:
            if test_mode and router.parent_router is not None:
                router._parent_router = None  # noqa: SLF001
            dp.include_router(router)
    except Exception:
        await conn.close()
        await bot.session.close()
        raise

    return AppContext(bot=bot, dp=dp, db_conn=conn)
