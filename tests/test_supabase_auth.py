"""Offline tests for Supabase Auth session and dashboard owner mapping."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from laplace import db
from laplace.config import Settings, get_settings
from laplace.models import Task, User
from laplace.social.models import AffiliateEvent
from laplace.web import supabase_auth
from laplace.web.app import create_app
from laplace.web.deps import require_api_key


def _protected_app() -> FastAPI:
    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(require_api_key)])
    def protected(request: Request) -> dict[str, int | None]:
        return {"user_id": getattr(request.state, "user_id", None)}

    return app


def _enable_auth(monkeypatch) -> Settings:
    settings = get_settings()
    monkeypatch.setattr(settings, "supabase_auth_enabled", True)
    monkeypatch.setattr(settings, "supabase_url", "https://project.supabase.co")
    monkeypatch.setattr(
        settings,
        "supabase_publishable_key",
        "sb_publishable_offline_test",
    )
    monkeypatch.setattr(settings, "supabase_anon_key", None)
    monkeypatch.setattr(
        settings,
        "supabase_auth_cookie_name",
        "arya_supabase_access_token",
    )
    monkeypatch.setattr(settings, "supabase_auth_timeout_s", 2.0)
    return settings


@dataclass
class _FakeAuthClient:
    identity: supabase_auth.AuthIdentity | None = None
    error: Exception | None = None
    tokens: list[str] = field(default_factory=list)

    def validate_access_token(self, access_token: str) -> supabase_auth.AuthIdentity:
        self.tokens.append(access_token)
        if self.error is not None:
            raise self.error
        assert self.identity is not None
        return self.identity


def _inject_client(monkeypatch, client: _FakeAuthClient) -> None:
    monkeypatch.setattr(
        supabase_auth,
        "get_supabase_auth_client",
        lambda _settings: client,
    )


def test_bearer_session_maps_auth_uuid_to_request_owner(
    session,
    monkeypatch,
):
    _enable_auth(monkeypatch)
    auth_user_id = uuid4()
    user = User(auth_user_id=auth_user_id)
    session.add(user)
    session.commit()
    fake = _FakeAuthClient(
        identity=supabase_auth.AuthIdentity(auth_user_id=auth_user_id)
    )
    _inject_client(monkeypatch, fake)

    with TestClient(_protected_app()) as client:
        response = client.get(
            "/protected",
            headers={"Authorization": "Bearer bearer-session"},
        )

    assert response.status_code == 200
    assert response.json() == {"user_id": user.id}
    assert fake.tokens == ["bearer-session"]


def test_http_only_cookie_session_maps_owner(session, monkeypatch):
    settings = _enable_auth(monkeypatch)
    auth_user_id = uuid4()
    user = User(auth_user_id=auth_user_id)
    session.add(user)
    session.commit()
    fake = _FakeAuthClient(
        identity=supabase_auth.AuthIdentity(auth_user_id=auth_user_id)
    )
    _inject_client(monkeypatch, fake)

    with TestClient(_protected_app()) as client:
        client.cookies.set(settings.supabase_auth_cookie_name, "cookie-session")
        response = client.get("/protected")

    assert response.status_code == 200
    assert response.json() == {"user_id": user.id}
    assert fake.tokens == ["cookie-session"]


def test_authenticated_owner_context_scopes_real_stats_dashboard(
    session,
    monkeypatch,
):
    _enable_auth(monkeypatch)
    first_auth_id = uuid4()
    selected_auth_id = uuid4()
    first = User(auth_user_id=first_auth_id)
    selected = User(auth_user_id=selected_auth_id)
    session.add_all([first, selected])
    session.flush()
    session.add_all(
        [
            AffiliateEvent(
                user_id=first.id,
                event_type="commission",
                amount=Decimal(111_111),
                currency="VND",
                occurred_at=datetime.now(UTC),
            ),
            AffiliateEvent(
                user_id=selected.id,
                event_type="commission",
                amount=Decimal(222_222),
                currency="VND",
                occurred_at=datetime.now(UTC),
            ),
        ]
    )
    session.commit()
    fake = _FakeAuthClient(
        identity=supabase_auth.AuthIdentity(auth_user_id=selected_auth_id)
    )
    _inject_client(monkeypatch, fake)

    with TestClient(create_app()) as client:
        response = client.get(
            "/stats",
            headers={"Authorization": "Bearer selected-owner-session"},
        )

    assert response.status_code == 200
    assert f"Owner #{selected.id}" in response.text
    assert "222,222 ₫" in response.text
    assert "111,111 ₫" not in response.text


def test_real_auth_dependency_scopes_task_api(session, monkeypatch):
    _enable_auth(monkeypatch)
    owner_auth_id = uuid4()
    owner = User(auth_user_id=owner_auth_id)
    other = User(auth_user_id=uuid4())
    session.add_all([owner, other])
    session.flush()
    own_task = Task(user_id=owner.id, request="own task")
    other_task = Task(user_id=other.id, request="other task")
    session.add_all([own_task, other_task])
    session.commit()
    _inject_client(
        monkeypatch,
        _FakeAuthClient(
            identity=supabase_auth.AuthIdentity(auth_user_id=owner_auth_id)
        ),
    )

    with TestClient(create_app()) as client:
        own = client.get(
            f"/api/tasks/{own_task.id}",
            headers={"Authorization": "Bearer owner-session"},
        )
        hidden = client.get(
            f"/api/tasks/{other_task.id}",
            headers={"Authorization": "Bearer owner-session"},
        )

    assert own.status_code == 200
    assert own.json()["id"] == own_task.id
    assert hidden.status_code == 404


def test_auth_enabled_rejects_missing_session_and_api_key_bypass(monkeypatch):
    settings = _enable_auth(monkeypatch)
    monkeypatch.setattr(settings, "api_key", "local-key")
    fake = _FakeAuthClient()
    _inject_client(monkeypatch, fake)

    with TestClient(_protected_app()) as client:
        response = client.get(
            "/protected",
            headers={"X-API-Key": "local-key"},
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert fake.tokens == []


def test_conflicting_cookie_and_bearer_fail_before_auth_call(monkeypatch):
    settings = _enable_auth(monkeypatch)
    fake = _FakeAuthClient()
    _inject_client(monkeypatch, fake)

    with TestClient(_protected_app()) as client:
        client.cookies.set(settings.supabase_auth_cookie_name, "cookie-session")
        response = client.get(
            "/protected",
            headers={"Authorization": "Bearer other-session"},
        )

    assert response.status_code == 401
    assert fake.tokens == []


def test_invalid_or_expired_session_fails_closed(monkeypatch):
    _enable_auth(monkeypatch)
    fake = _FakeAuthClient(
        error=supabase_auth.InvalidAuthSession("upstream-secret-must-not-leak")
    )
    _inject_client(monkeypatch, fake)

    with TestClient(_protected_app()) as client:
        response = client.get(
            "/protected",
            headers={"Authorization": "Bearer expired-session"},
        )

    assert response.status_code == 401
    assert "upstream-secret" not in response.text


def test_unmapped_auth_user_is_forbidden(session, monkeypatch):
    _enable_auth(monkeypatch)
    auth_user_id = uuid4()
    fake = _FakeAuthClient(
        identity=supabase_auth.AuthIdentity(auth_user_id=auth_user_id)
    )
    _inject_client(monkeypatch, fake)

    with TestClient(_protected_app()) as client:
        response = client.get(
            "/protected",
            headers={"Authorization": "Bearer stale-mapping"},
        )

    assert response.status_code == 403
    assert str(auth_user_id) not in response.text
    assert session.query(User).count() == 0


@pytest.mark.parametrize(
    "error",
    [
        supabase_auth.AuthConfigurationError("secret config detail"),
        supabase_auth.AuthServiceUnavailable("secret upstream detail"),
    ],
)
def test_auth_configuration_or_service_failure_returns_safe_503(
    monkeypatch,
    error,
):
    _enable_auth(monkeypatch)
    fake = _FakeAuthClient(error=error)
    _inject_client(monkeypatch, fake)

    with TestClient(_protected_app()) as client:
        response = client.get(
            "/protected",
            headers={"Authorization": "Bearer valid-shape"},
        )

    assert response.status_code == 503
    assert "secret" not in response.text


def test_auth_disabled_preserves_explicit_local_api_key_fallback(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "supabase_auth_enabled", False)
    monkeypatch.setattr(settings, "api_key", "local-key")

    with TestClient(_protected_app()) as client:
        denied = client.get(
            "/protected",
            headers={"Authorization": "Bearer ignored-with-auth-disabled"},
        )
        allowed = client.get(
            "/protected",
            headers={"X-API-Key": "local-key"},
        )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json() == {"user_id": None}


def test_http_client_uses_only_public_key_and_validates_uuid():
    auth_user_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://project.supabase.co/auth/v1/user"
        assert request.headers["apikey"] == "sb_publishable_offline_test"
        assert request.headers["authorization"] == "Bearer user-session"
        return httpx.Response(200, json={"id": str(auth_user_id)})

    client = supabase_auth.HTTPSupabaseAuthClient(
        supabase_url="https://project.supabase.co",
        publishable_key="sb_publishable_offline_test",
        timeout_s=2.0,
        transport=httpx.MockTransport(handler),
    )

    assert client.validate_access_token("user-session") == supabase_auth.AuthIdentity(
        auth_user_id=auth_user_id
    )


def _legacy_jwt(role: str) -> str:
    def encoded(value: dict) -> str:
        payload = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    return f"{encoded({'alg': 'HS256'})}.{encoded({'role': role})}.signature"


@pytest.mark.parametrize(
    "key",
    [
        "sb_secret_must_never_be_used_for_user_auth",
        _legacy_jwt("service_role"),
        _legacy_jwt("supabase_admin"),
    ],
)
def test_auth_client_rejects_service_secret_keys(key):
    with pytest.raises(supabase_auth.AuthConfigurationError):
        supabase_auth.HTTPSupabaseAuthClient(
            supabase_url="https://project.supabase.co",
            publishable_key=key,
            timeout_s=2.0,
        )


@pytest.mark.parametrize(
    "project_url",
    [
        "http://project.supabase.co",
        "https://user@example.test",
        "https://example.test/project-ref",
        "https://example.test?key=value",
    ],
)
def test_auth_client_rejects_unsafe_project_urls(project_url):
    with pytest.raises(supabase_auth.AuthConfigurationError):
        supabase_auth.HTTPSupabaseAuthClient(
            supabase_url=project_url,
            publishable_key="sb_publishable_offline_test",
            timeout_s=2.0,
        )


def test_auth_client_allows_http_only_for_loopback_emulator():
    client = supabase_auth.HTTPSupabaseAuthClient(
        supabase_url="http://127.0.0.1:54321/",
        publishable_key="sb_publishable_offline_test",
        timeout_s=2.0,
    )

    assert client.endpoint == "http://127.0.0.1:54321/auth/v1/user"


def test_auth_factory_never_reads_supabase_service_secret():
    settings = Settings(
        _env_file=None,
        supabase_url="https://project.supabase.co",
        supabase_secret_key="sb_secret_storage_only",
        supabase_publishable_key="sb_publishable_user_auth",
    )

    client = supabase_auth.get_supabase_auth_client(settings)

    assert isinstance(client, supabase_auth.HTTPSupabaseAuthClient)
    assert client.publishable_key == "sb_publishable_user_auth"
    assert "sb_secret" not in client.publishable_key


def test_sqlite_upgrade_adds_unique_auth_owner_mapping(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/legacy-users.db")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE users ("
                "id INTEGER PRIMARY KEY, "
                "tg_id INTEGER, "
                "profile_json JSON NOT NULL, "
                "created_at DATETIME NOT NULL"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users (id, profile_json, created_at) "
                "VALUES (1, '{}', '2026-07-28T00:00:00')"
            )
        )

    db._upgrade_sqlite_user_auth_column(engine)
    db._upgrade_sqlite_user_auth_column(engine)

    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("users")}
    indexes = {
        index["name"]: index for index in inspector.get_indexes("users")
    }
    assert "auth_user_id" in columns
    assert indexes["uq_users_auth_user_id"]["unique"] == 1

    auth_user_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa").hex
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, auth_user_id, profile_json, created_at) "
                "VALUES (2, :auth_user_id, '{}', '2026-07-28T00:00:00')"
            ),
            {"auth_user_id": auth_user_id},
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, auth_user_id, profile_json, created_at) "
                "VALUES (3, :auth_user_id, '{}', '2026-07-28T00:00:00')"
            ),
            {"auth_user_id": auth_user_id},
        )
