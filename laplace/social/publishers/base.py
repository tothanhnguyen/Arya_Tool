"""Abstractions for publishing approved social content.

The publisher boundary intentionally receives account identifiers instead of
credentials.  Concrete adapters resolve their own credentials outside this
module so secrets cannot accidentally leak into agent prompts or job traces.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable


class PublishErrorKind(str, Enum):
    """Stable error taxonomy used by workers to decide whether to retry."""

    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    CHECKPOINT = "checkpoint"
    INVALID_REQUEST = "invalid_request"
    UNKNOWN = "unknown"

    @property
    def retryable(self) -> bool:
        """Whether retrying later can reasonably succeed without user action."""

        return self in {self.NETWORK, self.RATE_LIMIT}


@dataclass(frozen=True, slots=True)
class AccountCheckResult:
    """Result of checking whether an account is ready for publishing."""

    account_id: str
    available: bool
    error_kind: PublishErrorKind | None = None
    message: str = ""

    def __post_init__(self) -> None:
        if not self.account_id.strip():
            raise ValueError("account_id must not be empty")
        if self.available and self.error_kind is not None:
            raise ValueError("an available account cannot have an error_kind")
        if not self.available and self.error_kind is None:
            raise ValueError("an unavailable account must have an error_kind")

    @property
    def ok(self) -> bool:
        """Alias that reads naturally at call sites."""

        return self.available


@dataclass(frozen=True, slots=True)
class PublishRequest:
    """Credential-free, fully resolved request passed to a publisher adapter."""

    account_id: str
    post_id: str
    idempotency_key: str
    caption: str = ""
    media_paths: tuple[str, ...] = ()
    affiliate_url: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("account_id", "post_id", "idempotency_key"):
            value = getattr(self, field_name)
            if not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        object.__setattr__(self, "media_paths", tuple(self.media_paths))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Outcome returned by a publisher for one idempotent request."""

    success: bool
    idempotency_key: str
    remote_post_id: str | None = None
    error_kind: PublishErrorKind | None = None
    message: str = ""
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key must not be empty")
        if self.success:
            if not self.remote_post_id:
                raise ValueError("a successful result must have remote_post_id")
            if self.error_kind is not None:
                raise ValueError("a successful result cannot have an error_kind")
        else:
            if self.remote_post_id is not None:
                raise ValueError("a failed result cannot have remote_post_id")
            if self.error_kind is None:
                raise ValueError("a failed result must have an error_kind")
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry_after_seconds cannot be negative")

    @property
    def retryable(self) -> bool:
        return not self.success and bool(self.error_kind and self.error_kind.retryable)


@runtime_checkable
class SocialPublisher(Protocol):
    """Synchronous boundary implemented by each social platform adapter."""

    name: str

    def check_account(self, account_id: str) -> AccountCheckResult:
        """Check that an account can publish without exposing its credentials."""
        ...

    def publish(self, request: PublishRequest) -> PublishResult:
        """Publish one approved request using its stable idempotency key."""
        ...
