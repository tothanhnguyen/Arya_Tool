"""Tests cho streaming status ve Telegram (laplace/bot/streaming.py).

Khong can Telegram that: FakeStatusMessage ghi lai cac lan edit_text.
Test async chay bang asyncio.run trong ham sync (repo khong dung pytest-asyncio).
"""

import asyncio

from pydantic import BaseModel

from laplace.agent.orchestrator import resume_task, run_task
from laplace.bot.streaming import (
    CLASSIFY,
    COMPOSE,
    EVALUATE,
    INITIAL_TEXT,
    PLAN,
    THINK,
    ProgressEvent,
    ProgressLLM,
    ProgressStreamer,
    tool_event,
)
from laplace.llm.mock import MockLLM
from laplace.models import Task
from laplace.schemas import Plan, ReActAction, RouteDecision, ToolResult, Verdict
from laplace.services.tasks import create_task, get_or_create_user
from laplace.tools.base import ToolContext, clear_registry, tool


class FakeStatusMessage:
    """Gia lap message trang thai cua aiogram: ghi lai moi lan edit."""

    def __init__(self, fail: bool = False):
        self.edits: list[str] = []
        self.markups: list[object] = []
        self.fail = fail

    async def edit_text(self, text: str, reply_markup=None) -> None:
        if self.fail:
            raise RuntimeError("edit failed")
        self.edits.append(text)
        self.markups.append(reply_markup)


def collect_events(llm: MockLLM, calls: list[tuple]) -> list[ProgressEvent]:
    """Chay tung cap (json_schema, ...) qua ProgressLLM, gom event phat ra."""
    events: list[ProgressEvent] = []
    wrapper = ProgressLLM(llm, events.append)
    for schema in calls:
        wrapper.complete([{"role": "user", "content": "x"}], json_schema=schema)
    return events


# ---------- ProgressLLM: suy ra giai doan tu schema + parsed ----------


def test_progress_llm_react_flow_events():
    llm = MockLLM(
        script=[
            {"route": "multi_step", "reason": "r"},
            {"thought": "t", "action": "tool", "tool": "web_search", "params": {}},
            {"thought": "t", "action": "final", "final_answer": "done"},
        ]
    )
    events = collect_events(
        llm,
        [
            RouteDecision.model_json_schema(),
            ReActAction.model_json_schema(),
            ReActAction.model_json_schema(),
        ],
    )
    assert events == [CLASSIFY, THINK, tool_event("web_search"), THINK]


def test_progress_llm_plan_execute_flow_events():
    llm = MockLLM(
        script=[
            {"route": "multi_step", "reason": "r"},
            {
                "steps": [
                    {"tool": "web_search", "params": {}},
                    {"tool": "report_builder", "params": {}},
                ]
            },
            {"decision": "continue", "reason": "next"},
            {"decision": "done", "reason": "ok"},
            "final text",
        ]
    )
    wrapper_events: list[ProgressEvent] = []
    wrapper = ProgressLLM(llm, wrapper_events.append)
    wrapper.complete([], json_schema=RouteDecision.model_json_schema())
    wrapper.complete([], json_schema=Plan.model_json_schema())
    wrapper.complete([], json_schema=Verdict.model_json_schema())
    wrapper.complete([], json_schema=Verdict.model_json_schema())
    wrapper.complete([])  # final answer, khong schema

    assert wrapper_events == [
        CLASSIFY,
        PLAN,
        tool_event("web_search"),  # buoc 1 sap chay
        EVALUATE,
        tool_event("report_builder"),  # continue -> buoc 2
        EVALUATE,  # decision done -> khong emit tool nua
        COMPOSE,
    ]


def test_progress_llm_unknown_tool_has_generic_label():
    ev = tool_event("something_new")
    assert "something_new" in ev.label
    assert ev.doing.startswith("🔧 Đang ")


def test_progress_llm_preserves_result_and_name():
    llm = MockLLM(script=["hello"])
    wrapper = ProgressLLM(llm, lambda e: None)
    result = wrapper.complete([{"role": "user", "content": "hi"}])
    assert result.content == "hello"
    assert wrapper.name == "mock"


def test_progress_llm_emit_error_does_not_break_call():
    def boom(_event):
        raise RuntimeError("emit failed")

    llm = MockLLM(script=[{"route": "direct", "reason": "r"}])
    wrapper = ProgressLLM(llm, boom)
    result = wrapper.complete([], json_schema=RouteDecision.model_json_schema())
    assert result.parsed == {"route": "direct", "reason": "r"}


# ---------- ProgressStreamer: render + edit mot message duy nhat ----------


def test_render_shows_done_and_current_phase():
    streamer = ProgressStreamer(FakeStatusMessage(), min_interval=0)
    streamer._apply(CLASSIFY)
    streamer._apply(tool_event("web_search"))
    text = streamer.render()
    assert INITIAL_TEXT in text
    assert CLASSIFY.done in text  # "✅ Phân loại yêu cầu"
    assert "🔍 Đang tìm kiếm web…" in text


def test_render_dedupes_repeated_phase():
    streamer = ProgressStreamer(FakeStatusMessage(), min_interval=0)
    streamer._apply(THINK)
    streamer._apply(THINK)  # retry schema -> khong nhan doi
    assert streamer.render().count(THINK.doing) == 1
    assert THINK.done not in streamer.render()


def test_render_caps_done_lines():
    streamer = ProgressStreamer(FakeStatusMessage(), min_interval=0, max_lines=3)
    for i in range(6):
        streamer._apply(ProgressEvent("🔧", f"bước {i}"))
    text = streamer.render()
    assert "…" in text
    assert "✅ Bước 0" not in text
    assert "✅ Bước 4" in text
    assert "🔧 Đang bước 5…" in text


def test_streamer_edits_single_message_during_run():
    status = FakeStatusMessage()
    streamer = ProgressStreamer(status, min_interval=0)

    def worker(llm):
        llm.complete([], json_schema=RouteDecision.model_json_schema())
        llm.complete([])
        return "ok"

    inner = MockLLM(script=[{"route": "direct", "reason": "r"}, "answer"])
    result = asyncio.run(streamer.run(worker, llm=inner))
    assert result == "ok"
    assert len(status.edits) >= 1  # co it nhat mot lan edit tien do
    assert all(INITIAL_TEXT in e for e in status.edits)


def test_streamer_run_propagates_worker_exception():
    status = FakeStatusMessage()
    streamer = ProgressStreamer(status, min_interval=0)

    def worker(_llm):
        raise ValueError("boom")

    try:
        asyncio.run(streamer.run(worker))
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_finalize_replaces_status_message():
    status = FakeStatusMessage()
    streamer = ProgressStreamer(status, min_interval=0)
    ok = asyncio.run(streamer.finalize("KẾT QUẢ CUỐI"))
    assert ok is True
    assert status.edits[-1] == "KẾT QUẢ CUỐI"


def test_finalize_returns_false_when_edit_fails():
    streamer = ProgressStreamer(FakeStatusMessage(fail=True), min_interval=0)
    assert asyncio.run(streamer.finalize("x")) is False


# ---------- Tich hop voi orchestrator that (MockLLM script + DB tam) ----------


class WipeParams(BaseModel):
    target: str = ""


def _register_wipe() -> list[str]:
    calls: list[str] = []

    @tool(
        name="wipe",
        description="Delete data. Destructive.",
        params=WipeParams,
        requires_confirmation=True,
    )
    def wipe(params: WipeParams, ctx: ToolContext) -> ToolResult:
        calls.append(params.target)
        return ToolResult(ok=True, data={"wiped": params.target})

    return calls


def test_streaming_end_to_end_with_run_task(session):
    """run_task chay qua streamer: tien do duoc edit, ket qua cuoi dung."""
    user = get_or_create_user(session)
    task = create_task(session, user_id=user.id, request="hoi don gian", strategy="react")
    session.commit()
    task_id = task.id

    llm = MockLLM(script=[{"route": "direct", "reason": "simple"}, "cau tra loi cuoi"])
    status = FakeStatusMessage()
    streamer = ProgressStreamer(status, min_interval=0)

    asyncio.run(streamer.run(lambda p: run_task(task_id, llm=p), llm=llm))

    session.expire_all()
    assert session.get(Task, task_id).status == "done"
    assert session.get(Task, task_id).result == "cau tra loi cuoi"
    # Message trang thai da duoc edit voi tien do (classify -> tong hop)
    joined = "\n".join(status.edits)
    assert CLASSIFY.done in joined or CLASSIFY.doing in joined
    assert COMPOSE.doing in joined


def test_streaming_confirm_flow_not_broken(session):
    """Task pause awaiting_confirm roi resume qua streamer van hoat dong."""
    clear_registry()
    wiped = _register_wipe()
    user = get_or_create_user(session)
    task = create_task(session, user_id=user.id, request="wipe notes", strategy="react")
    session.commit()
    task_id = task.id

    llm = MockLLM(
        script=[
            {"route": "multi_step", "reason": "destructive"},
            {"thought": "w", "action": "tool", "tool": "wipe", "params": {"target": "notes"}},
            {"thought": "ok", "action": "final", "final_answer": "da xoa notes"},
        ]
    )

    status1 = FakeStatusMessage()
    streamer1 = ProgressStreamer(status1, min_interval=0)
    asyncio.run(streamer1.run(lambda p: run_task(task_id, llm=p), llm=llm))

    session.expire_all()
    assert session.get(Task, task_id).status == "awaiting_confirm"
    assert wiped == []  # confirm-flow giu nguyen: tool chua chay

    status2 = FakeStatusMessage()
    streamer2 = ProgressStreamer(status2, min_interval=0)
    asyncio.run(streamer2.run(lambda p: resume_task(task_id, True, llm=p), llm=llm))

    session.expire_all()
    assert session.get(Task, task_id).status == "done"
    assert session.get(Task, task_id).result == "da xoa notes"
    assert wiped == ["notes"]
    clear_registry()
