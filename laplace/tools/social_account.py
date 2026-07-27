"""Agent tool for inspecting and pausing social accounts.

Only safe account metadata is returned.  ``auth_ref`` and authentication
material are never included in tool observations.
"""

import logging
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from laplace.db import session_scope
from laplace.schemas import ToolResult
from laplace.social.models import SocialAccount
from laplace.tools.base import ToolContext, tool

logger = logging.getLogger(__name__)


class SocialAccountParams(BaseModel):
    action: Literal["list", "status", "pause", "resume"]
    account_id: int | None = Field(
        default=None, gt=0, description="Required for status, pause and resume"
    )

    @model_validator(mode="after")
    def _account_id_required(self) -> "SocialAccountParams":
        if self.action != "list" and self.account_id is None:
            raise ValueError(f"account_id is required for action='{self.action}'")
        return self


def _account_dict(account: SocialAccount) -> dict:
    """Serialize only non-secret account metadata."""
    return {
        "id": account.id,
        "platform": account.platform,
        "display_name": account.display_name,
        "external_id": account.external_id,
        "auth_type": account.auth_type,
        "status": account.status,
        "timezone": account.timezone,
        "daily_post_limit": account.daily_post_limit,
        "cooldown_seconds": account.cooldown_seconds,
        "last_checked_at": (
            account.last_checked_at.isoformat() if account.last_checked_at else None
        ),
    }


@tool(
    name="social_account",
    description=(
        "List or inspect the user's connected social accounts, or pause/resume publishing "
        "for one account. action='list' needs no account_id; status, pause and resume require "
        "account_id. Pause and resume require user confirmation. Authentication references "
        "and secrets are never returned."
    ),
    params=SocialAccountParams,
    confirm_when=lambda p: p.action in ("pause", "resume"),
)
def social_account(params: SocialAccountParams, ctx: ToolContext) -> ToolResult:
    try:
        with session_scope() as session:
            if params.action == "list":
                accounts = session.scalars(
                    select(SocialAccount)
                    .where(SocialAccount.user_id == ctx.user_id)
                    .order_by(SocialAccount.id)
                ).all()
                return ToolResult(ok=True, data={"accounts": [_account_dict(a) for a in accounts]})

            account = session.scalar(
                select(SocialAccount).where(
                    SocialAccount.id == params.account_id,
                    SocialAccount.user_id == ctx.user_id,
                )
            )
            if account is None:
                return ToolResult(ok=False, error=f"Social account {params.account_id} not found")

            if params.action == "status":
                return ToolResult(ok=True, data=_account_dict(account))

            if params.action == "pause":
                if account.status != "active":
                    return ToolResult(
                        ok=False,
                        error=(
                            f"Social account {account.id} cannot be paused "
                            f"from status='{account.status}'"
                        ),
                    )
                account.status = "paused"
            else:
                if account.status != "paused":
                    return ToolResult(
                        ok=False,
                        error=(
                            f"Social account {account.id} cannot be resumed "
                            f"from status='{account.status}'"
                        ),
                    )
                account.status = "active"

            session.flush()
            return ToolResult(ok=True, data=_account_dict(account))
    except Exception as exc:
        logger.exception("social_account failed")
        return ToolResult(ok=False, error=f"social_account failed: {exc}")

