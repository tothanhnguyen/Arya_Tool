"""Telegram handlers (aiogram v3): chat -> task, confirm-flow qua inline button."""

import json
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from laplace.agent.orchestrator import resume_task, run_task
from laplace.bot.ratelimit import RateLimiter
from laplace.bot.streaming import ProgressStreamer
from laplace.db import session_scope
from laplace.models import Task, User
from laplace.services.tasks import (
    add_message,
    create_task,
    get_or_create_conversation,
    get_or_create_user,
)

logger = logging.getLogger(__name__)

router = Router(name="laplace")

MAX_INPUT_CHARS = 2000  # chan message qua dai
TG_CHUNK = 4000  # gioi han Telegram ~4096 ky tu / message

# Rate limit theo Telegram user id: moi user toi da RATE_MAX_REQUESTS yeu cau
# trong RATE_WINDOW_S giay — mot user spam khong the chiem het agent loop.
RATE_MAX_REQUESTS = 6
RATE_WINDOW_S = 60.0
rate_limiter = RateLimiter(max_requests=RATE_MAX_REQUESTS, window_s=RATE_WINDOW_S)

START_TEXT = (
    "Xin chào, tôi là Laplace's Demon — trợ lý nghiên cứu & báo cáo cá nhân.\n\n"
    "Tôi có thể:\n"
    "• Tìm kiếm và tổng hợp thông tin từ nhiều nguồn\n"
    "• Lưu ghi chú, quản lý việc cần làm\n"
    "• Viết báo cáo ngắn từ nhiều trang web\n"
    "• Chạy tác vụ định kỳ (ví dụ: tổng hợp tin mỗi sáng)\n\n"
    "Cứ nhắn yêu cầu bằng ngôn ngữ tự nhiên. Gõ /help để xem thêm."
)

HELP_TEXT = (
    "Cách dùng:\n"
    "• Nhắn yêu cầu bất kỳ, ví dụ: \"So sánh FastAPI và Flask, viết báo cáo ngắn\"\n"
    "• Hành động ghi/xóa (lưu ghi chú, xóa task, tạo lịch) sẽ hỏi xác nhận "
    "bằng nút ✅/❌ trước khi thực thi\n"
    "• /start — giới thiệu\n"
    "• /help — trợ giúp này\n\n"
    f"Lưu ý: tin nhắn tối đa {MAX_INPUT_CHARS} ký tự; "
    "mỗi người tối đa vài yêu cầu mỗi phút để bot phục vụ được mọi người."
)


async def _reply_chunks(message: Message, text: str) -> None:
    """Gui van ban dai, chia nho theo gioi han Telegram."""
    text = text or "(không có nội dung)"
    for i in range(0, len(text), TG_CHUNK):
        await message.answer(text[i : i + TG_CHUNK])


def _confirm_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Đồng ý", callback_data=f"confirm:{task_id}:yes"
                ),
                InlineKeyboardButton(
                    text="❌ Từ chối", callback_data=f"confirm:{task_id}:no"
                ),
            ]
        ]
    )


def _load_task_view(task_id: int) -> dict | None:
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            return None
        owner = session.get(User, task.user_id)
        return {
            "id": task.id,
            "status": task.status,
            "result": task.result,
            "error": task.error,
            "pending": (task.state_json or {}).get("pending"),
            "owner_tg_id": owner.tg_id if owner else None,
        }


async def _deliver(
    message: Message,
    streamer: ProgressStreamer | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Uu tien EDIT message trang thai (streaming); fallback gui message moi."""
    if streamer is not None and await streamer.finalize(text, reply_markup=reply_markup):
        return
    await message.answer(text, reply_markup=reply_markup)


async def _send_task_outcome(
    message: Message, task_id: int, streamer: ProgressStreamer | None = None
) -> None:
    """Gui ket qua task ve chat theo trang thai hien tai.

    Neu co `streamer`, message trang thai streaming duoc edit thanh ket qua
    cuoi thay vi gui message moi.
    """
    view = _load_task_view(task_id)
    if view is None:
        await _deliver(message, streamer, "⚠️ Không tìm thấy task.")
        return

    status = view["status"]
    if status == "awaiting_confirm":
        pending = view["pending"] or {}
        tool = pending.get("tool", "?")
        try:
            params = json.dumps(pending.get("params", {}), ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            params = str(pending.get("params", {}))
        text = (
            f"🔒 Hành động cần xác nhận (task #{task_id}):\n"
            f"Tool: {tool}\n"
            f"Tham số:\n{params[:1000]}\n\n"
            "Bạn có đồng ý thực hiện không?"
        )
        await _deliver(message, streamer, text, reply_markup=_confirm_keyboard(task_id))
    elif status == "done":
        result = view["result"] or "✅ Xong."
        if streamer is not None and await streamer.finalize(result[:TG_CHUNK]):
            # Phan con lai (neu dai hon 1 message Telegram) gui tiep phia sau
            for i in range(TG_CHUNK, len(result), TG_CHUNK):
                await message.answer(result[i : i + TG_CHUNK])
        else:
            await _reply_chunks(message, result)
    elif status == "failed":
        detail = (view["error"] or "lỗi không xác định")[:300]
        await _deliver(
            message,
            streamer,
            f"😥 Rất tiếc, tôi chưa hoàn thành được yêu cầu này ({detail}). "
            "Bạn thử diễn đạt lại hoặc thử lại sau nhé.",
        )
    else:
        await _deliver(message, streamer, f"⏳ Task #{task_id} đang ở trạng thái: {status}")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@router.message(F.text)
async def handle_text(message: Message) -> None:
    text = (message.text or "").strip()
    if text.startswith("/"):
        await message.answer("Lệnh không hỗ trợ. Gõ /help để xem hướng dẫn.")
        return
    if not text:
        return
    if len(text) > MAX_INPUT_CHARS:
        await message.answer(
            f"Tin nhắn quá dài ({len(text)} ký tự). "
            f"Vui lòng rút gọn dưới {MAX_INPUT_CHARS} ký tự."
        )
        return

    tg_id = message.from_user.id if message.from_user else None
    if tg_id is not None and not rate_limiter.allow(tg_id):
        wait_s = max(1, int(rate_limiter.retry_after(tg_id) + 0.999))
        await message.answer(
            f"🚦 Bạn đang gửi yêu cầu quá nhanh (tối đa {RATE_MAX_REQUESTS} yêu cầu "
            f"mỗi {int(RATE_WINDOW_S)} giây). Vui lòng đợi khoảng {wait_s} giây rồi thử lại."
        )
        return

    # Message trang thai duy nhat — se duoc EDIT theo tien do roi thay bang ket qua
    streamer = await ProgressStreamer.start(message)

    with session_scope() as session:
        user = get_or_create_user(session, tg_id=tg_id)
        conv = get_or_create_conversation(session, user.id)
        add_message(session, conv.id, "user", text)
        task = create_task(session, user_id=user.id, request=text, conversation_id=conv.id)
        task_id = task.id

    try:
        await streamer.run(lambda llm: run_task(task_id, llm=llm))
    except Exception:
        logger.exception("run_task crashed task_id=%s", task_id)
        await _deliver(
            message, streamer, "⚠️ Có lỗi xảy ra khi xử lý yêu cầu. Bạn thử lại sau nhé."
        )
        return

    await _send_task_outcome(message, task_id, streamer=streamer)


@router.callback_query(F.data.startswith("confirm:"))
async def handle_confirm(callback: CallbackQuery) -> None:
    try:
        _, task_id_str, choice = (callback.data or "").split(":")
        task_id = int(task_id_str)
    except ValueError:
        await callback.answer("Dữ liệu không hợp lệ", show_alert=True)
        return

    # Chi chu task duoc bam nut (chan nguoi khac trong group chat confirm thay)
    view = _load_task_view(task_id)
    if view is None:
        await callback.answer("Không tìm thấy task", show_alert=True)
        return
    owner_tg_id = view.get("owner_tg_id")
    if owner_tg_id is not None and (
        callback.from_user is None or callback.from_user.id != owner_tg_id
    ):
        await callback.answer("Nút xác nhận này không dành cho bạn.", show_alert=True)
        return

    approved = choice == "yes"
    await callback.answer("Đã ghi nhận ✅" if approved else "Đã hủy ❌")

    message = callback.message
    if message is None:
        return
    # Go nut de tranh bam lai nhieu lan
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Streaming tien do khi resume, giong nhu task thuong
    streamer = await ProgressStreamer.start(message)
    try:
        await streamer.run(lambda llm: resume_task(task_id, approved, llm=llm))
    except Exception:
        logger.exception("resume_task crashed task_id=%s", task_id)
        await _deliver(message, streamer, "⚠️ Có lỗi khi tiếp tục task. Bạn thử lại sau nhé.")
        return

    await _send_task_outcome(message, task_id, streamer=streamer)
