"""Test rate limit theo user: unit test RateLimiter + handler bot ton trong limiter.

Handler test dung message gia (duck-typing thay cho aiogram.types.Message) va
MockLLM heuristic (classify -> direct) nen chay hoan toan offline.
"""

import asyncio

import pytest

from laplace.bot import handlers
from laplace.bot.ratelimit import RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


# ------------------------------------------------------------------ unit test


def test_allows_up_to_max_then_blocks():
    clock = FakeClock()
    rl = RateLimiter(max_requests=3, window_s=60, clock=clock)
    assert all(rl.allow(1) for _ in range(3))
    assert rl.allow(1) is False
    assert rl.allow(1) is False  # van bi chan, khong bi tinh them quota


def test_window_slides_and_frees_quota():
    clock = FakeClock()
    rl = RateLimiter(max_requests=2, window_s=60, clock=clock)
    assert rl.allow(1) and rl.allow(1)
    assert rl.allow(1) is False

    clock.advance(59)
    assert rl.allow(1) is False  # chua het cua so
    clock.advance(1.5)  # request dau da roi khoi cua so
    assert rl.allow(1) is True


def test_keys_are_independent():
    clock = FakeClock()
    rl = RateLimiter(max_requests=1, window_s=60, clock=clock)
    assert rl.allow(111) is True
    assert rl.allow(111) is False
    # user khac khong bi anh huong boi user spam
    assert rl.allow(222) is True


def test_retry_after_reports_remaining_wait():
    clock = FakeClock()
    rl = RateLimiter(max_requests=2, window_s=60, clock=clock)
    assert rl.retry_after(1) == 0.0  # chua co request nao
    rl.allow(1)
    clock.advance(10)
    rl.allow(1)
    assert rl.retry_after(1) == pytest.approx(50.0)  # request dau het han sau 50s nua
    clock.advance(50)
    assert rl.retry_after(1) == 0.0


def test_blocked_attempts_do_not_extend_window():
    clock = FakeClock()
    rl = RateLimiter(max_requests=1, window_s=10, clock=clock)
    assert rl.allow(1) is True
    for _ in range(5):
        clock.advance(1)
        assert rl.allow(1) is False  # spam lien tuc trong luc bi chan
    clock.advance(5)  # tong 10s ke tu request duoc nhan
    assert rl.allow(1) is True


def test_reset_clears_state():
    rl = RateLimiter(max_requests=1, window_s=60)
    assert rl.allow(1) is True
    assert rl.allow(1) is False
    rl.reset(1)
    assert rl.allow(1) is True


def test_invalid_config_rejected():
    with pytest.raises(ValueError):
        RateLimiter(max_requests=0)
    with pytest.raises(ValueError):
        RateLimiter(window_s=0)


# ------------------------------------------------------------- handler test


class FakeUser:
    def __init__(self, tg_id: int):
        self.id = tg_id


class FakeMessage:
    """Duck-type toi thieu cua aiogram Message cho handle_text."""

    def __init__(self, text: str, tg_id: int = 12345):
        self.text = text
        self.from_user = FakeUser(tg_id)
        self.replies: list[str] = []

    async def answer(self, text: str, reply_markup=None) -> None:
        self.replies.append(text)


@pytest.fixture()
def tight_limiter(monkeypatch):
    """Thay limiter toan cuc bang limiter 1 req/60s de test nhanh."""
    rl = RateLimiter(max_requests=1, window_s=60)
    monkeypatch.setattr(handlers, "rate_limiter", rl)
    return rl


def test_handle_text_blocks_second_message(session, tight_limiter):
    first = FakeMessage("xin chao", tg_id=777)
    asyncio.run(handlers.handle_text(first))
    # Tin nhan dau duoc xu ly binh thuong (co "Đang xử lý" + ket qua mock)
    assert any("Đang xử lý" in r for r in first.replies)
    assert len(first.replies) >= 2

    second = FakeMessage("lai hoi nua", tg_id=777)
    asyncio.run(handlers.handle_text(second))
    # Bi chan ngay: chi co MOT reply bao qua nhanh, khong tao task
    assert len(second.replies) == 1
    assert "quá nhanh" in second.replies[0]
    assert "Đang xử lý" not in second.replies[0]


def test_handle_text_other_user_not_blocked(session, tight_limiter):
    asyncio.run(handlers.handle_text(FakeMessage("hi", tg_id=777)))

    other = FakeMessage("hi", tg_id=888)
    asyncio.run(handlers.handle_text(other))
    assert any("Đang xử lý" in r for r in other.replies)
    assert not any("quá nhanh" in r for r in other.replies)


def test_rate_limited_before_any_processing(session, tight_limiter):
    """Message bi chan khong duoc tinh vao DB (khong tao task moi)."""
    from sqlalchemy import select

    from laplace.models import Task

    asyncio.run(handlers.handle_text(FakeMessage("mot", tg_id=777)))
    session.expire_all()
    n_tasks_after_first = len(list(session.scalars(select(Task))))

    asyncio.run(handlers.handle_text(FakeMessage("hai", tg_id=777)))
    session.expire_all()
    assert len(list(session.scalars(select(Task)))) == n_tasks_after_first
