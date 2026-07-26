"""Trace viewer web read-only: danh sach task + timeline chi tiet tung buoc."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from laplace.db import session_scope
from laplace.models import LLMCall, Task
from laplace.services.trace import task_trace
from laplace.web.deps import require_api_key

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_api_key)])

RUNNING_STATUSES = {"pending", "running"}


def _duration_s(task: Task) -> float | None:
    if task.created_at and task.finished_at:
        return round((task.finished_at - task.created_at).total_seconds(), 1)
    return None


@router.get("/", response_class=HTMLResponse)
def tasks_list(request: Request):
    with session_scope() as session:
        tasks = session.scalars(select(Task).order_by(Task.id.desc()).limit(50)).all()
        cost_rows = session.execute(
            select(LLMCall.task_id, func.sum(LLMCall.cost_usd)).group_by(LLMCall.task_id)
        ).all()
        costs = {task_id: cost for task_id, cost in cost_rows}
        rows = [
            {
                "id": t.id,
                "request": (t.request[:80] + "…") if len(t.request) > 80 else t.request,
                "status": t.status,
                "strategy": t.strategy,
                "cost_usd": round(costs.get(t.id) or 0.0, 6),
                "created_at": t.created_at.strftime("%Y-%m-%d %H:%M:%S") if t.created_at else "",
                "duration_s": _duration_s(t),
            }
            for t in tasks
        ]
    return templates.TemplateResponse(request, "tasks_list.html", {"tasks": rows})


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(request: Request, task_id: int):
    with session_scope() as session:
        trace = task_trace(session, task_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Task khong ton tai")
    task = trace["task"]
    return templates.TemplateResponse(
        request,
        "task_detail.html",
        {
            "task": task,
            "steps": trace["steps"],
            "llm_calls": trace["llm_calls"],
            "totals": trace["totals"],
            "auto_refresh": task["status"] in RUNNING_STATUSES,
        },
    )
