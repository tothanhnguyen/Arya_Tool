"""fetch_page tool: tai 1 URL va trich title + noi dung text chinh."""

import logging

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from laplace.config import get_settings
from laplace.schemas import ToolResult
from laplace.tools.base import ToolContext, tool

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (compatible; LaplaceDemon/0.1; +https://github.com/laplace-demon)"
)
_STRIP_TAGS = ("script", "style", "nav", "header", "footer", "noscript", "iframe", "svg")


class FetchPageParams(BaseModel):
    url: str = Field(description="Full URL of the page to fetch, including http(s) scheme")
    max_chars: int = Field(
        default=4000, ge=100, le=20000, description="Maximum number of characters of extracted text"
    )


@tool(
    name="fetch_page",
    description=(
        "Download a single web page by URL and extract its title and main readable text "
        "(scripts, styles and navigation removed, truncated to max_chars). Read-only. "
        "Use this after web_search to read the content of a promising result."
    ),
    params=FetchPageParams,
    max_retries=1,
)
def fetch_page(params: FetchPageParams, ctx: ToolContext) -> ToolResult:
    settings = get_settings()
    try:
        resp = httpx.get(
            params.url,
            follow_redirects=True,
            timeout=settings.tool_timeout_s,
            headers={"User-Agent": _USER_AGENT},
        )
        resp.raise_for_status()
    except Exception as e:
        return ToolResult(ok=False, error=f"fetch_page failed for {params.url}: {e}")

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(_STRIP_TAGS):
            tag.decompose()
        title = soup.title.get_text(strip=True) if soup.title else ""
        text = " ".join(soup.stripped_strings)
        if len(text) > params.max_chars:
            text = text[: params.max_chars] + "…"
        return ToolResult(ok=True, data={"title": title, "url": str(resp.url), "text": text})
    except Exception as e:
        return ToolResult(ok=False, error=f"fetch_page could not parse {params.url}: {e}")
