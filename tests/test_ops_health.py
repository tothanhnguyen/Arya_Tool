"""Offline tests for public liveness and fail-safe readiness."""

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from laplace import db as db_module
from laplace.db import session_scope
from laplace.models import User
from laplace.ops.health import (
    ProbeCode,
    ProbeResult,
    ProbeStatus,
    ReadinessService,
    database_readiness_probe,
    scheduler_readiness_probe,
    storage_readiness_probe,
)
from laplace.social.models import MediaAsset, PublishJob, SocialAccount, SocialPost
from laplace.web import health as health_routes
from laplace.web.app import create_app


def _ready() -> ProbeResult:
    return ProbeResult(ProbeStatus.OK, ProbeCode.READY, {})


def _health_client(service: ReadinessService) -> TestClient:
    app = FastAPI()
    app.include_router(health_routes.router)
    app.dependency_overrides[health_routes.get_readiness_service] = lambda: service
    return TestClient(app)


def _seed_scheduler_jobs(session, now: datetime) -> None:
    user = User()
    session.add(user)
    session.flush()
    account = SocialAccount(
        user_id=user.id,
        platform="facebook",
        display_name="Health Page",
        external_id="health-page",
        auth_type="mock",
        auth_ref="mock://health-page",
    )
    media = MediaAsset(
        user_id=user.id,
        type="image",
        local_path="/tmp/health.png",
        original_name="health.png",
        mime_type="image/png",
        size_bytes=1,
        sha256="h" * 64,
    )
    session.add_all([account, media])
    session.flush()
    post = SocialPost(
        user_id=user.id,
        title="Health",
        caption="Health",
        hashtags_json=[],
        media_asset_id=media.id,
        status="approved",
        content_hash="i" * 64,
    )
    session.add(post)
    session.flush()
    session.add_all(
        [
            PublishJob(
                social_post_id=post.id,
                social_account_id=account.id,
                scheduled_at=now - timedelta(minutes=10),
                status="queued",
                idempotency_key="q" * 64,
                updated_at=now - timedelta(minutes=10),
            ),
            PublishJob(
                social_post_id=post.id,
                social_account_id=account.id,
                scheduled_at=now - timedelta(minutes=8),
                status="retry",
                next_retry_at=now - timedelta(minutes=2),
                idempotency_key="r" * 64,
                updated_at=now - timedelta(minutes=8),
            ),
            PublishJob(
                social_post_id=post.id,
                social_account_id=account.id,
                scheduled_at=now - timedelta(minutes=20),
                status="running",
                idempotency_key="s" * 64,
                updated_at=now - timedelta(minutes=20),
            ),
        ]
    )
    session.commit()


def test_liveness_is_independent_from_failing_dependencies() -> None:
    leaked = "postgresql://admin:secret@example.test/private"

    def explode():
        raise RuntimeError(leaked)

    service = ReadinessService(
        database_probe=explode,
        storage_probe=explode,
        scheduler_probe=explode,
    )
    client = _health_client(service)

    live = client.get("/health/live")
    ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    assert ready.status_code == 503
    assert leaked not in ready.text
    assert "example.test" not in ready.text
    assert ready.json()["checks"]["database"]["code"] == "database_unavailable"


def test_application_factory_exposes_operations_and_artifact_routes(session) -> None:
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        artifact = client.get("/api/artifacts/reconciliation")

    # The route exists but still requires an authenticated owner context.
    assert artifact.status_code == 401


def test_readiness_returns_only_stable_codes_and_numeric_indicators() -> None:
    unsafe = ProbeResult(
        ProbeStatus.OK,
        ProbeCode.READY,
        {
            "backlog": 2,
            "healthy": True,
            "url": "https://secret.example.test",
            "object_name": "private-object",
            "not_finite": float("inf"),
        },
    )
    service = ReadinessService(
        database_probe=lambda: unsafe,
        storage_probe=_ready,
        scheduler_probe=_ready,
    )
    response = _health_client(service).get("/health/ready")

    assert response.status_code == 200
    indicators = response.json()["checks"]["database"]["indicators"]
    assert indicators == {"backlog": 2, "healthy": True}
    assert "secret.example.test" not in response.text
    assert "private-object" not in response.text


def test_database_probe_separates_connectivity_from_schema_health(session) -> None:
    session.commit()
    connected = database_readiness_probe(
        engine_provider=db_module.get_engine,
        schema_check=lambda _engine: (True, {"missing_tables": 0}),
    )
    stale_schema = database_readiness_probe(
        engine_provider=db_module.get_engine,
        schema_check=lambda _engine: (False, {"missing_tables": 3}),
    )

    assert connected.ready
    assert stale_schema.status is ProbeStatus.UNAVAILABLE
    assert stale_schema.code is ProbeCode.SCHEMA_NOT_READY
    assert stale_schema.indicators == {"missing_tables": 3}


def test_storage_probe_fails_closed_without_exception_details() -> None:
    secret = "service-role-secret"

    def unavailable() -> bool:
        raise RuntimeError(secret)

    result = storage_readiness_probe(check=unavailable)

    assert result.status is ProbeStatus.UNAVAILABLE
    assert result.code is ProbeCode.STORAGE_UNAVAILABLE
    assert secret not in str(result.public_dict())


def test_scheduler_probe_reports_backlog_retry_lag_and_stale_running(session) -> None:
    now = datetime(2026, 7, 28, 13, 0, tzinfo=UTC)
    _seed_scheduler_jobs(session, now)

    result = scheduler_readiness_probe(
        session_factory=session_scope,
        scheduler_running=lambda: True,
        now=lambda: now,
        backlog_warning=2,
        retry_warning=1,
        lag_warning_seconds=300,
        stale_running_seconds=600,
    )

    assert result.status is ProbeStatus.DEGRADED
    assert result.code is ProbeCode.SCHEDULER_DEGRADED
    assert result.indicators == {
        "backlog": 2,
        "retry": 1,
        "lag_seconds": 600,
        "stale_running": 1,
    }


def test_scheduler_probe_is_unready_when_scheduler_is_stopped(session) -> None:
    now = datetime(2026, 7, 28, 13, 0, tzinfo=UTC)
    session.commit()

    result = scheduler_readiness_probe(
        session_factory=session_scope,
        scheduler_running=lambda: False,
        now=lambda: now,
    )

    assert result.status is ProbeStatus.UNAVAILABLE
    assert result.code is ProbeCode.SCHEDULER_STOPPED
    assert result.indicators["backlog"] == 0
