"""Tong hop token/chi phi LLM da ghi trong DB (bang llm_calls) theo task/user.

Runtime da ghi tung LLM call qua record_llm_call (services/trace); module nay
chi doc-tong-hop, phuc vu bao cao chi phi va kiem tra han muc o tang tren.
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from laplace.models import LLMCall, Task


def _usage_row(session: Session, where_clause: Any) -> dict[str, Any]:
    calls, prompt, completion, cost = session.execute(
        select(
            func.count(LLMCall.id),
            func.coalesce(func.sum(LLMCall.prompt_tokens), 0),
            func.coalesce(func.sum(LLMCall.completion_tokens), 0),
            func.coalesce(func.sum(LLMCall.cost_usd), 0.0),
        ).where(where_clause)
    ).one()
    return {
        "llm_calls": int(calls),
        "prompt_tokens": int(prompt),
        "completion_tokens": int(completion),
        "total_tokens": int(prompt) + int(completion),
        "cost_usd": round(float(cost), 6),
    }


def task_usage(session: Session, task_id: int) -> dict[str, Any]:
    """Tong token + chi phi cua MOT task (moi purpose: classify/plan/answer...)."""
    return _usage_row(session, LLMCall.task_id == task_id)


def user_usage(session: Session, user_id: int) -> dict[str, Any]:
    """Tong token + chi phi moi task cua MOT user (join qua tasks)."""
    task_ids = select(Task.id).where(Task.user_id == user_id)
    return _usage_row(session, LLMCall.task_id.in_(task_ids))
