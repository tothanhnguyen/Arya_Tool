"""Trace viewer web read-only: danh sach task + timeline chi tiet tung buoc."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from laplace.db import session_scope
from laplace.llm.usage import task_usage
from laplace.models import LLMCall, Task
from laplace.services.trace import replay_events, task_trace
from laplace.web.deps import require_api_key

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_api_key)])

RUNNING_STATUSES = {"pending", "running"}

# Replay: gioi han thoi gian cho giua 2 su kien khi auto-play (giay)
REPLAY_MIN_DELAY_S = 1.0
REPLAY_MAX_DELAY_S = 5.0
REPLAY_SPEEDS = (1, 2, 4, 8)


def _replay_delay_s(latency_ms: int, speed: int) -> float:
    """Thoi gian cho truoc su kien ke tiep: latency that chia toc do,
    kep trong [MIN, MAX] de demo khong bi qua nhanh/qua cham."""
    delay = (latency_ms or 0) / 1000.0 / max(speed, 1)
    return round(min(max(delay, REPLAY_MIN_DELAY_S), REPLAY_MAX_DELAY_S), 1)


def _duration_s(task: Task) -> float | None:
    if task.created_at and task.finished_at:
        return round((task.finished_at - task.created_at).total_seconds(), 1)
    return None


def _system_usage(session) -> dict:
    """Tong token + chi phi TOAN he thong: moi dong trong bang llm_calls,
    ke ca call khong gan task (vd: judge) va task da rot khoi trang danh sach."""
    calls, prompt, completion, cost = session.execute(
        select(
            func.count(LLMCall.id),
            func.coalesce(func.sum(LLMCall.prompt_tokens), 0),
            func.coalesce(func.sum(LLMCall.completion_tokens), 0),
            func.coalesce(func.sum(LLMCall.cost_usd), 0.0),
        )
    ).one()
    return {
        "llm_calls": int(calls),
        "prompt_tokens": int(prompt),
        "completion_tokens": int(completion),
        "total_tokens": int(prompt) + int(completion),
        "cost_usd": round(float(cost), 6),
    }


@router.get("/", response_class=HTMLResponse)
def tasks_list(request: Request):
    with session_scope() as session:
        tasks = session.scalars(select(Task).order_by(Task.id.desc()).limit(50)).all()
        rows = []
        for t in tasks:
            usage = task_usage(session, t.id)
            rows.append(
                {
                    "id": t.id,
                    "request": (t.request[:80] + "…") if len(t.request) > 80 else t.request,
                    "status": t.status,
                    "strategy": t.strategy,
                    "route": t.route,
                    "llm_calls": usage["llm_calls"],
                    "total_tokens": usage["total_tokens"],
                    "cost_usd": usage["cost_usd"],
                    "created_at": t.created_at.strftime("%Y-%m-%d %H:%M:%S")
                    if t.created_at
                    else "",
                    "duration_s": _duration_s(t),
                }
            )
        grand = _system_usage(session)
    return templates.TemplateResponse(
        request, "tasks_list.html", {"tasks": rows, "grand": grand}
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(request: Request, task_id: int):
    with session_scope() as session:
        trace = task_trace(session, task_id)
        usage = task_usage(session, task_id) if trace else None
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
            "usage": usage,
            "auto_refresh": task["status"] in RUNNING_STATUSES,
        },
    )


@router.get("/tasks/{task_id}/replay", response_class=HTMLResponse)
def task_replay(
    request: Request, task_id: int, upto: int = 0, auto: int = 0, speed: int = 4
):
    """Che do replay: phat lai timeline tu trace da luu trong DB, hoan toan
    offline (khong goi mang/LLM). Server-render thuan: buoc thu cong qua
    query param ?upto=N, auto-play qua meta refresh theo latency that (rut gon).
    """
    with session_scope() as session:
        data = replay_events(session, task_id)
    if not data:
        raise HTTPException(status_code=404, detail="Task khong ton tai")

    if speed not in REPLAY_SPEEDS:
        speed = 4
    events = data["events"]
    total = len(events)
    upto = max(0, min(upto, total))
    finished = upto >= total

    next_delay_s = None
    if auto and not finished:
        next_delay_s = _replay_delay_s(events[upto]["latency_ms"], speed)

    return templates.TemplateResponse(
        request,
        "task_replay.html",
        {
            "task": data["task"],
            "totals": data["totals"],
            "events": events[:upto],
            "total": total,
            "upto": upto,
            "auto": 1 if (auto and not finished) else 0,
            "speed": speed,
            "speeds": REPLAY_SPEEDS,
            "finished": finished,
            "next_delay_s": next_delay_s,
            "progress_pct": round(upto * 100 / total) if total else 100,
        },
    )
