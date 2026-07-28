"""Deterministic publish worker.

The agent may create or approve jobs, but it never performs the side effect
itself. This worker claims one persisted job, resolves a credential-free
``PublishRequest`` and calls a publisher adapter outside the database
transaction. Stable idempotency keys and per-account locks prevent duplicate
local execution. A persisted intent fence prevents blind replay when a process
dies after invoking the publisher but before recording its result.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select, update

from laplace.db import session_scope
from laplace.social.models import MediaAsset, PublishAttempt, PublishJob
from laplace.social.publishers import (
    PublishErrorKind,
    PublishRequest,
    PublishResult,
    SocialPublisher,
)
from laplace.social.storage import StorageError, materialize_media

_SIDE_EFFECT_STARTED = "side_effect_started"
_RECOVERY_SAFE_MESSAGE = "worker interrupted before publish"
_RECOVERY_UNKNOWN_MESSAGE = (
    "publish outcome unknown; manual reconciliation required"
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """SQLite may return timezone columns as naive; interpret them as UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    job_id: int
    status: str
    processed: bool
    remote_post_id: str | None = None
    error: str | None = None


class PublishPreconditionError(ValueError):
    """The stored job is not safe to execute."""


class PublishDeferredError(RuntimeError):
    """A valid job must wait for an account safety limit."""

    def __init__(self, message: str, retry_at: datetime) -> None:
        super().__init__(message)
        self.retry_at = retry_at


class PublishWorker:
    """Process persisted publish jobs using one deterministic adapter."""

    def __init__(self, publisher: SocialPublisher, *, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.publisher = publisher
        self.max_attempts = max_attempts
        self._locks_guard = threading.Lock()
        self._account_locks: dict[int, threading.Lock] = {}

    def process_due(self, *, now: datetime | None = None, limit: int = 20) -> list[WorkerOutcome]:
        """Process due queued/retry jobs in deterministic order."""
        if limit < 1:
            return []
        current = _as_utc(now or utcnow())
        with session_scope() as session:
            job_ids = list(
                session.scalars(
                    select(PublishJob.id)
                    .where(
                        PublishJob.status.in_(("queued", "retry")),
                        PublishJob.scheduled_at <= current,
                        or_(
                            PublishJob.next_retry_at.is_(None),
                            PublishJob.next_retry_at <= current,
                        ),
                    )
                    .order_by(PublishJob.scheduled_at, PublishJob.id)
                    .limit(limit)
                )
            )
        return [self.process_job(job_id, now=current) for job_id in job_ids]

    def process_job(self, job_id: int, *, now: datetime | None = None) -> WorkerOutcome:
        """Claim and execute one job at most once in this process."""
        current = _as_utc(now or utcnow())
        account_id = self._job_account_id(job_id)
        if account_id is None:
            return WorkerOutcome(job_id, "missing", False, error="publish job not found")

        lock = self._account_lock(account_id)
        with lock:
            claimed, existing = self._claim_job(job_id, current)
            if not claimed:
                return existing

            try:
                attempt_id, request, media_asset = self._prepare_attempt(job_id, current)
            except PublishDeferredError as exc:
                return self._defer_job(job_id, str(exc), exc.retry_at, current)
            except PublishPreconditionError as exc:
                return self._fail_precondition(job_id, str(exc), current)

            started = time.monotonic()
            account_check = self.publisher.check_account(request.account_id)
            if account_check.available:
                try:
                    with materialize_media(media_asset) as media_path:
                        self._mark_side_effect_started(
                            job_id,
                            attempt_id,
                        )
                        result = self.publisher.publish(
                            replace(request, media_paths=(str(media_path),))
                        )
                except StorageError as exc:
                    result = PublishResult(
                        success=False,
                        idempotency_key=request.idempotency_key,
                        error_kind=PublishErrorKind.NETWORK,
                        message=str(exc),
                    )
            else:
                result = PublishResult(
                    success=False,
                    idempotency_key=request.idempotency_key,
                    error_kind=account_check.error_kind or PublishErrorKind.UNKNOWN,
                    message=account_check.message or "account is unavailable",
                )
            latency_ms = int((time.monotonic() - started) * 1000)
            return self._finish_attempt(job_id, attempt_id, result, latency_ms, current)

    def recover_stale_jobs(
        self, *, stale_before: datetime, retry_at: datetime | None = None
    ) -> int:
        """Close stale attempts and retry only work known to predate publishing."""
        cutoff = _as_utc(stale_before)
        retry_time = _as_utc(retry_at or utcnow())
        with session_scope() as session:
            jobs = list(
                session.scalars(
                    select(PublishJob)
                    .where(
                        PublishJob.status == "running",
                        PublishJob.remote_post_id.is_(None),
                        PublishJob.updated_at < cutoff,
                    )
                    .order_by(PublishJob.id)
                    .with_for_update()
                )
            )
            for job in jobs:
                attempts = list(
                    session.scalars(
                        select(PublishAttempt)
                        .where(
                            PublishAttempt.publish_job_id == job.id,
                            PublishAttempt.status == "running",
                        )
                        .order_by(PublishAttempt.attempt_no)
                        .with_for_update()
                    )
                )
                persisted_max = max(
                    (attempt.attempt_no for attempt in attempts),
                    default=job.attempt_count,
                )
                job.attempt_count = max(job.attempt_count, persisted_max)
                outcome_unknown = any(
                    bool(
                        dict(attempt.response_json or {}).get(
                            _SIDE_EFFECT_STARTED
                        )
                    )
                    for attempt in attempts
                )
                attempt_status = (
                    "unknown" if outcome_unknown else "retryable_error"
                )
                error_type = (
                    "publish_outcome_unknown"
                    if outcome_unknown
                    else "worker_interrupted"
                )
                error_message = (
                    _RECOVERY_UNKNOWN_MESSAGE
                    if outcome_unknown
                    else _RECOVERY_SAFE_MESSAGE
                )
                for attempt in attempts:
                    attempt.status = attempt_status
                    attempt.finished_at = retry_time
                    attempt.error_type = error_type
                    attempt.error_message = error_message
                    attempt.response_json = {
                        "success": False,
                        "error_kind": error_type,
                        "reconciliation_required": outcome_unknown,
                        _SIDE_EFFECT_STARTED: outcome_unknown,
                    }

                if outcome_unknown:
                    job.status = "failed"
                    job.next_retry_at = None
                    job.last_error = (
                        f"[recovery_required] {_RECOVERY_UNKNOWN_MESSAGE}"
                    )
                else:
                    job.status = "retry"
                    job.next_retry_at = retry_time
                    job.last_error = f"[recovery] {_RECOVERY_SAFE_MESSAGE}"
                job.updated_at = retry_time
            return len(jobs)

    def _mark_side_effect_started(
        self,
        job_id: int,
        attempt_id: int,
    ) -> None:
        """Persist an intent fence before invoking the publisher."""
        with session_scope() as session:
            job = session.scalar(
                select(PublishJob)
                .where(
                    PublishJob.id == job_id,
                    PublishJob.status == "running",
                )
                .with_for_update()
            )
            attempt = session.scalar(
                select(PublishAttempt)
                .where(
                    PublishAttempt.id == attempt_id,
                    PublishAttempt.publish_job_id == job_id,
                    PublishAttempt.status == "running",
                )
                .with_for_update()
            )
            if job is None or attempt is None:
                raise RuntimeError("publish attempt is not running")
            response = dict(attempt.response_json or {})
            response[_SIDE_EFFECT_STARTED] = True
            attempt.response_json = response
            job.updated_at = utcnow()

    def _job_account_id(self, job_id: int) -> int | None:
        with session_scope() as session:
            return session.scalar(
                select(PublishJob.social_account_id).where(PublishJob.id == job_id)
            )

    def _account_lock(self, account_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._account_locks.setdefault(account_id, threading.Lock())

    def _claim_job(
        self, job_id: int, current: datetime
    ) -> tuple[bool, WorkerOutcome]:
        with session_scope() as session:
            job = session.get(PublishJob, job_id)
            if job is None:
                return False, WorkerOutcome(job_id, "missing", False, error="publish job not found")
            if job.status == "published":
                return False, WorkerOutcome(
                    job_id, job.status, False, remote_post_id=job.remote_post_id
                )
            if job.status not in {"queued", "retry"}:
                return False, WorkerOutcome(job_id, job.status, False, error=job.last_error)
            if _as_utc(job.scheduled_at) > current:
                return False, WorkerOutcome(job_id, job.status, False, error="job is not due")
            if job.next_retry_at is not None and _as_utc(job.next_retry_at) > current:
                return False, WorkerOutcome(
                    job_id, job.status, False, error="retry window has not arrived"
                )

            claimed = session.execute(
                update(PublishJob)
                .where(
                    PublishJob.id == job_id,
                    PublishJob.status.in_(("queued", "retry")),
                )
                .values(status="running", updated_at=current)
            ).rowcount
            if not claimed:
                return False, WorkerOutcome(job_id, "running", False, error="job already claimed")
            return True, WorkerOutcome(job_id, "running", True)

    def _prepare_attempt(
        self, job_id: int, current: datetime
    ) -> tuple[int, PublishRequest, MediaAsset]:
        with session_scope() as session:
            job = session.get(PublishJob, job_id)
            if job is None or job.status != "running":
                raise PublishPreconditionError("job is not in running state")

            account = job.social_account
            post = job.social_post
            if account.status != "active":
                raise PublishPreconditionError(f"account is {account.status}")
            if post.status not in {"approved", "scheduled", "published"}:
                raise PublishPreconditionError(f"post is {post.status}, not approved")
            if job.attempt_count >= self.max_attempts:
                raise PublishPreconditionError("maximum publish attempts reached")

            retry_at = self._account_limit_retry_at(session, job, current)
            if retry_at is not None:
                raise PublishDeferredError(
                    "account daily limit or cooldown is active",
                    retry_at,
                )

            persisted_max = session.scalar(
                select(func.max(PublishAttempt.attempt_no)).where(
                    PublishAttempt.publish_job_id == job.id
                )
            ) or 0
            attempt_no = max(job.attempt_count, persisted_max) + 1
            if attempt_no > self.max_attempts:
                raise PublishPreconditionError(
                    "maximum publish attempts reached"
                )

            job.attempt_count = attempt_no
            job.next_retry_at = None
            attempt = PublishAttempt(
                publish_job_id=job.id,
                attempt_no=attempt_no,
                status="running",
                request_json={
                    "account_id": account.external_id,
                    "post_id": str(post.id),
                    "media_count": 1,
                    "has_affiliate_url": post.affiliate_product is not None,
                    "idempotency_key": job.idempotency_key,
                },
                started_at=current,
            )
            session.add(attempt)
            session.flush()

            request = PublishRequest(
                account_id=account.external_id,
                post_id=str(post.id),
                idempotency_key=job.idempotency_key,
                caption=(
                    f"{post.caption}\n\n{' '.join(post.hashtags_json)}"
                    if post.hashtags_json
                    else post.caption
                ),
                media_paths=(post.media_asset.local_path,),
                affiliate_url=(
                    post.affiliate_product.affiliate_url
                    if post.affiliate_product is not None
                    else None
                ),
                metadata={"platform": account.platform},
            )
            return attempt.id, request, post.media_asset

    def _account_limit_retry_at(
        self, session, job: PublishJob, current: datetime
    ) -> datetime | None:
        account = job.social_account
        try:
            timezone = ZoneInfo(account.timezone)
        except ZoneInfoNotFoundError as exc:
            raise PublishPreconditionError(
                f"account timezone is invalid: {account.timezone}"
            ) from exc

        local_now = current.astimezone(timezone)
        next_local_day = (local_now + timedelta(days=1)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        local_day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        published_today = session.scalar(
            select(func.count(PublishJob.id)).where(
                PublishJob.social_account_id == account.id,
                PublishJob.status == "published",
                PublishJob.published_at >= local_day_start.astimezone(UTC),
                PublishJob.published_at < next_local_day.astimezone(UTC),
            )
        ) or 0

        retry_candidates: list[datetime] = []
        if published_today >= account.daily_post_limit:
            retry_candidates.append(next_local_day.astimezone(UTC))

        latest_published = session.scalar(
            select(func.max(PublishJob.published_at)).where(
                PublishJob.social_account_id == account.id,
                PublishJob.status == "published",
                PublishJob.published_at.is_not(None),
            )
        )
        if latest_published is not None:
            cooldown_ends = _as_utc(latest_published) + timedelta(
                seconds=account.cooldown_seconds
            )
            if cooldown_ends > current:
                retry_candidates.append(cooldown_ends)

        return max(retry_candidates) if retry_candidates else None

    def _finish_attempt(
        self,
        job_id: int,
        attempt_id: int,
        result: PublishResult,
        latency_ms: int,
        current: datetime,
    ) -> WorkerOutcome:
        with session_scope() as session:
            job = session.scalar(
                select(PublishJob)
                .where(PublishJob.id == job_id)
                .with_for_update()
            )
            attempt = session.scalar(
                select(PublishAttempt)
                .where(
                    PublishAttempt.id == attempt_id,
                    PublishAttempt.publish_job_id == job_id,
                )
                .with_for_update()
            )
            if (
                job is None
                or attempt is None
                or job.status != "running"
                or attempt.status != "running"
            ):
                raise RuntimeError("claimed publish state is not running")

            attempt.latency_ms = latency_ms
            attempt.finished_at = current
            attempt.response_json = {
                "success": result.success,
                "remote_post_id": result.remote_post_id,
                "error_kind": result.error_kind.value if result.error_kind else None,
                "retry_after_seconds": result.retry_after_seconds,
            }

            if result.success:
                attempt.status = "published"
                job.status = "published"
                job.remote_post_id = result.remote_post_id
                job.last_error = None
                job.next_retry_at = None
                job.published_at = current
                job.social_post.status = "published"
                return WorkerOutcome(
                    job_id, "published", True, remote_post_id=result.remote_post_id
                )

            error_kind = result.error_kind or PublishErrorKind.UNKNOWN
            attempt.error_type = error_kind.value
            attempt.error_message = result.message
            job.last_error = f"[{error_kind.value}] {result.message}".strip()

            if result.retryable and job.attempt_count < self.max_attempts:
                attempt.status = "retryable_error"
                job.status = "retry"
                retry_seconds = result.retry_after_seconds or min(
                    30 * (2 ** max(0, job.attempt_count - 1)), 3600
                )
                job.next_retry_at = current + timedelta(seconds=retry_seconds)
                return WorkerOutcome(job_id, "retry", True, error=job.last_error)

            attempt.status = "terminal_error"
            job.status = "failed"
            job.next_retry_at = None
            if error_kind is PublishErrorKind.AUTH:
                job.social_account.status = "expired"
            elif error_kind is PublishErrorKind.CHECKPOINT:
                job.social_account.status = "checkpoint"
            return WorkerOutcome(job_id, "failed", True, error=job.last_error)

    def _fail_precondition(
        self, job_id: int, message: str, current: datetime
    ) -> WorkerOutcome:
        with session_scope() as session:
            job = session.get(PublishJob, job_id)
            if job is not None:
                job.status = "failed"
                job.last_error = f"[precondition] {message}"
                job.next_retry_at = None
                job.updated_at = current
        return WorkerOutcome(job_id, "failed", True, error=f"[precondition] {message}")

    def _defer_job(
        self,
        job_id: int,
        message: str,
        retry_at: datetime,
        current: datetime,
    ) -> WorkerOutcome:
        with session_scope() as session:
            job = session.get(PublishJob, job_id)
            if job is not None:
                job.status = "retry"
                job.last_error = f"[guardrail] {message}"
                job.next_retry_at = retry_at
                job.updated_at = current
        return WorkerOutcome(
            job_id,
            "retry",
            True,
            error=f"[guardrail] {message}",
        )
