"""Test bot nho ngu canh hoi thoai: lich su chat duoc dua vao prompt,
cau tra loi assistant duoc luu lai cho cac task sau."""

from sqlalchemy import select

from laplace.agent.orchestrator import run_task
from laplace.llm.mock import MockLLM
from laplace.models import Message
from laplace.services.tasks import (
    add_message,
    conversation_context,
    create_task,
    get_or_create_conversation,
    get_or_create_user,
)


def _setup_conv(session):
    user = get_or_create_user(session, tg_id=111)
    conv = get_or_create_conversation(session, user.id)
    add_message(session, conv.id, "user", "tôi tên là Thanh")
    add_message(session, conv.id, "assistant", "Chào Thanh, rất vui được gặp bạn!")
    return user, conv


def _all_prompt_text(llm: MockLLM, call_idx: int) -> str:
    return "\n".join(m["content"] for m in llm.calls[call_idx]["messages"])


def test_conversation_flows_into_classify_and_direct_prompts(session):
    user, conv = _setup_conv(session)
    # Bot luu tin nhan user truoc khi chay task (nhu handler that)
    add_message(session, conv.id, "user", "tôi tên gì?")
    task = create_task(session, user_id=user.id, request="tôi tên gì?",
                       conversation_id=conv.id)
    session.commit()

    llm = MockLLM(script=[
        {"route": "direct", "reason": "follow-up about earlier message"},
        "Bạn tên là Thanh.",
    ])
    result = run_task(task.id, llm=llm)

    assert result.status == "done"
    classify_prompt = _all_prompt_text(llm, 0)
    answer_prompt = _all_prompt_text(llm, 1)
    for prompt in (classify_prompt, answer_prompt):
        assert "tôi tên là Thanh" in prompt          # lich su co mat
        assert "Recent conversation" in prompt
    # Request hien tai khong bi lap lai trong khoi hoi thoai (chi xuat hien
    # o phan "User request")
    assert classify_prompt.count("tôi tên gì?") == 1


def test_assistant_reply_saved_for_next_task(session):
    user, conv = _setup_conv(session)
    task = create_task(session, user_id=user.id, request="tôi tên gì?",
                       conversation_id=conv.id)
    session.commit()
    run_task(task.id, llm=MockLLM(script=[
        {"route": "direct", "reason": "x"}, "Bạn tên là Thanh.",
    ]))

    session.expire_all()
    contents = [m.content for m in session.scalars(
        select(Message).where(Message.conversation_id == conv.id).order_by(Message.id)
    )]
    assert "Bạn tên là Thanh." in contents  # finish_task da luu cau tra loi

    # Task tiep theo thay duoc cau tra loi truoc do trong ngu canh
    task2 = create_task(session, user_id=user.id, request="viết tắt tên tôi đi",
                        conversation_id=conv.id)
    session.commit()
    llm2 = MockLLM(script=[{"route": "direct", "reason": "x"}, "T."])
    run_task(task2.id, llm=llm2)
    assert "Bạn tên là Thanh." in _all_prompt_text(llm2, 0)


def test_react_loop_gets_conversation(session):
    from pydantic import BaseModel

    from laplace.schemas import ToolResult
    from laplace.tools.base import ToolContext, tool

    class EchoParams(BaseModel):
        text: str = ""

    @tool("echo_ctx", "echoes", params=EchoParams)
    def echo(params: EchoParams, ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, data={"echo": params.text})

    user, conv = _setup_conv(session)
    task = create_task(session, user_id=user.id, request="echo tên tôi",
                       conversation_id=conv.id)
    session.commit()
    llm = MockLLM(script=[
        {"route": "single_tool", "reason": "x"},
        {"action": "tool", "tool": "echo_ctx", "params": {"text": "Thanh"}, "thought": ""},
        {"action": "final", "final_answer": "Đã echo: Thanh"},
    ])
    result = run_task(task.id, llm=llm)
    assert result.status == "done"
    assert "tôi tên là Thanh" in _all_prompt_text(llm, 1)  # react prompt co ngu canh


def test_task_without_conversation_unaffected(session):
    user = get_or_create_user(session)
    task = create_task(session, user_id=user.id, request="hello")
    session.commit()
    llm = MockLLM(script=[{"route": "direct", "reason": "x"}, "hi"])
    result = run_task(task.id, llm=llm)
    assert result.status == "done"
    assert "Recent conversation" not in _all_prompt_text(llm, 0)
    assert conversation_context(session, task) == []
