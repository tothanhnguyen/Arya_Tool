"""Fail-safe operational probes with a deliberately small public surface."""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx
from sqlalchemy import and_, case, func, inspect, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from laplace.services.supabase_url import validate_supabase_origin
from laplace.social.models import PublishJob


class ProbeStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ProbeCode(StrEnum):
    READY = "ready"
    DATABASE_UNAVAILABLE = "database_unavailable"
    SCHEMA_NOT_READY = "schema_not_ready"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    SCHEDULER_STOPPED = "scheduler_stopped"
    SCHEDULER_DEGRADED = "scheduler_degraded"
    SCHEDULER_UNAVAILABLE = "scheduler_unavailable"


Indicator = bool | int | float
Probe = Callable[[], "ProbeResult"]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    status: ProbeStatus
    code: ProbeCode
    indicators: dict[str, Indicator]

    @property
    def ready(self) -> bool:
        return self.status is ProbeStatus.OK

    def public_dict(self) -> dict:
        indicators = {
            key: value
            for key, value in sorted(self.indicators.items())
            if key.replace("_", "").isalnum()
            and key[:1].isalpha()
            and isinstance(value, (bool, int, float))
            and (not isinstance(value, float) or math.isfinite(value))
        }
        return {
            "status": self.status.value,
            "code": self.code.value,
            "indicators": indicators,
        }


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    checks: dict[str, ProbeResult]

    @property
    def ready(self) -> bool:
        return all(result.ready for result in self.checks.values())

    def public_dict(self) -> dict:
        return {
            "status": "ok" if self.ready else "unavailable",
            "checks": {
                name: result.public_dict()
                for name, result in self.checks.items()
            },
        }


class ReadinessService:
    def __init__(
        self,
        *,
        database_probe: Probe,
        storage_probe: Probe,
        scheduler_probe: Probe,
    ) -> None:
        self._probes = {
            "database": (
                database_probe,
                ProbeCode.DATABASE_UNAVAILABLE,
            ),
            "storage": (
                storage_probe,
                ProbeCode.STORAGE_UNAVAILABLE,
            ),
            "scheduler": (
                scheduler_probe,
                ProbeCode.SCHEDULER_UNAVAILABLE,
            ),
        }

    def check(self) -> ReadinessReport:
        checks: dict[str, ProbeResult] = {}
        for name, (probe, fallback_code) in self._probes.items():
            try:
                result = probe()
            except Exception:
                result = ProbeResult(
                    status=ProbeStatus.UNAVAILABLE,
                    code=fallback_code,
                    indicators={},
                )
            if not isinstance(result, ProbeResult):
                result = ProbeResult(
                    status=ProbeStatus.UNAVAILABLE,
                    code=fallback_code,
                    indicators={},
                )
            checks[name] = result
        return ReadinessReport(checks=checks)


def liveness_report() -> dict[str, str]:
    """Process liveness never depends on downstream services."""
    return {"status": "ok"}


def _default_schema_check(engine: Engine) -> tuple[bool, dict[str, Indicator]]:
    if engine.dialect.name == "postgresql":
        from laplace.db import check_postgres_schema

        health = check_postgres_schema(engine)
        return health.ready, {
            "missing_tables": len(health.missing_tables),
            "missing_columns": len(health.missing_columns),
            "revision_available": health.current_revision is not None,
        }
    if engine.dialect.name == "sqlite":
        import laplace.models
        import laplace.services.artifacts  # noqa: F401 - registers Artifact metadata
        from laplace.db import Base

        expected = set(Base.metadata.tables)
        existing = set(inspect(engine).get_table_names())
        missing_count = len(expected - existing)
        return missing_count == 0, {"missing_tables": missing_count}
    return False, {}


def database_readiness_probe(
    *,
    engine_provider: Callable[[], Engine],
    schema_check: Callable[[Engine], tuple[bool, dict[str, Indicator]]] = _default_schema_check,
) -> ProbeResult:
    try:
        engine = engine_provider()
        with engine.connect() as connection:
            database_ok = connection.scalar(text("SELECT 1")) == 1
        if not database_ok:
            return ProbeResult(
                status=ProbeStatus.UNAVAILABLE,
                code=ProbeCode.DATABASE_UNAVAILABLE,
                indicators={},
            )
        schema_ok, indicators = schema_check(engine)
        if not schema_ok:
            return ProbeResult(
                status=ProbeStatus.UNAVAILABLE,
                code=ProbeCode.SCHEMA_NOT_READY,
                indicators=indicators,
            )
        return ProbeResult(
            status=ProbeStatus.OK,
            code=ProbeCode.READY,
            indicators=indicators,
        )
    except Exception:
        return ProbeResult(
            status=ProbeStatus.UNAVAILABLE,
            code=ProbeCode.DATABASE_UNAVAILABLE,
            indicators={},
        )


def storage_readiness_probe(*, check: Callable[[], bool]) -> ProbeResult:
    try:
        ready = check() is True
    except Exception:
        ready = False
    return ProbeResult(
        status=ProbeStatus.OK if ready else ProbeStatus.UNAVAILABLE,
        code=ProbeCode.READY if ready else ProbeCode.STORAGE_UNAVAILABLE,
        indicators={},
    )


def _writable_local_storage(root: Path) -> bool:
    candidate = root.expanduser().resolve()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.R_OK | os.W_OK | os.X_OK)


def _configured_storage_check() -> bool:
    from laplace.config import get_settings

    settings = get_settings()
    if settings.social_media_backend == "local":
        local_ready = _writable_local_storage(Path(settings.social_media_dir))
    elif settings.social_media_backend == "supabase":
        local_ready = True
    else:
        return False

    has_url = bool(settings.supabase_url)
    has_secret = bool(settings.supabase_secret_key)
    needs_remote = settings.social_media_backend == "supabase" or has_url or has_secret
    if not needs_remote:
        return local_ready
    if not has_url or not has_secret:
        return False

    buckets = {settings.supabase_artifact_bucket}
    if settings.social_media_backend == "supabase":
        buckets.add(settings.supabase_media_bucket)
    headers = {
        "apikey": settings.supabase_secret_key,
        "authorization": f"Bearer {settings.supabase_secret_key}",
    }
    project_url = validate_supabase_origin(settings.supabase_url)
    with httpx.Client(timeout=5.0) as client:
        for bucket in buckets:
            response = client.get(
                f"{project_url}/storage/v1/bucket/{quote(bucket, safe='')}",
                headers=headers,
            )
            if response.status_code != 200:
                return False
    return local_ready


class SessionFactory(Protocol):
    def __call__(self) -> AbstractContextManager[Session]: ...


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def scheduler_readiness_probe(
    *,
    session_factory: SessionFactory,
    scheduler_running: Callable[[], bool],
    now: Callable[[], datetime] | None = None,
    backlog_warning: int = 100,
    retry_warning: int = 20,
    lag_warning_seconds: int = 300,
    stale_running_seconds: int = 600,
) -> ProbeResult:
    if min(
        backlog_warning,
        retry_warning,
        lag_warning_seconds,
        stale_running_seconds,
    ) < 1:
        raise ValueError("scheduler health thresholds must be positive")
    current = _as_utc((now or (lambda: datetime.now(UTC)))())
    try:
        due_at = case(
            (
                and_(
                    PublishJob.next_retry_at.is_not(None),
                    PublishJob.next_retry_at > PublishJob.scheduled_at,
                ),
                PublishJob.next_retry_at,
            ),
            else_=PublishJob.scheduled_at,
        )
        due_filter = (
            PublishJob.status.in_(("queued", "retry")),
            PublishJob.scheduled_at <= current,
            or_(
                PublishJob.next_retry_at.is_(None),
                PublishJob.next_retry_at <= current,
            ),
        )
        with session_factory() as session:
            backlog = session.scalar(
                select(func.count(PublishJob.id)).where(
                    PublishJob.status.in_(("queued", "retry"))
                )
            ) or 0
            retries = session.scalar(
                select(func.count(PublishJob.id)).where(PublishJob.status == "retry")
            ) or 0
            stale_running = session.scalar(
                select(func.count(PublishJob.id)).where(
                    PublishJob.status == "running",
                    PublishJob.updated_at
                    < current - timedelta(seconds=stale_running_seconds),
                )
            ) or 0
            oldest_due = session.scalar(select(func.min(due_at)).where(*due_filter))
        lag_seconds = (
            max(0, int((current - _as_utc(oldest_due)).total_seconds()))
            if oldest_due is not None
            else 0
        )
        indicators: dict[str, Indicator] = {
            "backlog": int(backlog),
            "retry": int(retries),
            "lag_seconds": lag_seconds,
            "stale_running": int(stale_running),
        }
        if not scheduler_running():
            return ProbeResult(
                status=ProbeStatus.UNAVAILABLE,
                code=ProbeCode.SCHEDULER_STOPPED,
                indicators=indicators,
            )
        degraded = (
            backlog >= backlog_warning
            or retries >= retry_warning
            or lag_seconds >= lag_warning_seconds
            or stale_running > 0
        )
        return ProbeResult(
            status=ProbeStatus.DEGRADED if degraded else ProbeStatus.OK,
            code=ProbeCode.SCHEDULER_DEGRADED if degraded else ProbeCode.READY,
            indicators=indicators,
        )
    except Exception:
        return ProbeResult(
            status=ProbeStatus.UNAVAILABLE,
            code=ProbeCode.SCHEDULER_UNAVAILABLE,
            indicators={},
        )


def _default_scheduler_running() -> bool:
    from laplace import scheduler
    from laplace.social import scheduler as social_scheduler

    return scheduler.is_running() and social_scheduler.is_social_scheduler_running()


def build_default_readiness_service() -> ReadinessService:
    from laplace.db import get_engine, session_scope

    return ReadinessService(
        database_probe=lambda: database_readiness_probe(engine_provider=get_engine),
        storage_probe=lambda: storage_readiness_probe(check=_configured_storage_check),
        scheduler_probe=lambda: scheduler_readiness_probe(
            session_factory=session_scope,
            scheduler_running=_default_scheduler_running,
        ),
    )
