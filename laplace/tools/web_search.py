"""web_search tool.

Neu co LAPLACE_SEARCH_API_KEY -> goi Tavily API. Khong co key -> che do stub
tra ket qua deterministic de dev/demo offline van chay duoc.
"""

import logging

import httpx
from pydantic import BaseModel, Field

from laplace.config import get_settings
from laplace.schemas import ToolResult
from laplace.tools.base import ToolContext, tool

logger = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"


class WebSearchParams(BaseModel):
    query: str = Field(description="The search query, e.g. 'FastAPI vs Flask comparison'")
    max_results: int = Field(default=5, ge=1, le=10, description="Maximum number of results")


def _stub_results(query: str, max_results: int) -> list[dict]:
    """Deterministic fake results so the agent pipeline works offline."""
    return [
        {
            "title": f"[stub result] {query} — result {i}",
            "url": f"https://example.com/search/{i}?q={query.replace(' ', '+')}",
            "snippet": (
                f"[stub result] Deterministic offline snippet #{i} for query '{query}'. "
                "Set LAPLACE_SEARCH_API_KEY to enable real web search."
            ),
        }
        for i in range(1, max_results + 1)
    ]


@tool(
    name="web_search",
    description=(
        "Search the web for a query and return a list of results, each with a title, "
        "URL and text snippet. Read-only. Use this to find current information, news, "
        "prices, comparisons or sources before answering."
    ),
    params=WebSearchParams,
    max_retries=2,
)
def web_search(params: WebSearchParams, ctx: ToolContext) -> ToolResult:
    settings = get_settings()
    if not settings.search_api_key:
        return ToolResult(
            ok=True,
            data={"results": _stub_results(params.query, params.max_results), "stub": True},
        )
    try:
        resp = httpx.post(
            TAVILY_URL,
            json={
                "api_key": settings.search_api_key,
                "query": params.query,
                "max_results": params.max_results,
            },
            timeout=settings.tool_timeout_s,
        )
        resp.raise_for_status()
        payload = resp.json()
        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            }
            for item in payload.get("results", [])[: params.max_results]
        ]
        return ToolResult(ok=True, data={"results": results, "stub": False})
    except Exception as e:
        return ToolResult(ok=False, error=f"web_search failed: {e}")
