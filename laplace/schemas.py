"""Pydantic schemas dung chung cho agent loop, LLM structured output va API."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class RouteDecision(BaseModel):
    """Ket qua buoc classify."""

    route: Literal["direct", "single_tool", "multi_step", "clarify"]
    reason: str = ""


class PlanStep(BaseModel):
    tool: str
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""


class Plan(BaseModel):
    steps: list[PlanStep] = Field(default_factory=list)


class Verdict(BaseModel):
    """Danh gia observation sau moi buoc."""

    decision: Literal["done", "continue", "replan", "fail"]
    reason: str = ""


class ReActAction(BaseModel):
    """Mot buoc ReAct: hoac goi tool, hoac tra loi cuoi."""

    thought: str = ""
    action: Literal["tool", "final"]
    tool: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    final_answer: str | None = None


class ToolResult(BaseModel):
    ok: bool
    data: Any = None
    error: str | None = None

    def as_observation(self) -> dict[str, Any]:
        return self.model_dump()


class TaskCreateIn(BaseModel):
    user_id: int | None = None
    request: str
    strategy: Literal["react", "plan_execute"] = "react"


class TaskOut(BaseModel):
    id: int
    request: str
    status: str
    strategy: str
    result: str | None = None
    error: str | None = None


class ConfirmIn(BaseModel):
    approved: bool
