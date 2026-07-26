"""Test hien thi chi phi LLM trong trace viewer (T12).

Danh sach task: cot token + cost per-task va dong tong toan he thong.
Trang chi tiet: cac tile LLM calls / tokens / cost tu helper task_usage (T9).
"""

from datetime import datetime

from fastapi.testclient import TestClient

from laplace.models import LLMCall, Task, User
from laplace.web.app import create_app

T0 = datetime(2026, 1, 1, 12, 0, 0)


def _seed(session) -> tuple[int, int]:
    """Hai task: task A co 2 LLM call, task B khong co call nao.
    Them 1 call khong gan task (vd: judge) — chi duoc tinh vao tong he thong."""
    user = User(tg_id=None, profile_json={})
    session.add(user)
    session.flush()

    task_a = Task(user_id=user.id, request="task co chi phi", status="done", created_at=T0)
    task_b = Task(user_id=user.id, request="task chua ton dong nao", status="pending", created_at=T0)
    session.add_all([task_a, task_b])
    session.flush()

    session.add_all(
        [
            LLMCall(
                task_id=task_a.id,
                purpose="classify",
                provider="mock",
                model="mock-1",
                prompt_tokens=100,
                completion_tokens=20,
                cost_usd=0.001,
                latency_ms=500,
            ),
            LLMCall(
                task_id=task_a.id,
                purpose="answer",
                provider="mock",
                model="mock-1",
                prompt_tokens=200,
                completion_tokens=80,
                cost_usd=0.0035,
                latency_ms=700,
            ),
            # call khong gan task nao (judge/eval) — van thuoc tong he thong
            LLMCall(
                task_id=None,
                purpose="judge",
                provider="mock",
                model="mock-1",
                prompt_tokens=50,
                completion_tokens=10,
                cost_usd=0.0005,
                latency_ms=300,
            ),
        ]
    )
    session.commit()
    return task_a.id, task_b.id


def test_tasks_list_shows_tokens_and_cost_per_task(session):
    _seed(session)
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200
    # cot moi trong header
    assert "Tokens" in r.text
    assert "Cost (USD)" in r.text
    # task A: 300 prompt + 100 completion = 400 token, cost 0.0045
    assert ">400</td>" in r.text
    assert "0.004500" in r.text
    # task B chua co call nao: 0 token / 0 cost
    assert ">0</td>" in r.text
    assert "0.000000" in r.text


def test_tasks_list_shows_system_grand_total(session):
    _seed(session)
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert "Tổng toàn hệ thống" in r.text
    # 3 call (ke ca call khong gan task): 350 prompt + 110 completion = 460 token
    assert "3 LLM call" in r.text
    assert ">460</td>" in r.text
    # tong cost = 0.001 + 0.0035 + 0.0005 = 0.005
    assert "0.005000" in r.text


def test_tasks_list_empty_db(session):
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert "Chưa có task nào" in r.text


def test_task_detail_shows_usage_tiles(session):
    task_a, _ = _seed(session)
    app = create_app()
    with TestClient(app) as client:
        r = client.get(f"/tasks/{task_a}")
    assert r.status_code == 200
    for label in (
        "LLM calls",
        "Prompt tokens",
        "Completion tokens",
        "Total tokens",
        "Total cost (USD)",
    ):
        assert label in r.text
    # 2 call cua task A: 300 prompt / 100 completion / 400 tong / 0.0045 USD
    assert ">300</div>" in r.text
    assert ">100</div>" in r.text
    assert ">400</div>" in r.text
    assert "0.004500" in r.text
    # call "judge" khong gan task A -> khong duoc cong vao (460 khong xuat hien)
    assert ">460</div>" not in r.text


def test_task_detail_zero_usage(session):
    _, task_b = _seed(session)
    app = create_app()
    with TestClient(app) as client:
        r = client.get(f"/tasks/{task_b}")
    assert r.status_code == 200
    assert "0.000000" in r.text
    assert "Chưa có LLM call nào" in r.text
