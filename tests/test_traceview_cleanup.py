"""Test gom nhom theo ngay + don dep task trong trace viewer."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from laplace.models import LLMCall, Step, Task
from laplace.schemas import ToolResult
from laplace.services.tasks import create_task, get_or_create_user
from laplace.services.trace import record_step
from laplace.web.traceview import _to_local


@pytest.fixture()
def client(session):
    from laplace.web.app import create_app

    with TestClient(create_app(), follow_redirects=False) as c:
        yield c


def _seed_task(session, days_ago: int = 0, request: str = "viec test") -> int:
    user = get_or_create_user(session)
    task = create_task(session, user_id=user.id, request=request)
    task.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days_ago)
    record_step(session, task.id, 0, "web_search", {"query": "x"},
                observation=ToolResult(ok=True), status="ok")
    session.add(LLMCall(task_id=task.id, purpose="classify", provider="mock"))
    session.commit()
    return task.id


def test_cleanup_one_day_keeps_other_days(session, client):
    old_id = _seed_task(session, days_ago=3, request="task cu")
    new_id = _seed_task(session, days_ago=0, request="task moi")
    old_day = _to_local(session.get(Task, old_id).created_at).date().isoformat()

    r = client.post(f"/tasks/cleanup?day={old_day}")
    assert r.status_code == 303
    assert r.headers["location"] == "/?cleaned=1"

    session.expire_all()
    assert session.get(Task, old_id) is None
    assert session.get(Task, new_id) is not None
    # Trace cua task bi don cung phai sach
    assert session.scalars(select(Step).where(Step.task_id == old_id)).all() == []
    assert session.scalars(select(LLMCall).where(LLMCall.task_id == old_id)).all() == []


def test_delete_single_task(session, client):
    tid = _seed_task(session)
    r = client.post(f"/tasks/{tid}/delete")
    assert r.status_code == 303
    session.expire_all()
    assert session.get(Task, tid) is None

    assert client.post(f"/tasks/{tid}/delete").status_code == 404


def test_cleanup_invalid_day_format(client):
    assert client.post("/tasks/cleanup?day=hom-qua").status_code == 400


def test_tasks_grouped_by_day(session, client):
    _seed_task(session, days_ago=0)
    _seed_task(session, days_ago=3)
    r = client.get("/")
    assert r.status_code == 200
    assert "Hôm nay" in r.text
    assert "Dọn ngày" in r.text
    assert 'action="/tasks/cleanup?day=' in r.text
