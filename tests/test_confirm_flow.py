"""Tests cho human-in-the-loop confirm flow: pause -> resume (approve/reject)."""

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from laplace.agent.orchestrator import resume_task, run_task
from laplace.llm.mock import MockLLM
from laplace.models import Step, Task
from laplace.schemas import ToolResult
from laplace.services.tasks import create_task, get_or_create_user
from laplace.tools.base import ToolContext, clear_registry, tool


class WipeParams(BaseModel):
    target: str = ""


def register_wipe() -> list[str]:
    """Tool gia can confirm; tra ve list ghi lai cac lan thuc su duoc goi."""
    calls: list[str] = []

    @tool(
        name="wipe",
        description="Delete user data. Destructive.",
        params=WipeParams,
        requires_confirmation=True,
    )
    def wipe(params: WipeParams, ctx: ToolContext) -> ToolResult:
        calls.append(params.target)
        return ToolResult(ok=True, data={"wiped": params.target})

    return calls


@pytest.fixture(autouse=True)
def clean_registry():
    clear_registry()
    yield
    clear_registry()


def make_task(session, strategy: str = "react", request: str = "wipe my notes") -> Task:
    user = get_or_create_user(session)
    task = create_task(session, user_id=user.id, request=request, strategy=strategy)
    session.commit()
    return task


def get_steps(session, task_id: int) -> list[Step]:
    return list(
        session.scalars(select(Step).where(Step.task_id == task_id).order_by(Step.idx))
    )


def test_react_pauses_then_approved_resumes_to_done(session):
    wiped = register_wipe()
    task = make_task(session, strategy="react")
    llm = MockLLM(
        script=[
            {"route": "multi_step", "reason": "destructive action"},
            {"thought": "wipe it", "action": "tool", "tool": "wipe", "params": {"target": "notes"}},
            # phan script con lai chay khi resume:
            {"thought": "confirmed and done", "action": "final", "final_answer": "notes wiped"},
        ]
    )
    run_task(task.id, llm=llm)

    session.expire_all()
    task = session.get(Task, task.id)
    assert task.status == "awaiting_confirm"
    assert task.state_json["pending"]["tool"] == "wipe"
    assert task.state_json["pending"]["params"] == {"target": "notes"}
    assert wiped == []  # chua duoc thuc thi truoc khi confirm

    steps = get_steps(session, task.id)
    assert len(steps) == 1
    assert steps[0].status == "pending_confirm"

    resume_task(task.id, approved=True, llm=llm)

    session.expire_all()
    task = session.get(Task, task.id)
    assert task.status == "done"
    assert task.result == "notes wiped"
    assert wiped == ["notes"]

    steps = get_steps(session, task.id)
    assert len(steps) == 1  # step pending duoc cap nhat, khong tao step moi
    assert steps[0].status == "ok"
    assert steps[0].observation_json["ok"] is True
    assert steps[0].observation_json["data"] == {"wiped": "notes"}


def test_react_rejected_step_marked_and_llm_decides_next(session):
    wiped = register_wipe()
    task = make_task(session, strategy="react")
    llm = MockLLM(
        script=[
            {"route": "multi_step", "reason": "destructive action"},
            {"thought": "wipe it", "action": "tool", "tool": "wipe", "params": {"target": "all"}},
            # sau reject, LLM thay observation 'user rejected' va tu ket thuc:
            {"thought": "user said no", "action": "final", "final_answer": "ok, nothing deleted"},
        ]
    )
    run_task(task.id, llm=llm)
    session.expire_all()
    assert session.get(Task, task.id).status == "awaiting_confirm"

    resume_task(task.id, approved=False, llm=llm)

    session.expire_all()
    task = session.get(Task, task.id)
    assert task.status == "done"
    assert task.result == "ok, nothing deleted"
    assert wiped == []  # tool khong bao gio chay

    steps = get_steps(session, task.id)
    assert len(steps) == 1
    assert steps[0].status == "rejected"
    assert steps[0].observation_json == {"ok": False, "data": None, "error": "user rejected"}

    # LLM o luot sau reject nhin thay observation bi tu choi trong history
    last_messages = llm.calls[-1]["messages"]
    assert any("user rejected" in m["content"] for m in last_messages)


def test_plan_execute_pauses_then_approved_resumes_to_done(session):
    wiped = register_wipe()
    task = make_task(session, strategy="plan_execute")
    llm = MockLLM(
        script=[
            {"route": "multi_step", "reason": "destructive plan"},
            {"steps": [{"tool": "wipe", "params": {"target": "todos"}, "rationale": "clear"}]},
            # phan script con lai chay khi resume:
            {"decision": "done", "reason": "wiped"},
            "all done: todos wiped",
        ]
    )
    run_task(task.id, llm=llm)

    session.expire_all()
    task = session.get(Task, task.id)
    assert task.status == "awaiting_confirm"
    assert task.state_json["pending"]["tool"] == "wipe"
    assert task.state_json["plan"]["steps"][0]["tool"] == "wipe"
    assert task.state_json["cursor"] == 0
    assert wiped == []

    resume_task(task.id, approved=True, llm=llm)

    session.expire_all()
    task = session.get(Task, task.id)
    assert task.status == "done"
    assert task.result == "all done: todos wiped"
    assert wiped == ["todos"]

    steps = get_steps(session, task.id)
    assert len(steps) == 1
    assert steps[0].status == "ok"


def test_plan_execute_rejected_fails_task(session):
    wiped = register_wipe()
    task = make_task(session, strategy="plan_execute")
    llm = MockLLM(
        script=[
            {"route": "multi_step", "reason": "destructive plan"},
            {"steps": [{"tool": "wipe", "params": {"target": "everything"}}]},
            # khong can script them: reject -> fail ngay, khong goi LLM nua
        ]
    )
    run_task(task.id, llm=llm)
    session.expire_all()
    assert session.get(Task, task.id).status == "awaiting_confirm"

    resume_task(task.id, approved=False, llm=llm)

    session.expire_all()
    task = session.get(Task, task.id)
    assert task.status == "failed"
    assert "rejected" in (task.error or "")
    assert wiped == []

    steps = get_steps(session, task.id)
    assert len(steps) == 1
    assert steps[0].status == "rejected"


def test_resume_requires_awaiting_confirm_status(session):
    task = make_task(session, strategy="react")
    llm = MockLLM(script=[{"route": "direct", "reason": "simple"}, "done directly"])
    run_task(task.id, llm=llm)

    with pytest.raises(ValueError):
        resume_task(task.id, approved=True, llm=MockLLM(script=[]))
