"""SQLAlchemy models for the local social publishing domain.

Authentication material is intentionally represented only by ``auth_ref``.
The value points at an external secret store (for example macOS Keychain) or a
dedicated browser profile; raw cookies and access tokens do not belong in this
database.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from laplace.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class SocialAccount(Base):
    __tablename__ = "social_accounts"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_social_account_external_id"),
        CheckConstraint("daily_post_limit > 0", name="ck_social_account_daily_limit"),
        CheckConstraint("cooldown_seconds >= 0", name="ck_social_account_cooldown"),
        Index("ix_social_accounts_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    platform: Mapped[str] = mapped_column(String(32))
    display_name: Mapped[str] = mapped_column(String(200))
    external_id: Mapped[str] = mapped_column(String(255))
    auth_type: Mapped[str] = mapped_column(String(32))
    auth_ref: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(24), default="active")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Ho_Chi_Minh")
    daily_post_limit: Mapped[int] = mapped_column(Integer, default=2)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    publish_jobs: Mapped[list["PublishJob"]] = relationship(back_populates="social_account")


class MediaAsset(Base):
    __tablename__ = "media_assets"
    __table_args__ = (
        UniqueConstraint("user_id", "sha256", name="uq_media_asset_user_sha256"),
        CheckConstraint("size_bytes >= 0", name="ck_media_asset_size"),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0",
            name="ck_media_asset_duration",
        ),
        CheckConstraint(
            "("
            "storage_backend = 'local' "
            "AND storage_bucket IS NULL "
            "AND storage_key IS NULL"
            ") OR ("
            "storage_backend = 'supabase' "
            "AND storage_bucket IS NOT NULL "
            "AND storage_key IS NOT NULL"
            ")",
            name="ck_media_asset_storage",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(16))
    local_path: Mapped[str] = mapped_column(Text)
    storage_backend: Mapped[str] = mapped_column(String(16), default="local")
    storage_bucket: Mapped[str | None] = mapped_column(String(100), nullable=True)
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_name: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    posts: Mapped[list["SocialPost"]] = relationship(back_populates="media_asset")


class AffiliateProduct(Base):
    __tablename__ = "affiliate_products"
    __table_args__ = (Index("ix_affiliate_products_user_status", "user_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    network: Mapped[str] = mapped_column(String(64))
    merchant: Mapped[str] = mapped_column(String(200))
    product_name: Mapped[str] = mapped_column(String(500))
    product_url: Mapped[str] = mapped_column(Text)
    affiliate_url: Mapped[str] = mapped_column(Text)
    sub_id_template: Mapped[str] = mapped_column(String(500), default="{sub_id}")
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    posts: Mapped[list["SocialPost"]] = relationship(back_populates="affiliate_product")


class SocialPost(Base):
    __tablename__ = "social_posts"
    __table_args__ = (
        Index("ix_social_posts_user_status", "user_id", "status"),
        Index("ix_social_posts_content_hash", "content_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    caption: Mapped[str] = mapped_column(Text)
    hashtags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    media_asset_id: Mapped[int] = mapped_column(ForeignKey("media_assets.id"))
    affiliate_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("affiliate_products.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), default="draft")
    content_hash: Mapped[str] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    media_asset: Mapped[MediaAsset] = relationship(back_populates="posts")
    affiliate_product: Mapped[AffiliateProduct | None] = relationship(back_populates="posts")
    publish_jobs: Mapped[list["PublishJob"]] = relationship(back_populates="social_post")


class PublishJob(Base):
    __tablename__ = "publish_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_publish_job_idempotency_key"),
        CheckConstraint("attempt_count >= 0", name="ck_publish_job_attempt_count"),
        Index("ix_publish_jobs_due", "status", "scheduled_at", "next_retry_at"),
        Index("ix_publish_jobs_account_status", "social_account_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    social_post_id: Mapped[int] = mapped_column(ForeignKey("social_posts.id"), index=True)
    social_account_id: Mapped[int] = mapped_column(ForeignKey("social_accounts.id"), index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    idempotency_key: Mapped[str] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_post_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    social_post: Mapped[SocialPost] = relationship(back_populates="publish_jobs")
    social_account: Mapped[SocialAccount] = relationship(back_populates="publish_jobs")
    attempts: Mapped[list["PublishAttempt"]] = relationship(
        back_populates="publish_job", order_by="PublishAttempt.attempt_no"
    )


class PublishAttempt(Base):
    __tablename__ = "publish_attempts"
    __table_args__ = (
        UniqueConstraint("publish_job_id", "attempt_no", name="uq_publish_attempt_number"),
        CheckConstraint("attempt_no > 0", name="ck_publish_attempt_number"),
        CheckConstraint("latency_ms >= 0", name="ck_publish_attempt_latency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    publish_job_id: Mapped[int] = mapped_column(ForeignKey("publish_jobs.id"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))
    # These summaries must be redacted by the publisher before persistence.
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    publish_job: Mapped[PublishJob] = relationship(back_populates="attempts")


class AffiliateEvent(Base):
    """Normalized traffic/conversion event used by the affiliate dashboard."""

    __tablename__ = "affiliate_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('view', 'click', 'commission')",
            name="ck_affiliate_event_type",
        ),
        CheckConstraint("amount >= 0", name="ck_affiliate_event_amount"),
        UniqueConstraint(
            "user_id",
            "source",
            "external_event_id",
            name="uq_affiliate_event_owner_source_external",
        ),
        Index("ix_affiliate_events_user_occurred", "user_id", "occurred_at"),
        Index("ix_affiliate_events_type_occurred", "event_type", "occurred_at"),
        Index("ix_affiliate_events_post_type", "social_post_id", "event_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    social_post_id: Mapped[int | None] = mapped_column(
        ForeignKey("social_posts.id"), nullable=True
    )
    affiliate_product_id: Mapped[int | None] = mapped_column(
        ForeignKey("affiliate_products.id"), nullable=True
    )
    social_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("social_accounts.id"), nullable=True
    )
    publish_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("publish_jobs.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(24))
    amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 6),
        default=Decimal(0),
    )
    currency: Mapped[str] = mapped_column(String(8), default="VND")
    source: Mapped[str] = mapped_column(String(64), default="manual")
    external_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ContentGeneration(Base):
    """Audit metadata for an AI-generated social draft; prompts are not stored."""

    __tablename__ = "content_generations"
    __table_args__ = (
        UniqueConstraint("social_post_id", name="uq_content_generation_post"),
        CheckConstraint("retries >= 0", name="ck_content_generation_retries"),
        CheckConstraint("prompt_tokens >= 0", name="ck_content_generation_prompt_tokens"),
        CheckConstraint(
            "completion_tokens >= 0",
            name="ck_content_generation_completion_tokens",
        ),
        CheckConstraint("latency_ms >= 0", name="ck_content_generation_latency"),
        CheckConstraint("cost_usd >= 0", name="ck_content_generation_cost"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    social_post_id: Mapped[int] = mapped_column(ForeignKey("social_posts.id"), index=True)
    style_id: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(200))
    prompt_fingerprint: Mapped[str] = mapped_column(String(64))
    quality_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
