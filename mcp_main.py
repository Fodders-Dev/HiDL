"""Entrypoint for HiDL remote MCP Streamable HTTP + OAuth 2.1 server."""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web

from config import get_mcp_settings, get_settings
from db.database import connect, init_db
from mcp_server.app import create_mcp_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def serve() -> None:
    """Open shared WAL SQLite database and run the aiohttp MCP HTTP server."""
    app_cfg = get_settings(require_bot_token=False)
    mcp_cfg = get_mcp_settings()

    db_conn = await connect(app_cfg.database_url)
    await init_db(db_conn)
    app = create_mcp_app(db_conn, mcp_cfg, bot_token=app_cfg.bot_token)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=mcp_cfg.host, port=mcp_cfg.port)
    await site.start()

    logger.info(
        "HiDL MCP listening on http://%s:%s public=%s diary=%s readonly=%s owner_configured=%s",
        mcp_cfg.host,
        mcp_cfg.port,
        mcp_cfg.public_base_url,
        mcp_cfg.allow_diary,
        mcp_cfg.readonly,
        mcp_cfg.owner_telegram_id > 0,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    finally:
        logger.info("HiDL MCP shutting down...")
        await runner.cleanup()
        await db_conn.close()


def main() -> None:
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
