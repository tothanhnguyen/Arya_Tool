"""Supabase user-session validation and local dashboard owner mapping."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx
from fastapi import Request
from sqlalchemy import select

from laplace.config import Settings
from laplace.db import session_scope
from laplace.models import User
from laplace.services.supabase_url import (
    SupabaseURLValidationError,
    validate_supabase_origin,
)

MAX_ACCESS_TOKEN_LENGTH = 8_192
COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class AuthSessionError(RuntimeError):
    """Base class for safe authentication failures."""


class MissingAuthSession(AuthSessionError):
    pass


class InvalidAuthSession(AuthSessionError):
    pass


class AuthServiceUnavailable(AuthSessionError):
    pass


class AuthConfigurationError(AuthSessionError):
    pass


class OwnerMappingNotFound(AuthSessionError):
    pass


@dataclass(frozen=True, slots=True)
class AuthIdentity:
    auth_user_id: UUID


class AuthClient(Protocol):
    def validate_access_token(self, access_token: str) -> AuthIdentity: ...


def _jwt_role(value: str) -> str | None:
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + ("=" * (-len(parts[1]) % 4))
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded)
    except (UnicodeError, ValueError):
        return None
    role = data.get("role") if isinstance(data, dict) else None
    return role if isinstance(role, str) else None


def _validate_public_key(value: str | None) -> str:
    key = (value or "").strip()
    if (
        not key
        or len(key) > MAX_ACCESS_TOKEN_LENGTH
        or any(ord(char) < 33 or ord(char) > 126 for char in key)
    ):
        raise AuthConfigurationError("Supabase Auth public key is not configured.")
    if key.startswith("sb_secret_") or _jwt_role(key) in {
        "service_role",
        "supabase_admin",
    }:
        raise AuthConfigurationError(
            "Supabase Auth requires a publishable or legacy anon key."
        )
    return key


def _auth_user_endpoint(value: str | None) -> str:
    try:
        origin = validate_supabase_origin(value)
    except SupabaseURLValidationError as exc:
        raise AuthConfigurationError("Supabase URL is invalid.") from exc
    return f"{origin}/auth/v1/user"


class HTTPSupabaseAuthClient:
    """Validate a user access token through the public Supabase Auth endpoint."""

    def __init__(
        self,
        *,
        supabase_url: str,
        publishable_key: str,
        timeout_s: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if timeout_s <= 0 or timeout_s > 30:
            raise AuthConfigurationError("Supabase Auth timeout is invalid.")
        self.endpoint = _auth_user_endpoint(supabase_url)
        self.publishable_key = _validate_public_key(publishable_key)
        self.timeout_s = timeout_s
        self.transport = transport

    def validate_access_token(self, access_token: str) -> AuthIdentity:
        try:
            with httpx.Client(
                timeout=self.timeout_s,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = client.get(
                    self.endpoint,
                    headers={
                        "apikey": self.publishable_key,
                        "Authorization": f"Bearer {access_token}",
                    },
                )
        except httpx.RequestError as exc:
            raise AuthServiceUnavailable(
                "Supabase Auth is temporarily unavailable."
            ) from exc
        if response.status_code in {401, 403}:
            raise InvalidAuthSession("Supabase Auth session is invalid or expired.")
        if response.status_code != 200:
            raise AuthServiceUnavailable(
                "Supabase Auth is temporarily unavailable."
            )
        try:
            payload = response.json()
            raw_user_id = payload["id"]
            auth_user_id = UUID(raw_user_id)
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidAuthSession(
                "Supabase Auth returned an invalid user identity."
            ) from exc
        return AuthIdentity(auth_user_id=auth_user_id)


def get_supabase_auth_client(settings: Settings) -> AuthClient:
    public_key = settings.supabase_publishable_key or settings.supabase_anon_key
    return HTTPSupabaseAuthClient(
        supabase_url=settings.supabase_url or "",
        publishable_key=public_key or "",
        timeout_s=settings.supabase_auth_timeout_s,
    )


def _normalize_access_token(value: str) -> str:
    token = value.strip()
    if (
        not token
        or len(token) > MAX_ACCESS_TOKEN_LENGTH
        or any(ord(character) < 33 or ord(character) > 126 for character in token)
    ):
        raise InvalidAuthSession("Supabase Auth session token is invalid.")
    return token


def access_token_from_request(request: Request, cookie_name: str) -> str:
    authorization = request.headers.get("authorization")
    cookie_token = request.cookies.get(cookie_name)
    header_token: str | None = None
    if authorization is not None:
        scheme, separator, credentials = authorization.partition(" ")
        if not separator or scheme.casefold() != "bearer":
            raise InvalidAuthSession("Authorization header must use Bearer.")
        header_token = _normalize_access_token(credentials)
    normalized_cookie = (
        _normalize_access_token(cookie_token) if cookie_token is not None else None
    )
    if (
        header_token is not None
        and normalized_cookie is not None
        and header_token != normalized_cookie
    ):
        raise InvalidAuthSession("Conflicting Supabase Auth credentials.")
    token = header_token or normalized_cookie
    if token is None:
        raise MissingAuthSession("Supabase Auth session is required.")
    return token


def authenticate_dashboard_request(request: Request, settings: Settings) -> int:
    """Validate the session and attach the mapped integer owner to the request."""

    cookie_name = settings.supabase_auth_cookie_name.strip()
    if not COOKIE_NAME_RE.fullmatch(cookie_name):
        raise AuthConfigurationError("Supabase Auth cookie name is invalid.")
    access_token = access_token_from_request(request, cookie_name)
    identity = get_supabase_auth_client(settings).validate_access_token(access_token)
    with session_scope() as session:
        user_id = session.scalar(
            select(User.id).where(User.auth_user_id == identity.auth_user_id)
        )
    if user_id is None:
        raise OwnerMappingNotFound("Supabase Auth user is not mapped to an app owner.")
    request.state.user_id = user_id
    return user_id
