"""Integration tests for deterministic social publishing."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from laplace.db import session_scope
from laplace.models import User
from laplace.social.models import (
    MediaAsset,
    PublishAttempt,
    PublishJob,
    SocialAccount,
    SocialPost,
)
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


def _seed_stale_attempt(
    job_id: int,
    *,
    attempt_no: int = 1,
    side_effect_started: bool = False,
    prior_attempt: bool = False,
) -> tuple[int, datetime]:
    stale_time = datetime.now(UTC) - timedelta(hours=1)
    with session_scope() as db:
        job = db.get(PublishJob, job_id)
        assert job is not None
        if prior_attempt:
            db.add(
                PublishAttempt(
                    publish_job_id=job.id,
                    attempt_no=attempt_no - 1,
                    status="retryable_error",
                    error_type="network",
                    error_message="temporary network error",
                    started_at=stale_time - timedelta(minutes=5),
                    finished_at=stale_time - timedelta(minutes=4),
                )
            )
        attempt = PublishAttempt(
            publish_job_id=job.id,
            attempt_no=attempt_no,
            status="running",
            response_json={"side_effect_started": side_effect_started},
            error_type="old_error",
            error_message="sensitive stale detail",
            started_at=stale_time,
        )
        db.add(attempt)
        job.status = "running"
        job.attempt_count = max(1, attempt_no - 1)
        job.last_error = "sensitive stale detail"
        job.updated_at = stale_time
        db.flush()
        return attempt.id, stale_time


class SimulatedProcessCrash(RuntimeError):
    pass


class CrashAfterSideEffectPublisher:
    name = "mock"

    def __init__(self) -> None:
        self.mock = MockPublisher()

    def check_account(self, account_id):
        return self.mock.check_account(account_id)

    def publish(self, request):
        result = self.mock.publish(request)
        assert result.success
        raise SimulatedProcessCrash


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


def test_queued_job_survives_restart_with_new_mock_publisher(session):
    job_id = _seed_job(session)
    session.commit()
    old_publisher = MockPublisher()
    PublishWorker(old_publisher)
    new_publisher = MockPublisher()

    outcome = PublishWorker(new_publisher).process_due()
    job = _reload_job(job_id)

    assert [item.job_id for item in outcome] == [job_id]
    assert old_publisher.call_count == 0
    assert new_publisher.side_effect_count == 1
    assert job.status == "published"
    assert job.attempt_count == 1
    assert [attempt.attempt_no for attempt in job.attempts] == [1]


def test_retry_job_survives_restart_with_new_mock_publisher(session):
    job_id = _seed_job(session)
    session.commit()
    first_publisher = MockPublisher(["network"])
    first = PublishWorker(first_publisher).process_job(job_id)
    retry_job = _reload_job(job_id)
    second_publisher = MockPublisher()

    second = PublishWorker(second_publisher).process_job(
        job_id,
        now=retry_job.next_retry_at,
    )
    job = _reload_job(job_id)

    assert first.status == "retry"
    assert second.status == "published"
    assert first_publisher.side_effect_count == 0
    assert second_publisher.side_effect_count == 1
    assert [attempt.attempt_no for attempt in job.attempts] == [1, 2]
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


def test_stale_running_attempt_is_closed_before_safe_retry(session):
    job_id = _seed_job(session)
    session.commit()
    attempt_id, _stale_time = _seed_stale_attempt(job_id)
    retry_at = datetime.now(UTC)
    publisher = MockPublisher()
    worker = PublishWorker(publisher)

    recovered = worker.recover_stale_jobs(
        stale_before=retry_at - timedelta(minutes=10),
        retry_at=retry_at,
    )
    recovered_job = _reload_job(job_id)
    old_attempt = next(
        attempt for attempt in recovered_job.attempts if attempt.id == attempt_id
    )
    outcome = worker.process_job(job_id, now=retry_at)
    job = _reload_job(job_id)

    assert recovered == 1
    assert recovered_job.status == "retry"
    assert old_attempt.status == "retryable_error"
    assert old_attempt.finished_at is not None
    assert old_attempt.error_type == "worker_interrupted"
    assert old_attempt.error_message == "worker interrupted before publish"
    assert old_attempt.response_json == {
        "success": False,
        "error_kind": "worker_interrupted",
        "reconciliation_required": False,
        "side_effect_started": False,
    }
    assert recovered_job.last_error == (
        "[recovery] worker interrupted before publish"
    )
    assert "sensitive" not in (old_attempt.error_message or "")
    assert "sensitive" not in (recovered_job.last_error or "")
    assert outcome.status == "published"
    assert publisher.side_effect_count == 1
    assert [attempt.attempt_no for attempt in job.attempts] == [1, 2]
    assert [attempt.status for attempt in job.attempts] == [
        "retryable_error",
        "published",
    ]


def test_recovery_rolls_back_job_and_attempt_together(session, monkeypatch):
    from laplace.social import worker as worker_module

    job_id = _seed_job(session)
    session.commit()
    _seed_stale_attempt(job_id)
    retry_at = datetime.now(UTC)
    original_scope = worker_module.session_scope

    @contextmanager
    def interrupted_transaction():
        with original_scope() as db:
            yield db
            raise SimulatedProcessCrash

    monkeypatch.setattr(worker_module, "session_scope", interrupted_transaction)
    with pytest.raises(SimulatedProcessCrash):
        PublishWorker(MockPublisher()).recover_stale_jobs(
            stale_before=retry_at - timedelta(minutes=10),
            retry_at=retry_at,
        )
    job = _reload_job(job_id)

    assert job.status == "running"
    assert job.last_error == "sensitive stale detail"
    assert job.attempts[0].status == "running"
    assert job.attempts[0].finished_at is None
    assert job.attempts[0].error_message == "sensitive stale detail"


def test_attempt_number_advances_from_persisted_max_after_recovery(session):
    job_id = _seed_job(session)
    session.commit()
    _seed_stale_attempt(
        job_id,
        attempt_no=2,
        prior_attempt=True,
    )
    retry_at = datetime.now(UTC)
    worker = PublishWorker(MockPublisher(), max_attempts=5)

    recovered = worker.recover_stale_jobs(
        stale_before=retry_at - timedelta(minutes=10),
        retry_at=retry_at,
    )
    outcome = worker.process_job(job_id, now=retry_at)
    job = _reload_job(job_id)

    assert recovered == 1
    assert outcome.status == "published"
    assert job.attempt_count == 3
    assert [attempt.attempt_no for attempt in job.attempts] == [1, 2, 3]
    assert len({attempt.attempt_no for attempt in job.attempts}) == 3


def test_crash_after_mock_side_effect_persists_unknown_outcome_fence(session):
    job_id = _seed_job(session)
    session.commit()
    publisher = CrashAfterSideEffectPublisher()
    crash_time = datetime.now(UTC)

    with pytest.raises(SimulatedProcessCrash):
        PublishWorker(publisher).process_job(job_id, now=crash_time)
    job = _reload_job(job_id)

    assert publisher.mock.side_effect_count == 1
    assert job.status == "running"
    assert job.attempt_count == 1
    assert len(job.attempts) == 1
    assert job.attempts[0].status == "running"
    assert job.attempts[0].response_json == {"side_effect_started": True}


def test_new_mock_publisher_fails_closed_for_unknown_old_side_effect(session):
    job_id = _seed_job(session)
    session.commit()
    crashed_publisher = CrashAfterSideEffectPublisher()
    crash_time = datetime.now(UTC)
    with pytest.raises(SimulatedProcessCrash):
        PublishWorker(crashed_publisher).process_job(job_id, now=crash_time)

    restarted_publisher = MockPublisher()
    restarted_worker = PublishWorker(restarted_publisher)
    recovered = restarted_worker.recover_stale_jobs(
        stale_before=crash_time + timedelta(seconds=1),
        retry_at=crash_time + timedelta(minutes=1),
    )
    outcome = restarted_worker.process_job(
        job_id,
        now=crash_time + timedelta(minutes=1),
    )
    job = _reload_job(job_id)

    assert recovered == 1
    assert crashed_publisher.mock.side_effect_count == 1
    assert restarted_publisher.call_count == 0
    assert restarted_publisher.side_effect_count == 0
    assert not outcome.processed
    assert outcome.status == "failed"
    assert job.status == "failed"
    assert job.next_retry_at is None
    assert job.last_error == (
        "[recovery_required] publish outcome unknown; manual reconciliation required"
    )
    assert job.attempts[0].status == "unknown"
    assert job.attempts[0].finished_at is not None
    assert job.attempts[0].error_type == "publish_outcome_unknown"
    assert job.attempts[0].error_message == (
        "publish outcome unknown; manual reconciliation required"
    )
    assert job.attempts[0].response_json == {
        "success": False,
        "error_kind": "publish_outcome_unknown",
        "reconciliation_required": True,
        "side_effect_started": True,
    }
