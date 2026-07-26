"""Pydantic schemas dung chung cho agent loop, LLM structured output va API.

Cac schema LLM structured output (PlanStep, ReActAction) co model_validator
chat: output sai ngu nghia (action='tool' ma thieu ten tool, action='final'
ma khong co final_answer, plan step rong...) bi tu choi NGAY luc validate ->
kich hoat vong self-correction trong `call_structured`, thay vi lot vao agent
loop roi hong o executor voi loi kho hieu hon.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class RouteDecision(BaseModel):
    """Ket qua buoc classify."""

    route: Literal["direct", "single_tool", "multi_step", "clarify"]
    reason: str = ""


class PlanStep(BaseModel):
    tool: str
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""

    @model_validator(mode="after")
    def _tool_must_be_named(self) -> "PlanStep":
        if not self.tool.strip():
            raise ValueError("plan step requires a non-empty 'tool' name")
        return self


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

    @model_validator(mode="after")
    def _action_fields_must_match(self) -> "ReActAction":
        if self.action == "tool" and not (self.tool or "").strip():
            raise ValueError("action='tool' requires a non-empty 'tool' name")
        if self.action == "final" and not (self.final_answer or "").strip():
            raise ValueError("action='final' requires a non-empty 'final_answer'")
        return self


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
