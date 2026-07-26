"""Tests cho T9: tran chi phi LLM per-task (BudgetedLLM) + tong hop usage tu DB."""

import pytest

from laplace.agent.orchestrator import run_task
from laplace.llm.base import LLMResult, get_provider
from laplace.llm.budget import (
    ENV_MAX_COST,
    BudgetedLLM,
    LLMBudgetExceeded,
    max_cost_from_env,
)
from laplace.llm.mock import MockLLM
from laplace.llm.usage import task_usage, user_usage
from laplace.services.tasks import create_task, get_or_create_user


class CostlyMock(MockLLM):
    """MockLLM nhung moi call bao cost co dinh — gia lap provider ton tien."""

    def __init__(self, script, cost_per_call: float):
        super().__init__(script=script)
        self._cost = cost_per_call

    def complete(self, messages, *, json_schema=None) -> LLMResult:
        result = super().complete(messages, json_schema=json_schema)
        result.cost_usd = self._cost
        return result


# ---------------------------------------------------------------- BudgetedLLM

def test_budget_accumulates_and_passes_result_through():
    llm = BudgetedLLM(CostlyMock(["a", "b"], cost_per_call=0.01), max_cost_usd=1.0)
    r1 = llm.complete([{"role": "user", "content": "x"}])
    assert r1.content == "a"  # ket qua provider goc di xuyen qua nguyen ven
    llm.complete([{"role": "user", "content": "y"}])
    assert llm.calls == 2
    assert llm.cost_usd == pytest.approx(0.02)
    assert llm.prompt_tokens == 20 and llm.completion_tokens == 20  # MockLLM: 10+10/call


def test_budget_blocks_next_call_once_ceiling_reached():
    llm = BudgetedLLM(CostlyMock(["a", "b"], cost_per_call=0.06), max_cost_usd=0.05)
    llm.complete([{"role": "user", "content": "x"}])  # lan dau: chua cham tran -> chay
    with pytest.raises(LLMBudgetExceeded) as exc:
        llm.complete([{"role": "user", "content": "y"}])
    msg = str(exc.value)
    assert "chạm trần" in msg and ENV_MAX_COST in msg
    assert llm.calls == 1  # lan hai khong duoc goi xuong provider


def test_budget_name_delegates_and_rejects_bad_ceiling():
    llm = BudgetedLLM(MockLLM(), max_cost_usd=0.1)
    assert llm.name == "mock"
    with pytest.raises(ValueError):
        BudgetedLLM(MockLLM(), max_cost_usd=0)


def test_max_cost_from_env(monkeypatch):
    monkeypatch.delenv(ENV_MAX_COST, raising=False)
    assert max_cost_from_env() > 0  # mac dinh bat
    monkeypatch.setenv(ENV_MAX_COST, "0.5")
    assert max_cost_from_env() == pytest.approx(0.5)
    monkeypatch.setenv(ENV_MAX_COST, "khong-phai-so")
    assert max_cost_from_env() > 0  # sai dinh dang -> ve mac dinh, khong crash


# ------------------------------------------------------------- get_provider

@pytest.fixture()
def _fake_gemini_key(monkeypatch):
    from laplace.config import get_settings

    monkeypatch.setattr(get_settings(), "gemini_api_key", "test-key-khong-goi-that")


def test_get_provider_wraps_real_provider_with_budget(monkeypatch, _fake_gemini_key):
    monkeypatch.delenv(ENV_MAX_COST, raising=False)
    provider = get_provider("gemini")
    assert isinstance(provider, BudgetedLLM)
    assert provider.name == "gemini"


def test_get_provider_budget_disabled_via_env(monkeypatch, _fake_gemini_key):
    monkeypatch.setenv(ENV_MAX_COST, "0")
    provider = get_provider("gemini")
    assert not isinstance(provider, BudgetedLLM)


def test_get_provider_mock_never_wrapped(monkeypatch):
    monkeypatch.delenv(ENV_MAX_COST, raising=False)
    assert isinstance(get_provider("mock"), MockLLM)


# ----------------------------------------------- tich hop voi agent loop that

def _make_task(session, request: str = "Xin chào"):
    user = get_or_create_user(session)
    task = create_task(session, user_id=user.id, request=request)
    session.commit()  # run_task mo session rieng
    return task


def test_run_task_stops_failed_with_budget_error(session):
    """Vuot tran giua chung -> orchestrator dung task sach: failed + error ro rang."""
    task = _make_task(session)
    llm = BudgetedLLM(
        CostlyMock(
            [{"route": "direct", "reason": "greeting"}, "Chào bạn!"],
            cost_per_call=0.06,
        ),
        max_cost_usd=0.05,
    )
    result = run_task(task.id, llm=llm)
    assert result.status == "failed"
    assert "chạm trần" in (result.error or "")


def test_run_task_within_budget_unaffected(session):
    task = _make_task(session)
    llm = BudgetedLLM(
        CostlyMock(
            [{"route": "direct", "reason": "greeting"}, "Chào bạn!"],
            cost_per_call=0.001,
        ),
        max_cost_usd=0.05,
    )
    result = run_task(task.id, llm=llm)
    assert result.status == "done"
    assert result.result == "Chào bạn!"


# ------------------------------------------------------------------- usage

def test_task_and_user_usage_aggregate_from_db(session):
    task = _make_task(session)
    run_task(
        task.id,
        llm=MockLLM(script=[{"route": "direct", "reason": "greeting"}, "Chào!"]),
    )
    usage = task_usage(session, task.id)
    # MockLLM: 10 prompt + 10 completion moi call; direct route = 2 call (classify + answer)
    assert usage["llm_calls"] == 2
    assert usage["total_tokens"] == 40
    assert usage["cost_usd"] == 0.0

    # khong goi lai get_or_create_user: tg_id=None -> moi lan goi tao user MOI
    assert user_usage(session, task.user_id)["llm_calls"] == 2
    assert task_usage(session, task_id=999999)["llm_calls"] == 0  # task khong ton tai -> 0
