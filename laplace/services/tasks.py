"""CRUD service cho Task/User/Conversation."""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from laplace.models import Conversation, Message, Task, User


def get_or_create_user(session: Session, tg_id: int | None = None) -> User:
    if tg_id is not None:
        user = session.scalar(select(User).where(User.tg_id == tg_id))
        if user:
            return user
    user = User(tg_id=tg_id)
    session.add(user)
    session.flush()
    return user


def get_or_create_conversation(session: Session, user_id: int) -> Conversation:
    conv = session.scalar(
        select(Conversation).where(Conversation.user_id == user_id).order_by(Conversation.id.desc())
    )
    if conv is None:
        conv = Conversation(user_id=user_id)
        session.add(conv)
        session.flush()
    return conv


def add_message(session: Session, conversation_id: int, role: str, content: str) -> Message:
    msg = Message(conversation_id=conversation_id, role=role, content=content)
    session.add(msg)
    session.flush()
    return msg


def recent_messages(session: Session, conversation_id: int, limit: int = 10) -> list[Message]:
    rows = session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id.desc())
        .limit(limit)
    ).all()
    return list(reversed(rows))


def recover_orphan_tasks(session: Session) -> int:
    """Danh dau task mo coi khi app KHOI DONG.

    Luc khoi dong, khong the co task nao dang chay hop le trong tien trinh nay
    -> moi task 'running'/'pending' la di san cua tien trinh cu bi chet giua
    chung (vd: kill, crash, het quota roi bi restart). Danh 'failed' ro rang
    de bot/UI khong hien thi treo vinh vien. 'awaiting_confirm' GIU NGUYEN:
    resume duoc theo thiet ke (state day du trong state_json).
    """
    result = session.execute(
        update(Task)
        .where(Task.status.in_(("running", "pending")))
        .values(
            status="failed",
            error="interrupted by app restart",
            finished_at=datetime.now(UTC),
        )
    )
    return int(result.rowcount or 0)


def conversation_context(
    session: Session, task: Task, limit: int = 10, max_chars: int = 500
) -> list[dict[str, str]]:
    """Lich su hoi thoai gan nhat cua task, dang [{role, content}] cho prompt.

    - Task khong gan conversation (tao qua API) -> [] (agent chay y nhu cu).
    - Bo tin nhan cuoi neu chinh la request hien tai (bot da luu no truoc khi
      run_task) de khong lap lai trong prompt.
    - Cat moi tin con max_chars ky tu de prompt khong phinh.
    """
    if not task.conversation_id:
        return []
    msgs = recent_messages(session, task.conversation_id, limit=limit + 1)
    out = [{"role": m.role, "content": m.content[:max_chars]} for m in msgs]
    if out and out[-1]["role"] == "user" and task.request.startswith(out[-1]["content"]):
        out.pop()
    return out[-limit:]


def create_task(
    session: Session,
    user_id: int,
    request: str,
    strategy: str = "react",
    conversation_id: int | None = None,
) -> Task:
    task = Task(
        user_id=user_id, request=request, strategy=strategy, conversation_id=conversation_id
    )
    session.add(task)
    session.flush()
    return task


def finish_task(session: Session, task: Task, status: str, result: str | None = None,
                error: str | None = None) -> None:
    task.status = status
    task.result = result
    task.error = error
    task.finished_at = datetime.now(UTC)
    session.add(task)
    # Luu cau tra loi vao hoi thoai de cac task sau co ngu canh (bot chi luu
    # tin nhan nguoi dung; phia assistant luu tai day — mot cho duy nhat).
    if status == "done" and task.conversation_id and result:
        add_message(session, task.conversation_id, "assistant", result)
