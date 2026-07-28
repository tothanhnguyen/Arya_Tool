"""FastAPI dependencies dung chung cho API va trace viewer."""

from fastapi import Header, HTTPException, Request

from laplace.config import get_settings
from laplace.web import supabase_auth


def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> None:
    """Require Supabase user auth, or the explicit local API-key fallback."""
    settings = get_settings()
    if settings.supabase_auth_enabled:
        try:
            supabase_auth.authenticate_dashboard_request(request, settings)
        except (
            supabase_auth.MissingAuthSession,
            supabase_auth.InvalidAuthSession,
        ) as exc:
            raise HTTPException(
                status_code=401,
                detail="Supabase Auth session is missing, invalid, or expired.",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        except supabase_auth.OwnerMappingNotFound as exc:
            raise HTTPException(
                status_code=403,
                detail="Supabase Auth user is not mapped to a dashboard owner.",
            ) from exc
        except (
            supabase_auth.AuthConfigurationError,
            supabase_auth.AuthServiceUnavailable,
        ) as exc:
            raise HTTPException(
                status_code=503,
                detail="Supabase Auth is unavailable or not configured.",
            ) from exc
        return

    key = settings.api_key
    if key and x_api_key != key:
        raise HTTPException(status_code=401, detail="Thieu hoac sai X-API-Key")
