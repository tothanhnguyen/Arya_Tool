"""MockLLM: provider gia lap cho test va chay dev khong can API key.

Hai che do:
- Scripted: truyen `script` la list cac dict (se tra ve lam `parsed`) hoac str
  (tra ve lam `content`). Moi lan goi complete() lay phan tu tiep theo.
  Test dung che do nay de dieu khien chinh xac agent loop.
- Heuristic (khong co script): classify -> direct, tra loi echo don gian.
"""

from typing import Any

from laplace.llm.base import LLMResult


class MockLLM:
    name = "mock"

    def __init__(self, script: list[dict[str, Any] | str] | None = None):
        self._script = list(script) if script else None
        self.calls: list[dict[str, Any]] = []  # log de test assert

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        self.calls.append({"messages": messages, "json_schema": json_schema})
        if self._script is not None:
            if not self._script:
                raise RuntimeError("MockLLM script exhausted")
            item = self._script.pop(0)
            if isinstance(item, dict):
                return LLMResult(parsed=item, model="mock", prompt_tokens=10, completion_tokens=10)
            return LLMResult(content=item, model="mock", prompt_tokens=10, completion_tokens=10)

        # Heuristic mode
        if json_schema is not None:
            props = json_schema.get("properties", {})
            if "route" in props:
                return LLMResult(parsed={"route": "direct", "reason": "mock"}, model="mock")
            if "decision" in props:
                return LLMResult(parsed={"decision": "done", "reason": "mock"}, model="mock")
            if "action" in props:
                return LLMResult(
                    parsed={"action": "final", "final_answer": "[mock] xong"}, model="mock"
                )
            if "steps" in props:
                return LLMResult(parsed={"steps": []}, model="mock")
            return LLMResult(parsed={}, model="mock")
        last = messages[-1]["content"] if messages else ""
        return LLMResult(content=f"[mock] {last[:200]}", model="mock")
