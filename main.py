import asyncio
import logging

from config import get_llm_settings, get_settings, get_stt_settings
from hidl.app import create_app
from scheduler.reminder import ReminderScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _log_startup_summary() -> None:
    """Log non-secret runtime configuration summary."""
    settings = get_settings(require_bot_token=False)
    llm_cfg = get_llm_settings()
    stt_cfg = get_stt_settings()
    llm_desc = (
        f"{llm_cfg.primary.provider}:{llm_cfg.primary.model}"
        if llm_cfg.enabled and llm_cfg.primary.is_ready
        else "disabled"
    )
    failover_desc = (
        f"{llm_cfg.failover.provider}:{llm_cfg.failover.model}"
        if llm_cfg.failover and llm_cfg.failover.is_ready
        else "none"
    )
    stt_desc = ",".join(r.provider for r in stt_cfg.routes) if stt_cfg.routes else "disabled"
    logger.info(
        "HiDL starting: db=%s llm=%s failover=%s stt=%s",
        settings.database_url,
        llm_desc,
        failover_desc,
        stt_desc,
    )


async def main() -> None:
    """
    Точка входа «боевого» бота HiDL.

    Использует общую фабрику приложения, чтобы конфигурация совпадала
    с тестовым/симуляторным режимом.
    """
    _log_startup_summary()
    ctx = await create_app(test_mode=False)

    scheduler = ReminderScheduler(ctx.bot, ctx.db_conn)
    scheduler.start()

    try:
        await ctx.dp.start_polling(ctx.bot)
    finally:
        logger.info("HiDL shutting down...")
        try:
            scheduler.scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            await ctx.db_conn.close()
        except Exception:
            pass
        try:
            await ctx.bot.session.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
