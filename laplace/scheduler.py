"""Scheduler toi gian: APScheduler in-process chay cac ScheduledJob dinh ky.

- start_scheduler(on_result): doc ScheduledJob enabled trong DB, dang ky cron job.
- refresh_jobs(): nap lai danh sach job sau khi scheduler tool thay doi DB.
- Moi job: tao Task tu task_template -> run_task -> goi on_result(user_id, result).
"""

import logging
from collections.abc import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from laplace.db import session_scope
from laplace.models import ScheduledJob, Task
from laplace.services.tasks import create_task

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_on_result: Callable[[int, str], None] | None = None


def _run_scheduled_job(job_id: int, user_id: int, task_template: str) -> None:
    """Chay 1 job dinh ky: tao task -> run_task -> bao ket qua. Khong raise."""
    from laplace.agent.orchestrator import run_task

    try:
        with session_scope() as session:
            task = create_task(session, user_id=user_id, request=task_template)
            task_id = task.id
        logger.info("scheduled_job=%s tao task=%s", job_id, task_id)

        run_task(task_id)

        with session_scope() as session:
            task = session.get(Task, task_id)
            result = ""
            if task is not None:
                if task.status == "done":
                    result = task.result or "✅ Xong."
                else:
                    result = (
                        f"⚠️ Tác vụ định kỳ chưa hoàn thành "
                        f"(status={task.status}): {task.error or ''}"
                    )
        if _on_result is not None:
            _on_result(user_id, result)
    except Exception:
        logger.exception("scheduled_job=%s loi khi chay", job_id)


def is_running() -> bool:
    """Scheduler da start va dang chay hay chua (dung cho refresh best-effort)."""
    return _scheduler is not None and _scheduler.running


def refresh_jobs() -> None:
    """Nap lai job tu DB (goi sau khi scheduler tool them/xoa ScheduledJob)."""
    if _scheduler is None:
        return
    _scheduler.remove_all_jobs()
    with session_scope() as session:
        jobs = session.scalars(
            select(ScheduledJob).where(ScheduledJob.enabled.is_(True))
        ).all()
        rows = [(j.id, j.user_id, j.cron, j.task_template) for j in jobs]

    for job_id, user_id, cron, template in rows:
        try:
            trigger = CronTrigger.from_crontab(cron)
        except ValueError:
            logger.error("scheduled_job=%s cron khong hop le: %r", job_id, cron)
            continue
        _scheduler.add_job(
            _run_scheduled_job,
            trigger,
            args=[job_id, user_id, template],
            id=f"scheduled_job:{job_id}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    logger.info("Scheduler nap %d job", len(_scheduler.get_jobs()))


def start_scheduler(
    on_result: Callable[[int, str], None] | None = None,
) -> BackgroundScheduler:
    """Khoi dong BackgroundScheduler va nap job tu DB. Idempotent."""
    global _scheduler, _on_result
    _on_result = on_result
    if _scheduler is None:
        _scheduler = BackgroundScheduler()
        _scheduler.start()
    refresh_jobs()
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
