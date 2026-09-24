"""Thin LLM wrapper: JSON-only completions, model fallback, retries, usage accounting.

Every agent talks to the model through `LLM.json(task=..., ...)`. The `task` name is used for
logging/cost accounting and lets tests substitute a deterministic fake.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Protocol

from openai import APIStatusError, AsyncOpenAI, BadRequestError, NotFoundError, PermissionDeniedError
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from .config import settings

log = logging.getLogger(__name__)


class LLMProtocol(Protocol):
    async def json(self, *, task: str, system: str, user: str, model: str | None = None,
                   max_tokens: int = 4000) -> dict[str, Any]: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _is_reasoning(model: str) -> bool:
    return model.startswith(("gpt-5", "o1", "o3", "o4"))


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, APIStatusError):
        return exc.status_code in (408, 409, 429, 500, 502, 503, 504)
    return isinstance(exc, (asyncio.TimeoutError, ConnectionError)) or "timeout" in type(exc).__name__.lower()


def parse_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


class LLM:
    def __init__(self, api_key: str | None = None):
        self.client = AsyncOpenAI(api_key=api_key or settings.openai_api_key, timeout=180)
        self.sem = asyncio.Semaphore(settings.llm_concurrency)
        self.unavailable: set[str] = set()
        self.usage: dict[str, dict[str, int]] = {}

    def _candidates(self, model: str) -> list[str]:
        out = [model] + [m.strip() for m in settings.model_fallbacks if m.strip()]
        seen: list[str] = []
        for m in out:
            if m not in seen and m not in self.unavailable:
                seen.append(m)
        return seen

    def _account(self, task: str, model: str, resp: Any) -> None:
        u = getattr(resp, "usage", None)
        if not u:
            return
        rec = self.usage.setdefault(f"{task}:{model}", {"calls": 0, "in": 0, "out": 0})
        rec["calls"] += 1
        rec["in"] += getattr(u, "prompt_tokens", 0) or 0
        rec["out"] += getattr(u, "completion_tokens", 0) or 0

    async def json(self, *, task: str, system: str, user: str, model: str | None = None,
                   max_tokens: int = 4000) -> dict[str, Any]:
        model = model or settings.research_model
        last_exc: Exception | None = None
        for candidate in self._candidates(model):
            kwargs: dict[str, Any] = {
                "model": candidate,
                "messages": [
                    {"role": "system", "content": system + "\n\nRespond with a single JSON object only."},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
            }
            if _is_reasoning(candidate):
                kwargs["reasoning_effort"] = "low"
                kwargs["max_completion_tokens"] = max_tokens + 4000  # reasoning tokens count here
            else:
                kwargs["temperature"] = 0
                kwargs["max_tokens"] = max_tokens
            try:
                async with self.sem:
                    async for attempt in AsyncRetrying(
                        stop=stop_after_attempt(4),
                        wait=wait_exponential(min=2, max=30),
                        retry=retry_if_exception(_retryable),
                        reraise=True,
                    ):
                        with attempt:
                            resp = await self.client.chat.completions.create(**kwargs)
                self._account(task, candidate, resp)
                return parse_json(resp.choices[0].message.content or "{}")
            except (NotFoundError, PermissionDeniedError) as exc:
                log.warning("model %s unavailable (%s); trying fallback", candidate, exc)
                self.unavailable.add(candidate)
                last_exc = exc
            except BadRequestError as exc:
                if "model" in str(exc).lower() and ("does not exist" in str(exc) or "not supported" in str(exc)):
                    self.unavailable.add(candidate)
                    last_exc = exc
                    continue
                raise
            except json.JSONDecodeError as exc:
                log.warning("task %s: model returned invalid JSON", task)
                last_exc = exc
        raise RuntimeError(f"All models failed for task {task}: {last_exc}")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 96):
            batch = [t[:8000] or " " for t in texts[i:i + 96]]
            async with self.sem:
                resp = await self.client.embeddings.create(model=settings.embedding_model, input=batch)
            out.extend(d.embedding for d in resp.data)
        return out
