"""Hai chien luoc agent cam vao cung khung state machine.

- ReActStrategy: nghi -> goi tool -> quan sat, lap lai tung buoc.
- PlanExecuteStrategy: lap ke hoach truoc, thuc thi tuan tu, evaluate sau moi
  observation (done / continue / replan / fail).

Ca hai chia se:
- `call_structured`: goi LLM voi json_schema + validate Pydantic + retry
  (self-correction) toi da MAX_SCHEMA_RETRIES lan.
- Human-in-the-loop: truoc khi execute tool can confirm, luu du state vao
  `task.state_json` de resume_task khoi phuc va tiep tuc dung cho.
"""

import logging
import time
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from laplace.agent import prompts
from laplace.config import get_settings
from laplace.llm.base import LLMProvider
from laplace.models import Step, Task
from laplace.schemas import Plan, PlanStep, ReActAction, ToolResult, Verdict
from laplace.services.tasks import conversation_context, finish_task
from laplace.services.trace import record_llm_call, record_step
from laplace.tools.base import ToolContext, ToolSpec, get_tool
from laplace.tools.base import execute as execute_tool

logger = logging.getLogger(__name__)

MAX_SCHEMA_RETRIES = 2  # so lan retry them sau lan goi dau tien
MAX_REPLANS = 2

REJECTED_OBSERVATION = ToolResult(ok=False, error="user rejected")

ModelT = TypeVar("ModelT", bound=BaseModel)


class SchemaValidationError(Exception):
    """LLM khong tra ve structured output hop le sau khi da retry."""


def call_structured(
    session: Session,
    llm: LLMProvider,
    messages: list[dict[str, str]],
    model_cls: type[ModelT],
    *,
    purpose: str,
    task_id: int,
    step_id: int | None = None,
) -> ModelT:
    """Goi LLM ep JSON schema, validate bang Pydantic, retry kem thong bao loi.

    Self-correction: moi lan output sai schema, thong bao loi validation duoc
    gui lai cho LLM tu sua, toi da MAX_SCHEMA_RETRIES lan. Moi lan goi (ke ca
    retry) deu ghi record_llm_call; lan retry mang purpose "<purpose>:fixN" de
    trace viewer/metric dem duoc so lan tu sua.
    """
    msgs = list(messages)
    schema = model_cls.model_json_schema()
    last_error: ValidationError | None = None
    for attempt in range(MAX_SCHEMA_RETRIES + 1):
        result = llm.complete(msgs, json_schema=schema)
        record_llm_call(
            session, result,
            purpose=purpose if attempt == 0 else f"{purpose}:fix{attempt}",
            provider=llm.name, task_id=task_id, step_id=step_id,
        )
        try:
            return model_cls.model_validate(result.parsed)
        except ValidationError as e:
            last_error = e
            logger.warning(
                "structured output invalid purpose=%s model=%s attempt=%d/%d: %s",
                purpose, model_cls.__name__, attempt + 1, MAX_SCHEMA_RETRIES + 1, e,
            )
            msgs = msgs + [prompts.validation_error_message(model_cls.__name__, str(e))]
    raise SchemaValidationError(
        f"LLM failed to produce a valid {model_cls.__name__} after "
        f"{MAX_SCHEMA_RETRIES + 1} attempts: {last_error}"
    )


def _try_parse_params(spec: ToolSpec, params: dict[str, Any]) -> BaseModel | None:
    """Parse params de kiem tra needs_confirm; sai schema -> None (executor se bao loi)."""
    try:
        return spec.params_model(**params)
    except ValidationError:
        return None


def _run_tool(
    session: Session, task: Task, idx: int, tool_name: str, params: dict[str, Any]
) -> ToolResult:
    """Thuc thi tool + ghi Step (do latency bang time.monotonic).

    Commit truoc khi goi tool: tool tu mo session rieng de ghi DB (note_store,
    task_list, scheduler); neu session ngoai con giu write-lock SQLite thi tool
    se dinh 'database is locked' (self-deadlock trong cung mot thread).
    """
    session.commit()
    start = time.monotonic()
    obs = execute_tool(tool_name, params, ToolContext(user_id=task.user_id, task_id=task.id))
    latency_ms = int((time.monotonic() - start) * 1000)
    record_step(
        session, task.id, idx, tool_name, params,
        observation=obs, status="ok" if obs.ok else "error", latency_ms=latency_ms,
    )
    return obs


def _pause_for_confirm(
    session: Session,
    task: Task,
    idx: int,
    tool_name: str,
    params: dict[str, Any],
    extra_state: dict[str, Any],
    thought: str = "",
) -> None:
    """Dung task cho nguoi dung xac nhan; luu du state de resume."""
    step = record_step(
        session, task.id, idx, tool_name, params, observation=None, status="pending_confirm"
    )
    pending: dict[str, Any] = {"tool": tool_name, "params": params, "step_id": step.id}
    if thought:
        pending["thought"] = thought
    task.state_json = {"pending": pending, **extra_state}
    task.status = "awaiting_confirm"
    session.add(task)
    session.flush()


def _resolve_pending(
    session: Session, task: Task, approved: bool
) -> tuple[dict[str, Any], str, dict[str, Any], ToolResult]:
    """Khoi phuc state va xu ly pending step khi resume.

    approved=True -> thuc thi tool va cap nhat Step pending_confirm.
    approved=False -> Step chuyen 'rejected', observation user-rejected.
    """
    state = dict(task.state_json or {})
    pending = dict(state.pop("pending", {}) or {})
    tool_name: str = pending.get("tool", "")
    params: dict[str, Any] = pending.get("params", {}) or {}
    step = session.get(Step, pending["step_id"]) if pending.get("step_id") else None

    if not approved:
        obs = REJECTED_OBSERVATION.model_copy()
        if step is not None:
            step.status = "rejected"
            step.observation_json = obs.as_observation()
    else:
        session.commit()  # tha write-lock truoc khi tool mo session rieng
        start = time.monotonic()
        obs = execute_tool(
            tool_name, params, ToolContext(user_id=task.user_id, task_id=task.id)
        )
        latency_ms = int((time.monotonic() - start) * 1000)
        if step is not None:
            step.status = "ok" if obs.ok else "error"
            step.observation_json = obs.as_observation()
            step.latency_ms = latency_ms
    return state, tool_name, params, obs


class AgentStrategy(Protocol):
    """Interface chung: 2 chien luoc cam vao cung mot orchestrator."""

    name: str

    def run(self, session: Session, task: Task, llm: LLMProvider, route: str) -> None: ...

    def resume(self, session: Session, task: Task, llm: LLMProvider, approved: bool) -> None: ...


class ReActStrategy:
    """Moi vong: LLM chon (tool, params) hoac tra loi cuoi; observation vao history."""

    name = "react"

    def run(self, session: Session, task: Task, llm: LLMProvider, route: str) -> None:
        self._loop(session, task, llm, {"route": route, "history": [], "step_idx": 0})

    def resume(self, session: Session, task: Task, llm: LLMProvider, approved: bool) -> None:
        state, tool_name, params, obs = _resolve_pending(session, task, approved)
        history: list[dict[str, Any]] = state.get("history", [])
        history.append(
            {
                "thought": "(resumed after user confirmation)",
                "tool": tool_name,
                "params": params,
                "observation": obs.as_observation(),
            }
        )
        state["history"] = history
        state["step_idx"] = int(state.get("step_idx", 0)) + 1
        # approved=False: observation "user rejected" da nam trong history,
        # LLM tu quyet dinh buoc tiep theo trong vong lap nhu thuong.
        self._loop(session, task, llm, state)

    def _loop(
        self, session: Session, task: Task, llm: LLMProvider, state: dict[str, Any]
    ) -> None:
        settings = get_settings()
        deadline = time.monotonic() + settings.task_timeout_s
        history: list[dict[str, Any]] = state["history"]
        idx = int(state.get("step_idx", 0))
        conversation = conversation_context(session, task)
        while idx < settings.max_steps:
            if time.monotonic() >= deadline:
                finish_task(session, task, "failed", error="task timeout exceeded")
                return
            action = call_structured(
                session, llm,
                prompts.build_react_messages(task.request, history, conversation=conversation),
                ReActAction, purpose="react", task_id=task.id,
            )
            if action.action == "final":
                task.state_json = {}
                finish_task(session, task, "done", result=action.final_answer or "")
                return

            tool_name = action.tool or ""
            spec = get_tool(tool_name)
            if spec is not None:
                parsed = _try_parse_params(spec, action.params)
                if parsed is not None and spec.needs_confirm(parsed):
                    _pause_for_confirm(
                        session, task, idx, tool_name, action.params,
                        {"route": state.get("route"), "history": history, "step_idx": idx},
                        thought=action.thought,
                    )
                    return
            obs = _run_tool(session, task, idx, tool_name, action.params)
            history.append(
                {
                    "thought": action.thought,
                    "tool": tool_name,
                    "params": action.params,
                    "observation": obs.as_observation(),
                }
            )
            idx += 1
            state["step_idx"] = idx
        finish_task(session, task, "failed", error="step limit exceeded")


class PlanExecuteStrategy:
    """Lap plan truoc, thuc thi tuan tu; evaluate sau moi buoc, replan toi da 2 lan."""

    name = "plan_execute"

    def run(self, session: Session, task: Task, llm: LLMProvider, route: str) -> None:
        plan = call_structured(
            session, llm,
            prompts.build_plan_messages(
                task.request, conversation=conversation_context(session, task)
            ),
            Plan, purpose="plan", task_id=task.id,
        )
        state = {
            "route": route,
            "history": [],
            "plan": plan.model_dump(),
            "cursor": 0,
            "executed": 0,
            "replans": 0,
        }
        self._loop(session, task, llm, state)

    def resume(self, session: Session, task: Task, llm: LLMProvider, approved: bool) -> None:
        state, tool_name, params, obs = _resolve_pending(session, task, approved)
        if not approved:
            # Plan tuyen tinh: buoc bi tu choi lam ke hoach vo nghia -> fail ro rang.
            finish_task(
                session, task, "failed",
                error=f"user rejected tool '{tool_name}'",
            )
            return
        history: list[dict[str, Any]] = state.get("history", [])
        history.append(
            {"tool": tool_name, "params": params, "observation": obs.as_observation()}
        )
        state["history"] = history
        state["executed"] = int(state.get("executed", 0)) + 1
        state["cursor"] = int(state.get("cursor", 0)) + 1
        if self._evaluate(session, task, llm, state):
            self._loop(session, task, llm, state)

    def _loop(
        self, session: Session, task: Task, llm: LLMProvider, state: dict[str, Any]
    ) -> None:
        settings = get_settings()
        deadline = time.monotonic() + settings.task_timeout_s
        while True:
            if time.monotonic() >= deadline:
                finish_task(session, task, "failed", error="task timeout exceeded")
                return
            steps: list[dict[str, Any]] = state["plan"].get("steps", [])
            cursor = int(state["cursor"])
            if cursor >= len(steps):
                # Het plan ma evaluator chua chot "done" -> van sinh final answer.
                self._finalize(session, task, llm, state)
                return
            if int(state["executed"]) >= settings.max_steps:
                finish_task(session, task, "failed", error="step limit exceeded")
                return

            step = PlanStep.model_validate(steps[cursor])
            spec = get_tool(step.tool)
            if spec is not None:
                parsed = _try_parse_params(spec, step.params)
                if parsed is not None and spec.needs_confirm(parsed):
                    _pause_for_confirm(
                        session, task, int(state["executed"]), step.tool, step.params,
                        {
                            "route": state.get("route"),
                            "history": state["history"],
                            "plan": state["plan"],
                            "cursor": cursor,
                            "executed": state["executed"],
                            "replans": state["replans"],
                        },
                        thought=step.rationale,
                    )
                    return
            obs = _run_tool(session, task, int(state["executed"]), step.tool, step.params)
            state["history"].append(
                {"tool": step.tool, "params": step.params, "observation": obs.as_observation()}
            )
            state["executed"] = int(state["executed"]) + 1
            state["cursor"] = cursor + 1
            if not self._evaluate(session, task, llm, state):
                return

    def _evaluate(
        self, session: Session, task: Task, llm: LLMProvider, state: dict[str, Any]
    ) -> bool:
        """Goi LLM evaluate observation moi nhat. Tra ve True neu tiep tuc vong lap."""
        verdict = call_structured(
            session, llm,
            prompts.build_evaluate_messages(task.request, state["plan"], state["history"]),
            Verdict, purpose="evaluate", task_id=task.id,
        )
        if verdict.decision == "done":
            self._finalize(session, task, llm, state)
            return False
        if verdict.decision == "fail":
            finish_task(
                session, task, "failed",
                error=verdict.reason or "evaluator judged the task as failed",
            )
            return False
        if verdict.decision == "replan":
            if int(state.get("replans", 0)) >= MAX_REPLANS:
                finish_task(session, task, "failed", error="replan limit exceeded")
                return False
            state["replans"] = int(state.get("replans", 0)) + 1
            plan = call_structured(
                session, llm,
                prompts.build_plan_messages(
                    task.request, history=state["history"],
                    conversation=conversation_context(session, task),
                ),
                Plan, purpose="replan", task_id=task.id,
            )
            state["plan"] = plan.model_dump()
            state["cursor"] = 0
        return True  # continue | replan

    def _finalize(
        self, session: Session, task: Task, llm: LLMProvider, state: dict[str, Any]
    ) -> None:
        result = llm.complete(
            prompts.build_final_messages(
                task.request, state["history"],
                conversation=conversation_context(session, task),
            )
        )
        record_llm_call(session, result, purpose="final", provider=llm.name, task_id=task.id)
        task.state_json = {}
        finish_task(session, task, "done", result=result.content or "")


def get_strategy(name: str) -> AgentStrategy:
    if name == "plan_execute":
        return PlanExecuteStrategy()
    return ReActStrategy()
