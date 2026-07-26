"""Test filter/search tren danh sach task + export trace (JSON/Markdown)."""

import pytest
from fastapi.testclient import TestClient

from laplace.services.tasks import create_task, get_or_create_user


@pytest.fixture()
def client(session):
    from laplace.web.app import create_app

    with TestClient(create_app(), follow_redirects=False) as c:
        yield c


def _seed_task(session, request: str = "viec test", status: str = "done") -> int:
    user = get_or_create_user(session)
    task = create_task(session, user_id=user.id, request=request)
    task.status = status
    session.commit()
    return task.id


# ------------------------------------------------------------------ filter


def test_filter_q_matches_case_insensitive(session, client):
    _seed_task(session, request="tim gia vang hom nay")
    _seed_task(session, request="dat lich hop")
    r = client.get("/", params={"q": "GIA VANG"})
    assert r.status_code == 200
    assert "tim gia vang hom nay" in r.text
    assert "dat lich hop" not in r.text
    assert "1 task khớp" in r.text


def test_filter_q_no_match(session, client):
    _seed_task(session, request="viec binh thuong")
    r = client.get("/", params={"q": "khong-ton-tai-xyz"})
    assert r.status_code == 200
    assert "viec binh thuong" not in r.text
    assert "0 task khớp" in r.text


def test_filter_status_failed_only(session, client):
    _seed_task(session, request="task bi hong", status="failed")
    _seed_task(session, request="task thanh cong", status="done")
    r = client.get("/", params={"status": "failed"})
    assert r.status_code == 200
    assert "task bi hong" in r.text
    assert "task thanh cong" not in r.text
    assert "1 task khớp" in r.text


def test_no_filter_shows_all(session, client):
    _seed_task(session, request="task mot")
    _seed_task(session, request="task hai")
    r = client.get("/")
    assert r.status_code == 200
    assert "task mot" in r.text
    assert "task hai" in r.text
    assert "task khớp" not in r.text


def test_limit_clamped_to_min(session, client):
    for i in range(12):
        _seed_task(session, request=f"task so {i}")
    # limit=1 duoi nguong -> kep len 10
    r = client.get("/", params={"limit": 1})
    assert r.status_code == 200
    assert r.text.count('aria-label="Replay task') == 10


def test_limit_clamped_to_max(session, client):
    for i in range(3):
        _seed_task(session, request=f"task so {i}")
    # limit qua lon -> kep xuong 500, van tra du 3 task
    r = client.get("/", params={"limit": 99999})
    assert r.status_code == 200
    assert r.text.count('aria-label="Replay task') == 3


# ------------------------------------------------------------------ export


def test_export_json(session, client):
    tid = _seed_task(session, request="xuat json thu nghiem")
    r = client.get(f"/tasks/{tid}/export.json")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == f'attachment; filename="task-{tid}.json"'
    data = r.json()
    assert data["task"]["id"] == tid
    assert data["task"]["request"] == "xuat json thu nghiem"
    assert "steps" in data
    assert "llm_calls" in data
    assert "totals" in data


def test_export_markdown(session, client):
    tid = _seed_task(session, request="xuat markdown thu nghiem")
    r = client.get(f"/tasks/{tid}/export.md")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert f"task-{tid}.md" in r.headers["content-disposition"]
    assert "xuat markdown thu nghiem" in r.text
    assert "## Steps" in r.text
    assert "## LLM calls" in r.text
    assert "## Totals" in r.text


def test_export_404_unknown_task(client):
    assert client.get("/tasks/999999/export.json").status_code == 404
    assert client.get("/tasks/999999/export.md").status_code == 404
