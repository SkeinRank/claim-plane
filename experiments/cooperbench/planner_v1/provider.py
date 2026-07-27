"""Provider boundary for research-only planner calls.

The Claim Plane runtime stays model-agnostic. This module is imported only by
the CooperBench research layer and keeps provider behavior explicit and
cacheable for reproducible studies.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import time
from typing import Any, Mapping, Protocol, Sequence
from urllib import error, request

from .policy import HTTP_RETRIES, TEMPERATURE


@dataclass(frozen=True, slots=True)
class CompletionResult:
    content: str
    cost: float
    latency_seconds: float
    cached: bool
    finish_reason: str
    model: str
    role: str
    phase: str
    tool_calls: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "cost": self.cost,
            "latency_seconds": self.latency_seconds,
            "cached": self.cached,
            "finish_reason": self.finish_reason,
            "model": self.model,
            "role": self.role,
            "phase": self.phase,
            "tool_calls": [dict(item) for item in self.tool_calls],
        }


class CompletionProvider(Protocol):
    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str,
        seed: int,
        max_tokens: int,
        role: str,
        phase: str,
    ) -> CompletionResult:
        """Return one accepted completion."""


@dataclass(slots=True)
class ProviderStats:
    api_attempts: int = 0
    http_200_responses: int = 0
    accepted_responses: int = 0
    actual_cost: float = 0.0
    planner_cost: float = 0.0


class OpenRouterClient:
    """Minimal OpenRouter Chat Completions client matching the frozen notebook."""

    endpoint = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout_seconds: int = 300,
        retries: int = HTTP_RETRIES,
        sleep_base_seconds: float = 5.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "OPENROUTER_API_KEY is required for live Planner v1 execution"
            )
        if retries < 1:
            raise ValueError("retries must be at least 1")
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.sleep_base_seconds = sleep_base_seconds
        self.stats = ProviderStats()
        self._cache: dict[str, CompletionResult] = {}

    @staticmethod
    def cache_key(
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str,
        seed: int,
        max_tokens: int,
    ) -> str:
        payload = {
            "model": model,
            "messages": list(messages),
            "seed": seed,
            "max_tokens": max_tokens,
            "temperature": TEMPERATURE,
            "tools": None,
            "tool_choice": None,
            "parallel_tool_calls": None,
            "response_format": None,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )
        return hashlib.sha256(encoded).hexdigest()

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model: str,
        seed: int,
        max_tokens: int,
        role: str,
        phase: str,
    ) -> CompletionResult:
        key = self.cache_key(messages, model=model, seed=seed, max_tokens=max_tokens)
        cached = self._cache.get(key)
        if cached is not None:
            return CompletionResult(
                content=cached.content,
                cost=cached.cost,
                latency_seconds=cached.latency_seconds,
                cached=True,
                finish_reason=cached.finish_reason,
                model=cached.model,
                role=cached.role,
                phase=cached.phase,
                tool_calls=cached.tool_calls,
            )

        last_error = ""
        for attempt in range(self.retries):
            self.stats.api_attempts += 1
            payload = {
                "model": model,
                "messages": list(messages),
                "max_tokens": max_tokens,
                "temperature": TEMPERATURE,
                "seed": seed,
                "usage": {"include": True},
                "provider": {
                    "require_parameters": False,
                    "sort": "throughput",
                },
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = request.Request(
                self.endpoint,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            started = time.perf_counter()
            status = 0
            raw = b""
            try:
                with request.urlopen(req, timeout=self.timeout_seconds) as response:
                    status = int(response.status)
                    raw = response.read()
            except error.HTTPError as exc:
                status = int(exc.code)
                raw = exc.read()
            except (error.URLError, TimeoutError, OSError) as exc:
                last_error = str(exc)
                if attempt + 1 < self.retries:
                    time.sleep(self.sleep_base_seconds * (attempt + 1))
                continue

            latency = time.perf_counter() - started
            if status != 200:
                last_error = f"HTTP {status}: {raw.decode('utf-8', 'replace')[:500]}"
                if attempt + 1 < self.retries:
                    time.sleep(self.sleep_base_seconds * (attempt + 1))
                continue

            self.stats.http_200_responses += 1
            data = json.loads(raw.decode("utf-8"))
            usage = data.get("usage") or {}
            cost = float(usage.get("cost", 0) or 0)
            self.stats.actual_cost += cost
            if role == "planner":
                self.stats.planner_cost += cost

            choices = data.get("choices") or []
            if not choices:
                last_error = "HTTP 200 response contained no choices"
                if attempt + 1 < self.retries:
                    time.sleep(self.sleep_base_seconds * (attempt + 1))
                continue

            choice = choices[0]
            message = choice.get("message") or {}
            content = str(message.get("content") or "")
            tool_calls = tuple(message.get("tool_calls") or ())
            finish_reason = str(choice.get("finish_reason", "unknown"))
            if not content.strip() and not tool_calls:
                last_error = (
                    "empty response content and no tool_calls with "
                    f"finish_reason={finish_reason}"
                )
                if attempt + 1 < self.retries:
                    time.sleep(self.sleep_base_seconds * (attempt + 1))
                continue

            result = CompletionResult(
                content=content,
                cost=cost,
                latency_seconds=latency,
                cached=False,
                finish_reason=finish_reason,
                model=model,
                role=role,
                phase=phase,
                tool_calls=tool_calls,
            )
            self._cache[key] = result
            self.stats.accepted_responses += 1
            return result

        raise RuntimeError(
            f"OpenRouter failed after {self.retries} attempts: {last_error or 'unknown'}"
        )
