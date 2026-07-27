"""Agent tool for the reviewed social-content lifecycle."""

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from laplace.db import session_scope
from laplace.schemas import ToolResult
from laplace.social.models import AffiliateProduct, MediaAsset, PublishJob, SocialPost
from laplace.tools.base import ToolContext, tool

logger = logging.getLogger(__name__)


class SocialContentParams(BaseModel):
    action: Literal["list", "create_draft", "approve", "delete"]
    post_id: int | None = Field(default=None, gt=0, description="Required for approve/delete")
    status: Literal["draft", "approved", "scheduled", "published", "failed"] | None = Field(
        default=None, description="Optional list filter"
    )
    title: str = Field(default="", max_length=300)
    caption: str = Field(default="", max_length=10_000)
    hashtags: list[str] = Field(default_factory=list, max_length=30)
    media_asset_id: int | None = Field(default=None, gt=0)
    affiliate_product_id: int | None = Field(default=None, gt=0)

    @field_validator("hashtags")
    @classmethod
    def _normalise_hashtags(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            tag = raw.strip().lstrip("#").strip()
            if not tag or any(char.isspace() for char in tag):
                raise ValueError("hashtags must be non-blank and contain no whitespace")
            tag = f"#{tag}"
            key = tag.casefold()
            if key not in seen:
                seen.add(key)
                result.append(tag)
        return result

    @model_validator(mode="after")
    def _required_fields_for_action(self) -> "SocialContentParams":
        if self.action in ("approve", "delete") and self.post_id is None:
            raise ValueError(f"post_id is required for action='{self.action}'")
        if self.action == "create_draft":
            if not self.title.strip():
                raise ValueError("title is required for action='create_draft'")
            if not self.caption.strip():
                raise ValueError("caption is required for action='create_draft'")
            if self.media_asset_id is None:
                raise ValueError("media_asset_id is required for action='create_draft'")
        return self


def _content_hash(params: SocialContentParams) -> str:
    canonical = json.dumps(
        {
            "title": params.title.strip(),
            "caption": params.caption.strip(),
            "hashtags": params.hashtags,
            "media_asset_id": params.media_asset_id,
            "affiliate_product_id": params.affiliate_product_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _post_dict(post: SocialPost) -> dict:
    return {
        "id": post.id,
        "title": post.title,
        "caption": post.caption,
        "hashtags": post.hashtags_json,
        "media_asset_id": post.media_asset_id,
        "affiliate_product_id": post.affiliate_product_id,
        "status": post.status,
        "content_hash": post.content_hash,
        "approved_at": post.approved_at.isoformat() if post.approved_at else None,
        "created_at": post.created_at.isoformat() if post.created_at else None,
        "updated_at": post.updated_at.isoformat() if post.updated_at else None,
    }


@tool(
    name="social_content",
    description=(
        "Manage the user's social content library. list optionally filters by status. "
        "create_draft requires title, caption and media_asset_id, with optional hashtags and "
        "affiliate_product_id. approve/delete require post_id and user confirmation. "
        "Only approved content can later be scheduled."
    ),
    params=SocialContentParams,
    confirm_when=lambda p: p.action in ("approve", "delete"),
)
def social_content(params: SocialContentParams, ctx: ToolContext) -> ToolResult:
    try:
        with session_scope() as session:
            if params.action == "list":
                query = select(SocialPost).where(SocialPost.user_id == ctx.user_id)
                if params.status is not None:
                    query = query.where(SocialPost.status == params.status)
                posts = session.scalars(query.order_by(SocialPost.id)).all()
                return ToolResult(ok=True, data={"posts": [_post_dict(post) for post in posts]})

            if params.action == "create_draft":
                media = session.scalar(
                    select(MediaAsset).where(
                        MediaAsset.id == params.media_asset_id,
                        MediaAsset.user_id == ctx.user_id,
                    )
                )
                if media is None:
                    return ToolResult(
                        ok=False, error=f"Media asset {params.media_asset_id} not found"
                    )

                if params.affiliate_product_id is not None:
                    product = session.scalar(
                        select(AffiliateProduct).where(
                            AffiliateProduct.id == params.affiliate_product_id,
                            AffiliateProduct.user_id == ctx.user_id,
                        )
                    )
                    if product is None:
                        return ToolResult(
                            ok=False,
                            error=f"Affiliate product {params.affiliate_product_id} not found",
                        )
                    if product.status != "active":
                        return ToolResult(
                            ok=False,
                            error=(
                                f"Affiliate product {product.id} is not active "
                                f"(status='{product.status}')"
                            ),
                        )
                    valid_until = product.valid_until
                    if valid_until is not None:
                        # SQLite can deserialize timezone-aware values as naive.
                        expiry = (
                            valid_until.replace(tzinfo=UTC)
                            if valid_until.tzinfo is None
                            else valid_until
                        )
                        if expiry <= datetime.now(UTC):
                            return ToolResult(
                                ok=False, error=f"Affiliate product {product.id} has expired"
                            )

                post = SocialPost(
                    user_id=ctx.user_id,
                    title=params.title.strip(),
                    caption=params.caption.strip(),
                    hashtags_json=params.hashtags,
                    media_asset_id=media.id,
                    affiliate_product_id=params.affiliate_product_id,
                    status="draft",
                    content_hash=_content_hash(params),
                )
                session.add(post)
                session.flush()
                return ToolResult(ok=True, data=_post_dict(post))

            post = session.scalar(
                select(SocialPost).where(
                    SocialPost.id == params.post_id,
                    SocialPost.user_id == ctx.user_id,
                )
            )
            if post is None:
                return ToolResult(ok=False, error=f"Social post {params.post_id} not found")

            if params.action == "approve":
                if post.status != "draft":
                    return ToolResult(
                        ok=False,
                        error=f"Social post {post.id} cannot be approved from status='{post.status}'",
                    )
                post.status = "approved"
                post.approved_at = datetime.now(UTC)
                session.flush()
                return ToolResult(ok=True, data=_post_dict(post))

            if post.status in ("scheduled", "published"):
                return ToolResult(
                    ok=False,
                    error=f"Social post {post.id} cannot be deleted from status='{post.status}'",
                )
            has_live_job = session.scalar(
                select(PublishJob.id)
                .where(
                    PublishJob.social_post_id == post.id,
                    PublishJob.status.not_in(("cancelled", "failed")),
                )
                .limit(1)
            )
            if has_live_job is not None:
                return ToolResult(
                    ok=False, error=f"Social post {post.id} has a non-cancelled publish job"
                )
            session.delete(post)
            return ToolResult(ok=True, data={"deleted_id": post.id})
    except Exception as exc:
        logger.exception("social_content failed")
        return ToolResult(ok=False, error=f"social_content failed: {exc}")

