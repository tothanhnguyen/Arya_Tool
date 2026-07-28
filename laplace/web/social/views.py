"""Local dashboard forms for the reviewed social publishing workflow."""

import secrets
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from starlette.datastructures import UploadFile

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
from laplace.web.social.api import (
    AffiliateProductIn,
    DraftIn,
    MockAccountIn,
    ScheduleIn,
    UserActionIn,
    request_owner_id,
)
from laplace.web.social.api import cancel_job as cancel_job_api
from laplace.web.social.api import create_affiliate_product as create_product_api
from laplace.web.social.api import create_draft as create_draft_api
from laplace.web.social.api import create_mock_account as create_mock_account_api
from laplace.web.social.api import schedule_content as schedule_content_api
from laplace.web.social.api import upload_media as upload_media_api

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
_CANCELLABLE_STATUSES = ("queued", "retry", "failed")
_SAFE_STATUS_CODES = frozenset({400, 404, 409, 413, 415})


def _page_context(active_social_nav: str, **values: object) -> dict[str, object]:
    return {
        "active_nav": "social",
        "active_social_nav": active_social_nav,
        **values,
    }


def _verify_csrf(form: object) -> None:
    token = str(form.get("csrf", ""))  # type: ignore[union-attr]
    if not secrets.compare_digest(token, CSRF_TOKEN):
        raise HTTPException(status_code=403, detail="CSRF token sai hoặc thiếu.")


def _positive_int(value: object, label: str) -> int:
    try:
        parsed = int(str(value or ""))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{label} phải là số nguyên dương.",
        ) from exc
    if parsed <= 0:
        raise HTTPException(status_code=400, detail=f"{label} phải là số nguyên dương.")
    return parsed


def _form_owner_id(request: Request, form: object) -> int:
    raw_user_id = str(form.get("user_id", "")).strip()  # type: ignore[union-attr]
    supplied_user_id = (
        _positive_int(raw_user_id, "Workspace user") if raw_user_id else None
    )
    owner_id = request_owner_id(request, supplied_user_id)
    if owner_id is None:
        raise HTTPException(status_code=400, detail="Workspace user là bắt buộc.")
    return owner_id


def _public_text(value: object, label: str, max_length: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > max_length:
        raise HTTPException(
            status_code=400,
            detail=f"{label} phải có từ 1 đến {max_length} ký tự.",
        )
    if any(ord(character) < 32 and character not in "\n\t" for character in normalized):
        raise HTTPException(status_code=400, detail=f"{label} chứa ký tự không hợp lệ.")
    return normalized


def _optional_positive_int(value: object, label: str) -> int | None:
    normalized = str(value or "").strip()
    return _positive_int(normalized, label) if normalized else None


def _hashtags(value: object) -> list[str]:
    raw = str(value or "").strip()
    if len(raw) > 1_000:
        raise HTTPException(status_code=400, detail="Hashtag vượt giới hạn nhập liệu.")
    tags = raw.replace(",", " ").split()
    if len(tags) > 30 or any(len(tag.lstrip("#")) > 64 for tag in tags):
        raise HTTPException(status_code=400, detail="Hashtag không hợp lệ.")
    return tags


def _form_values(form: object, *names: str) -> dict[str, str]:
    return {
        name: str(form.get(name, ""))  # type: ignore[union-attr]
        for name in names
    }


def _safe_form_error(exc: Exception, fallback: str) -> tuple[str, int]:
    """Return a public message without reflecting storage/auth exception text."""
    if isinstance(exc, HTTPException):
        status_code = exc.status_code if exc.status_code in _SAFE_STATUS_CODES else 502
        if status_code == 404:
            return "Không tìm thấy dữ liệu thuộc workspace user đã chọn.", status_code
        if status_code == 413:
            return "File vượt giới hạn dung lượng cho phép.", status_code
        if status_code == 415:
            return "File không đúng định dạng JPEG, PNG, WebP hoặc MP4.", status_code
        if status_code == 409:
            return fallback, status_code
        return "Dữ liệu form chưa hợp lệ.", status_code
    if isinstance(exc, (ValidationError, ValueError, ZoneInfoNotFoundError)):
        return "Dữ liệu form chưa hợp lệ.", 400
    return fallback, 502


def _parse_schedule(value: object) -> datetime:
    raw = _public_text(value, "Thời gian đăng", 64)
    try:
        scheduled_at = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Thời gian đăng không hợp lệ.") from exc
    if scheduled_at.tzinfo is None or scheduled_at.utcoffset() is None:
        scheduled_at = scheduled_at.replace(
            tzinfo=ZoneInfo(get_settings().social_timezone)
        )
    return scheduled_at


def get_content_generation_service() -> ContentGenerationService:
    return ContentGenerationService()


def _content_context(
    *,
    owner_id: int | None = None,
    generated_post_id: int | None = None,
    error: str | None = None,
    form_values: dict[str, str] | None = None,
    notice: str | None = None,
) -> dict[str, object]:
    posts_statement = (
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
    )
    users_statement = select(User.id)
    media_statement = select(
        MediaAsset.id,
        MediaAsset.user_id,
        MediaAsset.original_name,
        MediaAsset.type,
    )
    products_statement = select(
        AffiliateProduct.id,
        AffiliateProduct.user_id,
        AffiliateProduct.product_name,
        AffiliateProduct.merchant,
        AffiliateProduct.network,
    ).where(AffiliateProduct.status == "active")
    if owner_id is not None:
        posts_statement = posts_statement.where(
            SocialPost.user_id == owner_id,
            MediaAsset.user_id == owner_id,
            or_(
                AffiliateProduct.id.is_(None),
                AffiliateProduct.user_id == owner_id,
            ),
        )
        users_statement = users_statement.where(User.id == owner_id)
        media_statement = media_statement.where(MediaAsset.user_id == owner_id)
        products_statement = products_statement.where(
            AffiliateProduct.user_id == owner_id
        )
    with session_scope() as session:
        rows = session.execute(
            posts_statement
            .order_by(SocialPost.updated_at.desc(), SocialPost.id.desc())
        ).mappings().all()
        posts = [dict(row) for row in rows]
        users = [
            {"id": row.id, "label": f"User #{row.id}"}
            for row in session.execute(users_statement.order_by(User.id)).all()
        ]
        media_assets = [
            dict(row)
            for row in session.execute(
                media_statement.order_by(
                    MediaAsset.created_at.desc(),
                    MediaAsset.id.desc(),
                )
            ).mappings()
        ]
        products = [
            dict(row)
            for row in session.execute(
                products_statement.order_by(
                    AffiliateProduct.product_name,
                    AffiliateProduct.id,
                )
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
        notice=notice,
        form_values=form_values or {},
        csrf_token=CSRF_TOKEN,
        content_model=get_settings().social_content_model,
        openrouter_ready=bool(get_settings().openrouter_api_key),
    )


def _accounts_context(
    *,
    owner_id: int | None = None,
    error: str | None = None,
    form_values: dict[str, str] | None = None,
    notice: str | None = None,
) -> dict[str, object]:
    accounts_statement = select(
        SocialAccount.id,
        SocialAccount.user_id,
        SocialAccount.display_name,
        SocialAccount.platform,
        SocialAccount.auth_type,
        SocialAccount.status,
        SocialAccount.daily_post_limit,
        SocialAccount.timezone,
        SocialAccount.last_checked_at,
    )
    users_statement = select(User.id)
    if owner_id is not None:
        accounts_statement = accounts_statement.where(
            SocialAccount.user_id == owner_id
        )
        users_statement = users_statement.where(User.id == owner_id)
    with session_scope() as session:
        rows = session.execute(
            accounts_statement.order_by(SocialAccount.display_name, SocialAccount.id)
        ).mappings().all()
        accounts = [dict(row) for row in rows]
        users = [
            {"id": row.id, "label": f"User #{row.id}"}
            for row in session.execute(users_statement.order_by(User.id)).all()
        ]
    return _page_context(
        "accounts",
        accounts=accounts,
        users=users,
        csrf_token=CSRF_TOKEN,
        error=error,
        notice=notice,
        form_values=form_values or {},
    )


def _calendar_context(
    *,
    owner_id: int | None = None,
    error: str | None = None,
    form_values: dict[str, str] | None = None,
    notice: str | None = None,
) -> dict[str, object]:
    jobs_statement = (
        select(
            PublishJob.id,
            PublishJob.status,
            PublishJob.scheduled_at,
            PublishJob.attempt_count,
            SocialPost.user_id,
            SocialPost.title.label("post_title"),
            SocialAccount.display_name.label("account_name"),
            SocialAccount.platform,
        )
        .join(SocialPost, PublishJob.social_post_id == SocialPost.id)
        .join(SocialAccount, PublishJob.social_account_id == SocialAccount.id)
        .where(PublishJob.status.in_(_SCHEDULED_STATUSES))
    )
    posts_statement = select(
        SocialPost.id,
        SocialPost.user_id,
        SocialPost.title,
        SocialPost.status,
    ).where(SocialPost.status.in_(("approved", "published")))
    accounts_statement = select(
        SocialAccount.id,
        SocialAccount.user_id,
        SocialAccount.display_name,
        SocialAccount.platform,
    ).where(SocialAccount.status == "active")
    users_statement = select(User.id)
    if owner_id is not None:
        jobs_statement = jobs_statement.where(
            SocialPost.user_id == owner_id,
            SocialAccount.user_id == owner_id,
        )
        posts_statement = posts_statement.where(SocialPost.user_id == owner_id)
        accounts_statement = accounts_statement.where(
            SocialAccount.user_id == owner_id
        )
        users_statement = users_statement.where(User.id == owner_id)
    with session_scope() as session:
        rows = session.execute(
            jobs_statement.order_by(PublishJob.scheduled_at, PublishJob.id)
        ).mappings().all()
        jobs = [dict(row) for row in rows]
        posts = [
            dict(row)
            for row in session.execute(
                posts_statement.order_by(
                    SocialPost.updated_at.desc(),
                    SocialPost.id.desc(),
                )
            ).mappings()
        ]
        accounts = [
            dict(row)
            for row in session.execute(
                accounts_statement.order_by(
                    SocialAccount.display_name,
                    SocialAccount.id,
                )
            ).mappings()
        ]
        users = [
            {"id": row.id, "label": f"User #{row.id}"}
            for row in session.execute(users_statement.order_by(User.id)).all()
        ]
    return _page_context(
        "calendar",
        jobs=jobs,
        posts=posts,
        accounts=accounts,
        users=users,
        csrf_token=CSRF_TOKEN,
        schedule_timezone=get_settings().social_timezone,
        error=error,
        notice=notice,
        form_values=form_values or {},
    )


def _jobs_context(
    *,
    owner_id: int | None = None,
    status_filter: str | None = None,
    error: str | None = None,
    notice: str | None = None,
) -> dict[str, object]:
    statement = (
        select(
            PublishJob.id,
            PublishJob.status,
            PublishJob.scheduled_at,
            PublishJob.attempt_count,
            PublishJob.remote_post_id,
            PublishJob.published_at,
            SocialPost.user_id,
            SocialPost.title.label("post_title"),
            SocialAccount.display_name.label("account_name"),
            SocialAccount.platform,
        )
        .join(SocialPost, PublishJob.social_post_id == SocialPost.id)
        .join(SocialAccount, PublishJob.social_account_id == SocialAccount.id)
        .order_by(PublishJob.created_at.desc(), PublishJob.id.desc())
    )
    if status_filter:
        statement = statement.where(PublishJob.status == status_filter)
    if owner_id is not None:
        statement = statement.where(
            SocialPost.user_id == owner_id,
            SocialAccount.user_id == owner_id,
        )
    with session_scope() as session:
        rows = session.execute(statement).mappings().all()
        jobs = [dict(row) for row in rows]
    return _page_context(
        "jobs",
        jobs=jobs,
        status_filter=status_filter,
        csrf_token=CSRF_TOKEN,
        cancellable_statuses=_CANCELLABLE_STATUSES,
        error=error,
        notice=notice,
    )


@router.get("/", response_class=HTMLResponse, name="social_overview")
def social_overview(request: Request) -> HTMLResponse:
    """Render aggregate counts without exposing account credentials."""
    owner_id = request_owner_id(request, None)
    account_statement = select(func.count()).select_from(SocialAccount)
    content_statement = select(func.count()).select_from(SocialPost)
    scheduled_statement = (
        select(func.count())
        .select_from(PublishJob)
        .join(SocialPost, PublishJob.social_post_id == SocialPost.id)
        .join(SocialAccount, PublishJob.social_account_id == SocialAccount.id)
        .where(PublishJob.status.in_(_SCHEDULED_STATUSES))
    )
    failed_statement = (
        select(func.count())
        .select_from(PublishJob)
        .join(SocialPost, PublishJob.social_post_id == SocialPost.id)
        .join(SocialAccount, PublishJob.social_account_id == SocialAccount.id)
        .where(PublishJob.status == "failed")
    )
    if owner_id is not None:
        account_statement = account_statement.where(SocialAccount.user_id == owner_id)
        content_statement = content_statement.where(SocialPost.user_id == owner_id)
        scheduled_statement = scheduled_statement.where(
            SocialPost.user_id == owner_id,
            SocialAccount.user_id == owner_id,
        )
        failed_statement = failed_statement.where(
            SocialPost.user_id == owner_id,
            SocialAccount.user_id == owner_id,
        )
    with session_scope() as session:
        account_count = session.scalar(account_statement) or 0
        content_count = session.scalar(content_statement) or 0
        scheduled_count = session.scalar(scheduled_statement) or 0
        failed_count = session.scalar(failed_statement) or 0

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
def social_accounts(
    request: Request,
    created: bool = Query(default=False),
) -> HTMLResponse:
    """List safe account metadata and expose a credential-free mock form."""
    return templates.TemplateResponse(
        request,
        "social_accounts.html",
        _accounts_context(
            owner_id=request_owner_id(request, None),
            notice="Đã tạo mock account." if created else None,
        ),
    )


@router.post(
    "/accounts/mock",
    response_class=HTMLResponse,
    dependencies=[Depends(require_loopback)],
)
async def create_mock_account(request: Request):
    form = await request.form()
    owner_id = request_owner_id(request, None)
    form_values = _form_values(
        form,
        "user_id",
        "platform",
        "display_name",
        "external_id",
    )
    try:
        _verify_csrf(form)
        body = MockAccountIn(
            user_id=_form_owner_id(request, form),
            platform=_public_text(form.get("platform"), "Platform", 32),
            display_name=_public_text(form.get("display_name"), "Tên hiển thị", 200),
            external_id=_public_text(form.get("external_id"), "External ID", 255),
        )
        create_mock_account_api(request, body)
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        error, status_code = _safe_form_error(exc, "Không thể tạo mock account.")
        return templates.TemplateResponse(
            request,
            "social_accounts.html",
            _accounts_context(
                owner_id=owner_id,
                error=error,
                form_values=form_values,
            ),
            status_code=status_code,
        )
    except Exception as exc:
        error, status_code = _safe_form_error(exc, "Không thể tạo mock account.")
        return templates.TemplateResponse(
            request,
            "social_accounts.html",
            _accounts_context(
                owner_id=owner_id,
                error=error,
                form_values=form_values,
            ),
            status_code=status_code,
        )
    return RedirectResponse(url="/social/accounts?created=true", status_code=303)


@router.get("/content", response_class=HTMLResponse, name="social_content")
def social_content(
    request: Request,
    generated: int | None = Query(default=None, gt=0),
    created: str | None = Query(default=None, max_length=16),
) -> HTMLResponse:
    """Content Studio: generate reviewed drafts and list the content library."""
    notice_by_action = {
        "media": "Đã lưu media.",
        "product": "Đã tạo affiliate product.",
        "draft": "Đã tạo draft thủ công.",
        "approved": "Đã duyệt nội dung.",
    }
    return templates.TemplateResponse(
        request,
        "social_content.html",
        _content_context(
            owner_id=request_owner_id(request, None),
            generated_post_id=generated,
            notice=notice_by_action.get(created or ""),
        ),
    )


@router.post(
    "/media",
    response_class=HTMLResponse,
    dependencies=[Depends(require_loopback)],
)
async def upload_social_media(request: Request):
    form = await request.form()
    owner_id = request_owner_id(request, None)
    form_values = _form_values(form, "user_id")
    try:
        _verify_csrf(form)
        user_id = _form_owner_id(request, form)
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise HTTPException(status_code=400, detail="Cần chọn file media.")
        await upload_media_api(request=request, user_id=user_id, file=file)
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        error, status_code = _safe_form_error(exc, "Không thể lưu media.")
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(
                owner_id=owner_id,
                error=error,
                form_values=form_values,
            ),
            status_code=status_code,
        )
    except Exception as exc:
        error, status_code = _safe_form_error(exc, "Không thể lưu media.")
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(
                owner_id=owner_id,
                error=error,
                form_values=form_values,
            ),
            status_code=status_code,
        )
    return RedirectResponse(url="/social/content?created=media", status_code=303)


@router.post(
    "/products",
    response_class=HTMLResponse,
    dependencies=[Depends(require_loopback)],
)
async def create_affiliate_product(request: Request):
    form = await request.form()
    owner_id = request_owner_id(request, None)
    names = (
        "user_id",
        "network",
        "merchant",
        "product_name",
        "product_url",
        "affiliate_url",
    )
    form_values = _form_values(form, *names)
    try:
        _verify_csrf(form)
        body = AffiliateProductIn(
            user_id=_form_owner_id(request, form),
            network=_public_text(form.get("network"), "Affiliate network", 64),
            merchant=_public_text(form.get("merchant"), "Merchant", 200),
            product_name=_public_text(form.get("product_name"), "Tên sản phẩm", 500),
            product_url=_public_text(form.get("product_url"), "Product URL", 2_000),
            affiliate_url=_public_text(form.get("affiliate_url"), "Affiliate URL", 2_000),
        )
        create_product_api(request, body)
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        error, status_code = _safe_form_error(exc, "Không thể tạo affiliate product.")
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(
                owner_id=owner_id,
                error=error,
                form_values=form_values,
            ),
            status_code=status_code,
        )
    except Exception as exc:
        error, status_code = _safe_form_error(exc, "Không thể tạo affiliate product.")
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(
                owner_id=owner_id,
                error=error,
                form_values=form_values,
            ),
            status_code=status_code,
        )
    return RedirectResponse(url="/social/content?created=product", status_code=303)


@router.post(
    "/content/drafts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_loopback)],
)
async def create_social_draft(request: Request):
    form = await request.form()
    owner_id = request_owner_id(request, None)
    names = (
        "user_id",
        "title",
        "caption",
        "hashtags",
        "media_asset_id",
        "affiliate_product_id",
    )
    form_values = _form_values(form, *names)
    try:
        _verify_csrf(form)
        body = DraftIn(
            user_id=_form_owner_id(request, form),
            title=_public_text(form.get("title"), "Tiêu đề", 300),
            caption=_public_text(form.get("caption"), "Caption", 10_000),
            hashtags=_hashtags(form.get("hashtags")),
            media_asset_id=_positive_int(form.get("media_asset_id"), "Media"),
            affiliate_product_id=_optional_positive_int(
                form.get("affiliate_product_id"),
                "Affiliate product",
            ),
        )
        create_draft_api(request, body)
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        error, status_code = _safe_form_error(exc, "Không thể tạo draft.")
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(
                owner_id=owner_id,
                error=error,
                form_values=form_values,
            ),
            status_code=status_code,
        )
    except Exception as exc:
        error, status_code = _safe_form_error(exc, "Không thể tạo draft.")
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(
                owner_id=owner_id,
                error=error,
                form_values=form_values,
            ),
            status_code=status_code,
        )
    return RedirectResponse(url="/social/content?created=draft", status_code=303)


@router.post(
    "/content/generate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_loopback)],
)
async def generate_social_content(request: Request):
    form = await request.form()
    owner_id = request_owner_id(request, None)
    if not secrets.compare_digest(str(form.get("csrf", "")), CSRF_TOKEN):
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(
                owner_id=owner_id,
                error="CSRF token sai hoặc thiếu.",
            ),
            status_code=403,
        )
    form_values = {key: str(value) for key, value in form.items() if key != "csrf"}
    try:
        body = GenerateDraftRequest(
            user_id=_form_owner_id(request, form),
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
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(
                owner_id=owner_id,
                error="Dữ liệu tạo draft chưa hợp lệ.",
                form_values=form_values,
            ),
            status_code=400,
        )
    except MissingAPIKeyError:
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(
                owner_id=owner_id,
                error="Copywriter chưa được cấu hình; chưa tạo draft.",
                form_values=form_values,
            ),
            status_code=400,
        )
    except (ValidationError, ValueError):
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(
                owner_id=owner_id,
                error="Dữ liệu tạo draft chưa hợp lệ.",
                form_values=form_values,
            ),
            status_code=400,
        )
    except Exception:
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(
                owner_id=owner_id,
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
    owner_id = request_owner_id(request, None)
    if not secrets.compare_digest(str(form.get("csrf", "")), CSRF_TOKEN):
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(
                owner_id=owner_id,
                error="CSRF token sai hoặc thiếu.",
            ),
            status_code=403,
        )
    try:
        user_id = _form_owner_id(request, form)
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(
                owner_id=owner_id,
                error="Dữ liệu form chưa hợp lệ.",
            ),
            status_code=400,
        )
    result = run_social_content(
        SocialContentParams(action="approve", post_id=post_id),
        ToolContext(user_id=user_id),
    )
    if not result.ok:
        return templates.TemplateResponse(
            request,
            "social_content.html",
            _content_context(
                owner_id=owner_id,
                error="Không thể duyệt draft ở trạng thái hiện tại.",
            ),
            status_code=409,
        )
    return RedirectResponse(url="/social/content?created=approved", status_code=303)


@router.get("/calendar", response_class=HTMLResponse, name="social_calendar")
def social_calendar(
    request: Request,
    changed: str | None = Query(default=None, max_length=16),
) -> HTMLResponse:
    """Schedule reviewed content and list upcoming jobs."""
    notice_by_action = {
        "scheduled": "Đã thêm bài vào lịch đăng.",
        "cancelled": "Đã hủy publish job.",
    }
    return templates.TemplateResponse(
        request,
        "social_calendar.html",
        _calendar_context(
            owner_id=request_owner_id(request, None),
            notice=notice_by_action.get(changed or ""),
        ),
    )


@router.post(
    "/calendar/schedule",
    response_class=HTMLResponse,
    dependencies=[Depends(require_loopback)],
)
async def schedule_social_content(request: Request):
    form = await request.form()
    owner_id = request_owner_id(request, None)
    names = ("user_id", "social_post_id", "social_account_id", "scheduled_at")
    form_values = _form_values(form, *names)
    try:
        _verify_csrf(form)
        body = ScheduleIn(
            user_id=_form_owner_id(request, form),
            social_post_id=_positive_int(form.get("social_post_id"), "Nội dung"),
            social_account_id=_positive_int(
                form.get("social_account_id"),
                "Social account",
            ),
            scheduled_at=_parse_schedule(form.get("scheduled_at")),
        )
        schedule_content_api(request, body)
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        error, status_code = _safe_form_error(
            exc,
            "Không thể lên lịch với owner hoặc trạng thái đã chọn.",
        )
        return templates.TemplateResponse(
            request,
            "social_calendar.html",
            _calendar_context(
                owner_id=owner_id,
                error=error,
                form_values=form_values,
            ),
            status_code=status_code,
        )
    except Exception as exc:
        error, status_code = _safe_form_error(
            exc,
            "Không thể lên lịch với owner hoặc trạng thái đã chọn.",
        )
        return templates.TemplateResponse(
            request,
            "social_calendar.html",
            _calendar_context(
                owner_id=owner_id,
                error=error,
                form_values=form_values,
            ),
            status_code=status_code,
        )
    return RedirectResponse(url="/social/calendar?changed=scheduled", status_code=303)


def _cancel_publish_job(request: Request, job_id: int, user_id: int) -> None:
    cancel_job_api(request, job_id, UserActionIn(user_id=user_id))


@router.post(
    "/calendar/jobs/{job_id}/cancel",
    response_class=HTMLResponse,
    dependencies=[Depends(require_loopback)],
)
async def cancel_calendar_job(request: Request, job_id: int):
    form = await request.form()
    owner_id = request_owner_id(request, None)
    try:
        _verify_csrf(form)
        _cancel_publish_job(
            request,
            job_id,
            _form_owner_id(request, form),
        )
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        error, status_code = _safe_form_error(exc, "Không thể hủy publish job.")
        return templates.TemplateResponse(
            request,
            "social_calendar.html",
            _calendar_context(owner_id=owner_id, error=error),
            status_code=status_code,
        )
    except Exception as exc:
        error, status_code = _safe_form_error(exc, "Không thể hủy publish job.")
        return templates.TemplateResponse(
            request,
            "social_calendar.html",
            _calendar_context(owner_id=owner_id, error=error),
            status_code=status_code,
        )
    return RedirectResponse(url="/social/calendar?changed=cancelled", status_code=303)


@router.get("/jobs", response_class=HTMLResponse, name="social_jobs")
def social_jobs(
    request: Request,
    status: str | None = Query(default=None, max_length=24),
    changed: str | None = Query(default=None, max_length=16),
) -> HTMLResponse:
    """List publish history, optionally filtered by its exact status."""
    return templates.TemplateResponse(
        request,
        "social_jobs.html",
        _jobs_context(
            owner_id=request_owner_id(request, None),
            status_filter=status,
            notice="Đã hủy publish job." if changed == "cancelled" else None,
        ),
    )


@router.post(
    "/jobs/{job_id}/cancel",
    response_class=HTMLResponse,
    dependencies=[Depends(require_loopback)],
)
async def cancel_history_job(request: Request, job_id: int):
    form = await request.form()
    owner_id = request_owner_id(request, None)
    status_filter = str(form.get("status_filter", "")).strip() or None
    if status_filter and len(status_filter) > 24:
        status_filter = None
    try:
        _verify_csrf(form)
        _cancel_publish_job(
            request,
            job_id,
            _form_owner_id(request, form),
        )
    except HTTPException as exc:
        if exc.status_code == 403:
            raise
        error, status_code = _safe_form_error(exc, "Không thể hủy publish job.")
        return templates.TemplateResponse(
            request,
            "social_jobs.html",
            _jobs_context(
                owner_id=owner_id,
                status_filter=status_filter,
                error=error,
            ),
            status_code=status_code,
        )
    except Exception as exc:
        error, status_code = _safe_form_error(exc, "Không thể hủy publish job.")
        return templates.TemplateResponse(
            request,
            "social_jobs.html",
            _jobs_context(
                owner_id=owner_id,
                status_filter=status_filter,
                error=error,
            ),
            status_code=status_code,
        )
    return RedirectResponse(
        url="/social/jobs?changed=cancelled",
        status_code=303,
    )


@router.get("/affiliate", name="social_affiliate")
def social_affiliate() -> RedirectResponse:
    """Keep the social workspace URL while reusing the affiliate analytics page."""
    return RedirectResponse(url="/stats", status_code=307)


@router.get("/settings", name="social_settings")
def social_settings() -> RedirectResponse:
    """Keep the social workspace URL while reusing the shared settings page."""
    return RedirectResponse(url="/settings", status_code=307)
