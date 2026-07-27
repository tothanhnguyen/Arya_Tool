"""Best-effort Telegram notifications for deterministic social publishing."""

from __future__ import annotations

import asyncio
import logging
import threading
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
PROFILE_TRACKING_STARTED_AT = "social_notification_tracking_started_at"
TERMINAL_STATUSES = frozenset({"published", "failed"})
ACCOUNT_ALERT_STATUSES = frozenset({"checkpoint", "expired"})
_DELIVERY_LOCK = threading.RLock()


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


def _tracking_started_at(profile: dict) -> datetime | None:
    raw = profile.get(PROFILE_TRACKING_STARTED_AT)
    if not isinstance(raw, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(raw))
    except ValueError:
        return None


def _daily_was_sent(user: User, local_day: str, timezone_name: str) -> bool:
    value = _profile(user).get(PROFILE_DAILY_SUMMARY)
    return (
        isinstance(value, dict)
        and value.get("date") == local_day
        and value.get("timezone") == timezone_name
    )


def _outcome_query():
    return (
        select(
            PublishJob.id,
            PublishJob.status,
            PublishJob.updated_at,
            SocialPost.title,
            SocialPost.user_id,
            SocialAccount.display_name,
            SocialAccount.status.label("account_status"),
            User.tg_id,
            User.profile_json,
        )
        .join(SocialPost, PublishJob.social_post_id == SocialPost.id)
        .join(SocialAccount, PublishJob.social_account_id == SocialAccount.id)
        .join(User, SocialPost.user_id == User.id)
        .where(SocialAccount.user_id == SocialPost.user_id)
    )


def _outcome_snapshot(job_id: int) -> dict | None:
    with session_scope() as session:
        row = session.execute(
            _outcome_query().where(PublishJob.id == job_id)
        ).mappings().one_or_none()
        return dict(row) if row is not None else None


def _terminal_snapshots() -> list[dict]:
    with session_scope() as session:
        rows = session.execute(
            _outcome_query()
            .where(
                PublishJob.status.in_(TERMINAL_STATUSES),
                User.tg_id.is_not(None),
            )
            .order_by(PublishJob.updated_at, PublishJob.id)
        ).mappings()
        return [dict(row) for row in rows]


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

    def initialize_delivery_tracking(self, at: datetime | None = None) -> int:
        """Start durable scans without replaying terminal jobs from before rollout."""
        started_at = _as_utc(at or self.now()).isoformat()
        initialized = 0
        with _DELIVERY_LOCK, session_scope() as session:
            users = session.scalars(
                select(User)
                .where(User.tg_id.is_not(None))
                .order_by(User.id)
                .with_for_update()
            ).all()
            for user in users:
                profile = _profile(user)
                if _tracking_started_at(profile) is not None:
                    continue
                profile[PROFILE_TRACKING_STARTED_AT] = started_at
                user.profile_json = profile
                initialized += 1
        return initialized

    def _ensure_tracking_started(self, user_id: int, at: datetime) -> None:
        requested_start = _as_utc(at)
        with _DELIVERY_LOCK, session_scope() as session:
            user = session.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            if user is None:
                return
            profile = _profile(user)
            existing_start = _tracking_started_at(profile)
            if existing_start is None or requested_start < existing_start:
                profile[PROFILE_TRACKING_STARTED_AT] = requested_start.isoformat()
                user.profile_json = profile

    def _deliver_outcome(self, snapshot: dict) -> bool:
        key = f"job:{snapshot['id']}:{snapshot['status']}"
        with _DELIVERY_LOCK, session_scope() as session:
            user = session.scalar(
                select(User).where(User.id == int(snapshot["user_id"])).with_for_update()
            )
            if user is None or user.tg_id is None:
                return False
            profile = _profile(user)
            raw_keys = profile.get(PROFILE_OUTCOME_KEYS, [])
            keys = (
                [item for item in raw_keys if isinstance(item, str)]
                if isinstance(raw_keys, list)
                else []
            )
            if key in keys:
                return False
            self.sender.send(int(user.tg_id), _outcome_text(snapshot))
            keys.append(key)
            profile[PROFILE_OUTCOME_KEYS] = keys
            user.profile_json = profile
        return True

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
            user_id = int(snapshot["user_id"])
            self._ensure_tracking_started(user_id, _as_utc(snapshot["updated_at"]))
            try:
                was_delivered = self._deliver_outcome(snapshot)
            except Exception:
                logger.exception(
                    "Social outcome notification failed job=%s",
                    snapshot["id"],
                )
                continue
            delivered += int(was_delivered)
        return delivered

    def notify_pending_outcomes(
        self,
        at: datetime | None = None,
        *,
        limit: int = 100,
    ) -> int:
        """Retry unsent terminal jobs from the database, independent of worker outcomes."""
        if limit < 1:
            return 0
        current = _as_utc(at or self.now())
        delivered = 0
        users_without_tracking: set[int] = set()
        for snapshot in _terminal_snapshots():
            user_id = int(snapshot["user_id"])
            profile = (
                dict(snapshot["profile_json"])
                if isinstance(snapshot["profile_json"], dict)
                else {}
            )
            tracking_started = _tracking_started_at(profile)
            if tracking_started is None:
                users_without_tracking.add(user_id)
                continue
            if _as_utc(snapshot["updated_at"]) < tracking_started:
                continue
            try:
                was_delivered = self._deliver_outcome(snapshot)
            except Exception:
                logger.exception(
                    "Pending social notification failed job=%s",
                    snapshot["id"],
                )
                continue
            delivered += int(was_delivered)
            if delivered >= limit:
                break
        for user_id in users_without_tracking:
            self._ensure_tracking_started(user_id, current)
        return delivered

    def _deliver_daily(
        self,
        user_id: int,
        *,
        local_day: str,
        text: str,
    ) -> bool:
        with _DELIVERY_LOCK, session_scope() as session:
            user = session.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            if (
                user is None
                or user.tg_id is None
                or _daily_was_sent(user, local_day, self.timezone_name)
            ):
                return False
            self.sender.send(int(user.tg_id), text)
            profile = _profile(user)
            profile[PROFILE_DAILY_SUMMARY] = {
                "date": local_day,
                "timezone": self.timezone_name,
            }
            user.profile_json = profile
        return True

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
                user.id
                for user in users
                if user.tg_id is not None
                and not _daily_was_sent(user, local_day, self.timezone_name)
            ]

        delivered = 0
        for user_id in recipients:
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
                was_delivered = self._deliver_daily(
                    user_id,
                    local_day=local_day,
                    text=text,
                )
            except Exception:
                logger.exception("Daily social summary failed user=%s", user_id)
                continue
            delivered += int(was_delivered)
        return delivered

    def send_daily_summaries_if_due(
        self,
        *,
        hour: int,
        at: datetime | None = None,
    ) -> int:
        if not 0 <= hour <= 23:
            raise ValueError("daily summary hour must be between 0 and 23")
        current = _as_utc(at or self.now())
        if current.astimezone(self.timezone).hour < hour:
            return 0
        return self.send_daily_summaries(current)


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
