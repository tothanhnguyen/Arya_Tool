"""Tests cho agent loop (orchestrator + 2 strategies) voi MockLLM scripted."""

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from laplace.agent.orchestrator import run_task
from laplace.config import get_settings
from laplace.llm.mock import MockLLM
from laplace.models import LLMCall, Step, Task
from laplace.schemas import ToolResult
from laplace.services.tasks import create_task, get_or_create_user
from laplace.tools.base import ToolContext, clear_registry, tool


class EchoParams(BaseModel):
    text: str = ""


def register_echo() -> list[str]:
    """Dang ky tool gia 'echo'; tra ve list ghi lai cac lan duoc goi."""
    calls: list[str] = []

    @tool(name="echo", description="Echo back the given text.", params=EchoParams)
    def echo(params: EchoParams, ctx: ToolContext) -> ToolResult:
        calls.append(params.text)
        return ToolResult(ok=True, data={"echo": params.text})

    return calls


@pytest.fixture(autouse=True)
def clean_registry():
    clear_registry()
    yield
    clear_registry()


def make_task(session, strategy: str = "react", request: str = "do the thing") -> Task:
    user = get_or_create_user(session)
    task = create_task(session, user_id=user.id, request=request, strategy=strategy)
    session.commit()  # run_task mo session rieng -> phai commit de nhin thay task
    return task


def get_steps(session, task_id: int) -> list[Step]:
    return list(
        session.scalars(select(Step).where(Step.task_id == task_id).order_by(Step.idx))
    )


def get_llm_calls(session, task_id: int) -> list[LLMCall]:
    return list(
        session.scalars(select(LLMCall).where(LLMCall.task_id == task_id).order_by(LLMCall.id))
    )


def test_direct_route_done_with_result(session):
    task = make_task(session, request="What is 2+2?")
    llm = MockLLM(
        script=[
            {"route": "direct", "reason": "simple question"},
            "The answer is 4.",
        ]
    )
    returned = run_task(task.id, llm=llm)
    assert returned.status == "done"

    session.expire_all()
    task = session.get(Task, task.id)
    assert task.status == "done"
    assert task.result == "The answer is 4."
    assert task.finished_at is not None

    calls = get_llm_calls(session, task.id)
    assert [c.purpose for c in calls] == ["classify", "answer"]
    assert all(c.provider == "mock" for c in calls)
    assert get_steps(session, task.id) == []


def test_react_tool_then_final(session):
    echo_calls = register_echo()
    task = make_task(session, strategy="react")
    llm = MockLLM(
        script=[
            {"route": "multi_step", "reason": "needs a tool"},
            {"thought": "echo it", "action": "tool", "tool": "echo", "params": {"text": "hi"}},
            {"thought": "enough", "action": "final", "final_answer": "echoed: hi"},
        ]
    )
    run_task(task.id, llm=llm)

    session.expire_all()
    task = session.get(Task, task.id)
    assert task.status == "done"
    assert task.result == "echoed: hi"
    assert echo_calls == ["hi"]

    steps = get_steps(session, task.id)
    assert len(steps) == 1
    assert steps[0].tool == "echo"
    assert steps[0].idx == 0
    assert steps[0].status == "ok"
    assert steps[0].params_json == {"text": "hi"}
    assert steps[0].observation_json["ok"] is True
    assert steps[0].observation_json["data"] == {"echo": "hi"}

    purposes = [c.purpose for c in get_llm_calls(session, task.id)]
    assert purposes == ["classify", "react", "react"]


def test_react_step_limit_exceeded(session):
    register_echo()
    task = make_task(session, strategy="react")
    max_steps = get_settings().max_steps
    script: list = [{"route": "multi_step", "reason": "loop forever"}]
    script += [
        {"thought": f"step {i}", "action": "tool", "tool": "echo", "params": {"text": str(i)}}
        for i in range(max_steps)
    ]
    llm = MockLLM(script=script)
    run_task(task.id, llm=llm)

    session.expire_all()
    task = session.get(Task, task.id)
    assert task.status == "failed"
    assert "step limit" in (task.error or "")
    assert len(get_steps(session, task.id)) == max_steps


def test_plan_execute_two_steps(session):
    echo_calls = register_echo()
    task = make_task(session, strategy="plan_execute")
    llm = MockLLM(
        script=[
            {"route": "multi_step", "reason": "two lookups"},
            {
                "steps": [
                    {"tool": "echo", "params": {"text": "a"}, "rationale": "first"},
                    {"tool": "echo", "params": {"text": "b"}, "rationale": "second"},
                ]
            },
            {"decision": "continue", "reason": "need step 2"},
            {"decision": "done", "reason": "all gathered"},
            "final report: a + b",
        ]
    )
    run_task(task.id, llm=llm)

    session.expire_all()
    task = session.get(Task, task.id)
    assert task.status == "done"
    assert task.result == "final report: a + b"
    assert echo_calls == ["a", "b"]

    steps = get_steps(session, task.id)
    assert [s.tool for s in steps] == ["echo", "echo"]
    assert [s.idx for s in steps] == [0, 1]
    assert all(s.status == "ok" for s in steps)

    purposes = [c.purpose for c in get_llm_calls(session, task.id)]
    assert purposes == ["classify", "plan", "evaluate", "evaluate", "final"]


def test_self_correction_on_invalid_schema(session):
    task = make_task(session)
    llm = MockLLM(
        script=[
            {"route": "not_a_valid_route"},  # sai Literal -> validation error -> retry
            {"route": "direct", "reason": "fixed"},
            "answer after retry",
        ]
    )
    run_task(task.id, llm=llm)

    session.expire_all()
    task = session.get(Task, task.id)
    assert task.status == "done"
    assert task.result == "answer after retry"

    # Retry co gui kem thong bao loi validation cho LLM
    retry_messages = llm.calls[1]["messages"]
    assert any("validation" in m["content"].lower() for m in retry_messages)

    # Ca 2 lan classify (sai + dung) deu duoc trace; lan retry danh dau :fix1
    purposes = [c.purpose for c in get_llm_calls(session, task.id)]
    assert purposes == ["classify", "classify:fix1", "answer"]


def test_schema_failure_after_retries_fails_task(session):
    task = make_task(session)
    llm = MockLLM(
        script=[
            {"route": "bad1"},
            {"route": "bad2"},
            {"route": "bad3"},  # 3 lan deu sai -> failed
        ]
    )
    run_task(task.id, llm=llm)

    session.expire_all()
    task = session.get(Task, task.id)
    assert task.status == "failed"
    assert "RouteDecision" in (task.error or "")
