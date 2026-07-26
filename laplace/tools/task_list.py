"""task_list tool: CRUD viec can lam cua user (bang todos)."""

import logging
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from laplace.db import session_scope
from laplace.models import Todo
from laplace.schemas import ToolResult
from laplace.tools.base import ToolContext, tool

logger = logging.getLogger(__name__)


class TaskListParams(BaseModel):
    action: Literal["add", "list", "done", "delete"] = Field(
        description="Operation to perform on the user's todo list"
    )
    todo_id: int | None = Field(
        default=None, description="Todo id, required for 'done' and 'delete'"
    )
    text: str = Field(default="", description="Todo text (required for 'add')")


def _todo_dict(todo: Todo) -> dict:
    return {
        "id": todo.id,
        "text": todo.text,
        "done": todo.done,
        "created_at": todo.created_at.isoformat() if todo.created_at else None,
    }


@tool(
    name="task_list",
    description=(
        "Manage the user's TODO list (actionable items to complete). "
        "USE FOR: 'thêm việc cần làm', adding a task, showing todos with status, "
        "marking one done, removing one. "
        "DO NOT USE FOR: saving reference text or summaries (use note_store) or "
        "recurring scheduled jobs (use scheduler). "
        "PARAMS: action is one of 'add'|'list'|'done'|'delete'; 'add' needs text; "
        "'done' and 'delete' need todo_id; 'delete' requires user confirmation. "
        "Example: {\"action\": \"add\", \"text\": \"ôn thi cuối kỳ\"}."
    ),
    params=TaskListParams,
    confirm_when=lambda p: p.action == "delete",
)
def task_list(params: TaskListParams, ctx: ToolContext) -> ToolResult:
    try:
        with session_scope() as session:
            if params.action == "add":
                if not params.text.strip():
                    return ToolResult(ok=False, error="'text' is required for action='add'")
                todo = Todo(user_id=ctx.user_id, text=params.text.strip())
                session.add(todo)
                session.flush()
                return ToolResult(ok=True, data=_todo_dict(todo))

            if params.action == "list":
                todos = session.scalars(
                    select(Todo).where(Todo.user_id == ctx.user_id).order_by(Todo.id)
                ).all()
                return ToolResult(ok=True, data={"todos": [_todo_dict(t) for t in todos]})

            # done / delete: can todo_id va todo phai thuoc dung user
            if params.todo_id is None:
                return ToolResult(
                    ok=False, error=f"'todo_id' is required for action='{params.action}'"
                )
            todo = session.scalar(
                select(Todo).where(Todo.id == params.todo_id, Todo.user_id == ctx.user_id)
            )
            if todo is None:
                return ToolResult(ok=False, error=f"Todo {params.todo_id} not found")

            if params.action == "done":
                todo.done = True
                session.flush()
                return ToolResult(ok=True, data=_todo_dict(todo))

            # delete
            session.delete(todo)
            return ToolResult(ok=True, data={"deleted_id": params.todo_id})
    except Exception as e:
        logger.exception("task_list failed")
        return ToolResult(ok=False, error=f"task_list failed: {e}")
