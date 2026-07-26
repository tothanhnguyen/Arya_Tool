"""Provider abstraction cho LLM layer.

Moi provider tra ve LLMResult. Khi caller truyen json_schema (dict JSON Schema,
thuong sinh tu Pydantic `Model.model_json_schema()`), provider phai co gang tra
`parsed` la dict hop le theo schema do; caller chiu trach nhiem validate lai
bang Pydantic va retry neu sai (self-correction o tang orchestrator).
"""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class LLMResult:
    content: str | None = None
    parsed: dict[str, Any] | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        """Goi LLM dong bo. messages theo dinh dang [{role, content}]."""
        ...


def _with_budget(provider: LLMProvider) -> LLMProvider:
    """Boc provider that bang tran chi phi moi lan chay task (env; <=0 la tat)."""
    from laplace.llm.budget import BudgetedLLM, max_cost_from_env

    ceiling = max_cost_from_env()
    if ceiling <= 0:
        return provider
    return BudgetedLLM(provider, ceiling)


def get_provider(name: str | None = None) -> LLMProvider:
    """Factory chon provider theo config (mock | openai | gemini).

    Provider that (ton tien) duoc boc BudgetedLLM: orchestrator lay provider
    moi tu day cho moi lan run/resume nen tran chi phi ap theo tung lan chay.
    Mock khong boc (test/eval dieu khien truc tiep, khong ton tien).
    """
    from laplace.config import get_settings

    settings = get_settings()
    name = name or settings.llm_provider
    if name == "openai":
        from laplace.llm.openai_provider import OpenAIProvider

        return _with_budget(
            OpenAIProvider(api_key=settings.openai_api_key, model=settings.openai_model)
        )
    if name == "gemini":
        # Gemini qua endpoint tuong thich OpenAI — dung chung adapter
        from laplace.llm.openai_provider import GEMINI_BASE_URL, OpenAIProvider

        return _with_budget(
            OpenAIProvider(
                api_key=settings.gemini_api_key,
                model=settings.gemini_model,
                base_url=GEMINI_BASE_URL,
                name="gemini",
            )
        )
    from laplace.llm.mock import MockLLM

    return MockLLM()
