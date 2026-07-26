"""Agent orchestrator — diem vao cua state machine.

Transitions:
    RECEIVED(pending) -> running -> CLASSIFY
      route=direct   -> answer -> done
      route=clarify  -> clarifying question -> done
      route=single_tool | multi_step -> strategy loop (react | plan_execute)
        -> EXECUTE_STEP -> OBSERVE -> (NEXT_STEP | REPLAN | AWAIT_CONFIRM | DONE | FAILED)
    awaiting_confirm --resume_task(approved)--> running -> tiep tuc loop -> done|failed

Ca hai ham deu SYNC; interface layer goi qua asyncio.to_thread. Moi call LLM va
moi tool execution deu duoc trace (llm_calls / steps) de trace viewer dung lai
timeline day du.
"""

from sqlalchemy import update

from laplace.agent import prompts
from laplace.agent.strategies import SchemaValidationError, call_structured, get_strategy
from laplace.db import session_scope
from laplace.llm.base import LLMProvider, get_provider
from laplace.models import Task
from laplace.schemas import RouteDecision
from laplace.services.tasks import conversation_context, finish_task
from laplace.services.trace import record_llm_call


def run_task(task_id: int, llm: LLMProvider | None = None) -> Task:
    """Chay mot task tu dau: classify -> route -> strategy loop.

    Tra ve Task o trang thai cuoi (done | failed | awaiting_confirm).
    """
    provider = llm or get_provider()
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        task.status = "running"
        session.add(task)
        # Commit ngay: khong giu write-lock SQLite suot ca loop (LLM call co the lau)
        session.commit()
        try:
            conversation = conversation_context(session, task)
            route = call_structured(
                session, provider,
                prompts.build_classify_messages(task.request, conversation=conversation),
                RouteDecision, purpose="classify", task_id=task.id,
            )
            task.route = route.route  # luu cho trace viewer + eval harness
            if route.route == "direct":
                result = provider.complete(
                    prompts.build_direct_messages(task.request, conversation=conversation)
                )
                record_llm_call(
                    session, result, purpose="answer",
                    provider=provider.name, task_id=task.id,
                )
                finish_task(session, task, "done", result=result.content or "")
            elif route.route == "clarify":
                result = provider.complete(
                    prompts.build_clarify_messages(
                        task.request, route.reason, conversation=conversation
                    )
                )
                record_llm_call(
                    session, result, purpose="clarify",
                    provider=provider.name, task_id=task.id,
                )
                finish_task(session, task, "done", result=result.content or "")
            else:  # single_tool | multi_step
                get_strategy(task.strategy).run(session, task, provider, route.route)
        except SchemaValidationError as e:
            finish_task(session, task, "failed", error=str(e))
        return task


def resume_task(task_id: int, approved: bool, llm: LLMProvider | None = None) -> Task:
    """Tiep tuc task dang awaiting_confirm sau khi nguoi dung bam confirm/reject.

    approved=True  -> thuc thi pending step roi tiep tuc vong lap nhu thuong.
    approved=False -> Step 'rejected'; ReAct de LLM tu xoay so, plan_execute fail.
    """
    provider = llm or get_provider()
    with session_scope() as session:
        # Compare-and-set nguyen tu: chi MOT lenh resume "gianh" duoc task.
        # Hai request confirm gan dong thoi -> request thu hai rowcount=0,
        # khong bao gio thuc thi pending tool lan thu hai.
        claimed = session.execute(
            update(Task)
            .where(Task.id == task_id, Task.status == "awaiting_confirm")
            .values(status="running")
        ).rowcount
        session.commit()
        task = session.get(Task, task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")
        if not claimed:
            raise ValueError(
                f"Task {task_id} is not awaiting confirmation (status={task.status})"
            )
        if not (task.state_json or {}).get("pending"):
            finish_task(session, task, "failed", error="no pending step to resume")
            raise ValueError(f"Task {task_id} has no pending step to resume")
        try:
            get_strategy(task.strategy).resume(session, task, provider, approved)
        except SchemaValidationError as e:
            finish_task(session, task, "failed", error=str(e))
        return task
