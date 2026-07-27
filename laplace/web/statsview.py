"""Owner-scoped affiliate traffic and commission dashboard at ``/stats``."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from laplace.config import get_settings
from laplace.db import session_scope
from laplace.models import User
from laplace.social.analytics import collect_affiliate_analytics
from laplace.web.deps import require_api_key
from laplace.web.traceview import templates

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_api_key)])
_MISSING_OWNER = object()


def _dashboard_user_id(request: Request, session: Session) -> int | None:
    """Resolve one unambiguous owner or reject an unsafe dashboard request."""
    authenticated = getattr(request.state, "user_id", _MISSING_OWNER)
    if authenticated is not _MISSING_OWNER:
        if type(authenticated) is not int or authenticated <= 0:
            raise HTTPException(
                status_code=403,
                detail="Dashboard owner context không hợp lệ.",
            )
        owner_id = session.scalar(select(User.id).where(User.id == authenticated))
        if owner_id is None:
            raise HTTPException(
                status_code=403,
                detail="Dashboard owner context không hợp lệ.",
            )
        return owner_id

    owner_ids = tuple(
        session.scalars(select(User.id).order_by(User.id).limit(2)).all()
    )
    if not owner_ids:
        return None
    if len(owner_ids) == 1:
        return owner_ids[0]
    raise HTTPException(
        status_code=403,
        detail="Không xác định được owner cho dashboard nhiều user.",
    )


@router.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request) -> HTMLResponse:
    settings = get_settings()
    with session_scope() as session:
        data = collect_affiliate_analytics(
            session,
            user_id=_dashboard_user_id(request, session),
            currency=settings.social_currency,
            timezone_name=settings.social_timezone,
        )
    return templates.TemplateResponse(
        request,
        "stats.html",
        {**data, "active_nav": "stats"},
    )
