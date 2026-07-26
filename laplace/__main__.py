"""Entrypoint: python -m laplace.

Chay web (uvicorn, port 8000) + scheduler; neu co Telegram token thi chay bot
polling song song trong cung event loop.
"""

import asyncio
import logging

import uvicorn

from laplace.config import get_settings
from laplace.db import init_db, session_scope
from laplace.models import User
from laplace.tools.base import load_builtin_tools
from laplace.web.app import create_app

logger = logging.getLogger(__name__)


def _telegram_notifier(token: str):
    """on_result cho scheduler: gui ket qua job dinh ky ve Telegram cua user.

    Chay trong thread cua APScheduler nen dung asyncio.run voi Bot rieng.
    """

    def notify(user_id: int, result: str) -> None:
        with session_scope() as session:
            user = session.get(User, user_id)
            tg_id = user.tg_id if user else None
        if not tg_id:
            logger.info("Job cua user=%s khong co tg_id, bo qua notify", user_id)
            return

        async def _send() -> None:
            from aiogram import Bot

            bot = Bot(token=token)
            try:
                text = result or "(không có nội dung)"
                for i in range(0, len(text), 4000):
                    await bot.send_message(tg_id, text[i : i + 4000])
            finally:
                await bot.session.close()

        try:
            asyncio.run(_send())
        except Exception:
            logger.exception("Gui ket qua job dinh ky that bai user=%s", user_id)

    return notify


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()

    # Chuan bi DB + tool registry truoc khi scheduler/bot dung den
    init_db()
    load_builtin_tools()

    from laplace.scheduler import start_scheduler

    on_result = None
    if settings.telegram_bot_token:
        on_result = _telegram_notifier(settings.telegram_bot_token)
    start_scheduler(on_result=on_result)

    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    )

    coros = [server.serve()]
    if settings.telegram_bot_token:
        from laplace.bot.runner import run_bot

        coros.append(run_bot())
    else:
        logger.warning(
            "Khong co LAPLACE_TELEGRAM_BOT_TOKEN — chi chay web + scheduler, "
            "bo qua Telegram bot."
        )

    await asyncio.gather(*coros)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
