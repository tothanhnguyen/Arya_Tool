"""Test che do replay trace: helper replay_events + route /tasks/{id}/replay.

Du lieu trace mau duoc tao truc tiep qua ORM voi timestamp kiem soat duoc,
khong can goi mang/LLM — dung nhu kich ban demo offline.
"""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from laplace.models import LLMCall, Step, Task, User
from laplace.services.trace import replay_events
from laplace.web.app import create_app

T0 = datetime(2026, 1, 1, 12, 0, 0)


def _seed_trace(session) -> int:
    """Mot task done voi 2 LLM call + 2 step xen ke theo thoi gian."""
    user = User(tg_id=None, profile_json={})
    session.add(user)
    session.flush()

    task = Task(
        user_id=user.id,
        request="thoi tiet Ha Noi hom nay",
        status="done",
        strategy="react",
        route="tool",
        result="Ha Noi 32 do C, nang nhe.",
        created_at=T0,
        finished_at=T0 + timedelta(seconds=9),
    )
    session.add(task)
    session.flush()

    session.add_all(
        [
            LLMCall(
                task_id=task.id,
                purpose="classify",
                provider="mock",
                model="mock-1",
                prompt_tokens=100,
                completion_tokens=10,
                cost_usd=0.001,
                latency_ms=800,
                created_at=T0 + timedelta(seconds=1),
            ),
            Step(
                task_id=task.id,
                idx=0,
                tool="web_search",
                params_json={"query": "thoi tiet Ha Noi"},
                observation_json={"ok": True, "data": "32 do C"},
                status="ok",
                latency_ms=1200,
                created_at=T0 + timedelta(seconds=3),
            ),
            LLMCall(
                task_id=task.id,
                purpose="answer",
                provider="mock",
                model="mock-1",
                prompt_tokens=200,
                completion_tokens=50,
                cost_usd=0.002,
                latency_ms=900,
                # cung thoi diem voi step 1 -> llm_call phai dung truoc
                created_at=T0 + timedelta(seconds=5),
            ),
            Step(
                task_id=task.id,
                idx=1,
                tool="report_builder",
                params_json={"title": "Bao cao"},
                observation_json={"ok": True},
                status="ok",
                latency_ms=50,
                created_at=T0 + timedelta(seconds=5),
            ),
        ]
    )
    session.commit()
    return task.id


def test_replay_events_ordering_and_finish(session):
    task_id = _seed_trace(session)
    data = replay_events(session, task_id)

    assert data["task"]["id"] == task_id
    kinds = [(ev["kind"], ev["title"]) for ev in data["events"]]
    assert kinds == [
        ("llm_call", "LLM · classify"),
        ("step", "Step 0 · web_search"),
        ("llm_call", "LLM · answer"),
        ("step", "Step 1 · report_builder"),
        ("finish", "Ket thuc · done"),
    ]
    finish = data["events"][-1]
    assert finish["detail"]["result"] == "Ha Noi 32 do C, nang nhe."
    assert data["totals"]["steps"] == 2
    assert data["totals"]["prompt_tokens"] == 300


def test_replay_events_missing_task(session):
    assert replay_events(session, 999999) == {}


def test_replay_events_running_task_has_no_finish(session):
    user = User(tg_id=None, profile_json={})
    session.add(user)
    session.flush()
    task = Task(user_id=user.id, request="dang chay", status="running", created_at=T0)
    session.add(task)
    session.commit()

    data = replay_events(session, task.id)
    assert all(ev["kind"] != "finish" for ev in data["events"])


def test_replay_page_step_by_step(session):
    task_id = _seed_trace(session)
    app = create_app()
    with TestClient(app) as client:
        # upto=0: chi hien request + huong dan, chua co su kien
        r0 = client.get(f"/tasks/{task_id}/replay")
        assert r0.status_code == 200
        assert "thoi tiet Ha Noi hom nay" in r0.text
        assert "0/5 su kien" in r0.text
        assert "web_search" not in r0.text

        # upto=2: hien classify + step 0, chua hien answer
        r2 = client.get(f"/tasks/{task_id}/replay?upto=2")
        assert "LLM · classify" in r2.text
        assert "web_search" in r2.text
        assert "LLM · answer" not in r2.text
        assert "2/5 su kien" in r2.text

        # upto vuot qua tong -> clamp va bao hoan tat
        rend = client.get(f"/tasks/{task_id}/replay?upto=99")
        assert "Replay hoan tat" in rend.text
        assert "Ha Noi 32 do C" in rend.text


def test_replay_page_auto_mode_meta_refresh(session):
    task_id = _seed_trace(session)
    app = create_app()
    with TestClient(app) as client:
        r = client.get(f"/tasks/{task_id}/replay?upto=1&auto=1&speed=2")
        assert r.status_code == 200
        # su kien ke tiep (step 0, latency 1200ms) o speed 2x -> cho 1.0s (clamp min)
        assert 'http-equiv="refresh"' in r.text
        assert f"/tasks/{task_id}/replay?upto=2&auto=1&speed=2" in r.text

        # het su kien -> khong con meta refresh
        rdone = client.get(f"/tasks/{task_id}/replay?upto=5&auto=1")
        assert 'http-equiv="refresh"' not in rdone.text


def test_replay_page_404_and_links(session):
    task_id = _seed_trace(session)
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/tasks/999999/replay").status_code == 404

        # trang detail va danh sach co link Replay
        detail = client.get(f"/tasks/{task_id}")
        assert f"/tasks/{task_id}/replay" in detail.text
        home = client.get("/")
        assert f"/tasks/{task_id}/replay" in home.text
