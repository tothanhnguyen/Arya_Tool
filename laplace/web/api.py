"""Task API: tao task, xem trang thai, confirm, doc trace."""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import select

from laplace.db import session_scope
from laplace.models import Task, User
from laplace.schemas import ConfirmIn, TaskCreateIn, TaskOut
from laplace.services.tasks import create_task, get_or_create_user
from laplace.services.trace import task_trace
from laplace.web.deps import require_api_key

logger = logging.getLogger(__name__)


def _request_owner_id(request: Request) -> int | None:
    owner_id = getattr(request.state, "user_id", None)
    return owner_id if isinstance(owner_id, int) and owner_id > 0 else None


def _require_authenticated_json_mutation(request: Request) -> None:
    """Cookie sessions may read APIs, but mutations require a Bearer credential."""

    if _request_owner_id(request) is None or request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    scheme, separator, credentials = request.headers.get("authorization", "").partition(" ")
    if not separator or scheme.casefold() != "bearer" or not credentials.strip():
        raise HTTPException(
            status_code=403,
            detail="Authenticated API mutations require a Bearer token.",
        )


def _owned_task(session, task_id: int, owner_id: int | None) -> Task | None:
    if owner_id is None:
        return session.get(Task, task_id)
    return session.scalar(
        select(Task).where(Task.id == task_id, Task.user_id == owner_id)
    )


router = APIRouter(
    tags=["tasks"],
    dependencies=[
        Depends(require_api_key),
        Depends(_require_authenticated_json_mutation),
    ],
)


def _task_out(task: Task) -> TaskOut:
    return TaskOut(
        id=task.id,
        request=task.request,
        status=task.status,
        strategy=task.strategy,
        result=task.result,
        error=task.error,
    )


def _run_task_bg(task_id: int) -> None:
    """Sync function -> starlette BackgroundTasks chay trong threadpool."""
    from laplace.agent.orchestrator import run_task

    try:
        run_task(task_id)
    except Exception:
        logger.exception("run_task failed task_id=%s", task_id)


def _resume_task_bg(task_id: int, approved: bool) -> None:
    from laplace.agent.orchestrator import resume_task

    try:
        resume_task(task_id, approved)
    except Exception:
        logger.exception("resume_task failed task_id=%s", task_id)


@router.post("/tasks", response_model=TaskOut, status_code=202)
def create_task_endpoint(
    request: Request,
    body: TaskCreateIn,
    background: BackgroundTasks,
) -> TaskOut:
    owner_id = _request_owner_id(request)
    if owner_id is not None and body.user_id not in {None, owner_id}:
        raise HTTPException(
            status_code=403,
            detail="User ID does not match the authenticated owner.",
        )
    with session_scope() as session:
        selected_user_id = owner_id if owner_id is not None else body.user_id
        if selected_user_id is not None:
            user = session.get(User, selected_user_id)
            if user is None:
                raise HTTPException(status_code=404, detail="User khong ton tai")
        else:
            user = get_or_create_user(session)
        task = create_task(
            session, user_id=user.id, request=body.request, strategy=body.strategy
        )
        out = _task_out(task)
    background.add_task(_run_task_bg, out.id)
    return out


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task_endpoint(request: Request, task_id: int) -> TaskOut:
    with session_scope() as session:
        task = _owned_task(session, task_id, _request_owner_id(request))
        if task is None:
            raise HTTPException(status_code=404, detail="Task khong ton tai")
        return _task_out(task)


@router.post("/tasks/{task_id}/confirm", response_model=TaskOut, status_code=202)
def confirm_task_endpoint(
    request: Request,
    task_id: int,
    body: ConfirmIn,
    background: BackgroundTasks,
) -> TaskOut:
    with session_scope() as session:
        task = _owned_task(session, task_id, _request_owner_id(request))
        if task is None:
            raise HTTPException(status_code=404, detail="Task khong ton tai")
        if task.status != "awaiting_confirm":
            raise HTTPException(
                status_code=409,
                detail=f"Task dang o trang thai '{task.status}', khong cho confirm",
            )
        out = _task_out(task)
    background.add_task(_resume_task_bg, task_id, body.approved)
    return out


@router.get("/tasks/{task_id}/trace")
def get_trace_endpoint(request: Request, task_id: int) -> dict:
    with session_scope() as session:
        task = _owned_task(session, task_id, _request_owner_id(request))
        if task is None:
            raise HTTPException(status_code=404, detail="Task khong ton tai")
        trace = task_trace(session, task_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Task khong ton tai")
    return trace
