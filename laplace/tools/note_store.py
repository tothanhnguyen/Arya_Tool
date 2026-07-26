"""note_store tool: CRUD ghi chu cua user (bang notes)."""

import logging
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from laplace.db import session_scope
from laplace.models import Note
from laplace.schemas import ToolResult
from laplace.tools.base import ToolContext, tool

logger = logging.getLogger(__name__)


class NoteStoreParams(BaseModel):
    action: Literal["create", "list", "update", "delete"] = Field(
        description="Operation to perform on the user's notes"
    )
    note_id: int | None = Field(
        default=None, description="Note id, required for 'update' and 'delete'"
    )
    title: str = Field(default="", description="Note title (required for 'create')")
    content: str = Field(default="", description="Note body text")


def _note_dict(note: Note) -> dict:
    return {
        "id": note.id,
        "title": note.title,
        "content": note.content,
        "created_at": note.created_at.isoformat() if note.created_at else None,
        "updated_at": note.updated_at.isoformat() if note.updated_at else None,
    }


@tool(
    name="note_store",
    description=(
        "Create, list, update or delete the user's saved NOTES (free-form reference "
        "text: facts, summaries, reminders to keep). "
        "USE FOR: 'lưu ghi chú', saving a summary or fact, showing/editing/removing "
        "saved notes. "
        "DO NOT USE FOR: actionable to-do items (use task_list) or looking things up "
        "on the web (use web_search). "
        "PARAMS: action is one of 'create'|'list'|'update'|'delete'; 'create' needs "
        "title (content optional); 'update' and 'delete' need note_id and require "
        "user confirmation. "
        "Example: {\"action\": \"create\", \"title\": \"deadline proposal\", "
        "\"content\": \"30/8\"}."
    ),
    params=NoteStoreParams,
    confirm_when=lambda p: p.action in ("update", "delete"),
)
def note_store(params: NoteStoreParams, ctx: ToolContext) -> ToolResult:
    try:
        with session_scope() as session:
            if params.action == "create":
                if not params.title.strip():
                    return ToolResult(ok=False, error="'title' is required for action='create'")
                note = Note(
                    user_id=ctx.user_id, title=params.title.strip(), content=params.content
                )
                session.add(note)
                session.flush()
                return ToolResult(ok=True, data=_note_dict(note))

            if params.action == "list":
                notes = session.scalars(
                    select(Note).where(Note.user_id == ctx.user_id).order_by(Note.id)
                ).all()
                return ToolResult(ok=True, data={"notes": [_note_dict(n) for n in notes]})

            # update / delete: can note_id va note phai thuoc dung user
            if params.note_id is None:
                return ToolResult(
                    ok=False, error=f"'note_id' is required for action='{params.action}'"
                )
            note = session.scalar(
                select(Note).where(Note.id == params.note_id, Note.user_id == ctx.user_id)
            )
            if note is None:
                return ToolResult(ok=False, error=f"Note {params.note_id} not found")

            if params.action == "update":
                if params.title.strip():
                    note.title = params.title.strip()
                if params.content:
                    note.content = params.content
                session.flush()
                return ToolResult(ok=True, data=_note_dict(note))

            # delete
            session.delete(note)
            return ToolResult(ok=True, data={"deleted_id": params.note_id})
    except Exception as e:
        logger.exception("note_store failed")
        return ToolResult(ok=False, error=f"note_store failed: {e}")
