"""Read-only social dashboard views for the affiliate MVP."""

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func, select

from laplace.config import get_settings
from laplace.db import session_scope
from laplace.llm.base import MissingAPIKeyError
from laplace.models import User
from laplace.social.content_generation import (
    ContentGenerationService,
    GenerateDraftRequest,
)
from laplace.social.models import (
    AffiliateProduct,
    ContentGeneration,
    MediaAsset,
    PublishJob,
    SocialAccount,
    SocialPost,
)
from laplace.social.styles import STYLES
from laplace.tools.base import ToolContext
from laplace.tools.social_content import SocialContentParams
from laplace.tools.social_content import social_content as run_social_content
from laplace.web.deps import require_api_key
from laplace.web.settings_page import CSRF_TOKEN, require_loopback

_SOCIAL_TEMPLATES_DIR = Path(__file__).parent / "templates"
_SHARED_TEMPLATES_DIR = Path(__file__).parents[1] / "templates"

# Search the feature folder first, then the existing shared template folder so
# social pages can extend the application's base shell without moving files.
templates = Jinja2Templates(
    directory=[str(_SOCIAL_TEMPLATES_DIR), str(_SHARED_TEMPLATES_DIR)]
)

router = APIRouter(
    prefix="/social",
    tags=["social"],
    include_in_schema=False,
    dependencies=[Depends(require_api_key)],
)

_SCHEDULED_STATUSES = ("queued", "retry")


def _page_context(active_social_nav: str, **values: object) -> dict[str, object]:
    return {
        "active_nav": "social",
        "active_social_nav": active_social_nav,
        **values,
    }


def get_content_generation_service() -> ContentGenerationService:
    return ContentGenerationService()


def _content_context(
    *,
    generated_post_id: int | None = None,
    error: str | None = None,
    form_values: dict[str, str] | None = None,
) -> dict[str, object]:
    with session_scope() as session:
        rows = session.execute(
            select(
                SocialPost.id,
                SocialPost.user_id,
                SocialPost.title,
                SocialPost.caption,
                SocialPost.hashtags_json,
                SocialPost.status,
                SocialPost.updated_at,
                MediaAsset.original_name.label("media_name"),
                MediaAsset.type.label("media_type"),
                AffiliateProduct.product_name,
                AffiliateProduct.network,
                ContentGeneration.style_id,
                ContentGeneration.model.label("generation_model"),
                ContentGeneration.quality_json,
            )
            .join(MediaAsset, SocialPost.media_asset_id == MediaAsset.id)
            .outerjoin(
                AffiliateProduct,
                SocialPost.affiliate_product_id == AffiliateProduct.id,
            )
            .outerjoin(
                ContentGeneration,
                ContentGeneration.social_post_id == SocialPost.id,
            )
            .order_by(SocialPost.updated_at.desc(), SocialPost.id.desc())
        ).mappings().all()
        posts = [dict(row) for row in rows]
        users = [
            {"id": row.id, "label": f"User #{row.id}"}
            for row in session.execute(select(User.id).order_by(User.id)).all()
        ]
        media_assets = [
            dict(row)
            for row in session.execute(
                select(
                    MediaAsset.id,
                    MediaAsset.user_id,
                    MediaAsset.original_name,
                    MediaAsset.type,
                ).order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc())
            ).mappings()
        ]
        products = [
            dict(row)
            for row in session.execute(
                select(
                    AffiliateProduct.id,
                    AffiliateProduct.user_id,
                    AffiliateProduct.product_name,
                    AffiliateProduct.merchant,
                    AffiliateProduct.network,
                )
                .where(AffiliateProduct.status == "active")
                .order_by(AffiliateProduct.product_name, AffiliateProduct.id)
            ).mappings()
        ]

    generated_post = next(
        (post for post in posts if post["id"] == generated_post_id),
        None,
    )
    return _page_context(
        "content",
        posts=posts,
        users=users,
        media_assets=media_assets,
        products=products,
        styles=[style.public_dict() for style in STYLES.values()],
        generated_post=generated_post,
        error=error,
        form_values=form_values or {},
        csrf_token=CSRF_TOKEN,
        content_model=get_settings().social_content_model,
        openrouter_ready=bool(get_settings().openrouter_api_key),
    )


@router.get("/", response_class=HTMLResponse, name="social_overview")
def social_overview(request: Request) -> HTMLResponse:
    """Render aggregate counts without exposing account credentials."""
    with session_scope() as session:
        account_count = session.scalar(select(func.count()).select_from(SocialAccount)) or 0
        content_count = session.scalar(select(func.count()).select_from(SocialPost)) or 0
        scheduled_count = (
            session.scalar(
                select(func.count())
                .select_from(PublishJob)
                .where(PublishJob.status.in_(_SCHEDULED_STATUSES))
            )
            or 0
        )
        failed_count = (
            session.scalar(
                select(func.count()).select_from(PublishJob).where(PublishJob.status == "failed")
            )
            or 0
        )

    summary_cards = (
        {
            "label": "Accounts",
            "value": account_count,
            "description": "Page hoặc tài khoản đã kết nối",
            "href": "/social/accounts",
        },
        {
            "label": "Content",
            "value": content_count,
            "description": "Nội dung trong thư viện",
            "href": "/social/content",
        },
        {
            "label": "Scheduled",
            "value": scheduled_count,
            "description": "Bài đang chờ đến lịch",
            "href": "/social/calendar",
        },
        {
            "label": "Failed",
            "value": failed_count,
            "description": "Tác vụ cần kiểm tra",
            "href": "/social/jobs?status=failed",
        },
    )
    return templates.TemplateResponse(
        request,
        "social_overview.html",
        _page_context("overview", summary_cards=summary_cards),
    )


@router.get("/accounts", response_class=HTMLResponse, name="social_accounts")
def social_accounts(request: Request) -> HTMLResponse:
    """List safe account metadata; authentication references are never queried."""
    with session_scope() as session:
        rows = session.execute(
            select(
                SocialAccount.id,
                SocialAccount.display_name,
                SocialAccount.platform,
                SocialAccount.auth_type,
                SocialAccount.status,
                SocialAccount.daily_post_limit,
                SocialAccount.timezone,
                SocialAccount.last_checked_at,
            ).order_by(SocialAccount.display_name, SocialAccount.id)
        ).mappings().all()
        accounts = [dict(row) for row in rows]

    return templates.TemplateResponse(
        request,
        "social_accounts.html",
        _page_context("accounts", accounts=accounts),
    )


@router.get("/content", response_class=HTMLResponse, name="social_content")
def social_content(
    request: Request,
    generated: int | None = Query(default=None, gt=0),
) -> HTMLResponse:
    """Content Studio: generate reviewed drafts and list the content library."""
    return templates.TemplateResponse(
        request,
        "social_content.html",
        _content_context(generated_post_id=generated),
    )


@router.post(
    "/content/generate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_loopback)],
)
async def generate_social_content(request: Request):
    form = await request.form()
    if not secrets.compare_digest(str(form.get("csrf", "")), CSRF_TOKEN):
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(error="CSRF token sai hoặc thiếu."),
            status_code=403,
        )
    form_values = {key: str(value) for key, value in form.items() if key != "csrf"}
    try:
        body = GenerateDraftRequest(
            user_id=int(str(form.get("user_id", "0"))),
            media_asset_id=int(str(form.get("media_asset_id", "0"))),
            affiliate_product_id=int(str(form.get("affiliate_product_id", "0"))),
            style_id=str(form.get("style_id", "")),
            platform=str(form.get("platform", "facebook")),
            audience=str(form.get("audience", "")),
            product_facts=str(form.get("product_facts", "")),
        )
        generated = await run_in_threadpool(
            get_content_generation_service().generate_draft,
            body,
        )
    except (ValidationError, ValueError, MissingAPIKeyError) as exc:
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(error=str(exc), form_values=form_values),
            status_code=400,
        )
    except Exception:
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(
                error="OpenRouter tạm thời không khả dụng; chưa tạo draft.",
                form_values=form_values,
            ),
            status_code=502,
        )
    return RedirectResponse(
        url=f"/social/content?generated={generated.post_id}",
        status_code=303,
    )


@router.post(
    "/content/{post_id}/approve",
    dependencies=[Depends(require_loopback)],
)
async def approve_social_content(request: Request, post_id: int):
    form = await request.form()
    if not secrets.compare_digest(str(form.get("csrf", "")), CSRF_TOKEN):
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(error="CSRF token sai hoặc thiếu."),
            status_code=403,
        )
    try:
        user_id = int(str(form.get("user_id", "0")))
    except ValueError:
        user_id = 0
    result = run_social_content(
        SocialContentParams(action="approve", post_id=post_id),
        ToolContext(user_id=user_id),
    )
    if not result.ok:
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(error=result.error or "Không thể duyệt draft."),
            status_code=409,
        )
    return RedirectResponse(url="/social/content", status_code=303)


@router.get("/calendar", response_class=HTMLResponse, name="social_calendar")
def social_calendar(request: Request) -> HTMLResponse:
    """List queued and retrying publish jobs in chronological order."""
    with session_scope() as session:
        rows = session.execute(
            select(
                PublishJob.id,
                PublishJob.status,
                PublishJob.scheduled_at,
                PublishJob.attempt_count,
                SocialPost.title.label("post_title"),
                SocialAccount.display_name.label("account_name"),
                SocialAccount.platform,
            )
            .join(SocialPost, PublishJob.social_post_id == SocialPost.id)
            .join(SocialAccount, PublishJob.social_account_id == SocialAccount.id)
            .where(PublishJob.status.in_(_SCHEDULED_STATUSES))
            .order_by(PublishJob.scheduled_at, PublishJob.id)
        ).mappings().all()
        jobs = [dict(row) for row in rows]

    return templates.TemplateResponse(
        request,
        "social_calendar.html",
        _page_context("calendar", jobs=jobs),
    )


@router.get("/jobs", response_class=HTMLResponse, name="social_jobs")
def social_jobs(
    request: Request,
    status: str | None = Query(default=None, max_length=24),
) -> HTMLResponse:
    """List publish history, optionally filtered by its exact status."""
    statement = (
        select(
            PublishJob.id,
            PublishJob.status,
            PublishJob.scheduled_at,
            PublishJob.attempt_count,
            PublishJob.remote_post_id,
            PublishJob.published_at,
            SocialPost.title.label("post_title"),
            SocialAccount.display_name.label("account_name"),
            SocialAccount.platform,
        )
        .join(SocialPost, PublishJob.social_post_id == SocialPost.id)
        .join(SocialAccount, PublishJob.social_account_id == SocialAccount.id)
        .order_by(PublishJob.created_at.desc(), PublishJob.id.desc())
    )
    if status:
        statement = statement.where(PublishJob.status == status)

    with session_scope() as session:
        rows = session.execute(statement).mappings().all()
        jobs = [dict(row) for row in rows]

    return templates.TemplateResponse(
        request,
        "social_jobs.html",
        _page_context("jobs", jobs=jobs, status_filter=status),
    )


@router.get("/affiliate", name="social_affiliate")
def social_affiliate() -> RedirectResponse:
    """Keep the social workspace URL while reusing the affiliate analytics page."""
    return RedirectResponse(url="/stats", status_code=307)


@router.get("/settings", name="social_settings")
def social_settings() -> RedirectResponse:
    """Keep the social workspace URL while reusing the shared settings page."""
    return RedirectResponse(url="/settings", status_code=307)
