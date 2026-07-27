"""OpenRouter transport used by the frozen coding-agent executor."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from urllib import error, request

from ..planner_v1.policy import HTTP_RETRIES, TEMPERATURE


@dataclass(slots=True)
class ProviderStats:
    api_attempts: int = 0
    http_200_responses: int = 0
    accepted_responses: int = 0
    actual_cost: float = 0.0
    cost_by_role: dict[str, float] = field(
        default_factory=lambda: {"planner": 0.0, "coder": 0.0}
    )


LLM_CACHE: dict[str, dict[str, Any]] = {}
STATS = ProviderStats()


def reset_provider_state() -> None:
    LLM_CACHE.clear()
    STATS.api_attempts = 0
    STATS.http_200_responses = 0
    STATS.accepted_responses = 0
    STATS.actual_cost = 0.0
    STATS.cost_by_role = {"planner": 0.0, "coder": 0.0}


def _cache_key(
    model: str,
    messages: Sequence[Mapping[str, Any]],
    seed: int,
    max_tokens: int,
    *,
    tools: Any = None,
    tool_choice: Any = None,
    parallel_tool_calls: Any = None,
    response_format: Any = None,
) -> str:
    payload = {
        "model": model,
        "messages": list(messages),
        "seed": seed,
        "max_tokens": max_tokens,
        "temperature": TEMPERATURE,
        "tools": tools,
        "tool_choice": tool_choice,
        "parallel_tool_calls": parallel_tool_calls,
        "response_format": response_format,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def llm(
    messages: Sequence[Mapping[str, Any]],
    *,
    model: str,
    seed: int,
    max_tokens: int,
    role: str,
    phase: str,
    tools: Any = None,
    tool_choice: Any = None,
    parallel_tool_calls: Any = None,
    response_format: Any = None,
) -> dict[str, Any]:
    """Return one accepted response with the published study accounting semantics."""

    key = _cache_key(
        model,
        messages,
        seed,
        max_tokens,
        tools=tools,
        tool_choice=tool_choice,
        parallel_tool_calls=parallel_tool_calls,
        response_format=response_format,
    )
    if key in LLM_CACHE:
        cached = dict(LLM_CACHE[key])
        cached["cached"] = True
        return cached

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required for live paper reproduction")

    last_error = ""
    for attempt in range(HTTP_RETRIES):
        STATS.api_attempts += 1
        request_payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "max_tokens": max_tokens,
            "temperature": TEMPERATURE,
            "seed": seed,
            "usage": {"include": True},
            "provider": {"require_parameters": False, "sort": "throughput"},
        }
        if tools is not None:
            request_payload["tools"] = tools
        if tool_choice is not None:
            request_payload["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            request_payload["parallel_tool_calls"] = parallel_tool_calls
        if response_format is not None:
            request_payload["response_format"] = response_format

        body = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = time.perf_counter()
        status = 0
        raw = b""
        try:
            with request.urlopen(req, timeout=300) as response:
                status = int(response.status)
                raw = response.read()
        except error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read()
        except (error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
            if attempt + 1 < HTTP_RETRIES:
                time.sleep(5 * (attempt + 1))
            continue

        latency = time.perf_counter() - started
        if status != 200:
            last_error = f"HTTP {status}: {raw.decode('utf-8', 'replace')[:500]}"
            if attempt + 1 < HTTP_RETRIES:
                time.sleep(5 * (attempt + 1))
            continue

        STATS.http_200_responses += 1
        data = json.loads(raw.decode("utf-8"))
        cost = float((data.get("usage") or {}).get("cost", 0) or 0)
        STATS.actual_cost += cost
        STATS.cost_by_role[role] = STATS.cost_by_role.get(role, 0.0) + cost

        choices = data.get("choices") or []
        if not choices:
            last_error = "HTTP 200 response contained no choices"
            if attempt + 1 < HTTP_RETRIES:
                time.sleep(5 * (attempt + 1))
            continue

        choice = choices[0]
        finish_reason = str(choice.get("finish_reason", "unknown"))
        raw_message = choice.get("message") or {}
        content = str(raw_message.get("content") or "")
        tool_calls = raw_message.get("tool_calls") or []
        if not content.strip() and not tool_calls:
            last_error = (
                "empty response content and no tool_calls with "
                f"finish_reason={finish_reason}"
            )
            if attempt + 1 < HTTP_RETRIES:
                time.sleep(5 * (attempt + 1))
            continue

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": raw_message.get("content"),
        }
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls

        result = {
            "content": content,
            "tool_calls": tool_calls,
            "assistant_message": assistant_message,
            "finish_reason": finish_reason,
            "provider_reported_error": finish_reason == "error",
            "cost": cost,
            "latency_seconds": latency,
            "cached": False,
            "role": role,
            "phase": phase,
            "model": model,
        }
        LLM_CACHE[key] = dict(result)
        STATS.accepted_responses += 1
        return result

    raise RuntimeError(
        f"OpenRouter failed after {HTTP_RETRIES} attempts: {last_error or 'unknown'}"
    )
