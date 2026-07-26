"""Ghi va doc execution trace: steps + llm_calls cua moi task."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from laplace.llm.base import LLMResult
from laplace.models import LLMCall, Step, Task
from laplace.schemas import ToolResult


def record_step(
    session: Session,
    task_id: int,
    idx: int,
    tool: str,
    params: dict[str, Any],
    observation: ToolResult | None = None,
    status: str = "ok",
    latency_ms: int = 0,
    retries: int = 0,
) -> Step:
    step = Step(
        task_id=task_id,
        idx=idx,
        tool=tool,
        params_json=params,
        observation_json=observation.as_observation() if observation else {},
        status=status,
        latency_ms=latency_ms,
        retries=retries,
    )
    session.add(step)
    session.flush()
    return step


def record_llm_call(
    session: Session,
    result: LLMResult,
    purpose: str,
    provider: str,
    task_id: int | None = None,
    step_id: int | None = None,
) -> LLMCall:
    call = LLMCall(
        task_id=task_id,
        step_id=step_id,
        purpose=purpose,
        provider=provider,
        model=result.model,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
    )
    session.add(call)
    session.flush()
    return call


def task_trace(session: Session, task_id: int) -> dict[str, Any]:
    """Toan bo trace cua mot task, dung cho trace viewer va replay."""
    task = session.get(Task, task_id)
    if task is None:
        return {}
    steps = session.scalars(
        select(Step).where(Step.task_id == task_id).order_by(Step.idx)
    ).all()
    calls = session.scalars(
        select(LLMCall).where(LLMCall.task_id == task_id).order_by(LLMCall.id)
    ).all()
    return {
        "task": {
            "id": task.id,
            "request": task.request,
            "status": task.status,
            "strategy": task.strategy,
            "result": task.result,
            "error": task.error,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        },
        "steps": [
            {
                "idx": s.idx,
                "tool": s.tool,
                "params": s.params_json,
                "observation": s.observation_json,
                "status": s.status,
                "latency_ms": s.latency_ms,
                "retries": s.retries,
            }
            for s in steps
        ],
        "llm_calls": [
            {
                "purpose": c.purpose,
                "provider": c.provider,
                "model": c.model,
                "prompt_tokens": c.prompt_tokens,
                "completion_tokens": c.completion_tokens,
                "cost_usd": c.cost_usd,
                "latency_ms": c.latency_ms,
            }
            for c in calls
        ],
        "totals": {
            "steps": len(steps),
            "prompt_tokens": sum(c.prompt_tokens for c in calls),
            "completion_tokens": sum(c.completion_tokens for c in calls),
            "cost_usd": round(sum(c.cost_usd for c in calls), 6),
        },
    }
