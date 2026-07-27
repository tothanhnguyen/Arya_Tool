"""In-process scheduler for persisted social publish jobs."""

from __future__ import annotations

import logging
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from laplace.bot.social_notifications import (
    SocialNotificationService,
    build_social_notifier,
)
from laplace.config import get_settings
from laplace.social.publishers import SocialPublisher, get_publisher
from laplace.social.worker import PublishWorker, WorkerOutcome, utcnow

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_worker: PublishWorker | None = None
_notifier: SocialNotificationService | None = None


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
        if _notifier is not None:
            try:
                _notifier.notify_outcomes(outcomes)
            except Exception:
                logger.exception("Social publish notifications failed")
    return outcomes


def run_daily_summary() -> int:
    if _notifier is None:
        return 0
    try:
        return _notifier.send_daily_summaries()
    except Exception:
        logger.exception("Social daily summary failed")
        return 0


def is_social_scheduler_running() -> bool:
    return _scheduler is not None and _scheduler.running


def start_social_scheduler(
    *,
    publisher: SocialPublisher | None = None,
    interval_seconds: int | None = None,
    notifier: SocialNotificationService | None = None,
    daily_summary_hour: int = 20,
) -> BackgroundScheduler:
    """Start the isolated social worker loop. Idempotent."""
    global _scheduler, _worker, _notifier

    settings = get_settings()
    seconds = interval_seconds or settings.social_poll_seconds
    if seconds < 1:
        raise ValueError("social scheduler interval must be at least 1 second")
    if not 0 <= daily_summary_hour <= 23:
        raise ValueError("daily summary hour must be between 0 and 23")

    if _worker is None:
        _worker = PublishWorker(publisher or get_publisher(settings.social_publisher))
        _worker.recover_stale_jobs(stale_before=utcnow() - timedelta(minutes=10))
    if _notifier is None:
        _notifier = notifier or build_social_notifier(
            settings.telegram_bot_token,
            timezone_name=settings.social_timezone,
        )

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
        _scheduler.add_job(
            run_daily_summary,
            "cron",
            hour=daily_summary_hour,
            minute=0,
            id="social_daily_summary",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        _scheduler.start()
    return _scheduler


def stop_social_scheduler() -> None:
    global _scheduler, _worker, _notifier
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
    _scheduler = None
    _worker = None
    _notifier = None
