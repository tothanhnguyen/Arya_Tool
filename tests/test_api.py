"""Test Task API + trace viewer. Khong goi mang, khong can token Telegram.

Fixture `session` (conftest) da reset engine sang SQLite tam theo tung test.
Settings mac dinh llm_provider=mock -> MockLLM heuristic tra direct answer.
BackgroundTasks trong TestClient chay sau response; de deterministic, test goi
run_task dong bo neu task chua ket thuc.
"""

from fastapi.testclient import TestClient

from laplace.web.app import create_app

TERMINAL = {"done", "failed"}


def _ensure_finished(client: TestClient, task_id: int) -> dict:
    """Poll GET; neu background chua chay xong thi goi run_task dong bo."""
    for _ in range(10):
        data = client.get(f"/api/tasks/{task_id}").json()
        if data["status"] in TERMINAL:
            return data
    if data["status"] == "pending":
        from laplace.agent.orchestrator import run_task

        run_task(task_id)
        data = client.get(f"/api/tasks/{task_id}").json()
    return data


def test_create_task_and_complete(session):
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/api/tasks", json={"request": "Xin chao, ban la ai?"})
        assert resp.status_code == 202
        body = resp.json()
        assert body["id"] > 0
        assert body["request"] == "Xin chao, ban la ai?"
        assert body["strategy"] == "react"

        data = _ensure_finished(client, body["id"])
        assert data["status"] == "done"
        assert data["result"]


def test_get_task_returns_task(session):
    app = create_app()
    with TestClient(app) as client:
        task_id = client.post("/api/tasks", json={"request": "hello"}).json()["id"]
        resp = client.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == task_id
        assert set(data) >= {"id", "request", "status", "strategy", "result", "error"}


def test_trace_endpoint_returns_json(session):
    app = create_app()
    with TestClient(app) as client:
        task_id = client.post("/api/tasks", json={"request": "trace test"}).json()["id"]
        _ensure_finished(client, task_id)

        resp = client.get(f"/api/tasks/{task_id}/trace")
        assert resp.status_code == 200
        trace = resp.json()
        assert set(trace) >= {"task", "steps", "llm_calls", "totals"}
        assert trace["task"]["id"] == task_id
        assert isinstance(trace["steps"], list)
        assert isinstance(trace["llm_calls"], list)


def test_404_cases(session):
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/api/tasks/999999").status_code == 404
        assert client.get("/api/tasks/999999/trace").status_code == 404
        assert (
            client.post("/api/tasks/999999/confirm", json={"approved": True}).status_code
            == 404
        )
        # user_id khong ton tai
        assert (
            client.post(
                "/api/tasks", json={"request": "x", "user_id": 999999}
            ).status_code
            == 404
        )


def test_confirm_conflict_when_not_awaiting(session):
    app = create_app()
    with TestClient(app) as client:
        task_id = client.post("/api/tasks", json={"request": "hello"}).json()["id"]
        _ensure_finished(client, task_id)
        resp = client.post(f"/api/tasks/{task_id}/confirm", json={"approved": True})
        assert resp.status_code == 409


def test_trace_viewer_pages(session):
    app = create_app()
    with TestClient(app) as client:
        task_id = client.post("/api/tasks", json={"request": "xem trace"}).json()["id"]
        _ensure_finished(client, task_id)

        home = client.get("/")
        assert home.status_code == 200
        assert "Arya_Tool" in home.text

        detail = client.get(f"/tasks/{task_id}")
        assert detail.status_code == 200
        assert "xem trace" in detail.text

        assert client.get("/tasks/999999").status_code == 404
