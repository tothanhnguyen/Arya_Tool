"""Tran chi phi LLM cho mot lan chay task (runtime — eval harness da tu theo doi).

BudgetedLLM boc mot provider that va cong don cost/token qua cac lan complete().
Cham tran -> nem LLMBudgetExceeded TRUOC lan goi API tiep theo, nen runaway loop
bi chan som; chi phi thuc te chi co the vuot tran toi da mot lan goi.

Thiet ke co chu dich: LLMBudgetExceeded ke thua SchemaValidationError vi
orchestrator (laplace/agent — vung file cua terminal khac, T9 khong duoc sua)
da bat san SchemaValidationError o moi nhanh va finish_task(failed, error=...).
Nho do task dung sach se (status=failed, error ro rang de bot bao user) ma
khong can them dong nao o tang agent.

Tran doc tu env LAPLACE_MAX_COST_PER_TASK_USD (USD; <=0 la tat han muc).
Khong them vao laplace/config.py vi file do thuoc vung so huu T2.

Gioi han biet truoc: moi lan run/resume tao provider moi qua get_provider(),
nen tran ap cho TUNG chang chay (run leg), khong cong don qua confirm/resume.
Muon xem tong chi phi ca task da ghi DB: dung laplace.llm.usage.task_usage().
"""

import os
from typing import Any

from laplace.agent.strategies import SchemaValidationError
from laplace.llm.base import LLMProvider, LLMResult

DEFAULT_MAX_COST_USD = 0.25
ENV_MAX_COST = "LAPLACE_MAX_COST_PER_TASK_USD"


def max_cost_from_env() -> float:
    """Doc tran chi phi tu env; rong/sai dinh dang -> mac dinh."""
    raw = os.environ.get(ENV_MAX_COST, "").strip()
    if not raw:
        return DEFAULT_MAX_COST_USD
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_MAX_COST_USD


class LLMBudgetExceeded(SchemaValidationError):
    """Chi phi LLM cua task vuot tran cho phep — dung task, khong goi API them."""


class BudgetedLLM:
    """Boc LLMProvider that, cong don chi phi, chan khi cham tran."""

    def __init__(self, inner: LLMProvider, max_cost_usd: float):
        if max_cost_usd <= 0:
            raise ValueError("max_cost_usd phai > 0 (tat han muc thi dung provider goc)")
        self._inner = inner
        self.max_cost_usd = max_cost_usd
        self.cost_usd = 0.0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0

    @property
    def name(self) -> str:
        return self._inner.name

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        if self.cost_usd >= self.max_cost_usd:
            raise LLMBudgetExceeded(
                f"Chi phí LLM của task đã chạm trần {self.max_cost_usd:.4f} USD "
                f"(đã dùng {self.cost_usd:.4f} USD sau {self.calls} lần gọi) — "
                f"dừng lại để tránh tốn thêm. Nếu task thật sự cần nhiều hơn, "
                f"tăng trần bằng biến môi trường {ENV_MAX_COST}."
            )
        result = self._inner.complete(messages, json_schema=json_schema)
        self.calls += 1
        self.cost_usd += result.cost_usd
        self.prompt_tokens += result.prompt_tokens
        self.completion_tokens += result.completion_tokens
        return result
