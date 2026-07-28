"""Offline tests for secret-safe operations preflight checks."""

import json

import httpx
import pytest

from laplace.ops import preflight as preflight_module
from laplace.ops.preflight import (
    CheckState,
    config_preflight,
    main,
    runtime_preflight,
)


def _valid_environment() -> dict[str, str]:
    return {
        "LAPLACE_DB_URL": (
            "postgresql://postgres.project-ref:db-password@"
            "aws-0-region.pooler.supabase.com:5432/postgres"
        ),
        "LAPLACE_SUPABASE_URL": "https://project-ref.supabase.co",
        "LAPLACE_SUPABASE_SECRET_KEY": "sb_secret_server-sentinel",
        "LAPLACE_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_public-sentinel",
        "LAPLACE_SUPABASE_AUTH_ENABLED": "true",
        "LAPLACE_SOCIAL_MEDIA_BACKEND": "supabase",
        "LAPLACE_SOCIAL_PUBLISHER": "mock",
        "LAPLACE_SOCIAL_BROWSER_PUBLISHER": "false",
    }


def test_config_preflight_accepts_safe_supabase_runtime_without_exposing_values():
    environment = _valid_environment()

    report = config_preflight(environment)
    serialized = json.dumps(report.public_dict())

    assert report.ready
    assert report.source == "process_environment_only"
    assert report.remote_access is False
    assert all(check.state is CheckState.VALID for check in report.checks)
    for name in (
        "LAPLACE_DB_URL",
        "LAPLACE_SUPABASE_URL",
        "LAPLACE_SUPABASE_SECRET_KEY",
        "LAPLACE_SUPABASE_PUBLISHABLE_KEY",
    ):
        assert environment[name] not in serialized
    assert "db-password" not in serialized
    assert "project-ref" not in serialized
    assert "sentinel" not in serialized


def test_config_preflight_fails_closed_for_missing_or_unsafe_configuration():
    report = config_preflight(
        {
            "LAPLACE_DB_URL": "sqlite:///./arya-tool.db",
            "LAPLACE_SUPABASE_URL": "http://project-ref.supabase.co",
            "LAPLACE_SUPABASE_SECRET_KEY": "same-key",
            "LAPLACE_SUPABASE_PUBLISHABLE_KEY": "same-key",
            "LAPLACE_SUPABASE_AUTH_ENABLED": "false",
            "LAPLACE_SOCIAL_MEDIA_BACKEND": "local",
            "LAPLACE_SOCIAL_PUBLISHER": "meta_graph",
            "LAPLACE_SOCIAL_BROWSER_PUBLISHER": "true",
        }
    )
    states = {check.code: check.state for check in report.checks}

    assert not report.ready
    assert states["database_connection"] is CheckState.INVALID
    assert states["supabase_origin"] is CheckState.INVALID
    assert states["auth_public_key"] is CheckState.UNSAFE
    assert states["supabase_auth"] is CheckState.UNSAFE
    assert states["storage_backend"] is CheckState.UNSAFE
    assert states["publisher"] is CheckState.UNSAFE
    assert states["browser_publisher"] is CheckState.UNSAFE


def test_config_preflight_rejects_transaction_pooler_and_disabled_ssl():
    environment = _valid_environment()
    environment["LAPLACE_DB_URL"] = (
        "postgresql://postgres.project-ref:db-password@"
        "aws-0-region.pooler.supabase.com:6543/postgres?sslmode=disable"
    )

    report = config_preflight(environment)
    state = {
        check.code: check.state
        for check in report.checks
    }["database_connection"]

    assert state is CheckState.INVALID

    environment["LAPLACE_DB_URL"] = (
        "postgresql://postgres.project-ref:db-password@"
        "aws-0-region.pooler.supabase.com:5432/postgres?sslmode=disable"
    )
    report = config_preflight(environment)
    state = {
        check.code: check.state
        for check in report.checks
    }["database_connection"]

    assert state is CheckState.UNSAFE


def test_config_preflight_rejects_public_key_in_server_secret_slot():
    environment = _valid_environment()
    environment["LAPLACE_SUPABASE_SECRET_KEY"] = "sb_publishable_wrong-slot"

    report = config_preflight(environment)
    state = {
        check.code: check.state
        for check in report.checks
    }["server_secret_key"]

    assert state is CheckState.INVALID


def test_config_preflight_accepts_explicit_false_boolean_alias():
    environment = _valid_environment()
    environment["LAPLACE_SOCIAL_BROWSER_PUBLISHER"] = "0"

    report = config_preflight(environment)
    state = {
        check.code: check.state
        for check in report.checks
    }["browser_publisher"]

    assert state is CheckState.VALID


def test_config_preflight_rejects_mismatched_database_and_api_projects():
    environment = _valid_environment()
    environment["LAPLACE_DB_URL"] = (
        "postgresql://postgres.other-project:db-password@"
        "aws-0-region.pooler.supabase.com:5432/postgres"
    )

    report = config_preflight(environment)
    state = {
        check.code: check.state
        for check in report.checks
    }["project_alignment"]

    assert state is CheckState.INVALID


def test_config_preflight_accepts_matching_direct_database_project():
    environment = _valid_environment()
    environment["LAPLACE_DB_URL"] = (
        "postgresql://postgres:db-password@"
        "db.project-ref.supabase.co/postgres"
    )

    report = config_preflight(environment)
    states = {check.code: check.state for check in report.checks}

    assert report.ready
    assert states["database_connection"] is CheckState.VALID
    assert states["project_alignment"] is CheckState.VALID


def test_config_preflight_sanitizes_malformed_database_urls():
    environment = _valid_environment()
    malformed_urls = (
        "not-a-database-url-secret-sentinel",
        "postgresql://postgres.project-ref:secret-sentinel@host:invalid/postgres",
    )

    for malformed_url in malformed_urls:
        environment["LAPLACE_DB_URL"] = malformed_url

        report = config_preflight(environment)
        states = {check.code: check.state for check in report.checks}
        serialized = json.dumps(report.public_dict())

        assert not report.ready
        assert states["database_connection"] is CheckState.INVALID
        assert states["project_alignment"] is CheckState.INVALID
        assert malformed_url not in serialized
        assert "secret-sentinel" not in serialized


def test_runtime_preflight_uses_loopback_and_sanitizes_health_payloads():
    secret = "runtime-secret-sentinel"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1"
        if request.url.path == "/health/live":
            return httpx.Response(200, json={"status": "ok", "detail": secret})
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "checks": {
                    component: {
                        "status": "ok",
                        "code": "ready",
                        "indicators": {"unsafe": secret},
                    }
                    for component in ("database", "storage", "scheduler")
                },
            },
        )

    report = runtime_preflight(transport=httpx.MockTransport(handler))
    serialized = json.dumps(report.public_dict())

    assert report.ready
    assert report.source == "loopback_health_endpoints"
    assert report.remote_access is True
    assert secret not in serialized


def test_runtime_preflight_reports_component_failure_without_response_details():
    secret = "storage-error-sentinel"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/live":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(
            503,
            json={
                "status": "unavailable",
                "checks": {
                    "database": {"status": "ok", "code": "ready"},
                    "storage": {
                        "status": "unavailable",
                        "code": "storage_unavailable",
                        "detail": secret,
                    },
                    "scheduler": {"status": "ok", "code": "ready"},
                },
            },
        )

    report = runtime_preflight(transport=httpx.MockTransport(handler))
    states = {check.code: check.state for check in report.checks}
    serialized = json.dumps(report.public_dict())

    assert not report.ready
    assert states["runtime_liveness"] is CheckState.VALID
    assert states["runtime_readiness"] is CheckState.UNAVAILABLE
    assert states["runtime_database"] is CheckState.VALID
    assert states["runtime_storage"] is CheckState.UNAVAILABLE
    assert states["runtime_scheduler"] is CheckState.VALID
    assert secret not in serialized


def test_runtime_preflight_sanitizes_unexpected_transport_errors():
    secret = "transport-error-secret-sentinel"

    def handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError(secret)

    report = runtime_preflight(transport=httpx.MockTransport(handler))
    serialized = json.dumps(report.public_dict())

    assert not report.ready
    assert all(check.state is CheckState.UNAVAILABLE for check in report.checks)
    assert secret not in serialized


@pytest.mark.parametrize(
    "timeout_s",
    (0, -1, 121, float("nan"), float("inf")),
)
def test_runtime_preflight_rejects_invalid_timeout(timeout_s):
    with pytest.raises(ValueError):
        runtime_preflight(timeout_s=timeout_s)


def test_runtime_cli_forwards_configurable_timeout(monkeypatch, capsys):
    captured = {}

    def fake_runtime_preflight(*, port, timeout_s):
        captured.update(port=port, timeout_s=timeout_s)
        return config_preflight(_valid_environment())

    monkeypatch.setattr(
        preflight_module,
        "runtime_preflight",
        fake_runtime_preflight,
    )

    assert main(["runtime", "--port", "8123", "--timeout", "45"]) == 0
    assert captured == {"port": 8123, "timeout_s": 45.0}
    assert json.loads(capsys.readouterr().out)["status"] == "ready"


def test_config_cli_returns_sanitized_failure_for_invalid_database(
    monkeypatch,
    capsys,
):
    environment = _valid_environment()
    environment["LAPLACE_DB_URL"] = "invalid-url-cli-secret-sentinel"
    environment["LAPLACE_DB_SSL_MODE"] = "require"
    environment["LAPLACE_SUPABASE_MEDIA_BUCKET"] = "arya-media"
    environment["LAPLACE_SUPABASE_ARTIFACT_BUCKET"] = "arya-artifacts"
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    exit_code = main(["config"])
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 2
    assert payload["status"] == "blocked"
    assert payload["checks"]["database_connection"] == {
        "status": "fail",
        "state": "invalid",
    }
    assert "invalid-url-cli-secret-sentinel" not in output
    assert "db-password" not in output
