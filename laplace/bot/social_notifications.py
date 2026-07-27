"""Best-effort Telegram notifications for deterministic social publishing."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select

from laplace.db import session_scope
from laplace.models import User
from laplace.social.models import PublishJob, SocialAccount, SocialPost
from laplace.social.worker import WorkerOutcome

logger = logging.getLogger(__name__)

TG_CHUNK = 4000
PROFILE_OUTCOME_KEYS = "social_notification_keys"
PROFILE_DAILY_SUMMARY = "social_daily_summary"
MAX_OUTCOME_KEYS = 200
TERMINAL_STATUSES = frozenset({"published", "failed"})
ACCOUNT_ALERT_STATUSES = frozenset({"checkpoint", "expired"})


class MessageSender(Protocol):
    """Synchronous sender used from the APScheduler worker thread."""

    def send(self, chat_id: int, text: str) -> None: ...


class TelegramMessageSender:
    """Create a short-lived aiogram session for one scheduler delivery."""

    def __init__(self, token: str) -> None:
        if not token.strip():
            raise ValueError("Telegram token must not be blank")
        self._token = token

    def send(self, chat_id: int, text: str) -> None:
        async def _send() -> None:
            from aiogram import Bot

            bot = Bot(token=self._token)
            try:
                content = text or "(không có nội dung)"
                for offset in range(0, len(content), TG_CHUNK):
                    await bot.send_message(chat_id, content[offset : offset + TG_CHUNK])
            finally:
                await bot.session.close()

        asyncio.run(_send())


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _profile(user: User) -> dict:
    return dict(user.profile_json or {})


def _outcome_was_sent(user_id: int, key: str) -> bool:
    with session_scope() as session:
        user = session.get(User, user_id)
        if user is None:
            return True
        keys = _profile(user).get(PROFILE_OUTCOME_KEYS, [])
        return isinstance(keys, list) and key in keys


def _mark_outcome_sent(user_id: int, key: str) -> None:
    with session_scope() as session:
        user = session.get(User, user_id)
        if user is None:
            return
        profile = _profile(user)
        raw_keys = profile.get(PROFILE_OUTCOME_KEYS, [])
        keys = [item for item in raw_keys if isinstance(item, str)] if isinstance(raw_keys, list) else []
        if key not in keys:
            keys.append(key)
        profile[PROFILE_OUTCOME_KEYS] = keys[-MAX_OUTCOME_KEYS:]
        user.profile_json = profile


def _daily_was_sent(user: User, local_day: str, timezone_name: str) -> bool:
    value = _profile(user).get(PROFILE_DAILY_SUMMARY)
    return (
        isinstance(value, dict)
        and value.get("date") == local_day
        and value.get("timezone") == timezone_name
    )


def _mark_daily_sent(user_id: int, local_day: str, timezone_name: str) -> None:
    with session_scope() as session:
        user = session.get(User, user_id)
        if user is None:
            return
        profile = _profile(user)
        profile[PROFILE_DAILY_SUMMARY] = {
            "date": local_day,
            "timezone": timezone_name,
        }
        user.profile_json = profile


def _outcome_snapshot(job_id: int) -> dict | None:
    with session_scope() as session:
        row = session.execute(
            select(
                PublishJob.id,
                PublishJob.status,
                SocialPost.title,
                SocialPost.user_id,
                SocialAccount.display_name,
                SocialAccount.status.label("account_status"),
                User.tg_id,
            )
            .join(SocialPost, PublishJob.social_post_id == SocialPost.id)
            .join(SocialAccount, PublishJob.social_account_id == SocialAccount.id)
            .join(User, SocialPost.user_id == User.id)
            .where(
                PublishJob.id == job_id,
                SocialAccount.user_id == SocialPost.user_id,
            )
        ).mappings().one_or_none()
        return dict(row) if row is not None else None


def _outcome_text(snapshot: dict) -> str:
    job_id = snapshot["id"]
    title = snapshot["title"]
    account = snapshot["display_name"]
    if snapshot["status"] == "published":
        return (
            f"✅ Đã đăng bài #{job_id}\n"
            f"• Nội dung: {title}\n"
            f"• Tài khoản: {account}"
        )
    if snapshot["account_status"] == "checkpoint":
        return (
            f"🛑 Bài #{job_id} chưa được đăng vì nền tảng yêu cầu checkpoint.\n"
            f"• Nội dung: {title}\n"
            f"• Tài khoản: {account}\n"
            "Tài khoản đã tạm dừng. Hãy đăng nhập và xác minh thủ công."
        )
    if snapshot["account_status"] == "expired":
        return (
            f"🔐 Bài #{job_id} chưa được đăng vì phiên xác thực đã hết hạn.\n"
            f"• Nội dung: {title}\n"
            f"• Tài khoản: {account}\n"
            "Hãy kết nối lại tài khoản trước khi tiếp tục."
        )
    return (
        f"❌ Đăng bài #{job_id} thất bại.\n"
        f"• Nội dung: {title}\n"
        f"• Tài khoản: {account}\n"
        "Mở trang Jobs để xem trạng thái và xử lý an toàn."
    )


def _daily_snapshot(
    user_id: int,
    *,
    start_utc: datetime,
    end_utc: datetime,
) -> dict | None:
    with session_scope() as session:
        account_count = (
            session.scalar(
                select(func.count(SocialAccount.id)).where(
                    SocialAccount.user_id == user_id
                )
            )
            or 0
        )
        if not account_count:
            return None

        owned_jobs = (
            select(PublishJob.id)
            .join(SocialPost, PublishJob.social_post_id == SocialPost.id)
            .join(SocialAccount, PublishJob.social_account_id == SocialAccount.id)
            .where(
                SocialPost.user_id == user_id,
                SocialAccount.user_id == user_id,
            )
        )
        published = (
            session.scalar(
                select(func.count(PublishJob.id)).where(
                    PublishJob.id.in_(owned_jobs),
                    PublishJob.status == "published",
                    PublishJob.published_at >= start_utc,
                    PublishJob.published_at < end_utc,
                )
            )
            or 0
        )
        failed = (
            session.scalar(
                select(func.count(PublishJob.id)).where(
                    PublishJob.id.in_(owned_jobs),
                    PublishJob.status == "failed",
                    PublishJob.updated_at >= start_utc,
                    PublishJob.updated_at < end_utc,
                )
            )
            or 0
        )
        waiting = (
            session.scalar(
                select(func.count(PublishJob.id)).where(
                    PublishJob.id.in_(owned_jobs),
                    PublishJob.status.in_(("queued", "retry")),
                )
            )
            or 0
        )
        alerts = (
            session.scalar(
                select(func.count(SocialAccount.id)).where(
                    SocialAccount.user_id == user_id,
                    SocialAccount.status.in_(ACCOUNT_ALERT_STATUSES),
                )
            )
            or 0
        )
        return {
            "accounts": int(account_count),
            "published": int(published),
            "failed": int(failed),
            "waiting": int(waiting),
            "alerts": int(alerts),
        }


class SocialNotificationService:
    def __init__(
        self,
        sender: MessageSender,
        *,
        timezone_name: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        try:
            self.timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown social timezone: {timezone_name}") from exc
        self.sender = sender
        self.timezone_name = timezone_name
        self.now = now or (lambda: datetime.now(UTC))

    def notify_outcomes(self, outcomes: list[WorkerOutcome]) -> int:
        """Send terminal outcomes only; retries/deferred states stay silent."""
        delivered = 0
        for outcome in outcomes:
            if not outcome.processed or outcome.status not in TERMINAL_STATUSES:
                continue
            snapshot = _outcome_snapshot(outcome.job_id)
            if (
                snapshot is None
                or snapshot["status"] not in TERMINAL_STATUSES
                or snapshot["tg_id"] is None
            ):
                continue
            key = f"job:{snapshot['id']}:{snapshot['status']}"
            user_id = int(snapshot["user_id"])
            if _outcome_was_sent(user_id, key):
                continue
            try:
                self.sender.send(int(snapshot["tg_id"]), _outcome_text(snapshot))
            except Exception:
                logger.exception(
                    "Social outcome notification failed job=%s",
                    snapshot["id"],
                )
                continue
            _mark_outcome_sent(user_id, key)
            delivered += 1
        return delivered

    def send_daily_summaries(self, at: datetime | None = None) -> int:
        current = _as_utc(at or self.now()).astimezone(self.timezone)
        local_day = current.date().isoformat()
        start_local = datetime.combine(current.date(), time.min, self.timezone)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(UTC)
        end_utc = end_local.astimezone(UTC)

        with session_scope() as session:
            users = session.scalars(
                select(User).where(User.tg_id.is_not(None)).order_by(User.id)
            ).all()
            recipients = [
                (user.id, int(user.tg_id))
                for user in users
                if user.tg_id is not None
                and not _daily_was_sent(user, local_day, self.timezone_name)
            ]

        delivered = 0
        for user_id, tg_id in recipients:
            summary = _daily_snapshot(
                user_id,
                start_utc=start_utc,
                end_utc=end_utc,
            )
            if summary is None:
                continue
            text = (
                f"📊 Tổng kết social ngày {current:%d/%m/%Y}\n"
                f"• Đã đăng: {summary['published']}\n"
                f"• Thất bại: {summary['failed']}\n"
                f"• Đang chờ/retry: {summary['waiting']}\n"
                f"• Tài khoản cần xử lý: {summary['alerts']}"
            )
            try:
                self.sender.send(tg_id, text)
            except Exception:
                logger.exception("Daily social summary failed user=%s", user_id)
                continue
            _mark_daily_sent(user_id, local_day, self.timezone_name)
            delivered += 1
        return delivered


def build_social_notifier(
    token: str | None,
    *,
    timezone_name: str,
) -> SocialNotificationService | None:
    if not token:
        return None
    return SocialNotificationService(
        TelegramMessageSender(token),
        timezone_name=timezone_name,
    )
