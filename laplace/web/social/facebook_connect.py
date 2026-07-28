"""Local-only Facebook browser-profile connection flow.

The flow deliberately does not accept cookies or access tokens. It only prepares
an isolated browser profile, opens Facebook for an explicit user-driven login,
and records that the operator has finished that login.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from laplace.config import get_settings
from laplace.db import session_scope
from laplace.models import User
from laplace.social.browser_profile import (
    BrowserProfileError,
    BrowserProfileInfo,
    BrowserProfileManager,
    BrowserProfileOwnershipError,
    BrowserUnavailableError,
)
from laplace.social.models import SocialAccount
from laplace.web.deps import require_api_key
from laplace.web.settings_page import CSRF_TOKEN, require_loopback
from laplace.web.social.api import (
    request_owner_id,
    require_authenticated_json_mutation,
)

_TEMPLATES = Jinja2Templates(
    directory=[
        str(Path(__file__).parent / "templates"),
        str(Path(__file__).parents[1] / "templates"),
    ]
)
_DEFAULT_PROFILE_ROOT = Path.home() / ".arya-tool" / "facebook-profiles"
_LOCAL_USER_ID = 1

router = APIRouter(
    prefix="/social/facebook",
    tags=["social-facebook"],
    include_in_schema=False,
    dependencies=[Depends(require_api_key), Depends(require_loopback)],
)

api_router = APIRouter(
    prefix="/api/social/facebook",
    tags=["social-facebook"],
    dependencies=[
        Depends(require_api_key),
        Depends(require_loopback),
        Depends(require_authenticated_json_mutation),
    ],
)


class PrepareProfileIn(BaseModel):
    user_id: int = Field(gt=0)
    display_name: str = Field(min_length=1, max_length=200)
    external_id: str = Field(min_length=1, max_length=255)


class ProfileOwnerIn(BaseModel):
    user_id: int = Field(gt=0)


def get_browser_profile_manager() -> BrowserProfileManager:
    """Build the local profile manager.

    Kept as a FastAPI dependency so deployments can select another private
    base directory without changing route behavior.
    """

    return BrowserProfileManager(_DEFAULT_PROFILE_ROOT)


ProfileManagerDep = Annotated[
    BrowserProfileManager,
    Depends(get_browser_profile_manager),
]


def _require_csrf_header(
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    if not secrets.compare_digest(x_csrf_token or "", CSRF_TOKEN):
        raise HTTPException(status_code=403, detail="CSRF token sai hoặc thiếu.")


def _positive_int(value: object, label: str) -> int:
    try:
        parsed = int(str(value or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{label} phải là số nguyên dương.") from exc
    if parsed <= 0:
        raise HTTPException(status_code=400, detail=f"{label} phải là số nguyên dương.")
    return parsed


def _form_owner_id(request: Request, form: object) -> int:
    raw_user_id = str(form.get("user_id", "")).strip()  # type: ignore[union-attr]
    supplied_user_id = _positive_int(raw_user_id, "User ID") if raw_user_id else None
    owner_id = request_owner_id(request, supplied_user_id)
    if owner_id is None:
        raise HTTPException(status_code=400, detail="User ID là bắt buộc.")
    return owner_id


def _public_text(value: object, label: str, max_length: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > max_length:
        raise HTTPException(
            status_code=400,
            detail=f"{label} phải có từ 1 đến {max_length} ký tự.",
        )
    if any(ord(character) < 32 for character in normalized):
        raise HTTPException(status_code=400, detail=f"{label} chứa ký tự không hợp lệ.")
    return normalized


def _verify_form_csrf(form: object) -> None:
    token = str(form.get("csrf", ""))  # type: ignore[union-attr]
    if not secrets.compare_digest(token, CSRF_TOKEN):
        raise HTTPException(status_code=403, detail="CSRF token sai hoặc thiếu.")


def _public_profile(info: BrowserProfileInfo) -> dict[str, str]:
    """Return safe metadata only; never expose the local profile directory."""

    return {
        "account_id": info.account_id,
        "user_id": info.user_id,
        "status": info.status,
        "login_url": info.login_url,
    }


def _public_account(account: SocialAccount) -> dict[str, object]:
    return {
        "social_account_id": account.id,
        "display_name": account.display_name,
        "external_id": account.external_id,
        "account_status": account.status,
    }


def _manager_error(exc: Exception) -> HTTPException:
    if isinstance(exc, BrowserUnavailableError):
        return HTTPException(
            status_code=503,
            detail="Không tìm thấy Chrome hoặc Chromium để mở browser profile.",
        )
    if isinstance(exc, BrowserProfileOwnershipError):
        return HTTPException(
            status_code=403,
            detail="Browser profile không thuộc user đã chọn.",
        )
    if isinstance(exc, FileNotFoundError):
        return HTTPException(
            status_code=404,
            detail="Browser profile chưa được chuẩn bị cho tài khoản này.",
        )
    if isinstance(exc, PermissionError):
        return HTTPException(
            status_code=403,
            detail="Browser profile không thuộc user đã chọn.",
        )
    if isinstance(exc, ValueError):
        return HTTPException(
            status_code=400,
            detail="Account ID hoặc User ID không hợp lệ.",
        )
    if isinstance(exc, BrowserProfileError):
        return HTTPException(
            status_code=409,
            detail="Browser profile chưa sẵn sàng cho thao tác này.",
        )
    return HTTPException(
        status_code=409,
        detail="Không thể cập nhật browser profile ở trạng thái hiện tại.",
    )


def _owned_account(session: Session, social_account_id: int, user_id: int) -> SocialAccount:
    account = session.scalar(
        select(SocialAccount).where(
            SocialAccount.id == social_account_id,
            SocialAccount.user_id == user_id,
            SocialAccount.platform == "facebook",
            SocialAccount.auth_type == "browser_profile",
        )
    )
    if account is None:
        raise HTTPException(
            status_code=404,
            detail="Không tìm thấy Facebook account thuộc user đã chọn.",
        )
    return account


async def _prepare_account(
    manager: BrowserProfileManager,
    *,
    user_id: int,
    display_name: str,
    external_id: str,
    allow_local_user_create: bool = False,
) -> tuple[BrowserProfileInfo, dict[str, object]]:
    settings = get_settings()
    try:
        with session_scope() as session:
            user = session.get(User, user_id)
            if user is None:
                if allow_local_user_create and user_id == _LOCAL_USER_ID:
                    session.add(
                        User(
                            id=_LOCAL_USER_ID,
                            profile_json={"source": "arya_local_dashboard"},
                        )
                    )
                    session.flush()
                else:
                    raise HTTPException(status_code=404, detail="User không tồn tại.")
            existing = session.scalar(
                select(SocialAccount).where(
                    SocialAccount.platform == "facebook",
                    SocialAccount.external_id == external_id,
                )
            )
            if existing is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Facebook account này đã tồn tại.",
                )

            account = SocialAccount(
                user_id=user_id,
                platform="facebook",
                display_name=display_name,
                external_id=external_id,
                auth_type="browser_profile",
                auth_ref="browser-profile://pending",
                status="login_required",
                daily_post_limit=settings.social_daily_post_limit,
                cooldown_seconds=1800,
                timezone=settings.social_timezone,
            )
            session.add(account)
            session.flush()
            account.auth_ref = f"browser-profile://{user_id}/{account.id}"
            try:
                info = await run_in_threadpool(
                    manager.prepare,
                    str(account.id),
                    str(user_id),
                )
            except Exception as exc:
                raise _manager_error(exc) from exc
            account_data = _public_account(account)
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="Facebook external ID đã được kết nối.",
        ) from exc
    return info, account_data


async def _launch_account(
    manager: BrowserProfileManager,
    *,
    social_account_id: int,
    user_id: int,
) -> tuple[BrowserProfileInfo, dict[str, object]]:
    with session_scope() as session:
        account = _owned_account(session, social_account_id, user_id)
        try:
            info = await run_in_threadpool(
                manager.launch_login,
                str(account.id),
                str(user_id),
            )
        except Exception as exc:
            raise _manager_error(exc) from exc
        return info, _public_account(account)


async def _ready_account(
    manager: BrowserProfileManager,
    *,
    social_account_id: int,
    user_id: int,
) -> tuple[BrowserProfileInfo, dict[str, object]]:
    with session_scope() as session:
        account = _owned_account(session, social_account_id, user_id)
        try:
            current = await run_in_threadpool(
                manager.status,
                str(account.id),
                str(user_id),
            )
            if current.status != "login_pending":
                raise HTTPException(
                    status_code=409,
                    detail="Hãy mở Facebook và tự đăng nhập trước khi xác nhận sẵn sàng.",
                )
            info = await run_in_threadpool(
                manager.mark_ready,
                str(account.id),
                str(user_id),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise _manager_error(exc) from exc
        account.status = "active"
        account.last_checked_at = datetime.now(UTC)
        return info, _public_account(account)


def _page_context(
    *,
    profile: BrowserProfileInfo | None = None,
    account: dict[str, object] | None = None,
    error: str | None = None,
    user_id: int | str = "",
    display_name: str = "",
    external_id: str = "",
) -> dict[str, object]:
    return {
        "active_nav": "social",
        "active_social_nav": "accounts",
        "csrf_token": CSRF_TOKEN,
        "profile": _public_profile(profile) if profile is not None else None,
        "account": account,
        "error": error,
        "user_id": user_id,
        "display_name": display_name,
        "external_id": external_id,
    }


def _redirect_to_profile(info: BrowserProfileInfo) -> RedirectResponse:
    query = urlencode({"user_id": info.user_id, "social_account_id": info.account_id})
    return RedirectResponse(f"/social/facebook/connect?{query}", status_code=303)


@router.get("/connect", response_class=HTMLResponse, name="facebook_connect")
def connect_page(
    request: Request,
    manager: ProfileManagerDep,
    user_id: int | None = Query(default=None, gt=0),
    social_account_id: int | None = Query(default=None, gt=0),
) -> HTMLResponse:
    resolved_user_id = request_owner_id(request, user_id)
    profile = None
    account_data = None
    error = None
    if resolved_user_id is not None or social_account_id is not None:
        try:
            if resolved_user_id is None or social_account_id is None:
                raise HTTPException(
                    status_code=400,
                    detail="Thiếu User ID hoặc Social Account ID.",
                )
            with session_scope() as session:
                account = _owned_account(
                    session,
                    social_account_id,
                    resolved_user_id,
                )
                account_data = _public_account(account)
            profile = manager.status(str(social_account_id), str(resolved_user_id))
        except HTTPException as exc:
            if (
                request_owner_id(request, None) is not None
                and exc.status_code in {403, 404}
            ):
                raise
            error = str(exc.detail)
        except Exception as exc:
            error = _manager_error(exc).detail

    return _TEMPLATES.TemplateResponse(
        request,
        "social_facebook_connect.html",
        _page_context(
            profile=profile,
            account=account_data,
            error=error,
            user_id=resolved_user_id or _LOCAL_USER_ID,
        ),
    )


@router.post("/connect/prepare")
async def prepare_profile_form(
    request: Request,
    manager: ProfileManagerDep,
) -> RedirectResponse:
    form = await request.form()
    _verify_form_csrf(form)
    user_id = _form_owner_id(request, form)
    display_name = _public_text(form.get("display_name"), "Tên hiển thị", 200)
    external_id = _public_text(form.get("external_id"), "Facebook external ID", 255)
    info, _ = await _prepare_account(
        manager,
        user_id=user_id,
        display_name=display_name,
        external_id=external_id,
        allow_local_user_create=request_owner_id(request, None) is None,
    )
    return _redirect_to_profile(info)


@router.post("/connect/{social_account_id}/launch")
async def launch_login_form(
    social_account_id: int,
    request: Request,
    manager: ProfileManagerDep,
) -> RedirectResponse:
    form = await request.form()
    _verify_form_csrf(form)
    user_id = _form_owner_id(request, form)
    info, _ = await _launch_account(
        manager,
        social_account_id=social_account_id,
        user_id=user_id,
    )
    return _redirect_to_profile(info)


@router.post("/connect/{social_account_id}/ready")
async def mark_ready_form(
    social_account_id: int,
    request: Request,
    manager: ProfileManagerDep,
) -> RedirectResponse:
    form = await request.form()
    _verify_form_csrf(form)
    user_id = _form_owner_id(request, form)
    info, _ = await _ready_account(
        manager,
        social_account_id=social_account_id,
        user_id=user_id,
    )
    return _redirect_to_profile(info)


@api_router.post(
    "/connect/prepare",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_csrf_header)],
)
async def prepare_profile_api(
    request: Request,
    body: PrepareProfileIn,
    manager: ProfileManagerDep,
) -> dict[str, object]:
    owner_id = request_owner_id(request, body.user_id)
    assert owner_id is not None
    display_name = _public_text(body.display_name, "Tên hiển thị", 200)
    external_id = _public_text(body.external_id, "Facebook external ID", 255)
    info, account = await _prepare_account(
        manager,
        user_id=owner_id,
        display_name=display_name,
        external_id=external_id,
    )
    return {**_public_profile(info), **account}


@api_router.post(
    "/connect/{social_account_id}/launch",
    dependencies=[Depends(_require_csrf_header)],
)
async def launch_login_api(
    request: Request,
    social_account_id: int,
    body: ProfileOwnerIn,
    manager: ProfileManagerDep,
) -> dict[str, object]:
    owner_id = request_owner_id(request, body.user_id)
    assert owner_id is not None
    info, account = await _launch_account(
        manager,
        social_account_id=social_account_id,
        user_id=owner_id,
    )
    return {**_public_profile(info), **account}


@api_router.post(
    "/connect/{social_account_id}/ready",
    dependencies=[Depends(_require_csrf_header)],
)
async def mark_ready_api(
    request: Request,
    social_account_id: int,
    body: ProfileOwnerIn,
    manager: ProfileManagerDep,
) -> dict[str, object]:
    owner_id = request_owner_id(request, body.user_id)
    assert owner_id is not None
    info, account = await _ready_account(
        manager,
        social_account_id=social_account_id,
        user_id=owner_id,
    )
    return {**_public_profile(info), **account}


@api_router.get("/connect/{social_account_id}/status")
def profile_status_api(
    request: Request,
    social_account_id: int,
    manager: ProfileManagerDep,
    user_id: int = Query(gt=0),
) -> dict[str, object]:
    owner_id = request_owner_id(request, user_id)
    assert owner_id is not None
    try:
        with session_scope() as session:
            account = _owned_account(session, social_account_id, owner_id)
            account_data = _public_account(account)
        info = manager.status(str(social_account_id), str(owner_id))
    except HTTPException:
        raise
    except Exception as exc:
        raise _manager_error(exc) from exc
    return {**_public_profile(info), **account_data}
