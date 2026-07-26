"""OpenAIProvider: adapter cho OpenAI Chat Completions API.

Implement protocol LLMProvider (xem laplace/llm/base.py). Khi caller truyen
json_schema, provider dung response_format json_schema (strict) va parse
content thanh dict cho LLMResult.parsed; parse loi thi parsed=None va content
giu nguyen de tang orchestrator tu retry (self-correction).
"""

import json
import logging
import time
from typing import Any

from laplace.llm.base import LLMResult

logger = logging.getLogger(__name__)

# Bang gia fallback USD per 1M tokens: {model: (input_per_1m, output_per_1m)}.
# Nguon chinh gio la bang gia theo preset (laplace/llm/presets.py) — bang nay
# giu lai cho tuong thich (evals/experiment.py import) va lam fallback chung.
PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o4-mini": (1.10, 4.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-3.1-flash-lite": (0.10, 0.40),
    # 3.5: uoc tinh theo tier tuong duong (chua co bang gia cong bo on dinh)
    "gemini-3.5-flash-lite": (0.10, 0.40),
    "gemini-3.5-flash": (0.30, 2.50),
}

# Model da canh bao "khong co gia" — chi warning 1 lan moi model cho do on log.
_WARNED_UNKNOWN_MODELS: set[str] = set()

# Endpoint tuong thich OpenAI cua Gemini
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

RATE_LIMIT_MAX_RETRIES = 5


def _retry_delay_from(error: Exception, attempt: int) -> float:
    """Lay 'retry in Xs' tu thong bao 429 (Gemini); khong co thi backoff tang dan."""
    import re

    match = re.search(r"retry in (\d+(?:\.\d+)?)s", str(error))
    if match:
        return min(float(match.group(1)) + 1.0, 90.0)
    return min(15.0 * (attempt + 1), 90.0)


def _compute_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    pricing: dict[str, tuple[float, float]] | None = None,
    *,
    warn_unknown: bool = False,
) -> float:
    """Tinh cost tu bang gia preset (uu tien) roi fallback PRICING chung.

    Model la (khong co gia) -> 0.0 + warning DUY NHAT 1 lan moi model
    (warn_unknown=False voi provider local nhu ollama: gia 0 la dung, khong on ao).
    """
    price = (pricing or {}).get(model) or PRICING.get(model)
    if price is None:
        if warn_unknown and model not in _WARNED_UNKNOWN_MODELS:
            _WARNED_UNKNOWN_MODELS.add(model)
            logger.warning(
                "Khong co gia cho model '%s' trong bang gia — tinh cost 0.0 "
                "(token van duoc ghi de theo doi).",
                model,
            )
        return 0.0
    input_per_1m, output_per_1m = price
    return (prompt_tokens * input_per_1m + completion_tokens * output_per_1m) / 1_000_000


class OpenAIProvider:
    """Adapter cho moi API tuong thich OpenAI chat.completions (OpenAI, Gemini...)."""

    def __init__(
        self,
        api_key: str | None,
        model: str = "gpt-4o-mini",
        *,
        base_url: str | None = None,
        name: str = "openai",
        pricing: dict[str, tuple[float, float]] | None = None,
        warn_unknown_price: bool = True,
    ):
        if not api_key:
            # get_provider() da bao loi than thien kem URL lay key truoc khi toi day;
            # nhanh nay chi con cho truong hop khoi tao truc tiep.
            raise RuntimeError(
                f"Thieu API key cho provider '{name}': dat LAPLACE_{name.upper()}_API_KEY "
                "trong .env (hoac chuyen LAPLACE_LLM_PROVIDER=mock de chay khong can key)."
            )
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.name = name
        self._pricing = pricing
        self._warn_unknown_price = warn_unknown_price

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        # Khong dung strict structured-outputs: schema Pydantic mac dinh khong
        # thoa dieu kien strict cua OpenAI (additionalProperties, required day du,
        # khong cho dict[str, Any] nhu tool params) -> request se bi 400.
        # Thay vao do: json_object mode + schema nhung vao system message; tang
        # orchestrator da validate bang Pydantic va retry (self-correction).
        msgs = list(messages)
        kwargs: dict[str, Any] = {"model": self.model, "messages": msgs}
        if json_schema is not None:
            msgs.append(
                {
                    "role": "system",
                    "content": (
                        "Respond with a single JSON object that conforms to this "
                        "JSON Schema. No prose, no markdown fences, JSON only.\n"
                        + json.dumps(json_schema)
                    ),
                }
            )
            kwargs["response_format"] = {"type": "json_object"}

        from openai import BadRequestError, RateLimitError

        start = time.monotonic()
        response = None
        for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
            try:
                response = self._client.chat.completions.create(**kwargs)
                break
            except BadRequestError:
                # Mot so backend tuong thich OpenAI khong nhan response_format:
                # bo di va dua vao schema trong system message + validate o tang tren.
                if "response_format" not in kwargs:
                    raise
                kwargs.pop("response_format")
            except RateLimitError as e:
                # Free tier Gemini gioi han theo phut; loi 429 kem "retry in Xs"
                if attempt == RATE_LIMIT_MAX_RETRIES:
                    raise
                time.sleep(_retry_delay_from(e, attempt))
        assert response is not None
        latency_ms = int((time.monotonic() - start) * 1000)

        content: str | None = response.choices[0].message.content
        parsed: dict[str, Any] | None = None
        if json_schema is not None and content:
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    parsed = data
            except (json.JSONDecodeError, ValueError):
                parsed = None  # content giu nguyen, tang tren tu xu ly retry

        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        model = response.model or self.model

        return LLMResult(
            content=content,
            parsed=parsed,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=_compute_cost(
                model,
                prompt_tokens,
                completion_tokens,
                self._pricing,
                warn_unknown=self._warn_unknown_price,
            ),
            latency_ms=latency_ms,
            model=model,
            extra={"finish_reason": response.choices[0].finish_reason},
        )
