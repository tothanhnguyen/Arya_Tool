"""Entrypoint: python -m laplace.

Chay web (uvicorn, port 8010) + scheduler; neu co Telegram token thi chay bot
polling song song trong cung event loop.
"""

import asyncio
import contextlib
import logging
import signal

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


def _noop_install_signal_handlers() -> None:
    """Thay the uvicorn install_signal_handlers: main() tu quan ly SIGINT/SIGTERM."""


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()

    # Chuan bi DB + tool registry truoc khi scheduler/bot dung den
    init_db()
    load_builtin_tools()

    # Task 'running'/'pending' con sot lai tu tien trinh truoc la mo coi
    from laplace.services.tasks import recover_orphan_tasks

    with session_scope() as session:
        orphans = recover_orphan_tasks(session)
    if orphans:
        logger.warning("Danh dau %d task mo coi (running/pending) thanh failed", orphans)

    from laplace.scheduler import start_scheduler, stop_scheduler
    from laplace.social.scheduler import (
        start_social_scheduler,
        stop_social_scheduler,
    )

    on_result = None
    if settings.telegram_bot_token:
        on_result = _telegram_notifier(settings.telegram_bot_token)
    start_scheduler(on_result=on_result)
    start_social_scheduler()

    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.web_host,
            port=settings.web_port,
            log_level="info",
        )
    )
    # Khong de uvicorn chiem SIGINT/SIGTERM — neu no chiem, bot polling va
    # scheduler khong duoc dung, process treo phai kill -9.
    server.install_signal_handlers = _noop_install_signal_handlers  # type: ignore[method-assign]

    server_task = asyncio.create_task(server.serve(), name="uvicorn")
    tasks: list[asyncio.Task] = [server_task]
    bot_task: asyncio.Task | None = None
    if settings.telegram_bot_token:
        from laplace.bot.runner import run_bot

        bot_task = asyncio.create_task(run_bot(), name="telegram-bot")
        tasks.append(bot_task)
    else:
        logger.warning(
            "Khong co LAPLACE_TELEGRAM_BOT_TOKEN — chi chay web + scheduler, "
            "bo qua Telegram bot."
        )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(sig: signal.Signals) -> None:
        logger.info("Nhan tin hieu %s — bat dau graceful shutdown", sig.name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop, sig)
        except NotImplementedError:
            # Fallback cho platform khong ho tro add_signal_handler (vd Windows)
            signal.signal(sig, lambda s, _f: _request_stop(signal.Signals(s)))

    # Cho den khi nhan signal hoac mot task ket thuc som (crash/loi token...)
    stop_wait = asyncio.create_task(stop_event.wait(), name="stop-signal")
    await asyncio.wait([stop_wait, *tasks], return_when=asyncio.FIRST_COMPLETED)
    stop_wait.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await stop_wait

    # Thu tu tat: scheduler -> bot polling -> uvicorn; gioi han ~5 giay
    stop_social_scheduler()
    stop_scheduler()
    if bot_task is not None and not bot_task.done():
        bot_task.cancel()
    server.should_exit = True

    _done, pending = await asyncio.wait(tasks, timeout=5)
    for t in pending:
        logger.warning("Task %s khong dung kip 5s, cancel cung", t.get_name())
        t.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for t, res in zip(tasks, results):
        if isinstance(res, Exception):
            logger.error("Task %s ket thuc voi loi: %r", t.get_name(), res)
    logger.info("Shutdown hoan tat.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
