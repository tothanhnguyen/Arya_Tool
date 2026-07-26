"""OpenAIProvider: adapter cho OpenAI Chat Completions API.

Implement protocol LLMProvider (xem laplace/llm/base.py). Khi caller truyen
json_schema, provider dung response_format json_schema (strict) va parse
content thanh dict cho LLMResult.parsed; parse loi thi parsed=None va content
giu nguyen de tang orchestrator tu retry (self-correction).
"""

import json
import time
from typing import Any

from laplace.llm.base import LLMResult

# Bang gia USD per 1M tokens: {model: (input_per_1m, output_per_1m)}.
# Model khong co trong bang -> cost 0.0 (van log token de theo doi).
PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "o4-mini": (1.10, 4.40),
}


def _compute_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    input_per_1m, output_per_1m = PRICING.get(model, (0.0, 0.0))
    return (prompt_tokens * input_per_1m + completion_tokens * output_per_1m) / 1_000_000


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str | None, model: str = "gpt-4o-mini"):
        if not api_key:
            raise RuntimeError(
                "Thieu OpenAI API key: dat LAPLACE_OPENAI_API_KEY trong .env "
                "(hoac chuyen LAPLACE_LLM_PROVIDER=mock de chay khong can key)."
            )
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self.model = model

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

        start = time.monotonic()
        response = self._client.chat.completions.create(**kwargs)
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
            cost_usd=_compute_cost(model, prompt_tokens, completion_tokens),
            latency_ms=latency_ms,
            model=model,
            extra={"finish_reason": response.choices[0].finish_reason},
        )
