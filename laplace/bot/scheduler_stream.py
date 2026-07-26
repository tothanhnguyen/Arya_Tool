"""Streaming tien do cho task chay tu scheduler (cron/hen gio) — lap lo hong T5.

Khac voi handler (co san `Message` aiogram trong update), job cua APScheduler
chay trong worker thread rieng, khong co message nao de tra loi. Module nay:

- Tu tao `Bot` aiogram (token tu config), gui MOT message trang thai vao chat
  cua user (tra cuu `User.tg_id` tu DB) — dong vai `message.answer` ma
  `ProgressStreamer.start` can, qua adapter `_ChatTarget`/`_StatusMessage`.
- Boc provider bang `ProgressLLM` (qua `ProgressStreamer.run`) va inject vao
  `run_task(task_id, llm=...)` — giong het duong handler, KHONG dung vao
  `laplace/agent/*`.
- Task xong thi `finalize()`: EDIT message trang thai thanh ket qua cuoi
  (phan vuot 4000 ky tu gui tiep thanh message moi).

Hop dong voi `laplace/scheduler.py` (best-effort, khong raise):
- Tra ve True  -> task DA duoc chay kem streaming, ket qua DA duoc bao
  (scheduler khong duoc chay lai run_task, khong goi on_result nua).
- Tra ve False -> chua chay task (thieu token / user khong co tg_id / khong
  gui duoc message trang thai ban dau) -> scheduler chay duong cu.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from laplace.bot.streaming import ProgressStreamer
from laplace.config import get_settings
from laplace.db import session_scope
from laplace.llm.base import LLMProvider
from laplace.models import User

logger = logging.getLogger(__name__)

TG_CHUNK = 4000  # gioi han Telegram ~4096 ky tu / message

SCHEDULED_CRASH_TEXT = (
    "⚠️ Có lỗi xảy ra khi chạy tác vụ định kỳ. "
    "Xem trace viewer để biết chi tiết."
)


class _StatusMessage:
    """Message trang thai da gui: `edit_text` tuong thich voi ProgressStreamer."""

    def __init__(self, bot: Any, chat_id: int, message_id: int):
        self._bot = bot
        self._chat_id = chat_id
        self._message_id = message_id

    async def edit_text(self, text: str, reply_markup: Any = None) -> None:
        await self._bot.edit_message_text(
            text=text,
            chat_id=self._chat_id,
            message_id=self._message_id,
            reply_markup=reply_markup,
        )


class _ChatTarget:
    """Doi tuong 'giong Message' toi thieu: `answer()` gui message moi vao chat."""

    def __init__(self, bot: Any, chat_id: int):
        self._bot = bot
        self._chat_id = chat_id

    async def answer(self, text: str) -> _StatusMessage:
        msg = await self._bot.send_message(self._chat_id, text)
        return _StatusMessage(self._bot, self._chat_id, msg.message_id)


def _default_bot(token: str) -> Any:
    from aiogram import Bot

    return Bot(token=token)


def _resolve_tg_id(user_id: int) -> int | None:
    with session_scope() as session:
        user = session.get(User, user_id)
        return user.tg_id if user else None


async def _deliver_final(
    streamer: ProgressStreamer, target: _ChatTarget, text: str
) -> None:
    """Uu tien EDIT message trang thai thanh ket qua; fallback gui message moi."""
    text = text or "(không có nội dung)"
    try:
        if await streamer.finalize(text[:TG_CHUNK]):
            start = TG_CHUNK  # phan con lai (neu dai) gui tiep phia sau
        else:
            start = 0  # edit that bai -> gui toan bo bang message moi
        for i in range(start, len(text), TG_CHUNK):
            await target.answer(text[i : i + TG_CHUNK])
    except Exception:
        logger.exception("gui ket qua task dinh ky (streaming) that bai")


async def _stream(
    task_id: int,
    tg_id: int,
    run: Callable[..., Any],
    token: str,
    bot_factory: Callable[[str], Any] | None,
    min_interval: float,
    llm: LLMProvider | None,
) -> bool:
    bot = (bot_factory or _default_bot)(token)
    try:
        target = _ChatTarget(bot, tg_id)
        try:
            streamer = await ProgressStreamer.start(target, min_interval=min_interval)
        except Exception:
            # Chua chay task -> de scheduler fallback duong cu
            logger.warning(
                "khong gui duoc message trang thai (task=%s), fallback khong streaming",
                task_id,
                exc_info=True,
            )
            return False

        # Tu day tro di task DA (bat dau) chay -> moi nhanh deu tra ve True,
        # khong duoc de scheduler chay lai run_task lan nua.
        try:
            await streamer.run(lambda provider: run(task_id, llm=provider), llm=llm)
        except Exception:
            logger.exception("run_task (scheduler, streaming) crashed task_id=%s", task_id)
            await _deliver_final(streamer, target, SCHEDULED_CRASH_TEXT)
            return True

        try:
            from laplace.scheduler import task_result_text

            final = task_result_text(task_id)
        except Exception:
            logger.exception("doc ket qua task dinh ky that bai task_id=%s", task_id)
            final = SCHEDULED_CRASH_TEXT
        await _deliver_final(streamer, target, final)
        return True
    finally:
        try:
            await bot.session.close()
        except Exception:
            logger.debug("dong session bot that bai", exc_info=True)


def stream_scheduled_task(
    task_id: int,
    user_id: int,
    run: Callable[..., Any],
    *,
    token: str | None = None,
    bot_factory: Callable[[str], Any] | None = None,
    min_interval: float = 1.5,
    llm: LLMProvider | None = None,
) -> bool:
    """Chay task dinh ky kem streaming tien do ve Telegram (neu co the).

    `run` la ham sync dang `run(task_id, llm=provider)` (vd run_task cua
    orchestrator); `llm` la provider goc de boc (mac dinh: theo config). Goi
    tu thread cua APScheduler nen dung asyncio.run voi Bot rieng (giong
    _telegram_notifier trong __main__).
    """
    token = token or get_settings().telegram_bot_token
    if not token:
        return False
    tg_id = _resolve_tg_id(user_id)
    if not tg_id:
        logger.info("user=%s khong co tg_id, bo qua streaming", user_id)
        return False
    try:
        return asyncio.run(
            _stream(task_id, tg_id, run, token, bot_factory, min_interval, llm)
        )
    except Exception:
        # Chi toi day khi loi setup (tao Bot/loop...) — task chua chay.
        # (_stream tu nuot loi sau khi run bat dau, nen khong the double-run.)
        logger.exception("streaming task scheduler that bai task_id=%s", task_id)
        return False
