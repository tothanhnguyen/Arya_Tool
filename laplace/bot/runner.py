"""Khoi chay Telegram bot: long-polling voi aiogram v3."""

import logging

from aiogram import Bot, Dispatcher

from laplace.bot.handlers import router
from laplace.config import get_settings
from laplace.db import init_db
from laplace.tools.base import load_builtin_tools

logger = logging.getLogger(__name__)


async def run_bot() -> None:
    """Chay bot long-polling. Raise RuntimeError neu thieu token."""
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError(
            "Thieu Telegram bot token. Dat bien moi truong LAPLACE_TELEGRAM_BOT_TOKEN "
            "(hoac trong .env) roi chay lai."
        )

    # Idempotent: dam bao DB va tool registry san sang khi bot chay doc lap
    init_db()
    load_builtin_tools()

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Telegram bot bat dau polling...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
