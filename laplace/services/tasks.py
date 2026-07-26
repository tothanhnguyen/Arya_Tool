"""CRUD service cho Task/User/Conversation."""

from datetime import UTC, datetime

from sqlalchemy import select
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
