"""Durable Codex worker-run protocol for managed swarm execution."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

SWARM_CODEX_RUN_PROTOCOL = "claim-plane.swarm-codex-run.v1"
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _clean(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CodexRunState(str, Enum):
    RESERVED = "reserved"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    SPAWN_FAILED = "spawn_failed"
    LOST = "lost"

    @property
    def terminal(self) -> bool:
        return self in {
            CodexRunState.SUCCEEDED,
            CodexRunState.FAILED,
            CodexRunState.TIMED_OUT,
            CodexRunState.CANCELLED,
            CodexRunState.TOKEN_BUDGET_EXCEEDED,
            CodexRunState.SPAWN_FAILED,
            CodexRunState.LOST,
        }

    @property
    def active(self) -> bool:
        return self in {
            CodexRunState.RESERVED,
            CodexRunState.RUNNING,
            CodexRunState.CANCELLING,
        }


@dataclass(frozen=True, slots=True)
class CodexUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("cached_input_tokens", self.cached_input_tokens),
            ("output_tokens", self.output_tokens),
            ("reasoning_output_tokens", self.reasoning_output_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    @property
    def total_tokens(self) -> int:
        # cached_input_tokens and reasoning_output_tokens are reported subsets.
        return self.input_tokens + self.output_tokens

    def plus(self, other: "CodexUsage") -> "CodexUsage":
        return CodexUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_output_tokens=(
                self.reasoning_output_tokens + other.reasoning_output_tokens
            ),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
            "total_tokens": self.total_tokens,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "CodexUsage":
        raw = data or {}
        return cls(
            input_tokens=int(raw.get("input_tokens") or 0),
            cached_input_tokens=int(raw.get("cached_input_tokens") or 0),
            output_tokens=int(raw.get("output_tokens") or 0),
            reasoning_output_tokens=int(raw.get("reasoning_output_tokens") or 0),
        )


@dataclass(frozen=True, slots=True)
class CodexRunBudget:
    token_limit: int | None
    wall_time_limit_seconds: int
    cost_limit_usd: str | None = None

    def __post_init__(self) -> None:
        if self.token_limit is not None and (
            isinstance(self.token_limit, bool)
            or not isinstance(self.token_limit, int)
            or self.token_limit <= 0
        ):
            raise ValueError("token_limit must be a positive integer or null")
        if (
            isinstance(self.wall_time_limit_seconds, bool)
            or not isinstance(self.wall_time_limit_seconds, int)
            or self.wall_time_limit_seconds <= 0
        ):
            raise ValueError("wall_time_limit_seconds must be positive")
        if self.cost_limit_usd is not None:
            object.__setattr__(
                self,
                "cost_limit_usd",
                _clean(self.cost_limit_usd, field_name="cost_limit_usd"),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_limit": self.token_limit,
            "wall_time_limit_seconds": self.wall_time_limit_seconds,
            "cost_limit_usd": self.cost_limit_usd,
            "cost_metering": "unavailable_from_codex_jsonl",
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CodexRunBudget":
        return cls(
            token_limit=(
                None if data.get("token_limit") is None else int(data["token_limit"])
            ),
            wall_time_limit_seconds=int(data.get("wall_time_limit_seconds") or 0),
            cost_limit_usd=(
                None
                if data.get("cost_limit_usd") is None
                else str(data.get("cost_limit_usd"))
            ),
        )


@dataclass(frozen=True, slots=True)
class CodexRunRecord:
    run_id: str
    session_id: str
    work_id: str
    attempt: int
    state: CodexRunState
    repository_identity: str
    graph_version: int
    graph_fingerprint: str
    budget_version: int
    budget_fingerprint: str
    worktree_path: str
    branch: str
    base_commit: str
    command: tuple[str, ...]
    prompt_sha256: str
    budget: CodexRunBudget
    run_directory: str
    events_path: str
    stderr_path: str
    final_message_path: str
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    runner_pid: int | None = None
    agent_pid: int | None = None
    exit_code: int | None = None
    duration_seconds: float | None = None
    usage: CodexUsage = field(default_factory=CodexUsage)
    event_count: int = 0
    last_event_type: str | None = None
    codex_thread_id: str | None = None
    intent_id: str | None = None
    termination_reason: str | None = None
    error: str | None = None
    heartbeat_at: str | None = None
    lease_expires_at: str | None = None
    replacement_of_run_id: str | None = None
    recovery_generation: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SWARM_CODEX_RUN_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SWARM_CODEX_RUN_PROTOCOL:
            raise ValueError(f"unsupported Codex-run protocol {self.protocol!r}")
        for field_name in ("run_id", "session_id", "work_id"):
            value = _clean(str(getattr(self, field_name)), field_name=field_name)
            if field_name == "run_id" and not _RUN_ID_RE.fullmatch(value):
                raise ValueError("run_id is not safe")
            object.__setattr__(self, field_name, value)
        if self.attempt <= 0:
            raise ValueError("attempt must be positive")
        object.__setattr__(self, "state", CodexRunState(self.state))
        identity = _clean(self.repository_identity, field_name="repository_identity")
        if not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ValueError("repository_identity must be a SHA-256 digest")
        object.__setattr__(self, "repository_identity", identity)
        for name in ("graph_version", "budget_version"):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("graph_fingerprint", "budget_fingerprint"):
            value = _clean(str(getattr(self, name)), field_name=name).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{name} must be a SHA-256 digest")
            object.__setattr__(self, name, value)
        worktree = Path(_clean(self.worktree_path, field_name="worktree_path"))
        if not worktree.is_absolute():
            raise ValueError("worktree_path must be absolute")
        object.__setattr__(self, "worktree_path", str(worktree.resolve()))
        object.__setattr__(self, "branch", _clean(self.branch, field_name="branch"))
        base_commit = _clean(self.base_commit, field_name="base_commit").lower()
        if not re.fullmatch(r"[0-9a-f]{40,64}", base_commit):
            raise ValueError("base_commit must be a full Git object id")
        object.__setattr__(self, "base_commit", base_commit)
        command = tuple(
            _clean(item, field_name="command item") for item in self.command
        )
        if not command:
            raise ValueError("command must not be empty")
        object.__setattr__(self, "command", command)
        prompt_sha = _clean(self.prompt_sha256, field_name="prompt_sha256").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", prompt_sha):
            raise ValueError("prompt_sha256 must be a SHA-256 digest")
        object.__setattr__(self, "prompt_sha256", prompt_sha)
        if not isinstance(self.budget, CodexRunBudget):
            object.__setattr__(self, "budget", CodexRunBudget.from_dict(self.budget))
        for name in (
            "run_directory",
            "events_path",
            "stderr_path",
            "final_message_path",
        ):
            path = Path(_clean(str(getattr(self, name)), field_name=name))
            if not path.is_absolute():
                raise ValueError(f"{name} must be absolute")
            object.__setattr__(self, name, str(path.resolve()))
        object.__setattr__(
            self, "created_at", _clean(self.created_at, field_name="created_at")
        )
        object.__setattr__(
            self, "updated_at", _clean(self.updated_at, field_name="updated_at")
        )
        if not isinstance(self.usage, CodexUsage):
            object.__setattr__(self, "usage", CodexUsage.from_dict(self.usage))
        if self.event_count < 0:
            raise ValueError("event_count must be non-negative")
        if self.recovery_generation < 0:
            raise ValueError("recovery_generation must be non-negative")
        if self.replacement_of_run_id is not None:
            replacement = _clean(
                self.replacement_of_run_id, field_name="replacement_of_run_id"
            )
            if not _RUN_ID_RE.fullmatch(replacement):
                raise ValueError("replacement_of_run_id is not safe")
            if replacement == self.run_id:
                raise ValueError("a run cannot replace itself")
            object.__setattr__(self, "replacement_of_run_id", replacement)
        object.__setattr__(self, "metadata", dict(self.metadata))

    def with_updates(self, **changes: Any) -> "CodexRunRecord":
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "work_id": self.work_id,
            "attempt": self.attempt,
            "state": self.state.value,
            "repository_identity": self.repository_identity,
            "graph_version": self.graph_version,
            "graph_fingerprint": self.graph_fingerprint,
            "budget_version": self.budget_version,
            "budget_fingerprint": self.budget_fingerprint,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "base_commit": self.base_commit,
            "command": list(self.command),
            "prompt_sha256": self.prompt_sha256,
            "budget": self.budget.to_dict(),
            "run_directory": self.run_directory,
            "events_path": self.events_path,
            "stderr_path": self.stderr_path,
            "final_message_path": self.final_message_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "runner_pid": self.runner_pid,
            "agent_pid": self.agent_pid,
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "usage": self.usage.to_dict(),
            "event_count": self.event_count,
            "last_event_type": self.last_event_type,
            "codex_thread_id": self.codex_thread_id,
            "intent_id": self.intent_id,
            "termination_reason": self.termination_reason,
            "error": self.error,
            "heartbeat_at": self.heartbeat_at,
            "lease_expires_at": self.lease_expires_at,
            "replacement_of_run_id": self.replacement_of_run_id,
            "recovery_generation": self.recovery_generation,
            "metadata": dict(self.metadata),
        }

    def fingerprint(self) -> str:
        return _digest(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CodexRunRecord":
        return cls(
            protocol=str(data.get("protocol") or SWARM_CODEX_RUN_PROTOCOL),
            run_id=str(data.get("run_id") or ""),
            session_id=str(data.get("session_id") or ""),
            work_id=str(data.get("work_id") or ""),
            attempt=int(data.get("attempt") or 0),
            state=CodexRunState(data.get("state") or CodexRunState.RESERVED.value),
            repository_identity=str(data.get("repository_identity") or ""),
            graph_version=int(data.get("graph_version") or 0),
            graph_fingerprint=str(data.get("graph_fingerprint") or ""),
            budget_version=int(data.get("budget_version") or 0),
            budget_fingerprint=str(data.get("budget_fingerprint") or ""),
            worktree_path=str(data.get("worktree_path") or ""),
            branch=str(data.get("branch") or ""),
            base_commit=str(data.get("base_commit") or ""),
            command=tuple(data.get("command") or ()),
            prompt_sha256=str(data.get("prompt_sha256") or ""),
            budget=CodexRunBudget.from_dict(data.get("budget") or {}),
            run_directory=str(data.get("run_directory") or ""),
            events_path=str(data.get("events_path") or ""),
            stderr_path=str(data.get("stderr_path") or ""),
            final_message_path=str(data.get("final_message_path") or ""),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            started_at=(
                None if data.get("started_at") is None else str(data["started_at"])
            ),
            finished_at=(
                None if data.get("finished_at") is None else str(data["finished_at"])
            ),
            runner_pid=(
                None if data.get("runner_pid") is None else int(data["runner_pid"])
            ),
            agent_pid=None if data.get("agent_pid") is None else int(data["agent_pid"]),
            exit_code=None if data.get("exit_code") is None else int(data["exit_code"]),
            duration_seconds=(
                None
                if data.get("duration_seconds") is None
                else float(data["duration_seconds"])
            ),
            usage=CodexUsage.from_dict(data.get("usage")),
            event_count=int(data.get("event_count") or 0),
            last_event_type=(
                None
                if data.get("last_event_type") is None
                else str(data["last_event_type"])
            ),
            codex_thread_id=(
                None
                if data.get("codex_thread_id") is None
                else str(data["codex_thread_id"])
            ),
            intent_id=None if data.get("intent_id") is None else str(data["intent_id"]),
            termination_reason=(
                None
                if data.get("termination_reason") is None
                else str(data["termination_reason"])
            ),
            error=None if data.get("error") is None else str(data["error"]),
            heartbeat_at=(
                None if data.get("heartbeat_at") is None else str(data["heartbeat_at"])
            ),
            lease_expires_at=(
                None
                if data.get("lease_expires_at") is None
                else str(data["lease_expires_at"])
            ),
            replacement_of_run_id=(
                None
                if data.get("replacement_of_run_id") is None
                else str(data["replacement_of_run_id"])
            ),
            recovery_generation=int(data.get("recovery_generation") or 0),
            metadata=dict(data.get("metadata") or {}),
        )
