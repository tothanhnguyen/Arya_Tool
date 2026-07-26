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


class MissingAPIKeyError(RuntimeError):
    """Thieu API key cho provider da chon; message kem URL trang lay key dung hang."""


def resolve_provider_config(name: str) -> tuple[Any, str | None, str]:
    """Tra (preset, api_key, model) cho mot preset theo settings hien tai.

    Thu tu chon model: LAPLACE_LLM_MODEL (override chung) > field rieng trong
    Settings (openai_model/gemini_model — giu hanh vi cu) > model mac dinh preset.
    Dung chung cho get_provider va cong cu check/setup (khong boc budget).
    """
    from laplace.config import get_settings
    from laplace.llm.presets import PRESETS

    preset = PRESETS[name]
    settings = get_settings()
    api_key = getattr(settings, preset.settings_field, None)
    model = (
        settings.llm_model
        or getattr(settings, f"{name}_model", None)
        or preset.default_model
    )
    return preset, api_key, model


def get_provider(name: str | None = None) -> LLMProvider:
    """Factory chon provider theo preset registry (laplace/llm/presets.py).

    mock (hoac ten la) -> MockLLM nhu cu; con lai tra OpenAIProvider voi
    base_url/model/bang gia tu preset. Thieu key -> MissingAPIKeyError kem
    URL trang lay key cua dung hang do.

    Provider that (ton tien) duoc boc BudgetedLLM: orchestrator lay provider
    moi tu day cho moi lan run/resume nen tran chi phi ap theo tung lan chay.
    Mock khong boc (test/eval dieu khien truc tiep, khong ton tien).
    """
    from laplace.config import get_settings
    from laplace.llm.presets import PRESETS, missing_key_message

    settings = get_settings()
    name = name or settings.llm_provider
    if name not in PRESETS:  # mock va moi ten la -> MockLLM (hanh vi cu)
        from laplace.llm.mock import MockLLM

        return MockLLM()

    preset, api_key, model = resolve_provider_config(name)
    if preset.requires_key and not api_key:
        raise MissingAPIKeyError(missing_key_message(preset))

    from laplace.llm.openai_provider import OpenAIProvider

    return _with_budget(
        OpenAIProvider(
            api_key=api_key or "ollama",  # ollama khong can key that, SDK van doi chuoi
            model=model,
            base_url=preset.base_url,
            name=name,
            pricing=preset.pricing,
            warn_unknown_price=not preset.local,
        )
    )
