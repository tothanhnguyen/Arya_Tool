"""Integration tests for deterministic social publishing."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from laplace.db import session_scope
from laplace.models import User
from laplace.social.models import MediaAsset, PublishJob, SocialAccount, SocialPost
from laplace.social.publishers import MockPublisher
from laplace.social.worker import PublishWorker


def _seed_job(session, *, scheduled_at=None, account_status="active") -> int:
    now = datetime.now(UTC)
    user = User(tg_id=4242)
    session.add(user)
    session.flush()
    account = SocialAccount(
        user_id=user.id,
        platform="facebook",
        display_name="Page A",
        external_id="page-a",
        auth_type="page_token",
        auth_ref="keychain://laplace-affiliate/page-a",
        status=account_status,
    )
    media = MediaAsset(
        user_id=user.id,
        type="video",
        local_path="/tmp/review.mp4",
        original_name="review.mp4",
        mime_type="video/mp4",
        size_bytes=42,
        sha256="a" * 64,
    )
    session.add_all([account, media])
    session.flush()
    post = SocialPost(
        user_id=user.id,
        title="Review",
        caption="Useful review",
        hashtags_json=["#review"],
        media_asset_id=media.id,
        status="approved",
        content_hash="b" * 64,
        approved_at=now,
    )
    session.add(post)
    session.flush()
    job = PublishJob(
        social_post_id=post.id,
        social_account_id=account.id,
        scheduled_at=scheduled_at or now - timedelta(seconds=1),
        idempotency_key="c" * 64,
    )
    session.add(job)
    session.flush()
    return job.id


def _reload_job(job_id: int) -> PublishJob:
    with session_scope() as session:
        job = session.get(PublishJob, job_id)
        assert job is not None
        _ = job.social_account.status
        _ = job.social_post.status
        _ = list(job.attempts)
        session.expunge(job)
        return job


def test_worker_publishes_approved_job_once(session):
    job_id = _seed_job(session)
    session.commit()
    publisher = MockPublisher()
    worker = PublishWorker(publisher)

    first = worker.process_job(job_id)
    repeated = worker.process_job(job_id)
    job = _reload_job(job_id)

    assert first.status == "published"
    assert first.remote_post_id
    assert repeated.status == "published"
    assert not repeated.processed
    assert publisher.side_effect_count == 1
    assert job.status == "published"
    assert job.attempt_count == 1
    assert job.social_post.status == "published"
    assert job.attempts[0].status == "published"
    assert "auth_ref" not in job.attempts[0].request_json


def test_retryable_error_preserves_key_and_then_succeeds(session):
    job_id = _seed_job(session)
    session.commit()
    publisher = MockPublisher(["network", "success"])
    worker = PublishWorker(publisher)
    now = datetime.now(UTC)

    first = worker.process_job(job_id, now=now)
    job_after_error = _reload_job(job_id)
    second = worker.process_job(job_id, now=job_after_error.next_retry_at)
    job = _reload_job(job_id)

    assert first.status == "retry"
    assert second.status == "published"
    assert publisher.idempotency_keys == ["c" * 64, "c" * 64]
    assert publisher.side_effect_count == 1
    assert job.attempt_count == 2
    assert [attempt.status for attempt in job.attempts] == [
        "retryable_error",
        "published",
    ]


def test_checkpoint_is_terminal_and_pauses_that_account(session):
    job_id = _seed_job(session)
    session.commit()
    worker = PublishWorker(MockPublisher(["checkpoint"]))

    outcome = worker.process_job(job_id)
    job = _reload_job(job_id)

    assert outcome.status == "failed"
    assert job.status == "failed"
    assert job.social_account.status == "checkpoint"
    assert job.attempt_count == 1
    assert job.attempts[0].error_type == "checkpoint"


def test_future_job_is_not_processed(session):
    job_id = _seed_job(session, scheduled_at=datetime.now(UTC) + timedelta(hours=1))
    session.commit()
    publisher = MockPublisher()
    worker = PublishWorker(publisher)

    outcome = worker.process_job(job_id)

    assert not outcome.processed
    assert outcome.error == "job is not due"
    assert publisher.call_count == 0


def test_inactive_account_fails_before_publisher_call(session):
    job_id = _seed_job(session, account_status="paused")
    session.commit()
    publisher = MockPublisher()
    worker = PublishWorker(publisher)

    outcome = worker.process_job(job_id)
    job = _reload_job(job_id)

    assert outcome.status == "failed"
    assert "[precondition]" in (job.last_error or "")
    assert publisher.call_count == 0
    assert publisher.account_checks == []


def test_process_due_skips_future_jobs(session):
    due_id = _seed_job(session)
    session.commit()
    with session_scope() as db:
        due = db.get(PublishJob, due_id)
        assert due is not None
        account = due.social_account
        post = due.social_post
        future = PublishJob(
            social_post_id=post.id,
            social_account_id=account.id,
            scheduled_at=datetime.now(UTC) + timedelta(hours=1),
            idempotency_key="d" * 64,
        )
        db.add(future)
        db.flush()
        future_id = future.id

    publisher = MockPublisher()
    outcomes = PublishWorker(publisher).process_due()

    assert [outcome.job_id for outcome in outcomes] == [due_id]
    assert _reload_job(due_id).status == "published"
    assert _reload_job(future_id).status == "queued"


def test_daily_limit_defers_without_calling_publisher(session):
    now = datetime(2026, 7, 27, 16, 30, tzinfo=UTC)
    prior_published_at = now - timedelta(hours=1)
    job_id = _seed_job(session, scheduled_at=now - timedelta(seconds=1))
    session.commit()
    with session_scope() as db:
        job = db.get(PublishJob, job_id)
        assert job is not None
        job.social_account.timezone = "Asia/Ho_Chi_Minh"
        job.social_account.daily_post_limit = 1
        prior = PublishJob(
            social_post_id=job.social_post_id,
            social_account_id=job.social_account_id,
            scheduled_at=prior_published_at,
            status="published",
            idempotency_key="e" * 64,
            published_at=prior_published_at,
        )
        db.add(prior)

    publisher = MockPublisher()
    outcome = PublishWorker(publisher).process_job(job_id, now=now)
    job = _reload_job(job_id)

    assert outcome.status == "retry"
    assert job.next_retry_at is not None
    assert job.next_retry_at.replace(tzinfo=UTC) == datetime(
        2026, 7, 27, 17, tzinfo=UTC
    )
    assert job.attempt_count == 0
    assert publisher.call_count == 0


def test_cooldown_defers_without_calling_publisher(session):
    job_id = _seed_job(session)
    session.commit()
    with session_scope() as db:
        job = db.get(PublishJob, job_id)
        assert job is not None
        job.social_account.daily_post_limit = 10
        job.social_account.cooldown_seconds = 3600
        prior = PublishJob(
            social_post_id=job.social_post_id,
            social_account_id=job.social_account_id,
            scheduled_at=datetime.now(UTC) - timedelta(minutes=10),
            status="published",
            idempotency_key="f" * 64,
            published_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        db.add(prior)

    publisher = MockPublisher()
    outcome = PublishWorker(publisher).process_job(job_id)
    job = _reload_job(job_id)

    assert outcome.status == "retry"
    assert job.next_retry_at is not None
    assert job.attempt_count == 0
    assert publisher.call_count == 0


def test_concurrent_calls_publish_one_side_effect(session):
    job_id = _seed_job(session)
    session.commit()
    publisher = MockPublisher()
    worker = PublishWorker(publisher)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(worker.process_job, (job_id, job_id)))

    assert publisher.side_effect_count == 1
    assert sum(item.processed for item in outcomes) == 1
    assert _reload_job(job_id).status == "published"


def test_stale_running_job_recovers_with_same_idempotency_key(session):
    job_id = _seed_job(session)
    session.commit()
    stale_time = datetime.now(UTC) - timedelta(hours=1)
    with session_scope() as db:
        job = db.get(PublishJob, job_id)
        assert job is not None
        job.status = "running"
        job.updated_at = stale_time
        key = job.idempotency_key

    publisher = MockPublisher()
    worker = PublishWorker(publisher)
    recovered = worker.recover_stale_jobs(
        stale_before=datetime.now(UTC) - timedelta(minutes=10),
        retry_at=datetime.now(UTC),
    )
    outcome = worker.process_job(job_id)

    assert recovered == 1
    assert outcome.status == "published"
    assert publisher.idempotency_keys == [key]
