"""Pydantic contracts and enums for social publishing."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Platform(StrEnum):
    FACEBOOK = "facebook"
    THREADS = "threads"


class AuthType(StrEnum):
    PAGE_TOKEN = "page_token"
    BROWSER_PROFILE = "browser_profile"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    CHECKPOINT = "checkpoint"
    PAUSED = "paused"


class MediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


class AffiliateProductStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    DISABLED = "disabled"


class SocialPostStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    FAILED = "failed"


class PublishJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PUBLISHED = "published"
    RETRY = "retry"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PublishAttemptStatus(StrEnum):
    RUNNING = "running"
    PUBLISHED = "published"
    RETRYABLE_ERROR = "retryable_error"
    TERMINAL_ERROR = "terminal_error"
    UNKNOWN = "unknown"


class AffiliateEventType(StrEnum):
    VIEW = "view"
    CLICK = "click"
    COMMISSION = "commission"


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def _require_aware(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("datetime must include a timezone")
    return value


def _clean_text(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("value must not be blank")
    return value


class SocialAccountCreate(BaseModel):
    user_id: int = Field(gt=0)
    platform: Platform
    display_name: str = Field(min_length=1, max_length=200)
    external_id: str = Field(min_length=1, max_length=255)
    auth_type: AuthType
    auth_ref: str = Field(
        min_length=1,
        max_length=500,
        description="Reference to a secret/profile; never a raw cookie or access token.",
    )
    timezone: str = Field(default="Asia/Ho_Chi_Minh", min_length=1, max_length=64)
    daily_post_limit: int = Field(default=2, gt=0, le=100)
    cooldown_seconds: int = Field(default=1800, ge=0, le=86_400)

    _strip_names = field_validator(
        "display_name", "external_id", "auth_ref", "timezone", mode="after"
    )(_clean_text)


class SocialAccountRead(ORMModel):
    id: int
    user_id: int
    platform: Platform
    display_name: str
    external_id: str
    auth_type: AuthType
    auth_ref: str
    status: AccountStatus
    timezone: str
    daily_post_limit: int
    cooldown_seconds: int
    last_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MediaAssetCreate(BaseModel):
    user_id: int = Field(gt=0)
    type: MediaType
    local_path: str = Field(min_length=1)
    storage_backend: Literal["local", "supabase"] = "local"
    storage_bucket: str | None = Field(default=None, max_length=100)
    storage_key: str | None = None
    original_name: str = Field(min_length=1, max_length=500)
    mime_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    duration_seconds: float | None = Field(default=None, ge=0)
    metadata_json: dict[str, Any] = Field(default_factory=dict)

    _strip_paths = field_validator("local_path", "original_name", "mime_type", mode="after")(
        _clean_text
    )


class MediaAssetRead(ORMModel):
    id: int
    user_id: int
    type: MediaType
    local_path: str
    storage_backend: Literal["local", "supabase"]
    storage_bucket: str | None
    storage_key: str | None
    original_name: str
    mime_type: str
    size_bytes: int
    sha256: str
    duration_seconds: float | None
    metadata_json: dict[str, Any]
    created_at: datetime


class AffiliateProductCreate(BaseModel):
    user_id: int = Field(gt=0)
    network: str = Field(min_length=1, max_length=64)
    merchant: str = Field(min_length=1, max_length=200)
    product_name: str = Field(min_length=1, max_length=500)
    product_url: str = Field(min_length=1)
    affiliate_url: str = Field(min_length=1)
    sub_id_template: str = Field(default="{sub_id}", min_length=1, max_length=500)
    valid_until: datetime | None = None

    _strip_text = field_validator(
        "network",
        "merchant",
        "product_name",
        "product_url",
        "affiliate_url",
        "sub_id_template",
        mode="after",
    )(_clean_text)
    _aware_expiry = field_validator("valid_until", mode="after")(_require_aware)

    @field_validator("sub_id_template")
    @classmethod
    def _template_accepts_sub_id(cls, value: str) -> str:
        if "{sub_id}" not in value:
            raise ValueError("sub_id_template must contain '{sub_id}'")
        return value


class AffiliateProductRead(ORMModel):
    id: int
    user_id: int
    network: str
    merchant: str
    product_name: str
    product_url: str
    affiliate_url: str
    sub_id_template: str
    valid_until: datetime | None
    status: AffiliateProductStatus
    created_at: datetime
    updated_at: datetime


class SocialPostCreate(BaseModel):
    user_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=300)
    caption: str = Field(min_length=1, max_length=10_000)
    hashtags: list[str] = Field(default_factory=list, max_length=30)
    media_asset_id: int = Field(gt=0)
    affiliate_product_id: int | None = Field(default=None, gt=0)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    _strip_title_caption = field_validator("title", "caption", mode="after")(_clean_text)

    @field_validator("hashtags")
    @classmethod
    def _normalise_hashtags(cls, values: list[str]) -> list[str]:
        normalised: list[str] = []
        seen: set[str] = set()
        for raw in values:
            tag = raw.strip().lstrip("#").strip()
            if not tag:
                raise ValueError("hashtags must not contain blank values")
            if any(char.isspace() for char in tag):
                raise ValueError("hashtags must not contain whitespace")
            tag = f"#{tag}"
            key = tag.casefold()
            if key not in seen:
                seen.add(key)
                normalised.append(tag)
        return normalised


class SocialPostUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    caption: str | None = Field(default=None, min_length=1, max_length=10_000)
    hashtags: list[str] | None = Field(default=None, max_length=30)
    media_asset_id: int | None = Field(default=None, gt=0)
    affiliate_product_id: int | None = Field(default=None, gt=0)
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    _strip_title_caption = field_validator("title", "caption", mode="after")(
        lambda value: None if value is None else _clean_text(value)
    )

    @field_validator("hashtags")
    @classmethod
    def _normalise_optional_hashtags(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        return SocialPostCreate._normalise_hashtags(values)

    @model_validator(mode="after")
    def _has_change(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class SocialPostRead(ORMModel):
    id: int
    user_id: int
    title: str
    caption: str
    hashtags_json: list[str]
    media_asset_id: int
    affiliate_product_id: int | None
    status: SocialPostStatus
    content_hash: str
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PublishJobCreate(BaseModel):
    social_post_id: int = Field(gt=0)
    social_account_id: int = Field(gt=0)
    scheduled_at: datetime
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")

    _aware_schedule = field_validator("scheduled_at", mode="after")(_require_aware)


class PublishJobRead(ORMModel):
    id: int
    social_post_id: int
    social_account_id: int
    scheduled_at: datetime
    status: PublishJobStatus
    idempotency_key: str
    attempt_count: int
    next_retry_at: datetime | None
    remote_post_id: str | None
    last_error: str | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PublishAttemptCreate(BaseModel):
    publish_job_id: int = Field(gt=0)
    attempt_no: int = Field(gt=0)
    status: PublishAttemptStatus
    request_json: dict[str, Any] = Field(default_factory=dict)
    response_json: dict[str, Any] = Field(default_factory=dict)
    error_type: str | None = Field(default=None, max_length=64)
    error_message: str | None = None
    latency_ms: int = Field(default=0, ge=0)
    started_at: datetime
    finished_at: datetime | None = None

    _aware_datetimes = field_validator("started_at", "finished_at", mode="after")(_require_aware)

    @model_validator(mode="after")
    def _finished_after_start(self) -> Self:
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not be before started_at")
        return self


class PublishAttemptRead(ORMModel):
    id: int
    publish_job_id: int
    attempt_no: int
    status: PublishAttemptStatus
    request_json: dict[str, Any]
    response_json: dict[str, Any]
    error_type: str | None
    error_message: str | None
    latency_ms: int
    started_at: datetime
    finished_at: datetime | None
    created_at: datetime


class AffiliateEventCreate(BaseModel):
    user_id: int = Field(gt=0)
    event_type: AffiliateEventType
    amount: Decimal = Field(
        default=Decimal(0),
        ge=0,
        max_digits=18,
        decimal_places=6,
        allow_inf_nan=False,
    )
    currency: str = Field(default="VND", min_length=3, max_length=8)
    source: str = Field(default="manual", min_length=1, max_length=64)
    external_event_id: str | None = Field(default=None, max_length=255)
    social_post_id: int | None = Field(default=None, gt=0)
    affiliate_product_id: int | None = Field(default=None, gt=0)
    social_account_id: int | None = Field(default=None, gt=0)
    publish_job_id: int | None = Field(default=None, gt=0)
    occurred_at: datetime
    metadata_json: dict[str, Any] = Field(default_factory=dict)

    _aware_occurred_at = field_validator("occurred_at", mode="after")(_require_aware)

    @model_validator(mode="after")
    def _commission_amount_only(self) -> Self:
        if self.event_type is AffiliateEventType.COMMISSION and self.amount <= 0:
            raise ValueError("commission events require amount > 0")
        if self.event_type is not AffiliateEventType.COMMISSION and self.amount != 0:
            raise ValueError("view/click events must have amount = 0")
        self.currency = self.currency.strip().upper()
        self.source = _clean_text(self.source)
        return self


class AffiliateEventRead(ORMModel):
    id: int
    user_id: int
    event_type: AffiliateEventType
    amount: Decimal
    currency: str
    source: str
    external_event_id: str | None
    social_post_id: int | None
    affiliate_product_id: int | None
    social_account_id: int | None
    publish_job_id: int | None
    occurred_at: datetime
    created_at: datetime
