"""Validation shared by clients that send credentials to Supabase."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


class SupabaseURLValidationError(ValueError):
    """The configured project URL is not a safe Supabase API origin."""


def _is_loopback(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def validate_supabase_origin(value: str | None) -> str:
    """Return a credential-safe origin.

    Production credentials require HTTPS. Plain HTTP is accepted only for a
    loopback Supabase emulator.
    """

    raw = (value or "").strip()
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise SupabaseURLValidationError("Supabase URL is invalid.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SupabaseURLValidationError("Supabase URL is invalid.")
    if parsed.scheme == "http" and not _is_loopback(hostname):
        raise SupabaseURLValidationError(
            "Supabase URL must use HTTPS outside a loopback emulator."
        )
    return f"{parsed.scheme}://{parsed.netloc}"
