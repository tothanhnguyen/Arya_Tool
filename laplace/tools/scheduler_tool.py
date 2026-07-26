"""scheduler tool: CRUD job dinh ky (bang scheduled_jobs), validate cron 5 truong."""

import logging
from typing import Literal

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field
from sqlalchemy import select

from laplace.db import session_scope
from laplace.models import ScheduledJob
from laplace.schemas import ToolResult
from laplace.tools.base import ToolContext, tool

logger = logging.getLogger(__name__)


class SchedulerParams(BaseModel):
    action: Literal["create", "list", "delete"] = Field(
        description="Operation to perform on the user's scheduled jobs"
    )
    job_id: int | None = Field(default=None, description="Job id, required for 'delete'")
    cron: str = Field(
        default="",
        description=(
            "Standard 5-field crontab expression (minute hour day month day_of_week), "
            "e.g. '0 8 * * *' for every day at 08:00. Required for 'create'."
        ),
    )
    task_template: str = Field(
        default="",
        description=(
            "The natural-language request the agent will run on each trigger, "
            "e.g. 'Summarize the latest AI news'. Required for 'create'."
        ),
    )


def _job_dict(job: ScheduledJob) -> dict:
    return {
        "id": job.id,
        "cron": job.cron,
        "task_template": job.task_template,
        "enabled": job.enabled,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


@tool(
    name="scheduler",
    description=(
        "Manage recurring scheduled jobs for the user. Use action='create' with a 5-field "
        "cron expression and a task_template (the request to run on schedule), "
        "action='list' to show existing jobs, and action='delete' with job_id to remove one. "
        "Always requires user confirmation."
    ),
    params=SchedulerParams,
    requires_confirmation=True,
)
def scheduler_tool(params: SchedulerParams, ctx: ToolContext) -> ToolResult:
    try:
        with session_scope() as session:
            if params.action == "create":
                if not params.task_template.strip():
                    return ToolResult(
                        ok=False, error="'task_template' is required for action='create'"
                    )
                try:
                    CronTrigger.from_crontab(params.cron)
                except Exception as e:
                    return ToolResult(
                        ok=False, error=f"Invalid cron expression '{params.cron}': {e}"
                    )
                job = ScheduledJob(
                    user_id=ctx.user_id,
                    cron=params.cron.strip(),
                    task_template=params.task_template.strip(),
                    enabled=True,
                )
                session.add(job)
                session.flush()
                return ToolResult(ok=True, data=_job_dict(job))

            if params.action == "list":
                jobs = session.scalars(
                    select(ScheduledJob)
                    .where(ScheduledJob.user_id == ctx.user_id)
                    .order_by(ScheduledJob.id)
                ).all()
                return ToolResult(ok=True, data={"jobs": [_job_dict(j) for j in jobs]})

            # delete
            if params.job_id is None:
                return ToolResult(ok=False, error="'job_id' is required for action='delete'")
            job = session.scalar(
                select(ScheduledJob).where(
                    ScheduledJob.id == params.job_id, ScheduledJob.user_id == ctx.user_id
                )
            )
            if job is None:
                return ToolResult(ok=False, error=f"Scheduled job {params.job_id} not found")
            session.delete(job)
            return ToolResult(ok=True, data={"deleted_id": params.job_id})
    except Exception as e:
        logger.exception("scheduler failed")
        return ToolResult(ok=False, error=f"scheduler failed: {e}")
