"""Test structured output + self-correction (T8 — PLAN.md muc 8.5).

- Schema chat: ReActAction/PlanStep tu choi output thieu truong ngu nghia
  (action='tool' khong ten tool, action='final' khong final_answer...).
- Self-correction: LLM tra sai schema -> nhan thong bao loi validation va tu
  sua, toi da MAX_SCHEMA_RETRIES lan; lan retry ghi trace purpose "<p>:fixN".
"""

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from laplace.agent.orchestrator import run_task
from laplace.agent.strategies import MAX_SCHEMA_RETRIES
from laplace.llm.mock import MockLLM
from laplace.models import LLMCall, Task
from laplace.schemas import Plan, PlanStep, ReActAction, ToolResult
from laplace.services.tasks import create_task, get_or_create_user
from laplace.tools.base import ToolContext, clear_registry, tool


class EchoParams(BaseModel):
    text: str = ""


@pytest.fixture(autouse=True)
def echo_registry():
    clear_registry()

    @tool(name="echo", description="Echo back the given text.", params=EchoParams)
    def echo(params: EchoParams, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, data={"echo": params.text})

    yield
    clear_registry()


def make_task(session, strategy: str = "react", request: str = "do the thing") -> Task:
    user = get_or_create_user(session)
    task = create_task(session, user_id=user.id, request=request, strategy=strategy)
    session.commit()
    return task


def purposes(session, task_id: int) -> list[str]:
    calls = session.scalars(
        select(LLMCall).where(LLMCall.task_id == task_id).order_by(LLMCall.id)
    )
    return [c.purpose for c in calls]


# ------------------------------------------------------------ schema validators


def test_react_action_tool_requires_tool_name():
    with pytest.raises(ValidationError, match="non-empty 'tool'"):
        ReActAction.model_validate({"action": "tool", "params": {}})
    with pytest.raises(ValidationError, match="non-empty 'tool'"):
        ReActAction.model_validate({"action": "tool", "tool": "   "})


def test_react_action_final_requires_answer():
    with pytest.raises(ValidationError, match="non-empty 'final_answer'"):
        ReActAction.model_validate({"action": "final"})
    with pytest.raises(ValidationError, match="non-empty 'final_answer'"):
        ReActAction.model_validate({"action": "final", "final_answer": ""})


def test_react_action_valid_shapes_pass():
    a = ReActAction.model_validate({"action": "tool", "tool": "echo", "params": {"text": "x"}})
    assert a.tool == "echo"
    b = ReActAction.model_validate({"action": "final", "final_answer": "done"})
    assert b.final_answer == "done"


def test_plan_step_requires_tool_name():
    with pytest.raises(ValidationError, match="non-empty 'tool'"):
        PlanStep.model_validate({"tool": ""})
    with pytest.raises(ValidationError):
        Plan.model_validate({"steps": [{"tool": "  ", "params": {}}]})
    assert Plan.model_validate({"steps": []}).steps == []  # plan rong van hop le


# ------------------------------------------------- self-correction trong ReAct


def test_react_self_corrects_invalid_action_and_finishes(session):
    task = make_task(session)
    llm = MockLLM(
        script=[
            {"route": "single_tool", "reason": "one call"},
            {"action": "tool", "params": {"text": "hi"}},  # SAI: thieu ten tool
            {"action": "tool", "tool": "echo", "params": {"text": "hi"}},  # da sua
            {"action": "final", "final_answer": "echoed"},
        ]
    )
    returned = run_task(task.id, llm=llm)
    assert returned.status == "done"

    session.expire_all()
    assert session.get(Task, task.id).result == "echoed"
    # Lan retry duoc danh dau :fix1 de dem so lan tu sua trong trace/metric
    assert purposes(session, task.id) == ["classify", "react", "react:fix1", "react"]

    # LLM phai NHAN duoc thong bao loi validation de tu sua
    retry_messages = llm.calls[2]["messages"]
    assert retry_messages[-1]["role"] == "user"
    assert "failed schema validation" in retry_messages[-1]["content"]
    assert "ReActAction" in retry_messages[-1]["content"]


def test_react_fails_after_retry_budget_exhausted(session):
    task = make_task(session)
    bad = {"action": "tool", "params": {}}  # luon thieu ten tool
    llm = MockLLM(
        script=[{"route": "single_tool", "reason": "x"}]
        + [dict(bad) for _ in range(MAX_SCHEMA_RETRIES + 1)]
    )
    returned = run_task(task.id, llm=llm)
    assert returned.status == "failed"

    session.expire_all()
    refreshed = session.get(Task, task.id)
    assert "ReActAction" in (refreshed.error or "")
    assert f"{MAX_SCHEMA_RETRIES + 1} attempts" in refreshed.error
    assert purposes(session, task.id) == [
        "classify", "react", "react:fix1", "react:fix2",
    ]


# ------------------------------------------- self-correction trong plan_execute


def test_plan_execute_self_corrects_empty_tool_step(session):
    task = make_task(session, strategy="plan_execute")
    llm = MockLLM(
        script=[
            {"route": "multi_step", "reason": "plan needed"},
            {"steps": [{"tool": "", "params": {}}]},  # SAI: step khong co tool
            {"steps": [{"tool": "echo", "params": {"text": "hi"}, "rationale": "echo"}]},
            {"decision": "done", "reason": "enough"},
            "final answer text",
        ]
    )
    returned = run_task(task.id, llm=llm)
    assert returned.status == "done"

    session.expire_all()
    assert session.get(Task, task.id).result == "final answer text"
    assert purposes(session, task.id) == [
        "classify", "plan", "plan:fix1", "evaluate", "final",
    ]


def test_purpose_unchanged_when_output_valid_first_try(session):
    """Khong co retry -> khong co purpose :fixN nao trong trace."""
    task = make_task(session)
    llm = MockLLM(
        script=[
            {"route": "single_tool", "reason": "x"},
            {"action": "tool", "tool": "echo", "params": {"text": "a"}},
            {"action": "final", "final_answer": "ok"},
        ]
    )
    run_task(task.id, llm=llm)
    assert all(":fix" not in p for p in purposes(session, task.id))
