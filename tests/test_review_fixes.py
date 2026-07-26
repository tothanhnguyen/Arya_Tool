"""Regression tests cho cac loi phat hien o vong code review.

1. SQLite self-deadlock: tool ghi DB (note_store) goi qua agent loop phai chay
   duoc — truoc fix, session ngoai giu write-lock lam tool treo 5s roi loi
   "database is locked".
2. Tool timeout duoc enforce trong registry executor.
3. Task timeout duoc enforce trong strategy loop.
4. API key bao ve REST API + trace viewer khi LAPLACE_API_KEY duoc dat.
"""

import time

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select

from laplace.agent.orchestrator import run_task
from laplace.config import get_settings
from laplace.llm.mock import MockLLM
from laplace.models import Note, Step
from laplace.schemas import ToolResult
from laplace.services.tasks import create_task, get_or_create_user
from laplace.tools.base import ToolContext, execute, load_builtin_tools, tool


def _make_task(session, request="test", strategy="react"):
    user = get_or_create_user(session)
    task = create_task(session, user_id=user.id, request=request, strategy=strategy)
    # Commit de fixture session khong giu write-lock khi run_task mo session rieng
    session.commit()
    return task


def test_db_tool_through_agent_loop_no_deadlock(session):
    """note_store create qua ReAct loop phai xong nhanh va ghi duoc Note."""
    load_builtin_tools()
    task = _make_task(session, request="Luu ghi chu: deadline proposal 30/8")
    llm = MockLLM(
        script=[
            {"route": "single_tool", "reason": "user wants to save a note"},
            {
                "thought": "save the note",
                "action": "tool",
                "tool": "note_store",
                "params": {"action": "create", "title": "deadline", "content": "30/8"},
            },
            {"action": "final", "final_answer": "Đã lưu ghi chú."},
        ]
    )
    start = time.monotonic()
    result = run_task(task.id, llm=llm)
    elapsed = time.monotonic() - start

    assert result.status == "done"
    assert elapsed < 3, f"agent loop bi block {elapsed:.1f}s — nghi van database lock"
    session.expire_all()
    notes = session.scalars(select(Note)).all()
    assert len(notes) == 1
    step = session.scalars(select(Step).where(Step.task_id == task.id)).one()
    assert step.status == "ok"
    assert step.observation_json["ok"] is True


class _SleepParams(BaseModel):
    seconds: float = 3.0


def test_tool_timeout_enforced(session):
    @tool("sleepy", "sleeps", params=_SleepParams, timeout_s=1)
    def sleepy(params: _SleepParams, ctx: ToolContext) -> ToolResult:
        time.sleep(params.seconds)
        return ToolResult(ok=True)

    start = time.monotonic()
    result = execute("sleepy", {"seconds": 3}, ToolContext(user_id=1))
    elapsed = time.monotonic() - start

    assert result.ok is False
    assert "timed out" in (result.error or "")
    assert elapsed < 2.5


def test_task_timeout_enforced(session, monkeypatch):
    monkeypatch.setattr(get_settings(), "task_timeout_s", 0)
    task = _make_task(session, request="viec gi do nhieu buoc")
    llm = MockLLM(script=[{"route": "multi_step", "reason": "x"}])

    result = run_task(task.id, llm=llm)

    assert result.status == "failed"
    assert "timeout" in (result.error or "")


@pytest.fixture()
def client(session):
    from laplace.web.app import create_app

    with TestClient(create_app()) as c:
        yield c


def test_api_key_protects_api_and_viewer(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "api_key", "s3cret")

    assert client.get("/api/tasks/1").status_code == 401
    assert client.get("/").status_code == 401
    # Co key dung -> qua duoc auth (404 vi task khong ton tai, 200 cho viewer)
    assert client.get("/api/tasks/1", headers={"X-API-Key": "s3cret"}).status_code == 404
    assert client.get("/", headers={"X-API-Key": "s3cret"}).status_code == 200


def test_no_api_key_means_open(client):
    assert get_settings().api_key is None
    assert client.get("/").status_code == 200
