"""Authenticated JSON API for the local social affiliate workflow."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from laplace.config import get_settings
from laplace.db import session_scope
from laplace.llm.base import MissingAPIKeyError
from laplace.models import User
from laplace.social.content_generation import (
    ContentGenerationService,
    GenerateDraftRequest,
)
from laplace.social.models import (
    AffiliateEvent,
    AffiliateProduct,
    MediaAsset,
    PublishJob,
    SocialAccount,
    SocialPost,
)
from laplace.social.schemas import AffiliateEventCreate
from laplace.social.storage import (
    StorageConfigurationError,
    StorageError,
    get_media_storage,
)
from laplace.social.styles import STYLES
from laplace.tools.base import ToolContext
from laplace.tools.social_content import SocialContentParams, social_content
from laplace.tools.social_schedule import SocialScheduleParams, social_schedule
from laplace.web.deps import require_api_key

router = APIRouter(
    prefix="/api/social",
    tags=["social"],
    dependencies=[Depends(require_api_key)],
)

_ALLOWED_MEDIA = {
    "image/jpeg": (".jpg", "image"),
    "image/png": (".png", "image"),
    "image/webp": (".webp", "image"),
    "video/mp4": (".mp4", "video"),
}


def _matches_media_signature(content_type: str, payload: bytes) -> bool:
    if content_type == "image/jpeg":
        return payload.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return payload.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/webp":
        return (
            len(payload) >= 12
            and payload.startswith(b"RIFF")
            and payload[8:12] == b"WEBP"
        )
    if content_type == "video/mp4":
        return len(payload) >= 12 and payload[4:8] == b"ftyp"
    return False


class MockAccountIn(BaseModel):
    user_id: int = Field(gt=0)
    platform: Literal["facebook", "instagram", "tiktok"]
    display_name: str = Field(min_length=1, max_length=200)
    external_id: str = Field(min_length=1, max_length=255)


class AffiliateProductIn(BaseModel):
    user_id: int = Field(gt=0)
    network: str = Field(min_length=1, max_length=64)
    merchant: str = Field(min_length=1, max_length=200)
    product_name: str = Field(min_length=1, max_length=500)
    product_url: HttpUrl
    affiliate_url: HttpUrl


class DraftIn(BaseModel):
    user_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=300)
    caption: str = Field(min_length=1, max_length=10_000)
    hashtags: list[str] = Field(default_factory=list, max_length=30)
    media_asset_id: int = Field(gt=0)
    affiliate_product_id: int | None = Field(default=None, gt=0)


class UserActionIn(BaseModel):
    user_id: int = Field(gt=0)


class ScheduleIn(BaseModel):
    user_id: int = Field(gt=0)
    social_post_id: int = Field(gt=0)
    social_account_id: int = Field(gt=0)
    scheduled_at: datetime


def _require_user(user_id: int) -> None:
    with session_scope() as session:
        if session.get(User, user_id) is None:
            raise HTTPException(status_code=404, detail="User không tồn tại")


def _tool_data(result) -> dict:
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.error or "Thao tác thất bại")
    return result.data or {}


def get_content_generation_service() -> ContentGenerationService:
    return ContentGenerationService()


def _validate_event_ownership(session, body: AffiliateEventCreate) -> None:
    checks = (
        (body.social_post_id, SocialPost, "Social post"),
        (body.affiliate_product_id, AffiliateProduct, "Affiliate product"),
        (body.social_account_id, SocialAccount, "Social account"),
    )
    for resource_id, model, label in checks:
        if resource_id is None:
            continue
        owned = session.scalar(
            select(model.id).where(model.id == resource_id, model.user_id == body.user_id)
        )
        if owned is None:
            raise HTTPException(status_code=404, detail=f"{label} không tồn tại")

    if body.publish_job_id is not None:
        owned_job = session.scalar(
            select(PublishJob.id)
            .join(SocialPost, PublishJob.social_post_id == SocialPost.id)
            .join(SocialAccount, PublishJob.social_account_id == SocialAccount.id)
            .where(
                PublishJob.id == body.publish_job_id,
                SocialPost.user_id == body.user_id,
                SocialAccount.user_id == body.user_id,
            )
        )
        if owned_job is None:
            raise HTTPException(status_code=404, detail="Publish job không tồn tại")


@router.post("/accounts/mock", status_code=status.HTTP_201_CREATED)
def create_mock_account(body: MockAccountIn) -> dict:
    """Create a credential-free local account for end-to-end mock testing."""
    _require_user(body.user_id)
    with session_scope() as session:
        existing = session.scalar(
            select(SocialAccount).where(
                SocialAccount.platform == body.platform,
                SocialAccount.external_id == body.external_id,
            )
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="Account đã tồn tại")
        account = SocialAccount(
            user_id=body.user_id,
            platform=body.platform,
            display_name=body.display_name.strip(),
            external_id=body.external_id.strip(),
            auth_type="mock",
            auth_ref=f"mock://{body.external_id.strip()}",
            daily_post_limit=get_settings().social_daily_post_limit,
            timezone=get_settings().social_timezone,
        )
        session.add(account)
        session.flush()
        return {
            "id": account.id,
            "platform": account.platform,
            "display_name": account.display_name,
            "status": account.status,
            "auth_type": account.auth_type,
        }


@router.post("/media", status_code=status.HTTP_201_CREATED)
async def upload_media(user_id: int, file: Annotated[UploadFile, File()]) -> dict:
    _require_user(user_id)
    content_type = (file.content_type or "").lower()
    media_config = _ALLOWED_MEDIA.get(content_type)
    if media_config is None:
        raise HTTPException(
            status_code=415,
            detail="Chỉ hỗ trợ JPEG, PNG, WebP và MP4",
        )
    settings = get_settings()
    payload = await file.read(settings.social_media_max_bytes + 1)
    await file.close()
    if not payload:
        raise HTTPException(status_code=400, detail="File rỗng")
    if len(payload) > settings.social_media_max_bytes:
        raise HTTPException(status_code=413, detail="File vượt giới hạn dung lượng")
    if not _matches_media_signature(content_type, payload):
        raise HTTPException(
            status_code=415,
            detail="Nội dung file không khớp MIME đã khai báo",
        )

    digest = hashlib.sha256(payload).hexdigest()
    suffix, media_type = media_config
    with session_scope() as session:
        existing = session.scalar(
            select(MediaAsset).where(
                MediaAsset.user_id == user_id,
                MediaAsset.sha256 == digest,
            )
        )
        if existing is not None:
            return {
                "id": existing.id,
                "type": existing.type,
                "original_name": existing.original_name,
                "mime_type": existing.mime_type,
                "size_bytes": existing.size_bytes,
                "storage_backend": existing.storage_backend,
                "duplicate": True,
            }

    try:
        storage = get_media_storage()
        stored = await run_in_threadpool(
            storage.store,
            user_id=user_id,
            digest=digest,
            suffix=suffix,
            payload=payload,
            content_type=content_type,
        )
    except StorageConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        with session_scope() as session:
            asset = MediaAsset(
                user_id=user_id,
                type=media_type,
                local_path=stored.location,
                storage_backend=stored.backend,
                storage_bucket=stored.bucket,
                storage_key=stored.key,
                original_name=Path(file.filename or f"upload{suffix}").name,
                mime_type=content_type,
                size_bytes=len(payload),
                sha256=digest,
            )
            session.add(asset)
            session.flush()
            response = {
                "id": asset.id,
                "type": asset.type,
                "original_name": asset.original_name,
                "mime_type": asset.mime_type,
                "size_bytes": asset.size_bytes,
                "storage_backend": asset.storage_backend,
                "duplicate": False,
            }
        return response
    except IntegrityError:
        # Two identical uploads can race. The content-addressed object is shared,
        # so keep it and return the row committed by the other request.
        with session_scope() as session:
            existing = session.scalar(
                select(MediaAsset).where(
                    MediaAsset.user_id == user_id,
                    MediaAsset.sha256 == digest,
                )
            )
            if existing is None:
                raise
            return {
                "id": existing.id,
                "type": existing.type,
                "original_name": existing.original_name,
                "mime_type": existing.mime_type,
                "size_bytes": existing.size_bytes,
                "storage_backend": existing.storage_backend,
                "duplicate": True,
            }
    except Exception:
        if stored.created:
            try:
                await run_in_threadpool(storage.delete, stored)
            except StorageError:
                pass
        raise


@router.post("/products", status_code=status.HTTP_201_CREATED)
def create_affiliate_product(body: AffiliateProductIn) -> dict:
    _require_user(body.user_id)
    with session_scope() as session:
        product = AffiliateProduct(
            user_id=body.user_id,
            network=body.network.strip(),
            merchant=body.merchant.strip(),
            product_name=body.product_name.strip(),
            product_url=str(body.product_url),
            affiliate_url=str(body.affiliate_url),
        )
        session.add(product)
        session.flush()
        return {
            "id": product.id,
            "network": product.network,
            "merchant": product.merchant,
            "product_name": product.product_name,
            "status": product.status,
        }


@router.post("/events", status_code=status.HTTP_201_CREATED)
def record_affiliate_event(body: AffiliateEventCreate) -> dict:
    """Record normalized view, click or commission data from an approved source."""
    _require_user(body.user_id)
    if body.currency != get_settings().social_currency.upper():
        raise HTTPException(
            status_code=409,
            detail=f"Dashboard hiện dùng tiền tệ {get_settings().social_currency.upper()}",
        )
    with session_scope() as session:
        _validate_event_ownership(session, body)
        if body.external_event_id is not None:
            existing = session.scalar(
                select(AffiliateEvent.id).where(
                    AffiliateEvent.source == body.source,
                    AffiliateEvent.external_event_id == body.external_event_id,
                )
            )
            if existing is not None:
                raise HTTPException(status_code=409, detail="Event đã được ghi nhận")
        event = AffiliateEvent(
            user_id=body.user_id,
            social_post_id=body.social_post_id,
            affiliate_product_id=body.affiliate_product_id,
            social_account_id=body.social_account_id,
            publish_job_id=body.publish_job_id,
            event_type=body.event_type.value,
            amount=body.amount,
            currency=body.currency,
            source=body.source,
            external_event_id=body.external_event_id,
            metadata_json=body.metadata_json,
            occurred_at=body.occurred_at,
        )
        session.add(event)
        session.flush()
        return {
            "id": event.id,
            "event_type": event.event_type,
            "amount": event.amount,
            "currency": event.currency,
            "occurred_at": event.occurred_at,
        }


@router.post("/content/drafts", status_code=status.HTTP_201_CREATED)
def create_draft(body: DraftIn) -> dict:
    _require_user(body.user_id)
    return _tool_data(
        social_content(
            SocialContentParams(
                action="create_draft",
                title=body.title,
                caption=body.caption,
                hashtags=body.hashtags,
                media_asset_id=body.media_asset_id,
                affiliate_product_id=body.affiliate_product_id,
            ),
            ToolContext(user_id=body.user_id),
        )
    )


@router.get("/content/styles")
def list_writing_styles() -> dict:
    return {"styles": [style.public_dict() for style in STYLES.values()]}


@router.post("/content/generate", status_code=status.HTTP_201_CREATED)
async def generate_content_draft(body: GenerateDraftRequest) -> dict:
    """Generate one validated draft with the dedicated OpenRouter Free writer."""
    try:
        generated = await run_in_threadpool(
            get_content_generation_service().generate_draft,
            body,
        )
    except MissingAPIKeyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="Content model tạm thời không khả dụng; chưa tạo draft.",
        ) from exc
    return generated.as_dict()


@router.post("/content/{post_id}/approve")
def approve_content(post_id: int, body: UserActionIn) -> dict:
    return _tool_data(
        social_content(
            SocialContentParams(action="approve", post_id=post_id),
            ToolContext(user_id=body.user_id),
        )
    )


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
def schedule_content(body: ScheduleIn) -> dict:
    return _tool_data(
        social_schedule(
            SocialScheduleParams(
                action="create",
                social_post_id=body.social_post_id,
                social_account_id=body.social_account_id,
                scheduled_at=body.scheduled_at,
            ),
            ToolContext(user_id=body.user_id),
        )
    )


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, body: UserActionIn) -> dict:
    return _tool_data(
        social_schedule(
            SocialScheduleParams(action="cancel", job_id=job_id),
            ToolContext(user_id=body.user_id),
        )
    )
