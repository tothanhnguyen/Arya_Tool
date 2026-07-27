"""Tests for owner-only social outcome notifications and daily summaries."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from laplace.bot.social_notifications import SocialNotificationService
from laplace.db import session_scope
from laplace.models import User
from laplace.social.models import MediaAsset, PublishJob, SocialAccount, SocialPost
from laplace.social.worker import WorkerOutcome


class FakeSender:
    def __init__(self, *, fail_count: int = 0) -> None:
        self.fail_count = fail_count
        self.messages: list[tuple[int, str]] = []

    def send(self, chat_id: int, text: str) -> None:
        if self.fail_count:
            self.fail_count -= 1
            raise RuntimeError("telegram unavailable")
        self.messages.append((chat_id, text))


def _seed_job(
    session,
    *,
    suffix: str,
    tg_id: int,
    status: str,
    account_status: str = "active",
    at: datetime,
) -> tuple[int, int]:
    user = User(tg_id=tg_id)
    session.add(user)
    session.flush()
    account = SocialAccount(
        user_id=user.id,
        platform="facebook",
        display_name=f"Page {suffix}",
        external_id=f"page-{suffix}",
        auth_type="page_token",
        auth_ref=f"keychain://secret-{suffix}",
        status=account_status,
    )
    media = MediaAsset(
        user_id=user.id,
        type="image",
        local_path=f"/private/{suffix}.png",
        original_name=f"{suffix}.png",
        mime_type="image/png",
        size_bytes=10,
        sha256=suffix * 64,
    )
    session.add_all([account, media])
    session.flush()
    post = SocialPost(
        user_id=user.id,
        title=f"Post {suffix}",
        caption="Caption",
        hashtags_json=[],
        media_asset_id=media.id,
        status="published" if status == "published" else "failed",
        content_hash=suffix * 64,
    )
    session.add(post)
    session.flush()
    job = PublishJob(
        social_post_id=post.id,
        social_account_id=account.id,
        scheduled_at=at - timedelta(minutes=5),
        status=status,
        idempotency_key=(suffix * 63) + "1",
        published_at=at if status == "published" else None,
        updated_at=at,
        last_error="[checkpoint] token=must-not-leak" if status == "failed" else None,
    )
    session.add(job)
    session.flush()
    return user.id, job.id


def test_terminal_outcome_is_owner_only_safe_and_deduplicated(session):
    at = datetime(2026, 7, 28, 13, 0, tzinfo=UTC)
    user_id, job_id = _seed_job(
        session,
        suffix="a",
        tg_id=111,
        status="published",
        at=at,
    )
    session.commit()
    sender = FakeSender()
    service = SocialNotificationService(
        sender,
        timezone_name="Asia/Ho_Chi_Minh",
        now=lambda: at,
    )
    outcome = WorkerOutcome(job_id, "published", True, remote_post_id="remote-1")

    assert service.notify_outcomes([outcome]) == 1
    assert service.notify_outcomes([outcome]) == 0
    assert service.notify_outcomes([WorkerOutcome(job_id, "published", False)]) == 0
    assert service.notify_outcomes([WorkerOutcome(job_id, "retry", True)]) == 0

    assert len(sender.messages) == 1
    chat_id, text = sender.messages[0]
    assert chat_id == 111
    assert "Đã đăng bài" in text
    assert "Post a" in text
    assert "keychain://" not in text
    assert "remote-1" not in text
    with session_scope() as db:
        profile = db.get(User, user_id).profile_json
    assert f"job:{job_id}:published" in profile["social_notification_keys"]


def test_checkpoint_notification_redacts_raw_error(session):
    at = datetime(2026, 7, 28, 13, 0, tzinfo=UTC)
    _, job_id = _seed_job(
        session,
        suffix="b",
        tg_id=222,
        status="failed",
        account_status="checkpoint",
        at=at,
    )
    session.commit()
    sender = FakeSender()
    service = SocialNotificationService(
        sender,
        timezone_name="Asia/Ho_Chi_Minh",
    )

    delivered = service.notify_outcomes(
        [WorkerOutcome(job_id, "failed", True, error="[checkpoint] token=must-not-leak")]
    )

    assert delivered == 1
    assert sender.messages[0][0] == 222
    assert "checkpoint" in sender.messages[0][1]
    assert "must-not-leak" not in sender.messages[0][1]
    assert "keychain://" not in sender.messages[0][1]


def test_daily_summary_is_scoped_by_owner_timezone_and_date(session):
    at = datetime(2026, 7, 28, 13, 0, tzinfo=UTC)
    _seed_job(
        session,
        suffix="c",
        tg_id=333,
        status="published",
        at=at - timedelta(hours=1),
    )
    _seed_job(
        session,
        suffix="d",
        tg_id=444,
        status="failed",
        account_status="expired",
        at=at - timedelta(hours=2),
    )
    session.commit()
    sender = FakeSender()
    service = SocialNotificationService(
        sender,
        timezone_name="Asia/Ho_Chi_Minh",
        now=lambda: at,
    )

    assert service.send_daily_summaries() == 2
    assert service.send_daily_summaries() == 0

    by_chat = dict(sender.messages)
    assert "Đã đăng: 1" in by_chat[333]
    assert "Thất bại: 0" in by_chat[333]
    assert "Đã đăng: 0" in by_chat[444]
    assert "Thất bại: 1" in by_chat[444]
    assert "Tài khoản cần xử lý: 1" in by_chat[444]
    assert "28/07/2026" in by_chat[333]

    next_day = at + timedelta(days=1)
    assert service.send_daily_summaries(next_day) == 2
    assert len(sender.messages) == 4


def test_failed_delivery_is_not_marked_and_can_retry(session):
    at = datetime(2026, 7, 28, 13, 0, tzinfo=UTC)
    _, job_id = _seed_job(
        session,
        suffix="e",
        tg_id=555,
        status="published",
        at=at,
    )
    session.commit()
    sender = FakeSender(fail_count=1)
    service = SocialNotificationService(
        sender,
        timezone_name="Asia/Ho_Chi_Minh",
    )
    outcome = WorkerOutcome(job_id, "published", True)

    assert service.notify_outcomes([outcome]) == 0
    assert service.notify_outcomes([outcome]) == 1
    assert len(sender.messages) == 1


def test_pending_terminal_delivery_retries_after_service_restart_without_outcome(session):
    at = datetime(2026, 7, 28, 13, 0, tzinfo=UTC)
    user_id, job_id = _seed_job(
        session,
        suffix="f",
        tg_id=666,
        status="published",
        at=at,
    )
    session.commit()
    sender = FakeSender(fail_count=1)
    first_service = SocialNotificationService(
        sender,
        timezone_name="Asia/Ho_Chi_Minh",
        now=lambda: at,
    )
    assert first_service.initialize_delivery_tracking(at - timedelta(minutes=1)) == 1

    assert first_service.notify_pending_outcomes(at) == 0

    restarted_service = SocialNotificationService(
        sender,
        timezone_name="Asia/Ho_Chi_Minh",
        now=lambda: at + timedelta(minutes=1),
    )
    assert restarted_service.notify_pending_outcomes() == 1
    assert restarted_service.notify_pending_outcomes() == 0
    assert len(sender.messages) == 1
    with session_scope() as db:
        profile = db.get(User, user_id).profile_json
    assert f"job:{job_id}:published" in profile["social_notification_keys"]


def test_tracking_initialization_does_not_replay_historical_terminal_jobs(session):
    at = datetime(2026, 7, 28, 13, 0, tzinfo=UTC)
    _seed_job(
        session,
        suffix="g",
        tg_id=777,
        status="published",
        at=at - timedelta(days=1),
    )
    session.commit()
    sender = FakeSender()
    service = SocialNotificationService(
        sender,
        timezone_name="Asia/Ho_Chi_Minh",
        now=lambda: at,
    )

    assert service.initialize_delivery_tracking() == 1
    assert service.notify_pending_outcomes() == 0
    assert sender.messages == []


def test_failed_current_outcome_moves_tracking_back_for_durable_retry(session):
    at = datetime(2026, 7, 28, 13, 0, tzinfo=UTC)
    _, job_id = _seed_job(
        session,
        suffix="j",
        tg_id=1010,
        status="published",
        at=at,
    )
    session.commit()
    sender = FakeSender(fail_count=1)
    service = SocialNotificationService(
        sender,
        timezone_name="Asia/Ho_Chi_Minh",
    )
    assert service.initialize_delivery_tracking(at + timedelta(minutes=1)) == 1

    assert service.notify_outcomes([WorkerOutcome(job_id, "published", True)]) == 0

    restarted_service = SocialNotificationService(
        sender,
        timezone_name="Asia/Ho_Chi_Minh",
    )
    assert restarted_service.notify_pending_outcomes(at + timedelta(minutes=2)) == 1
    assert len(sender.messages) == 1


def test_daily_summary_retries_and_catches_up_after_configured_hour(session):
    at = datetime(2026, 7, 28, 13, 0, tzinfo=UTC)
    _seed_job(
        session,
        suffix="h",
        tg_id=888,
        status="published",
        at=at - timedelta(hours=1),
    )
    session.commit()
    sender = FakeSender(fail_count=1)
    service = SocialNotificationService(
        sender,
        timezone_name="Asia/Ho_Chi_Minh",
    )

    assert service.send_daily_summaries_if_due(hour=20, at=at - timedelta(minutes=1)) == 0
    assert service.send_daily_summaries_if_due(hour=20, at=at) == 0

    restarted_service = SocialNotificationService(
        sender,
        timezone_name="Asia/Ho_Chi_Minh",
    )
    assert (
        restarted_service.send_daily_summaries_if_due(
            hour=20,
            at=at + timedelta(hours=1),
        )
        == 1
    )
    assert (
        restarted_service.send_daily_summaries_if_due(
            hour=20,
            at=at + timedelta(hours=2),
        )
        == 0
    )
    assert len(sender.messages) == 1


def test_concurrent_terminal_delivery_is_serialized_in_process(session):
    at = datetime(2026, 7, 28, 13, 0, tzinfo=UTC)
    _, job_id = _seed_job(
        session,
        suffix="i",
        tg_id=999,
        status="published",
        at=at,
    )
    session.commit()
    sender = FakeSender()
    service = SocialNotificationService(
        sender,
        timezone_name="Asia/Ho_Chi_Minh",
    )
    outcome = WorkerOutcome(job_id, "published", True)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: service.notify_outcomes([outcome]), range(2)))

    assert sum(results) == 1
    assert len(sender.messages) == 1
