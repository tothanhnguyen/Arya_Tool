"""Secret-safe, non-mutating checks for a Supabase-backed runtime."""

from __future__ import annotations

import argparse
import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

import httpx
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from laplace.db import _normalize_db_url
from laplace.services.supabase_url import (
    SupabaseURLValidationError,
    validate_supabase_origin,
)
from laplace.web.supabase_auth import (
    AuthConfigurationError,
    _jwt_role,
    _validate_public_key,
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_SECURE_SSL_MODES = frozenset({"require", "verify-ca", "verify-full"})
_EXPECTED_BUCKETS = ("arya-media", "arya-artifacts")
_DEFAULT_RUNTIME_TIMEOUT_SECONDS = 30.0
_MAX_RUNTIME_TIMEOUT_SECONDS = 120.0


class CheckState(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    INVALID = "invalid"
    UNSAFE = "unsafe"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    code: str
    state: CheckState

    @property
    def passed(self) -> bool:
        return self.state is CheckState.VALID

    def public_dict(self) -> dict[str, str]:
        return {
            "status": "pass" if self.passed else "fail",
            "state": self.state.value,
        }


@dataclass(frozen=True, slots=True)
class PreflightReport:
    checks: tuple[PreflightCheck, ...]
    source: str
    remote_access: bool

    @property
    def ready(self) -> bool:
        return all(check.passed for check in self.checks)

    def public_dict(self) -> dict:
        return {
            "status": "ready" if self.ready else "blocked",
            "source": self.source,
            "remote_access": self.remote_access,
            "checks": {
                check.code: check.public_dict()
                for check in self.checks
            },
        }


def _configured_value(environment: Mapping[str, str], name: str) -> str:
    return environment.get(name, "")


def _server_key_state(value: str) -> CheckState:
    if not value:
        return CheckState.MISSING
    if (
        len(value) > 16_384
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        return CheckState.INVALID
    if not (
        (value.startswith("sb_secret_") and len(value) > len("sb_secret_") + 8)
        or _jwt_role(value) in {"service_role", "supabase_admin"}
    ):
        return CheckState.INVALID
    return CheckState.VALID


def _database_state(environment: Mapping[str, str]) -> CheckState:
    raw_url = _configured_value(environment, "LAPLACE_DB_URL")
    if not raw_url:
        return CheckState.MISSING
    try:
        parsed = make_url(_normalize_db_url(raw_url))
        port = parsed.port
    except (ArgumentError, TypeError, ValueError):
        return CheckState.INVALID
    if (
        parsed.drivername != "postgresql+psycopg"
        or not parsed.username
        or not parsed.password
        or not parsed.host
        or not parsed.database
        or port not in {None, 5432}
    ):
        return CheckState.INVALID

    configured_ssl_mode = _configured_value(
        environment,
        "LAPLACE_DB_SSL_MODE",
    ) or "require"
    query_ssl_mode = parsed.query.get("sslmode")
    effective_ssl_mode = (
        str(query_ssl_mode).casefold()
        if query_ssl_mode is not None
        else configured_ssl_mode.casefold()
    )
    if effective_ssl_mode not in _SECURE_SSL_MODES:
        return CheckState.UNSAFE
    return CheckState.VALID


def _supabase_origin_state(environment: Mapping[str, str]) -> CheckState:
    raw_origin = _configured_value(environment, "LAPLACE_SUPABASE_URL")
    if not raw_origin:
        return CheckState.MISSING
    if raw_origin != raw_origin.strip():
        return CheckState.INVALID
    try:
        validate_supabase_origin(raw_origin)
    except SupabaseURLValidationError:
        return CheckState.INVALID
    return CheckState.VALID


def _project_alignment_state(environment: Mapping[str, str]) -> CheckState:
    raw_origin = _configured_value(environment, "LAPLACE_SUPABASE_URL")
    raw_database = _configured_value(environment, "LAPLACE_DB_URL")
    if not raw_origin or not raw_database:
        return CheckState.MISSING
    try:
        origin = validate_supabase_origin(raw_origin)
        api_host = urlsplit(origin).hostname
        database = make_url(_normalize_db_url(raw_database))
    except (
        ArgumentError,
        SupabaseURLValidationError,
        TypeError,
        ValueError,
    ):
        return CheckState.INVALID
    if api_host is None or database.host is None:
        return CheckState.INVALID

    normalized_api_host = api_host.casefold()
    suffix = ".supabase.co"
    if not normalized_api_host.endswith(suffix):
        # Self-hosted installations do not expose a standard project reference.
        return CheckState.VALID
    project_ref = normalized_api_host.removesuffix(suffix)
    database_host = database.host.casefold()
    direct_host = f"db.{project_ref}.supabase.co"
    pooler_identity = (database.username or "").casefold()
    pooler_matches = (
        database_host.endswith(".pooler.supabase.com")
        and pooler_identity.endswith(f".{project_ref}")
    )
    return (
        CheckState.VALID
        if database_host == direct_host or pooler_matches
        else CheckState.INVALID
    )


def _auth_public_key_state(environment: Mapping[str, str]) -> CheckState:
    public_key = _configured_value(
        environment,
        "LAPLACE_SUPABASE_PUBLISHABLE_KEY",
    ) or _configured_value(environment, "LAPLACE_SUPABASE_ANON_KEY")
    if not public_key:
        return CheckState.MISSING
    try:
        _validate_public_key(public_key)
    except AuthConfigurationError:
        return CheckState.INVALID
    secret_key = _configured_value(
        environment,
        "LAPLACE_SUPABASE_SECRET_KEY",
    )
    if secret_key and public_key == secret_key:
        return CheckState.UNSAFE
    return CheckState.VALID


def _required_true_state(environment: Mapping[str, str], name: str) -> CheckState:
    value = _configured_value(environment, name).casefold()
    if not value:
        return CheckState.MISSING
    if value in _TRUE_VALUES:
        return CheckState.VALID
    if value in _FALSE_VALUES:
        return CheckState.UNSAFE
    return CheckState.INVALID


def _safe_default_state(
    environment: Mapping[str, str],
    name: str,
    *,
    default: str,
    expected: str,
) -> CheckState:
    value = _configured_value(environment, name) or default
    return (
        CheckState.VALID
        if value == expected
        else CheckState.UNSAFE
    )


def _safe_false_state(environment: Mapping[str, str], name: str) -> CheckState:
    value = _configured_value(environment, name).casefold()
    if not value:
        return CheckState.VALID
    if value in _FALSE_VALUES:
        return CheckState.VALID
    if value in _TRUE_VALUES:
        return CheckState.UNSAFE
    return CheckState.INVALID


def _bucket_state(environment: Mapping[str, str]) -> CheckState:
    media_bucket = (
        _configured_value(environment, "LAPLACE_SUPABASE_MEDIA_BUCKET")
        or _EXPECTED_BUCKETS[0]
    )
    artifact_bucket = (
        _configured_value(environment, "LAPLACE_SUPABASE_ARTIFACT_BUCKET")
        or _EXPECTED_BUCKETS[1]
    )
    if (media_bucket, artifact_bucket) != _EXPECTED_BUCKETS:
        return CheckState.INVALID
    return CheckState.VALID


def config_preflight(environment: Mapping[str, str]) -> PreflightReport:
    """Check process environment only; never load dotenv or contact services."""

    secret_key = _configured_value(
        environment,
        "LAPLACE_SUPABASE_SECRET_KEY",
    )
    checks = (
        PreflightCheck("database_connection", _database_state(environment)),
        PreflightCheck("supabase_origin", _supabase_origin_state(environment)),
        PreflightCheck("project_alignment", _project_alignment_state(environment)),
        PreflightCheck("server_secret_key", _server_key_state(secret_key)),
        PreflightCheck("auth_public_key", _auth_public_key_state(environment)),
        PreflightCheck(
            "supabase_auth",
            _required_true_state(environment, "LAPLACE_SUPABASE_AUTH_ENABLED"),
        ),
        PreflightCheck(
            "storage_backend",
            _safe_default_state(
                environment,
                "LAPLACE_SOCIAL_MEDIA_BACKEND",
                default="local",
                expected="supabase",
            ),
        ),
        PreflightCheck("storage_buckets", _bucket_state(environment)),
        PreflightCheck(
            "publisher",
            _safe_default_state(
                environment,
                "LAPLACE_SOCIAL_PUBLISHER",
                default="mock",
                expected="mock",
            ),
        ),
        PreflightCheck(
            "browser_publisher",
            _safe_false_state(
                environment,
                "LAPLACE_SOCIAL_BROWSER_PUBLISHER",
            ),
        ),
    )
    return PreflightReport(
        checks=checks,
        source="process_environment_only",
        remote_access=False,
    )


def _runtime_check_state(
    payload: object,
    *,
    component: str | None = None,
) -> CheckState:
    if not isinstance(payload, dict):
        return CheckState.INVALID
    if component is None:
        return (
            CheckState.VALID
            if payload.get("status") == "ok"
            else CheckState.UNAVAILABLE
        )
    checks = payload.get("checks")
    if not isinstance(checks, dict):
        return CheckState.INVALID
    result = checks.get(component)
    if not isinstance(result, dict):
        return CheckState.INVALID
    return (
        CheckState.VALID
        if result.get("status") == "ok" and result.get("code") == "ready"
        else CheckState.UNAVAILABLE
    )


def runtime_preflight(
    *,
    port: int = 8010,
    timeout_s: float = _DEFAULT_RUNTIME_TIMEOUT_SECONDS,
    transport: httpx.BaseTransport | None = None,
) -> PreflightReport:
    """Probe only the loopback app and discard all untrusted response details."""

    if not 1 <= port <= 65_535:
        raise ValueError("port must be between 1 and 65535")
    if (
        not math.isfinite(timeout_s)
        or timeout_s <= 0
        or timeout_s > _MAX_RUNTIME_TIMEOUT_SECONDS
    ):
        raise ValueError("timeout must be between 0 and 120 seconds")
    live_state = CheckState.UNAVAILABLE
    readiness_state = CheckState.UNAVAILABLE
    component_states = {
        "database": CheckState.UNAVAILABLE,
        "storage": CheckState.UNAVAILABLE,
        "scheduler": CheckState.UNAVAILABLE,
    }
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            timeout=timeout_s,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            live_response = client.get("/health/live")
            if live_response.status_code == 200:
                try:
                    live_state = _runtime_check_state(live_response.json())
                except ValueError:
                    live_state = CheckState.INVALID

            ready_response = client.get("/health/ready")
            try:
                ready_payload = ready_response.json()
            except ValueError:
                ready_payload = None
            if ready_response.status_code in {200, 503}:
                readiness_state = _runtime_check_state(ready_payload)
                if ready_response.status_code != 200:
                    readiness_state = CheckState.UNAVAILABLE
                for component in component_states:
                    component_states[component] = _runtime_check_state(
                        ready_payload,
                        component=component,
                    )
    except Exception:
        pass

    checks = (
        PreflightCheck("runtime_liveness", live_state),
        PreflightCheck("runtime_readiness", readiness_state),
        *(
            PreflightCheck(f"runtime_{component}", state)
            for component, state in component_states.items()
        ),
    )
    return PreflightReport(
        checks=checks,
        source="loopback_health_endpoints",
        remote_access=True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sanitized Arya_Tool operations preflight",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser(
        "config",
        help="validate the process environment without loading a dotenv file",
    )
    runtime_parser = subparsers.add_parser(
        "runtime",
        help="probe liveness/readiness through loopback only",
    )
    runtime_parser.add_argument("--port", type=int, default=8010)
    runtime_parser.add_argument(
        "--timeout",
        dest="timeout_s",
        type=float,
        default=_DEFAULT_RUNTIME_TIMEOUT_SECONDS,
        help="per-request timeout in seconds (default: 30, maximum: 120)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = (
            config_preflight(os.environ)
            if args.action == "config"
            else runtime_preflight(
                port=args.port,
                timeout_s=args.timeout_s,
            )
        )
    except ValueError:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "source": "local_preflight",
                    "remote_access": False,
                    "checks": {
                        "input": {
                            "status": "fail",
                            "state": CheckState.INVALID.value,
                        }
                    },
                },
                ensure_ascii=True,
            )
        )
        return 2
    print(json.dumps(report.public_dict(), ensure_ascii=True, indent=2))
    return 0 if report.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
