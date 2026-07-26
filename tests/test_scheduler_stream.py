"""Tests cho streaming tien do cua task chay tu scheduler (T11).

- laplace/bot/scheduler_stream.py: FakeBot gia lap aiogram Bot (send/edit),
  khong can Telegram that; test async chay qua asyncio.run ben trong ham sync.
- laplace/scheduler.py: _run_scheduled_job uu tien streaming, fallback
  on_result nhu cu khi streaming khong kha dung.
"""

from laplace import scheduler as scheduler_module
from laplace.agent.orchestrator import run_task
from laplace.bot import scheduler_stream as stream_module
from laplace.bot.scheduler_stream import SCHEDULED_CRASH_TEXT, stream_scheduled_task
from laplace.bot.streaming import CLASSIFY, COMPOSE, INITIAL_TEXT
from laplace.db import session_scope
from laplace.llm.mock import MockLLM
from laplace.models import Task
from laplace.scheduler import task_result_text
from laplace.services.tasks import create_task, get_or_create_user


class FakeSession:
    def __init__(self):
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeMessage:
    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeBot:
    """Gia lap aiogram Bot: ghi lai send_message / edit_message_text."""

    def __init__(self, fail_send: bool = False, fail_edit: bool = False):
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.session = FakeSession()
        self.fail_send = fail_send
        self.fail_edit = fail_edit
        self._next_id = 0

    async def send_message(self, chat_id: int, text: str) -> FakeMessage:
        if self.fail_send:
            raise RuntimeError("send failed")
        self._next_id += 1
        self.sent.append((chat_id, text))
        return FakeMessage(self._next_id)

    async def edit_message_text(
        self, text: str, chat_id: int, message_id: int, reply_markup=None
    ) -> None:
        if self.fail_edit:
            raise RuntimeError("edit failed")
        self.edits.append((chat_id, message_id, text))


def _make_task(session, tg_id: int | None = 111) -> tuple[int, int]:
    """Tao user (+tg_id) va task pending; tra ve (user_id, task_id)."""
    user = get_or_create_user(session, tg_id=tg_id)
    task = create_task(session, user_id=user.id, request="viec dinh ky", strategy="react")
    session.commit()
    return user.id, task.id


def _mark_done(task_id: int, result: str) -> None:
    with session_scope() as s:
        task = s.get(Task, task_id)
        task.status = "done"
        task.result = result


# ---------- task_result_text ----------


def test_task_result_text_done_and_failed(session):
    user = get_or_create_user(session)
    task = create_task(session, user_id=user.id, request="x")
    session.commit()
    task_id = task.id

    _mark_done(task_id, "ket qua A")
    assert task_result_text(task_id) == "ket qua A"

    with session_scope() as s:
        t = s.get(Task, task_id)
        t.status = "failed"
        t.error = "loi X"
    text = task_result_text(task_id)
    assert "chưa hoàn thành" in text
    assert "status=failed" in text
    assert "loi X" in text

    assert task_result_text(999_999) == ""


# ---------- stream_scheduled_task: cac duong fallback (tra ve False) ----------


def test_stream_returns_false_without_token(session, monkeypatch):
    from laplace.config import get_settings

    monkeypatch.setattr(get_settings(), "telegram_bot_token", None)
    user_id, task_id = _make_task(session)
    assert stream_scheduled_task(task_id, user_id, run_task) is False


def test_stream_returns_false_without_tg_id(session):
    user_id, task_id = _make_task(session, tg_id=None)
    bot = FakeBot()
    ok = stream_scheduled_task(
        task_id, user_id, run_task, token="tok", bot_factory=lambda _t: bot
    )
    assert ok is False
    assert bot.sent == []  # chua he dong den Telegram


def test_stream_returns_false_when_initial_send_fails(session):
    """Khong gui duoc message trang thai -> task CHUA chay, scheduler fallback."""
    user_id, task_id = _make_task(session)
    ran = []
    bot = FakeBot(fail_send=True)
    ok = stream_scheduled_task(
        task_id,
        user_id,
        lambda tid, llm=None: ran.append(tid),
        token="tok",
        bot_factory=lambda _t: bot,
        min_interval=0,
    )
    assert ok is False
    assert ran == []  # khong duoc chay task (caller se chay duong cu)
    assert bot.session.closed is True


# ---------- stream_scheduled_task: duong chinh ----------


def test_stream_end_to_end_with_run_task(session):
    """Chay run_task that (MockLLM): gui message trang thai, edit tien do,
    finalize bang ket qua cuoi."""
    user_id, task_id = _make_task(session, tg_id=222)
    llm = MockLLM(script=[{"route": "direct", "reason": "r"}, "tra loi cuoi"])
    bot = FakeBot()

    ok = stream_scheduled_task(
        task_id,
        user_id,
        run_task,
        token="tok",
        bot_factory=lambda _t: bot,
        min_interval=0,
        llm=llm,
    )

    assert ok is True
    session.expire_all()
    assert session.get(Task, task_id).status == "done"
    # 1 message trang thai duy nhat, gui vao dung chat tg_id
    assert bot.sent == [(222, INITIAL_TEXT)]
    # Co edit tien do (classify -> tong hop) va edit cuoi la ket qua
    joined = "\n".join(text for _c, _m, text in bot.edits)
    assert CLASSIFY.done in joined or CLASSIFY.doing in joined
    assert COMPOSE.doing in joined
    assert bot.edits[-1] == (222, 1, "tra loi cuoi")
    assert bot.session.closed is True


def test_stream_run_crash_reports_and_returns_true(session):
    """run_task crash: task DA chay -> tra True (khong double-run), bao loi."""
    user_id, task_id = _make_task(session)

    def boom(_tid, llm=None):
        raise RuntimeError("agent crashed")

    bot = FakeBot()
    ok = stream_scheduled_task(
        task_id, user_id, boom, token="tok", bot_factory=lambda _t: bot, min_interval=0
    )
    assert ok is True
    assert bot.edits[-1][2] == SCHEDULED_CRASH_TEXT


def test_stream_long_result_is_chunked(session):
    """Ket qua > 4000 ky tu: finalize phan dau, phan con lai gui message moi."""
    user_id, task_id = _make_task(session)
    long_result = "x" * 9000

    def fake_run(tid, llm=None):
        _mark_done(tid, long_result)

    bot = FakeBot()
    ok = stream_scheduled_task(
        task_id, user_id, fake_run, token="tok", bot_factory=lambda _t: bot, min_interval=0
    )
    assert ok is True
    assert bot.edits[-1][2] == "x" * 4000
    # message dau la INITIAL_TEXT, 2 message sau la phan chunk con lai
    chunk_texts = [text for _c, text in bot.sent[1:]]
    assert chunk_texts == ["x" * 4000, "x" * 1000]


def test_stream_edit_fail_falls_back_to_new_messages(session):
    """Edit that bai (rate limit...): van tra True va gui ket qua bang message moi."""
    user_id, task_id = _make_task(session)

    def fake_run(tid, llm=None):
        _mark_done(tid, "KET QUA CUOI")

    bot = FakeBot(fail_edit=True)
    ok = stream_scheduled_task(
        task_id, user_id, fake_run, token="tok", bot_factory=lambda _t: bot, min_interval=0
    )
    assert ok is True
    assert (111, "KET QUA CUOI") in bot.sent


# ---------- scheduler._run_scheduled_job: noi streaming + fallback ----------


def test_run_scheduled_job_prefers_streaming(session, monkeypatch):
    """Streaming thanh cong -> khong goi on_result (tranh bao ket qua 2 lan)."""
    user = get_or_create_user(session, tg_id=333)
    session.commit()
    user_id = user.id

    streamed: list[tuple[int, int]] = []
    notified: list[tuple[int, str]] = []

    def fake_stream(task_id, uid, run, **kwargs):
        streamed.append((task_id, uid))
        return True

    monkeypatch.setattr(stream_module, "stream_scheduled_task", fake_stream)
    monkeypatch.setattr(scheduler_module, "_on_result", lambda u, r: notified.append((u, r)))

    scheduler_module._run_scheduled_job(1, user_id, "tong hop tin moi sang")

    assert len(streamed) == 1
    assert streamed[0][1] == user_id
    assert notified == []  # streaming da bao ket qua, khong notify lan 2
    # Task da duoc tao tu template
    with session_scope() as s:
        task = s.get(Task, streamed[0][0])
        assert task is not None
        assert task.request == "tong hop tin moi sang"


def test_run_scheduled_job_falls_back_to_on_result(session, monkeypatch):
    """Streaming khong kha dung -> chay run_task thuong + on_result nhu cu."""
    user = get_or_create_user(session)  # khong co tg_id
    session.commit()
    user_id = user.id

    notified: list[tuple[int, str]] = []
    monkeypatch.setattr(stream_module, "stream_scheduled_task", lambda *a, **k: False)
    monkeypatch.setattr(scheduler_module, "_on_result", lambda u, r: notified.append((u, r)))

    import laplace.agent.orchestrator as orch

    monkeypatch.setattr(orch, "run_task", lambda tid, llm=None: _mark_done(tid, "KQ CRON"))

    scheduler_module._run_scheduled_job(2, user_id, "viec hang ngay")

    assert notified == [(user_id, "KQ CRON")]


def test_run_scheduled_job_stream_import_error_falls_back(session, monkeypatch):
    """Loi bat ngo tu duong streaming -> khong lam vo job, van chay duong cu."""
    user = get_or_create_user(session)
    session.commit()
    user_id = user.id

    def raise_stream(*_a, **_k):
        raise RuntimeError("aiogram not available")

    notified: list[tuple[int, str]] = []
    monkeypatch.setattr(stream_module, "stream_scheduled_task", raise_stream)
    monkeypatch.setattr(scheduler_module, "_on_result", lambda u, r: notified.append((u, r)))

    import laplace.agent.orchestrator as orch

    monkeypatch.setattr(orch, "run_task", lambda tid, llm=None: _mark_done(tid, "OK"))

    scheduler_module._run_scheduled_job(3, user_id, "viec khac")

    assert notified == [(user_id, "OK")]
