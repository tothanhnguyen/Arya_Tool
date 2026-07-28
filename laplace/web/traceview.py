"""Trace viewer web: danh sach task (gom theo ngay) + timeline chi tiet + don dep."""

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select

from laplace.db import session_scope
from laplace.llm.usage import task_usage
from laplace.models import LLMCall, Step, Task
from laplace.services.trace import replay_events, task_trace
from laplace.web.deps import require_api_key

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

RUNNING_STATUSES = {"pending", "running"}

# Filter tren trang danh sach: option cung theo gia tri he thong
FILTER_STATUSES = ("pending", "running", "awaiting_confirm", "done", "failed")
FILTER_STRATEGIES = ("react", "plan_execute")
FILTER_ROUTES = ("direct", "single_tool", "multi_step", "clarify")
LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_MIN = 10
LIST_LIMIT_MAX = 500

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


def _request_owner_id(request: Request) -> int | None:
    owner_id = getattr(request.state, "user_id", None)
    return owner_id if isinstance(owner_id, int) and owner_id > 0 else None


def _origin_tuple(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    return parsed.scheme, parsed.hostname.casefold(), port


def _require_authenticated_trace_mutation(request: Request) -> None:
    """Require Bearer auth or a same-origin browser POST for destructive HTML actions."""

    if _request_owner_id(request) is None or request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    scheme, separator, credentials = request.headers.get("authorization", "").partition(" ")
    if separator and scheme.casefold() == "bearer" and credentials.strip():
        return
    source = request.headers.get("origin") or request.headers.get("referer")
    if source is None or _origin_tuple(source) != _origin_tuple(str(request.base_url)):
        raise HTTPException(
            status_code=403,
            detail="Authenticated form mutations require a same-origin request.",
        )


router = APIRouter(
    include_in_schema=False,
    dependencies=[
        Depends(require_api_key),
        Depends(_require_authenticated_trace_mutation),
    ],
)


def _task_is_owned(session, task_id: int, owner_id: int | None) -> bool:
    if owner_id is None:
        return session.get(Task, task_id) is not None
    return (
        session.scalar(
            select(Task.id).where(Task.id == task_id, Task.user_id == owner_id)
        )
        is not None
    )


# ------------------------------------------------------------------ thoi gian
# DB luu naive UTC (models.utcnow); hien thi va gom nhom theo gio dia phuong.

_LOCAL_TZ = datetime.now().astimezone().tzinfo


def _to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC).astimezone(_LOCAL_TZ)


def _utc_range_for_local_day(day: date) -> tuple[datetime, datetime]:
    """[start, end) theo UTC-naive cua mot ngay dia phuong — dung cho query DB."""
    start_local = datetime.combine(day, time.min).replace(tzinfo=_LOCAL_TZ)
    start = start_local.astimezone(UTC).replace(tzinfo=None)
    return start, start + timedelta(days=1)


def _day_label(day: date) -> str:
    today = datetime.now(_LOCAL_TZ).date()
    if day == today:
        return "Hôm nay"
    if day == today - timedelta(days=1):
        return "Hôm qua"
    return day.strftime("%d/%m/%Y")


def _delete_tasks(session, task_ids: list[int]) -> int:
    """Xoa task kem toan bo trace (steps + llm_calls). Tra ve so task da xoa."""
    if not task_ids:
        return 0
    session.execute(sa_delete(LLMCall).where(LLMCall.task_id.in_(task_ids)))
    session.execute(sa_delete(Step).where(Step.task_id.in_(task_ids)))
    result = session.execute(sa_delete(Task).where(Task.id.in_(task_ids)))
    return int(result.rowcount or 0)


def _system_usage(session, owner_id: int | None = None) -> dict:
    """Tong token + chi phi TOAN he thong: moi dong trong bang llm_calls,
    ke ca call khong gan task (vd: judge) va task da rot khoi trang danh sach."""
    statement = (
        select(
            func.count(LLMCall.id),
            func.coalesce(func.sum(LLMCall.prompt_tokens), 0),
            func.coalesce(func.sum(LLMCall.completion_tokens), 0),
            func.coalesce(func.sum(LLMCall.cost_usd), 0.0),
        )
        .select_from(LLMCall)
    )
    if owner_id is not None:
        statement = statement.join(Task, LLMCall.task_id == Task.id).where(
            Task.user_id == owner_id
        )
    calls, prompt, completion, cost = session.execute(statement).one()
    return {
        "llm_calls": int(calls),
        "prompt_tokens": int(prompt),
        "completion_tokens": int(completion),
        "total_tokens": int(prompt) + int(completion),
        "cost_usd": round(float(cost), 6),
    }


@router.get("/", response_class=HTMLResponse)
def tasks_list(
    request: Request,
    cleaned: int | None = None,
    q: str = "",
    status: str = "",
    strategy: str = "",
    route: str = "",
    limit: int = LIST_LIMIT_DEFAULT,
):
    owner_id = _request_owner_id(request)
    limit = max(LIST_LIMIT_MIN, min(limit, LIST_LIMIT_MAX))
    filtering = bool(q or status or strategy or route)
    stmt = select(Task)
    if owner_id is not None:
        stmt = stmt.where(Task.user_id == owner_id)
    if q:
        stmt = stmt.where(Task.request.ilike(f"%{q}%"))
    if status:
        stmt = stmt.where(Task.status == status)
    if strategy:
        stmt = stmt.where(Task.strategy == strategy)
    if route:
        stmt = stmt.where(Task.route == route)
    stmt = stmt.order_by(Task.id.desc()).limit(limit)
    with session_scope() as session:
        tasks = session.scalars(stmt).all()
        groups: list[dict] = []  # [{label, day_iso, tasks, count, cost_usd}]
        for t in tasks:
            usage = task_usage(session, t.id)
            local_dt = _to_local(t.created_at)
            day = local_dt.date() if local_dt else date.min
            row = {
                "id": t.id,
                "request": (t.request[:80] + "…") if len(t.request) > 80 else t.request,
                "status": t.status,
                "strategy": t.strategy,
                "route": t.route,
                "llm_calls": usage["llm_calls"],
                "total_tokens": usage["total_tokens"],
                "cost_usd": usage["cost_usd"],
                "created_at": local_dt.strftime("%H:%M:%S") if local_dt else "",
                "duration_s": _duration_s(t),
            }
            if not groups or groups[-1]["day_iso"] != day.isoformat():
                groups.append(
                    {
                        "label": _day_label(day),
                        "day_iso": day.isoformat(),
                        "tasks": [],
                        "cost_usd": 0.0,
                    }
                )
            groups[-1]["tasks"].append(row)
            groups[-1]["cost_usd"] = round(groups[-1]["cost_usd"] + row["cost_usd"], 6)
        grand = _system_usage(session, owner_id)
    return templates.TemplateResponse(
        request,
        "tasks_list.html",
        {
            "groups": groups,
            "grand": grand,
            "cleaned": cleaned,
            "filters": {"q": q, "status": status, "strategy": strategy, "route": route},
            "filtering": filtering,
            "matched": len(tasks),
            "status_options": FILTER_STATUSES,
            "strategy_options": FILTER_STRATEGIES,
            "route_options": FILTER_ROUTES,
        },
    )


@router.post("/tasks/cleanup")
def cleanup_day(request: Request, day: str):
    """Xoa moi task (kem trace) cua MOT ngay dia phuong. day=YYYY-MM-DD."""
    try:
        target = date.fromisoformat(day)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="day phai co dang YYYY-MM-DD") from e
    start, end = _utc_range_for_local_day(target)
    with session_scope() as session:
        statement = select(Task.id).where(
            Task.created_at >= start,
            Task.created_at < end,
        )
        owner_id = _request_owner_id(request)
        if owner_id is not None:
            statement = statement.where(Task.user_id == owner_id)
        ids = list(
            session.scalars(statement)
        )
        n = _delete_tasks(session, ids)
    return RedirectResponse(url=f"/?cleaned={n}", status_code=303)


@router.post("/tasks/{task_id}/delete")
def delete_one_task(request: Request, task_id: int):
    with session_scope() as session:
        if not _task_is_owned(session, task_id, _request_owner_id(request)):
            raise HTTPException(status_code=404, detail="Task khong ton tai")
        n = _delete_tasks(session, [task_id])
    if not n:
        raise HTTPException(status_code=404, detail="Task khong ton tai")
    return RedirectResponse(url="/?cleaned=1", status_code=303)


def _load_trace_or_404(task_id: int, owner_id: int | None = None) -> dict:
    with session_scope() as session:
        if not _task_is_owned(session, task_id, owner_id):
            raise HTTPException(status_code=404, detail="Task khong ton tai")
        trace = task_trace(session, task_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Task khong ton tai")
    return trace


def _trace_markdown(trace: dict) -> str:
    """Xuat trace thanh markdown gon: request/status/route, bang steps,
    bang llm_calls va totals."""
    task = trace["task"]
    totals = trace["totals"]
    lines = [
        f"# Task #{task['id']}",
        "",
        "## Request",
        "",
        task["request"],
        "",
        "## Status",
        "",
        f"- Status: {task['status']}",
        f"- Strategy: {task['strategy']}",
        f"- Route: {task['route'] or '-'}",
        f"- Created: {task['created_at'] or '-'}",
        f"- Finished: {task['finished_at'] or '-'}",
    ]
    if task["result"]:
        lines += ["", "## Result", "", task["result"]]
    if task["error"]:
        lines += ["", "## Error", "", task["error"]]

    lines += ["", "## Steps", ""]
    if trace["steps"]:
        lines += [
            "| # | Tool | Status | Latency (ms) | Retries |",
            "|---:|---|---|---:|---:|",
        ]
        lines += [
            f"| {s['idx']} | {s['tool']} | {s['status']} "
            f"| {s['latency_ms']} | {s['retries']} |"
            for s in trace["steps"]
        ]
    else:
        lines.append("Khong co step nao.")

    lines += ["", "## LLM calls", ""]
    if trace["llm_calls"]:
        lines += [
            (
                "| Purpose | Provider | Model | Prompt | Completion "
                "| Cost (USD) | Latency (ms) |"
            ),
            "|---|---|---|---:|---:|---:|---:|",
        ]
        lines += [
            f"| {c['purpose']} | {c['provider']} | {c['model']} "
            f"| {c['prompt_tokens']} | {c['completion_tokens']} "
            f"| {c['cost_usd']:.6f} | {c['latency_ms']} |"
            for c in trace["llm_calls"]
        ]
    else:
        lines.append("Khong co LLM call nao.")

    lines += [
        "",
        "## Totals",
        "",
        f"- Steps: {totals['steps']}",
        f"- Prompt tokens: {totals['prompt_tokens']}",
        f"- Completion tokens: {totals['completion_tokens']}",
        f"- Cost (USD): {totals['cost_usd']:.6f}",
        "",
    ]
    return "\n".join(lines)


@router.get("/tasks/{task_id}/export.json")
def export_task_json(request: Request, task_id: int):
    """Tai toan bo trace cua task duoi dang file JSON."""
    trace = _load_trace_or_404(task_id, _request_owner_id(request))
    return JSONResponse(
        content=trace,
        headers={
            "Content-Disposition": f'attachment; filename="task-{task_id}.json"'
        },
    )


@router.get("/tasks/{task_id}/export.md")
def export_task_markdown(request: Request, task_id: int):
    """Tai trace cua task duoi dang file Markdown gon."""
    trace = _load_trace_or_404(task_id, _request_owner_id(request))
    return PlainTextResponse(
        _trace_markdown(trace),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="task-{task_id}.md"'},
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(request: Request, task_id: int):
    with session_scope() as session:
        trace = (
            task_trace(session, task_id)
            if _task_is_owned(session, task_id, _request_owner_id(request))
            else None
        )
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
        data = (
            replay_events(session, task_id)
            if _task_is_owned(session, task_id, _request_owner_id(request))
            else None
        )
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
