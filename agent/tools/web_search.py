"""Web search via TinyFish API. Gracefully degrades if no key is configured."""

from __future__ import annotations

import httpx
from langchain_core.tools import tool

from agent.config import settings

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def _tinyfish_search(query: str, max_results: int = 6) -> list[dict]:
    if not settings.TINYFISH_API_KEY:
        return []
    params = {
        "query": query,
        "max_results": max_results,
        "location": "US",
        "language": "en",
    }
    headers = {"X-API-Key": settings.TINYFISH_API_KEY}
    resp = httpx.get(settings.TINYFISH_ENDPOINT, params=params, headers=headers, timeout=25.0)
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", data) if isinstance(data, dict) else data


def _format_results(results: list[dict], max_results: int) -> str:
    if not results:
        return "Web search returned no results."
    lines = []
    for i, item in enumerate(results[:max_results], 1):
        title = item.get("title", "Untitled")
        url = item.get("url", item.get("link", ""))
        snippet = item.get("snippet", item.get("content", ""))
        lines.append(f"{i}. {title}\n   URL: {url}\n   {snippet}")
    return "\n\n".join(lines)


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using the TinyFish search API. Returns a list of the most
    relevant results (title, URL, snippet). Use this to find source URLs, then
    call fetch_url or crawl_website to extract full content."""
    try:
        results = _tinyfish_search(query, max_results)
        return _format_results(results, max_results)
    except Exception as exc:  # noqa: BLE001
        return f"Web search failed: {exc}"