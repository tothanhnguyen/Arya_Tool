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


def get_provider(name: str | None = None) -> LLMProvider:
    """Factory chon provider theo config (mock | openai)."""
    from laplace.config import get_settings

    settings = get_settings()
    name = name or settings.llm_provider
    if name == "openai":
        from laplace.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(api_key=settings.openai_api_key, model=settings.openai_model)
    from laplace.llm.mock import MockLLM

    return MockLLM()
