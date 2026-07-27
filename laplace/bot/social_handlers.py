"""Telegram commands for the local social affiliate workspace."""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from laplace.config import get_settings
from laplace.db import session_scope
from laplace.models import User
from laplace.social.models import PublishJob, SocialAccount, SocialPost

router = Router(name="laplace-social")


def _workspace_user_id(tg_id: int) -> int | None:
    with session_scope() as session:
        return session.scalar(select(User.id).where(User.tg_id == tg_id))


def _accounts_text(tg_id: int) -> str:
    user_id = _workspace_user_id(tg_id)
    if user_id is None:
        return "Chưa có workspace cho tài khoản Telegram này. Hãy nhắn bot một yêu cầu trước."
    with session_scope() as session:
        rows = session.execute(
            select(
                SocialAccount.id,
                SocialAccount.display_name,
                SocialAccount.platform,
                SocialAccount.status,
                SocialAccount.daily_post_limit,
            )
            .where(SocialAccount.user_id == user_id)
            .order_by(SocialAccount.id)
        ).all()
    if not rows:
        return "Chưa có Page/tài khoản social nào trong workspace."
    lines = ["Các tài khoản social:"]
    lines.extend(
        f"• #{row.id} · {row.display_name} ({row.platform}) · "
        f"{row.status} · limit {row.daily_post_limit}/ngày"
        for row in rows
    )
    return "\n".join(lines)


def _queue_text(tg_id: int, *, today_only: bool = False) -> str:
    user_id = _workspace_user_id(tg_id)
    if user_id is None:
        return "Chưa có workspace cho tài khoản Telegram này."

    statement = (
        select(
            PublishJob.id,
            PublishJob.scheduled_at,
            PublishJob.status,
            SocialPost.title,
            SocialAccount.display_name,
        )
        .join(SocialPost, PublishJob.social_post_id == SocialPost.id)
        .join(SocialAccount, PublishJob.social_account_id == SocialAccount.id)
        .where(
            SocialPost.user_id == user_id,
            SocialAccount.user_id == user_id,
            PublishJob.status.in_(("queued", "retry")),
        )
        .order_by(PublishJob.scheduled_at, PublishJob.id)
        .limit(20)
    )

    timezone = ZoneInfo(get_settings().social_timezone)
    if today_only:
        local_now = datetime.now(timezone)
        local_start = datetime.combine(local_now.date(), time.min, timezone)
        local_end = datetime.combine(local_now.date(), time.max, timezone)
        statement = statement.where(
            PublishJob.scheduled_at >= local_start.astimezone(UTC),
            PublishJob.scheduled_at <= local_end.astimezone(UTC),
        )

    with session_scope() as session:
        rows = session.execute(statement).all()
    label = "Lịch hôm nay" if today_only else "Hàng đợi đăng bài"
    if not rows:
        return f"{label}: chưa có job."
    lines = [f"{label}:"]
    for row in rows:
        scheduled = row.scheduled_at
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=UTC)
        local_time = scheduled.astimezone(timezone).strftime("%d/%m %H:%M")
        lines.append(
            f"• #{row.id} · {local_time} · {row.title} → "
            f"{row.display_name} · {row.status}"
        )
    return "\n".join(lines)


def _report_text(tg_id: int) -> str:
    user_id = _workspace_user_id(tg_id)
    if user_id is None:
        return "Chưa có workspace cho tài khoản Telegram này."
    with session_scope() as session:
        counts = dict(
            session.execute(
                select(PublishJob.status, func.count(PublishJob.id))
                .join(SocialPost, PublishJob.social_post_id == SocialPost.id)
                .join(SocialAccount, PublishJob.social_account_id == SocialAccount.id)
                .where(
                    SocialPost.user_id == user_id,
                    SocialAccount.user_id == user_id,
                )
                .group_by(PublishJob.status)
            ).all()
        )
    return (
        "Báo cáo publish:\n"
        f"• Đã đăng: {counts.get('published', 0)}\n"
        f"• Đang chờ/retry: {counts.get('queued', 0) + counts.get('retry', 0)}\n"
        f"• Thất bại: {counts.get('failed', 0)}\n"
        f"• Đã hủy: {counts.get('cancelled', 0)}\n\n"
        "Doanh thu/hoa hồng sẽ có sau khi cấu hình import đối soát affiliate."
    )


def _account_action_preview(tg_id: int, account_id: int, action: str) -> tuple[str, bool]:
    user_id = _workspace_user_id(tg_id)
    if user_id is None:
        return "Chưa có workspace cho tài khoản Telegram này.", False
    expected_status = "active" if action == "pause" else "paused"
    with session_scope() as session:
        row = session.execute(
            select(SocialAccount.display_name, SocialAccount.status).where(
                SocialAccount.id == account_id,
                SocialAccount.user_id == user_id,
            )
        ).one_or_none()
    if row is None:
        return f"Không tìm thấy tài khoản social #{account_id}.", False
    if row.status != expected_status:
        message = (
            f"Tài khoản #{account_id} đang ở trạng thái {row.status}, "
            f"không thể {action}."
        )
        return message, False
    verb = "tạm dừng" if action == "pause" else "tiếp tục"
    return f"Xác nhận {verb} đăng bài cho “{row.display_name}” (#{account_id})?", True


def _apply_account_action(tg_id: int, account_id: int, action: str) -> str:
    user_id = _workspace_user_id(tg_id)
    if user_id is None:
        return "Không có quyền thực hiện."
    expected_status = "active" if action == "pause" else "paused"
    target_status = "paused" if action == "pause" else "active"
    with session_scope() as session:
        account = session.scalar(
            select(SocialAccount).where(
                SocialAccount.id == account_id,
                SocialAccount.user_id == user_id,
            )
        )
        if account is None:
            return "Không tìm thấy tài khoản hoặc bạn không có quyền."
        if account.status != expected_status:
            return f"Không thay đổi: tài khoản đang ở trạng thái {account.status}."
        account.status = target_status
        name = account.display_name
    return f"Đã cập nhật “{name}” sang trạng thái {target_status}."


def _telegram_id(message: Message) -> int | None:
    return message.from_user.id if message.from_user is not None else None


@router.message(Command("accounts"))
async def cmd_accounts(message: Message) -> None:
    tg_id = _telegram_id(message)
    await message.answer(_accounts_text(tg_id) if tg_id is not None else "Không xác định user.")


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    tg_id = _telegram_id(message)
    await message.answer(
        _queue_text(tg_id, today_only=True) if tg_id is not None else "Không xác định user."
    )


@router.message(Command("queue"))
async def cmd_queue(message: Message) -> None:
    tg_id = _telegram_id(message)
    await message.answer(_queue_text(tg_id) if tg_id is not None else "Không xác định user.")


@router.message(Command("report"))
async def cmd_report(message: Message) -> None:
    tg_id = _telegram_id(message)
    await message.answer(_report_text(tg_id) if tg_id is not None else "Không xác định user.")


def _parse_account_id(message: Message) -> int | None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        return None
    try:
        account_id = int(parts[1])
    except ValueError:
        return None
    return account_id if account_id > 0 else None


async def _request_account_action(message: Message, action: str) -> None:
    tg_id = _telegram_id(message)
    account_id = _parse_account_id(message)
    if tg_id is None:
        await message.answer("Không xác định user.")
        return
    if account_id is None:
        await message.answer(f"Cú pháp: /{action} <account_id>")
        return
    text, allowed = _account_action_preview(tg_id, account_id, action)
    markup = None
    if allowed:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Xác nhận",
                        callback_data=f"socialacct:{action}:{account_id}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Hủy",
                        callback_data=f"socialacct:cancel:{account_id}",
                    ),
                ]
            ]
        )
    await message.answer(text, reply_markup=markup)


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    await _request_account_action(message, "pause")


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    await _request_account_action(message, "resume")


@router.callback_query(F.data.startswith("socialacct:"))
async def handle_account_action(callback: CallbackQuery) -> None:
    try:
        _, action, account_id_text = (callback.data or "").split(":")
        account_id = int(account_id_text)
    except ValueError:
        await callback.answer("Dữ liệu không hợp lệ", show_alert=True)
        return
    if callback.from_user is None:
        await callback.answer("Không xác định user", show_alert=True)
        return
    if action == "cancel":
        result = "Đã hủy thao tác."
    elif action in {"pause", "resume"}:
        result = _apply_account_action(callback.from_user.id, account_id, action)
    else:
        await callback.answer("Hành động không hợp lệ", show_alert=True)
        return
    await callback.answer("Đã xử lý")
    if callback.message is not None:
        await callback.message.edit_text(result, reply_markup=None)
