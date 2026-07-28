from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from laplace.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    auth_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        unique=True,
        nullable=True,
    )
    tg_id: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    profile_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | system
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id"), nullable=True
    )
    request: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    # pending | running | awaiting_confirm | done | failed
    strategy: Mapped[str] = mapped_column(String(24), default="react")  # react | plan_execute
    route: Mapped[str | None] = mapped_column(String(24), nullable=True)  # ket qua classify
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state_json: Mapped[dict] = mapped_column(JSON, default=dict)  # plan, cursor, pending step
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Step(Base):
    __tablename__ = "steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    idx: Mapped[int] = mapped_column(Integer)
    tool: Mapped[str] = mapped_column(String(64))
    params_json: Mapped[dict] = mapped_column(JSON, default=dict)
    observation_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    # pending | pending_confirm | running | ok | error | rejected
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    step_id: Mapped[int | None] = mapped_column(ForeignKey("steps.id"), nullable=True)
    purpose: Mapped[str] = mapped_column(String(32))  # classify | plan | evaluate | answer | ...
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    cron: Mapped[str] = mapped_column(String(64))  # cron expression 5 truong
    task_template: Mapped[str] = mapped_column(Text)  # noi dung request se chay dinh ky
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Todo(Base):
    __tablename__ = "todos"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    text: Mapped[str] = mapped_column(Text)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
