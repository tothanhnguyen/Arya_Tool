"""In-process scheduler for persisted social publish jobs."""

from __future__ import annotations

import logging
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from laplace.config import get_settings
from laplace.social.publishers import SocialPublisher, get_publisher
from laplace.social.worker import PublishWorker, WorkerOutcome, utcnow

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_worker: PublishWorker | None = None


def run_publish_tick() -> list[WorkerOutcome]:
    """Process one bounded batch. Exceptions are contained by the scheduler."""
    if _worker is None:
        return []
    try:
        outcomes = _worker.process_due()
    except Exception:
        logger.exception("Social publish tick failed")
        return []
    if outcomes:
        logger.info(
            "Social publish tick processed %d job(s): %s",
            len(outcomes),
            ", ".join(f"{item.job_id}:{item.status}" for item in outcomes),
        )
    return outcomes


def is_social_scheduler_running() -> bool:
    return _scheduler is not None and _scheduler.running


def start_social_scheduler(
    *,
    publisher: SocialPublisher | None = None,
    interval_seconds: int | None = None,
) -> BackgroundScheduler:
    """Start the isolated social worker loop. Idempotent."""
    global _scheduler, _worker

    settings = get_settings()
    seconds = interval_seconds or settings.social_poll_seconds
    if seconds < 1:
        raise ValueError("social scheduler interval must be at least 1 second")

    if _worker is None:
        _worker = PublishWorker(publisher or get_publisher(settings.social_publisher))
        _worker.recover_stale_jobs(stale_before=utcnow() - timedelta(minutes=10))

    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=settings.social_timezone)
        _scheduler.add_job(
            run_publish_tick,
            "interval",
            seconds=seconds,
            id="social_publish_worker",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        _scheduler.start()
    return _scheduler


def stop_social_scheduler() -> None:
    global _scheduler, _worker
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
    _scheduler = None
    _worker = None
