"""Owner-scoped affiliate traffic and commission dashboard at ``/stats``."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from laplace.config import get_settings
from laplace.db import session_scope
from laplace.models import User
from laplace.social.analytics import collect_affiliate_analytics
from laplace.web.deps import require_api_key
from laplace.web.traceview import templates

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_api_key)])


def _dashboard_user_id(request: Request, session) -> int | None:
    """Use the authenticated owner when available, else the local MVP owner."""
    authenticated = getattr(request.state, "user_id", None)
    if isinstance(authenticated, int) and authenticated > 0:
        return session.scalar(select(User.id).where(User.id == authenticated))
    return session.scalar(select(User.id).order_by(User.id).limit(1))


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
