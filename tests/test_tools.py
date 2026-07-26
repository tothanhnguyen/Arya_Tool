"""Test 6 builtin tool. Khong goi mang that: web_search chay che do stub,
fetch_page chi test duong loi voi URL khong hop le (fail truoc khi ra mang)."""

import pytest

from laplace.config import get_settings
from laplace.tools.base import ToolContext, all_tools, execute, get_tool, load_builtin_tools

# Import 6 module tool de decorator dang ky vao registry (chay luc collect).
load_builtin_tools()

BUILTIN_NAMES = {
    "web_search",
    "fetch_page",
    "note_store",
    "task_list",
    "report_builder",
    "scheduler",
}


@pytest.fixture()
def ctx():
    return ToolContext(user_id=1)


def test_builtin_tools_registered():
    load_builtin_tools()
    assert BUILTIN_NAMES <= set(all_tools())


# ---------------------------------------------------------------- note_store


def test_note_store_crud(session, ctx):
    created = execute(
        "note_store",
        {"action": "create", "title": "Deadline", "content": "proposal 30/8"},
        ctx,
    )
    assert created.ok is True
    note_id = created.data["id"]
    assert created.data["title"] == "Deadline"

    listed = execute("note_store", {"action": "list"}, ctx)
    assert listed.ok is True
    assert [n["id"] for n in listed.data["notes"]] == [note_id]

    # user khac khong thay ghi chu cua user 1
    other = execute("note_store", {"action": "list"}, ToolContext(user_id=2))
    assert other.ok is True
    assert other.data["notes"] == []

    updated = execute(
        "note_store",
        {"action": "update", "note_id": note_id, "title": "Deadline v2", "content": "1/9"},
        ctx,
    )
    assert updated.ok is True
    assert updated.data["title"] == "Deadline v2"
    assert updated.data["content"] == "1/9"

    deleted = execute("note_store", {"action": "delete", "note_id": note_id}, ctx)
    assert deleted.ok is True
    assert execute("note_store", {"action": "list"}, ctx).data["notes"] == []


def test_note_store_errors(session, ctx):
    # create thieu title
    assert execute("note_store", {"action": "create"}, ctx).ok is False
    # update/delete note khong ton tai hoac thieu note_id
    assert execute("note_store", {"action": "update", "note_id": 999}, ctx).ok is False
    assert execute("note_store", {"action": "delete"}, ctx).ok is False
    # action ngoai Literal -> loi validate schema
    assert execute("note_store", {"action": "purge_all"}, ctx).ok is False


def test_note_store_confirm_when():
    spec = get_tool("note_store")
    assert spec is not None
    model = spec.params_model
    assert spec.needs_confirm(model(action="delete", note_id=1)) is True
    assert spec.needs_confirm(model(action="update", note_id=1)) is True
    assert spec.needs_confirm(model(action="create", title="x")) is False
    assert spec.needs_confirm(model(action="list")) is False


# ----------------------------------------------------------------- task_list


def test_task_list_crud(session, ctx):
    added = execute("task_list", {"action": "add", "text": "viet proposal"}, ctx)
    assert added.ok is True
    todo_id = added.data["id"]
    assert added.data["done"] is False

    listed = execute("task_list", {"action": "list"}, ctx)
    assert listed.ok is True
    assert len(listed.data["todos"]) == 1

    done = execute("task_list", {"action": "done", "todo_id": todo_id}, ctx)
    assert done.ok is True
    assert done.data["done"] is True

    deleted = execute("task_list", {"action": "delete", "todo_id": todo_id}, ctx)
    assert deleted.ok is True
    assert execute("task_list", {"action": "list"}, ctx).data["todos"] == []


def test_task_list_errors_and_confirm(session, ctx):
    assert execute("task_list", {"action": "add", "text": "   "}, ctx).ok is False
    assert execute("task_list", {"action": "done", "todo_id": 999}, ctx).ok is False
    assert execute("task_list", {"action": "delete"}, ctx).ok is False

    spec = get_tool("task_list")
    assert spec is not None
    model = spec.params_model
    assert spec.needs_confirm(model(action="delete", todo_id=1)) is True
    assert spec.needs_confirm(model(action="add", text="x")) is False
    assert spec.needs_confirm(model(action="done", todo_id=1)) is False


# ------------------------------------------------------------ report_builder


def test_report_builder_writes_markdown_file(tmp_path, monkeypatch, ctx):
    monkeypatch.chdir(tmp_path)
    result = execute(
        "report_builder",
        {
            "title": "FastAPI vs Flask",
            "sections": [
                {"heading": "Overview", "content": "Both are Python web frameworks."},
                {"heading": "Verdict", "content": "FastAPI for this project."},
            ],
        },
        ctx,
    )
    assert result.ok is True
    markdown = result.data["markdown"]
    assert "# FastAPI vs Flask" in markdown
    assert "## Overview" in markdown
    assert "## Verdict" in markdown

    # Ten file duoc namespace theo user/task de tranh ghi de cheo nhau
    report_path = tmp_path / "reports" / "u1-t0-fastapi-vs-flask.md"
    assert result.data["path"] == str(report_path.relative_to(tmp_path))
    assert report_path.exists()
    assert report_path.read_text(encoding="utf-8") == markdown


def test_report_builder_empty_title(tmp_path, monkeypatch, ctx):
    monkeypatch.chdir(tmp_path)
    assert execute("report_builder", {"title": "   ", "sections": []}, ctx).ok is False


# ---------------------------------------------------------------- web_search


def test_web_search_stub_mode(monkeypatch, ctx):
    # Bao dam khong co API key -> stub, khong goi mang
    monkeypatch.setattr(get_settings(), "search_api_key", None)

    result = execute("web_search", {"query": "gia RAM DDR5 32GB"}, ctx)
    assert result.ok is True
    assert result.data["stub"] is True
    results = result.data["results"]
    assert len(results) == 5
    for item in results:
        assert set(item) == {"title", "url", "snippet"}
        assert "[stub result]" in item["title"]

    limited = execute("web_search", {"query": "abc", "max_results": 2}, ctx)
    assert limited.ok is True
    assert len(limited.data["results"]) == 2


# ---------------------------------------------------------------- fetch_page


def test_fetch_page_invalid_url_offline(ctx):
    # URL khong co scheme -> httpx loi ngay tai client, khong ra mang
    result = execute("fetch_page", {"url": "not-a-valid-url"}, ctx)
    assert result.ok is False
    assert result.error


# ----------------------------------------------------------------- scheduler


def test_scheduler_crud_valid_cron(session, ctx):
    created = execute(
        "scheduler",
        {"action": "create", "cron": "0 8 * * *", "task_template": "Summarize AI news"},
        ctx,
    )
    assert created.ok is True
    job_id = created.data["id"]
    assert created.data["cron"] == "0 8 * * *"
    assert created.data["enabled"] is True

    listed = execute("scheduler", {"action": "list"}, ctx)
    assert listed.ok is True
    assert [j["id"] for j in listed.data["jobs"]] == [job_id]

    deleted = execute("scheduler", {"action": "delete", "job_id": job_id}, ctx)
    assert deleted.ok is True
    assert execute("scheduler", {"action": "list"}, ctx).data["jobs"] == []


def test_scheduler_invalid_cron(session, ctx):
    result = execute(
        "scheduler",
        {"action": "create", "cron": "99 99 * * *", "task_template": "x"},
        ctx,
    )
    assert result.ok is False
    assert "cron" in (result.error or "").lower()

    garbage = execute(
        "scheduler",
        {"action": "create", "cron": "not a cron", "task_template": "x"},
        ctx,
    )
    assert garbage.ok is False


def test_scheduler_errors_and_confirm(session, ctx):
    assert execute("scheduler", {"action": "delete"}, ctx).ok is False
    assert execute("scheduler", {"action": "delete", "job_id": 999}, ctx).ok is False
    assert (
        execute("scheduler", {"action": "create", "cron": "0 8 * * *"}, ctx).ok is False
    )  # thieu task_template

    spec = get_tool("scheduler")
    assert spec is not None
    # Chi action ghi/nguy hiem (create, delete) can confirm; list (chi doc) thi khong.
    assert spec.needs_confirm(spec.params_model(action="list")) is False
    assert (
        spec.needs_confirm(
            spec.params_model(action="create", cron="0 8 * * *", task_template="x")
        )
        is True
    )
    assert spec.needs_confirm(spec.params_model(action="delete", job_id=1)) is True


def _run_scheduler_task(session, request: str, script: list) -> "object":
    """Chay 1 task qua agent loop that (MockLLM co script) roi tra ve Task."""
    from laplace.agent.orchestrator import run_task
    from laplace.llm.mock import MockLLM
    from laplace.models import Task
    from laplace.services.tasks import create_task, get_or_create_user

    user = get_or_create_user(session)
    task = create_task(session, user_id=user.id, request=request, strategy="react")
    session.commit()
    run_task(task.id, llm=MockLLM(script=script))
    session.expire_all()
    return session.get(Task, task.id)


def test_scheduler_list_runs_without_confirm(session):
    """action='list' (chi doc) phai chay thang toi done, khong pause cho confirm."""
    task = _run_scheduler_task(
        session,
        "Liet ke lich nhac cua toi",
        [
            {"route": "single_tool", "reason": "list scheduled jobs"},
            {"thought": "list", "action": "tool", "tool": "scheduler",
             "params": {"action": "list"}},
            {"thought": "done", "action": "final", "final_answer": "Ban chua co lich nao."},
        ],
    )
    assert task.status == "done"
    assert (task.state_json or {}).get("pending") is None


@pytest.mark.parametrize(
    "params",
    [
        {"action": "create", "cron": "0 8 * * *", "task_template": "Nhac hop"},
        {"action": "delete", "job_id": 1},
    ],
)
def test_scheduler_write_actions_pause_for_confirm(session, params):
    """create/delete phai dung o awaiting_confirm truoc khi thuc thi."""
    task = _run_scheduler_task(
        session,
        "Thao tac lich nhac",
        [
            {"route": "single_tool", "reason": "write action on scheduler"},
            {"thought": "do it", "action": "tool", "tool": "scheduler", "params": params},
        ],
    )
    assert task.status == "awaiting_confirm"
    assert task.state_json["pending"]["tool"] == "scheduler"
    assert task.state_json["pending"]["params"]["action"] == params["action"]
