"""Luồng kết nối Instagram cục bộ qua browser profile tách biệt.

Form chỉ nhận metadata công khai của tài khoản. Arya_Tool không nhận cookie,
access token hay mật khẩu và không dùng luồng này để tự động đăng bài.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from laplace.db import session_scope
from laplace.models import User
from laplace.social.instagram_profile import (
    InstagramBrowserUnavailableError,
    InstagramProfileError,
    InstagramProfileInfo,
    InstagramProfileManager,
    InstagramProfileOwnershipError,
)
from laplace.social.models import SocialAccount
from laplace.web.deps import require_api_key
from laplace.web.settings_page import CSRF_TOKEN, require_loopback

_TEMPLATES = Jinja2Templates(
    directory=[
        str(Path(__file__).parent / "templates"),
        str(Path(__file__).parents[1] / "templates"),
    ]
)
_DEFAULT_PROFILE_ROOT = Path.home() / ".arya-tool" / "instagram-profiles"
_LOCAL_USER_ID = 1
_PLATFORM = "instagram"
_AUTH_TYPE = "browser_profile"

router = APIRouter(
    prefix="/social/instagram",
    tags=["social-instagram"],
    include_in_schema=False,
    dependencies=[Depends(require_api_key), Depends(require_loopback)],
)


def get_instagram_profile_manager() -> InstagramProfileManager:
    """Tạo manager; dependency riêng giúp kiểm thử mà không mở trình duyệt thật."""

    return InstagramProfileManager(_DEFAULT_PROFILE_ROOT)


InstagramProfileManagerDep = Annotated[
    InstagramProfileManager,
    Depends(get_instagram_profile_manager),
]


def _verify_csrf(form: object) -> None:
    token = str(form.get("csrf", ""))  # type: ignore[union-attr]
    if not secrets.compare_digest(token, CSRF_TOKEN):
        raise HTTPException(status_code=403, detail="CSRF token sai hoặc thiếu.")


def _positive_int(value: object, label: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{label} không hợp lệ.") from exc
    if parsed <= 0:
        raise HTTPException(status_code=400, detail=f"{label} không hợp lệ.")
    return parsed


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


def _expected_auth_ref(account_id: int, user_id: int) -> str:
    return f"browser-profile://instagram/{user_id}/{account_id}"


def _owned_account(account_id: int, user_id: int) -> SocialAccount:
    with session_scope() as session:
        account = session.scalar(
            select(SocialAccount).where(
                SocialAccount.id == account_id,
                SocialAccount.user_id == user_id,
                SocialAccount.platform == _PLATFORM,
                SocialAccount.auth_type == _AUTH_TYPE,
            )
        )
        if account is None:
            raise HTTPException(
                status_code=404,
                detail="Không tìm thấy tài khoản Instagram thuộc user này.",
            )
        if account.auth_ref != _expected_auth_ref(account.id, user_id):
            raise HTTPException(
                status_code=409,
                detail="Tham chiếu browser profile không hợp lệ; đã dừng để bảo vệ tài khoản.",
            )
        session.expunge(account)
        return account


def _manager_error(exc: Exception) -> HTTPException:
    if isinstance(exc, InstagramBrowserUnavailableError):
        return HTTPException(
            status_code=503,
            detail="Không tìm thấy Chrome hoặc Chromium để mở Instagram.",
        )
    if isinstance(exc, InstagramProfileOwnershipError | PermissionError):
        return HTTPException(
            status_code=403,
            detail="Browser profile không thuộc user đã chọn.",
        )
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail="ID browser profile không hợp lệ.")
    if isinstance(exc, InstagramProfileError):
        return HTTPException(
            status_code=409,
            detail="Browser profile chưa sẵn sàng cho thao tác này.",
        )
    return HTTPException(
        status_code=409,
        detail="Không thể cập nhật browser profile ở trạng thái hiện tại.",
    )


def _safe_profile(info: InstagramProfileInfo) -> dict[str, str]:
    return {
        "account_id": info.account_id,
        "user_id": info.user_id,
        "login_url": info.login_url,
        "status": info.status,
    }


def _page_context(
    *,
    account: SocialAccount | None = None,
    profile: InstagramProfileInfo | None = None,
    error: str | None = None,
    user_id: int = _LOCAL_USER_ID,
) -> dict[str, object]:
    return {
        "active_nav": "social",
        "active_social_nav": "accounts",
        "csrf_token": CSRF_TOKEN,
        "account": (
            {
                "id": account.id,
                "user_id": account.user_id,
                "display_name": account.display_name,
                "external_id": account.external_id,
                "status": account.status,
            }
            if account is not None
            else None
        ),
        "profile": _safe_profile(profile) if profile is not None else None,
        "error": error,
        "user_id": user_id,
    }


def _redirect(account_id: int, user_id: int) -> RedirectResponse:
    query = urlencode({"user_id": user_id, "account_id": account_id})
    return RedirectResponse(f"/social/instagram/connect?{query}", status_code=303)


@router.get("/connect", response_class=HTMLResponse, name="instagram_connect")
def connect_page(
    request: Request,
    manager: InstagramProfileManagerDep,
    user_id: int | None = Query(default=None, gt=0),
    account_id: int | None = Query(default=None, gt=0),
) -> HTMLResponse:
    account = None
    profile = None
    error = None
    if user_id is not None or account_id is not None:
        if user_id is None or account_id is None:
            error = "Cần đủ User ID và Account ID để xem trạng thái."
        else:
            try:
                account = _owned_account(account_id, user_id)
                profile = manager.status(str(account.id), str(user_id))
            except HTTPException as exc:
                error = str(exc.detail)
            except Exception as exc:
                error = str(_manager_error(exc).detail)

    return _TEMPLATES.TemplateResponse(
        request,
        "social_instagram_connect.html",
        _page_context(
            account=account,
            profile=profile,
            error=error,
            user_id=user_id or _LOCAL_USER_ID,
        ),
    )


@router.post("/prepare")
async def prepare_account(
    request: Request,
    manager: InstagramProfileManagerDep,
) -> RedirectResponse:
    form = await request.form()
    _verify_csrf(form)
    user_id = _positive_int(form.get("user_id"), "User ID")
    display_name = _public_text(form.get("display_name"), "Tên hiển thị", 200)
    external_id = _public_text(form.get("external_id"), "Instagram ID công khai", 255)

    try:
        with session_scope() as session:
            user = session.get(User, user_id)
            if user is None:
                if user_id == _LOCAL_USER_ID:
                    session.add(
                        User(
                            id=_LOCAL_USER_ID,
                            profile_json={"source": "arya_local_dashboard"},
                        )
                    )
                    session.flush()
                else:
                    raise HTTPException(status_code=404, detail="User không tồn tại.")
            duplicate = session.scalar(
                select(SocialAccount.id).where(
                    SocialAccount.platform == _PLATFORM,
                    SocialAccount.external_id == external_id,
                )
            )
            if duplicate is not None:
                raise HTTPException(status_code=409, detail="Tài khoản Instagram đã tồn tại.")
            account = SocialAccount(
                user_id=user_id,
                platform=_PLATFORM,
                display_name=display_name,
                external_id=external_id,
                auth_type=_AUTH_TYPE,
                auth_ref="browser-profile://instagram/pending",
                status="login_required",
            )
            session.add(account)
            session.flush()
            account.auth_ref = _expected_auth_ref(account.id, user_id)
            await run_in_threadpool(manager.prepare, str(account.id), str(user_id))
            account_id = account.id
    except HTTPException:
        raise
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Tài khoản Instagram đã tồn tại.") from exc
    except Exception as exc:
        raise _manager_error(exc) from exc
    return _redirect(account_id, user_id)


@router.post("/{account_id}/launch")
async def launch_login(
    account_id: int,
    request: Request,
    manager: InstagramProfileManagerDep,
) -> RedirectResponse:
    form = await request.form()
    _verify_csrf(form)
    user_id = _positive_int(form.get("user_id"), "User ID")
    account = _owned_account(account_id, user_id)
    try:
        await run_in_threadpool(manager.launch_login, str(account.id), str(user_id))
    except Exception as exc:
        raise _manager_error(exc) from exc
    return _redirect(account.id, user_id)


@router.post("/{account_id}/ready")
async def mark_ready(
    account_id: int,
    request: Request,
    manager: InstagramProfileManagerDep,
) -> RedirectResponse:
    form = await request.form()
    _verify_csrf(form)
    user_id = _positive_int(form.get("user_id"), "User ID")
    account = _owned_account(account_id, user_id)
    try:
        await run_in_threadpool(manager.mark_ready, str(account.id), str(user_id))
    except Exception as exc:
        raise _manager_error(exc) from exc

    with session_scope() as session:
        owned = session.scalar(
            select(SocialAccount).where(
                SocialAccount.id == account.id,
                SocialAccount.user_id == user_id,
                SocialAccount.platform == _PLATFORM,
                SocialAccount.auth_type == _AUTH_TYPE,
                SocialAccount.auth_ref == _expected_auth_ref(account.id, user_id),
            )
        )
        if owned is None:
            raise HTTPException(
                status_code=409,
                detail="Ownership của tài khoản đã thay đổi; chưa kích hoạt.",
            )
        owned.status = "active"
        owned.last_checked_at = datetime.now(UTC)
    return _redirect(account.id, user_id)
