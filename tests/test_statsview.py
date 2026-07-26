"""Test trang thong ke /stats: co du lieu va DB trong."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from laplace.models import LLMCall
from laplace.schemas import ToolResult
from laplace.services.tasks import create_task, get_or_create_user
from laplace.services.trace import record_step


@pytest.fixture()
def client(session):
    from laplace.web.app import create_app

    with TestClient(create_app(), follow_redirects=False) as c:
        yield c


def _seed_task(
    session,
    days_ago: int,
    status: str,
    tool: str = "web_search",
    step_status: str = "ok",
) -> int:
    user = get_or_create_user(session)
    task = create_task(session, user_id=user.id, request="viec thong ke")
    task.status = status
    created = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days_ago)
    task.created_at = created
    record_step(
        session,
        task.id,
        0,
        tool,
        {"q": "x"},
        observation=ToolResult(ok=(step_status != "error")),
        status=step_status,
    )
    session.add(
        LLMCall(
            task_id=task.id,
            purpose="answer",
            provider="mock",
            prompt_tokens=120,
            completion_tokens=30,
            cost_usd=0.0015,
            latency_ms=800,
            created_at=created,
        )
    )
    session.commit()
    return task.id


def test_stats_page_with_data(session, client):
    _seed_task(session, days_ago=0, status="done", tool="web_search")
    _seed_task(session, days_ago=2, status="failed", tool="python_exec", step_status="error")

    r = client.get("/stats")
    assert r.status_code == 200
    assert "Top tools" in r.text
    assert "<svg" in r.text
    # Ten tool da seed xuat hien (trong chart va bang fallback)
    assert "web_search" in r.text
    assert "python_exec" in r.text
    # Moi chart co bang du lieu fallback
    assert "Bảng dữ liệu" in r.text
    # Hatch pattern cho failed/loi (khong phan biet chi bang do xam)
    assert "hatch-failed" in r.text


def test_stats_empty_db(client):
    r = client.get("/stats")
    assert r.status_code == 200
    assert 'class="empty"' in r.text
    assert "Chưa có dữ liệu" in r.text
