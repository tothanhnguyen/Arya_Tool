"""Agent tool for deterministic social publish jobs."""

import hashlib
import logging
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from laplace.db import session_scope
from laplace.schemas import ToolResult
from laplace.social.models import PublishJob, SocialAccount, SocialPost
from laplace.tools.base import ToolContext, tool

logger = logging.getLogger(__name__)


class SocialScheduleParams(BaseModel):
    action: Literal["list", "create", "cancel"]
    job_id: int | None = Field(default=None, gt=0, description="Required for cancel")
    social_post_id: int | None = Field(default=None, gt=0, description="Required for create")
    social_account_id: int | None = Field(default=None, gt=0, description="Required for create")
    scheduled_at: datetime | None = Field(default=None, description="Required for create")
    status: Literal[
        "queued", "running", "published", "retry", "failed", "cancelled"
    ] | None = Field(default=None, description="Optional list filter")

    @field_validator("scheduled_at")
    @classmethod
    def _schedule_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("scheduled_at must include a timezone")
        return value

    @model_validator(mode="after")
    def _required_fields_for_action(self) -> "SocialScheduleParams":
        if self.action == "create":
            missing = [
                name
                for name in ("social_post_id", "social_account_id", "scheduled_at")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"{', '.join(missing)} required for action='create'")
        if self.action == "cancel" and self.job_id is None:
            raise ValueError("job_id is required for action='cancel'")
        return self


def _job_dict(job: PublishJob) -> dict:
    return {
        "id": job.id,
        "social_post_id": job.social_post_id,
        "social_account_id": job.social_account_id,
        "scheduled_at": job.scheduled_at.isoformat(),
        "status": job.status,
        "idempotency_key": job.idempotency_key,
        "attempt_count": job.attempt_count,
        "next_retry_at": job.next_retry_at.isoformat() if job.next_retry_at else None,
        "remote_post_id": job.remote_post_id,
        "published_at": job.published_at.isoformat() if job.published_at else None,
    }


def _idempotency_key(account_id: int, post_id: int, scheduled_at: datetime) -> str:
    canonical_time = scheduled_at.astimezone(UTC).isoformat(timespec="microseconds")
    raw = f"{account_id}:{post_id}:{canonical_time}"
    return hashlib.sha256(raw.encode()).hexdigest()


@tool(
    name="social_schedule",
    description=(
        "List, create or cancel deterministic social publish jobs. create requires an "
        "approved/published post, active account and timezone-aware scheduled_at; cancel "
        "requires job_id. Create and cancel require user confirmation. Running or already "
        "published jobs cannot be cancelled."
    ),
    params=SocialScheduleParams,
    confirm_when=lambda p: p.action in ("create", "cancel"),
)
def social_schedule(params: SocialScheduleParams, ctx: ToolContext) -> ToolResult:
    try:
        with session_scope() as session:
            if params.action == "list":
                query = (
                    select(PublishJob)
                    .join(SocialPost, PublishJob.social_post_id == SocialPost.id)
                    .join(SocialAccount, PublishJob.social_account_id == SocialAccount.id)
                    .where(
                        SocialPost.user_id == ctx.user_id,
                        SocialAccount.user_id == ctx.user_id,
                    )
                )
                if params.status is not None:
                    query = query.where(PublishJob.status == params.status)
                jobs = session.scalars(query.order_by(PublishJob.scheduled_at, PublishJob.id)).all()
                return ToolResult(ok=True, data={"jobs": [_job_dict(job) for job in jobs]})

            if params.action == "create":
                assert params.social_post_id is not None
                assert params.social_account_id is not None
                assert params.scheduled_at is not None
                if params.scheduled_at <= datetime.now(UTC):
                    return ToolResult(ok=False, error="scheduled_at must be in the future")

                post = session.scalar(
                    select(SocialPost).where(
                        SocialPost.id == params.social_post_id,
                        SocialPost.user_id == ctx.user_id,
                    )
                )
                if post is None:
                    return ToolResult(
                        ok=False, error=f"Social post {params.social_post_id} not found"
                    )
                account = session.scalar(
                    select(SocialAccount).where(
                        SocialAccount.id == params.social_account_id,
                        SocialAccount.user_id == ctx.user_id,
                    )
                )
                if account is None:
                    return ToolResult(
                        ok=False, error=f"Social account {params.social_account_id} not found"
                    )

                key = _idempotency_key(account.id, post.id, params.scheduled_at)
                existing = session.scalar(
                    select(PublishJob).where(PublishJob.idempotency_key == key)
                )
                if existing is not None:
                    # Ownership has already been proven through account + post above.
                    return ToolResult(
                        ok=True, data={**_job_dict(existing), "already_existed": True}
                    )

                if post.status not in ("approved", "published"):
                    return ToolResult(
                        ok=False,
                        error=(
                            f"Social post {post.id} cannot be scheduled "
                            f"from status='{post.status}'"
                        ),
                    )
                if account.status != "active":
                    return ToolResult(
                        ok=False,
                        error=(
                            f"Social account {account.id} is not active "
                            f"(status='{account.status}')"
                        ),
                    )

                job = PublishJob(
                    social_post_id=post.id,
                    social_account_id=account.id,
                    scheduled_at=params.scheduled_at,
                    status="queued",
                    idempotency_key=key,
                )
                if post.status == "approved":
                    post.status = "scheduled"
                session.add(job)
                session.flush()
                return ToolResult(ok=True, data={**_job_dict(job), "already_existed": False})

            job = session.scalar(
                select(PublishJob)
                .join(SocialPost, PublishJob.social_post_id == SocialPost.id)
                .join(SocialAccount, PublishJob.social_account_id == SocialAccount.id)
                .where(
                    PublishJob.id == params.job_id,
                    SocialPost.user_id == ctx.user_id,
                    SocialAccount.user_id == ctx.user_id,
                )
            )
            if job is None:
                return ToolResult(ok=False, error=f"Publish job {params.job_id} not found")
            if job.status in ("running", "published"):
                return ToolResult(
                    ok=False,
                    error=f"Publish job {job.id} cannot be cancelled from status='{job.status}'",
                )
            if job.status == "cancelled":
                return ToolResult(ok=True, data=_job_dict(job))

            job.status = "cancelled"
            post = job.social_post
            if post.status == "scheduled":
                other_live_job = session.scalar(
                    select(PublishJob.id)
                    .where(
                        PublishJob.social_post_id == post.id,
                        PublishJob.id != job.id,
                        PublishJob.status.in_(("queued", "retry", "running")),
                    )
                    .limit(1)
                )
                if other_live_job is None:
                    post.status = "approved"
            session.flush()
            return ToolResult(ok=True, data=_job_dict(job))
    except Exception as exc:
        logger.exception("social_schedule failed")
        return ToolResult(ok=False, error=f"social_schedule failed: {exc}")
