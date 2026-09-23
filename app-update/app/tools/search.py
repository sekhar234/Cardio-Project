"""Web search via Tavily.

Search results are used only to *discover* candidate URLs. Snippets are never used as evidence:
every fact must come from a page the crawlability agent cleared and that we fetched ourselves.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

from ..config import settings

log = logging.getLogger(__name__)


class SearchProtocol(Protocol):
    async def search(self, query: str, max_results: int = 6) -> list[dict[str, Any]]: ...


class TavilySearch:
    URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.tavily_api_key

    async def search(self, query: str, max_results: int = 6) -> list[dict[str, Any]]:
        payload = {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,  # we fetch pages ourselves, after the crawlability check
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(self.URL, json=payload,
                                  headers={"Authorization": f"Bearer {self.api_key}"})
            r.raise_for_status()
            data = r.json()
        return [
            {"url": x.get("url", ""), "title": x.get("title", ""), "snippet": x.get("content", "")[:500],
             "score": x.get("score", 0.0)}
            for x in data.get("results", [])
            if x.get("url")
        ]
