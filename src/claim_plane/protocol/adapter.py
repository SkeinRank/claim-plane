"""Runtime-neutral contract for coding-agent adapters.

The adapter boundary carries stable request, session, run, and intent identities while
keeping runtime-specific payloads outside Claim Plane Core. Adapters may translate their
native lifecycle into these operations, but only Claim Plane grants mutation authority.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

AGENT_ADAPTER_PROTOCOL = "claim-plane.agent-adapter.v1"
AGENT_ADAPTER_PROTOCOL_VERSION = "1.0"


class AdapterOperation(str, Enum):
    """Stable operations exposed by every coding-agent adapter."""

    ENROLL_PROJECT = "enroll_project"
    UNENROLL_PROJECT = "unenroll_project"
    DOCTOR = "doctor"
    START_SESSION = "start_session"
    STOP_SESSION = "stop_session"
    SUBMIT_TASK = "submit_task"
    PROPOSE_INTENT = "propose_intent"
    REQUEST_MUTATION = "request_mutation"
    REQUEST_AMENDMENT = "request_amendment"
    OBSERVE_RESULT = "observe_result"
    VERIFY_COMPLETION = "verify_completion"
    INSPECT = "inspect"
    CANCEL = "cancel"
    RESUME = "resume"


class AdapterStatus(str, Enum):
    """Portable result state for one adapter request."""

    SUCCEEDED = "succeeded"
    DENIED = "denied"
    CANCELLED = "cancelled"
    FAILED = "failed"


class AdapterErrorCode(str, Enum):
    """Structured failures that callers may handle without parsing messages."""

    INVALID_REQUEST = "invalid_request"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    ADAPTER_MISMATCH = "adapter_mismatch"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    STALE_INTENT_VERSION = "stale_intent_version"
    UNKNOWN_SESSION = "unknown_session"
    UNKNOWN_INTENT = "unknown_intent"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    CORRUPT_STATE = "corrupt_state"
    RUNTIME_FAILURE = "runtime_failure"


class AdapterProtocolError(RuntimeError):
    """A machine-readable adapter failure."""

    def __init__(
        self,
        code: AdapterErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = AdapterErrorCode(code)
        self.retryable = bool(retryable)
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": str(self),
            "retryable": self.retryable,
            "details": dict(self.details),
        }


def _required_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _optional_identifier(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_identifier(value, field_name=field_name)


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AdapterRequest:
    """One idempotent request crossing the runtime-neutral adapter boundary."""

    request_id: str
    operation: AdapterOperation
    adapter: str
    project_root: str
    protocol: str = AGENT_ADAPTER_PROTOCOL
    session_id: str | None = None
    run_id: str | None = None
    intent_id: str | None = None
    intent_version: int | None = None
    timeout_seconds: float = 30.0
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _required_identifier(self.request_id, field_name="request_id"),
        )
        object.__setattr__(self, "operation", AdapterOperation(self.operation))
        object.__setattr__(
            self, "adapter", _required_identifier(self.adapter, field_name="adapter")
        )
        object.__setattr__(
            self,
            "project_root",
            _required_identifier(self.project_root, field_name="project_root"),
        )
        object.__setattr__(
            self,
            "session_id",
            _optional_identifier(self.session_id, field_name="session_id"),
        )
        object.__setattr__(
            self, "run_id", _optional_identifier(self.run_id, field_name="run_id")
        )
        object.__setattr__(
            self,
            "intent_id",
            _optional_identifier(self.intent_id, field_name="intent_id"),
        )
        if self.protocol != AGENT_ADAPTER_PROTOCOL:
            raise ValueError(
                f"adapter request protocol must be {AGENT_ADAPTER_PROTOCOL!r}"
            )
        if self.intent_version is not None and self.intent_version < 0:
            raise ValueError("intent_version must be non-negative")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        object.__setattr__(self, "payload", dict(self.payload))

    @classmethod
    def create(
        cls,
        operation: AdapterOperation | str,
        *,
        adapter: str,
        project_root: str,
        request_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        intent_id: str | None = None,
        intent_version: int | None = None,
        timeout_seconds: float = 30.0,
        payload: Mapping[str, Any] | None = None,
    ) -> "AdapterRequest":
        return cls(
            request_id=request_id or f"adapter-{uuid.uuid4().hex}",
            operation=AdapterOperation(operation),
            adapter=adapter,
            project_root=project_root,
            session_id=session_id,
            run_id=run_id,
            intent_id=intent_id,
            intent_version=intent_version,
            timeout_seconds=timeout_seconds,
            payload=dict(payload or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "request_id": self.request_id,
            "operation": self.operation.value,
            "adapter": self.adapter,
            "project_root": self.project_root,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "intent_id": self.intent_id,
            "intent_version": self.intent_version,
            "timeout_seconds": self.timeout_seconds,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdapterRequest":
        return cls(
            protocol=str(data.get("protocol") or ""),
            request_id=str(data.get("request_id") or ""),
            operation=AdapterOperation(str(data.get("operation") or "")),
            adapter=str(data.get("adapter") or ""),
            project_root=str(data.get("project_root") or ""),
            session_id=data.get("session_id"),
            run_id=data.get("run_id"),
            intent_id=data.get("intent_id"),
            intent_version=(
                int(data["intent_version"])
                if data.get("intent_version") is not None
                else None
            ),
            timeout_seconds=float(data.get("timeout_seconds", 30.0)),
            payload=dict(data.get("payload") or {}),
        )

    def fingerprint(self) -> str:
        payload = self.to_dict()
        payload.pop("request_id", None)
        return _canonical_digest(payload)


@dataclass(frozen=True, slots=True)
class AdapterResponse:
    """Portable result returned by an adapter implementation."""

    request_id: str
    operation: AdapterOperation
    adapter: str
    status: AdapterStatus
    protocol: str = AGENT_ADAPTER_PROTOCOL
    session_id: str | None = None
    run_id: str | None = None
    intent_id: str | None = None
    intent_version: int | None = None
    replayed: bool = False
    payload: Mapping[str, Any] = field(default_factory=dict)
    error: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _required_identifier(self.request_id, field_name="request_id"),
        )
        object.__setattr__(self, "operation", AdapterOperation(self.operation))
        object.__setattr__(
            self, "adapter", _required_identifier(self.adapter, field_name="adapter")
        )
        object.__setattr__(self, "status", AdapterStatus(self.status))
        object.__setattr__(
            self,
            "session_id",
            _optional_identifier(self.session_id, field_name="session_id"),
        )
        object.__setattr__(
            self, "run_id", _optional_identifier(self.run_id, field_name="run_id")
        )
        object.__setattr__(
            self,
            "intent_id",
            _optional_identifier(self.intent_id, field_name="intent_id"),
        )
        if self.protocol != AGENT_ADAPTER_PROTOCOL:
            raise ValueError(
                f"adapter response protocol must be {AGENT_ADAPTER_PROTOCOL!r}"
            )
        if self.intent_version is not None and self.intent_version < 0:
            raise ValueError("intent_version must be non-negative")
        object.__setattr__(self, "payload", dict(self.payload))
        if self.error is not None:
            object.__setattr__(self, "error", dict(self.error))

    @property
    def succeeded(self) -> bool:
        return self.status is AdapterStatus.SUCCEEDED

    def as_replayed(self) -> "AdapterResponse":
        return replace(self, replayed=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "request_id": self.request_id,
            "operation": self.operation.value,
            "adapter": self.adapter,
            "status": self.status.value,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "intent_id": self.intent_id,
            "intent_version": self.intent_version,
            "replayed": self.replayed,
            "payload": dict(self.payload),
            "error": dict(self.error) if self.error is not None else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdapterResponse":
        return cls(
            protocol=str(data.get("protocol") or ""),
            request_id=str(data.get("request_id") or ""),
            operation=AdapterOperation(str(data.get("operation") or "")),
            adapter=str(data.get("adapter") or ""),
            status=AdapterStatus(str(data.get("status") or "")),
            session_id=data.get("session_id"),
            run_id=data.get("run_id"),
            intent_id=data.get("intent_id"),
            intent_version=(
                int(data["intent_version"])
                if data.get("intent_version") is not None
                else None
            ),
            replayed=bool(data.get("replayed")),
            payload=dict(data.get("payload") or {}),
            error=(
                dict(data["error"])
                if isinstance(data.get("error"), Mapping)
                else None
            ),
        )


@runtime_checkable
class AgentAdapter(Protocol):
    """Versioned runtime-neutral interface implemented by coding-agent adapters."""

    name: str
    protocol_version: str

    def enroll_project(self, request: AdapterRequest) -> AdapterResponse: ...

    def unenroll_project(self, request: AdapterRequest) -> AdapterResponse: ...

    def doctor(self, request: AdapterRequest) -> AdapterResponse: ...

    def start_session(self, request: AdapterRequest) -> AdapterResponse: ...

    def stop_session(self, request: AdapterRequest) -> AdapterResponse: ...

    def submit_task(self, request: AdapterRequest) -> AdapterResponse: ...

    def propose_intent(self, request: AdapterRequest) -> AdapterResponse: ...

    def request_mutation(self, request: AdapterRequest) -> AdapterResponse: ...

    def request_amendment(self, request: AdapterRequest) -> AdapterResponse: ...

    def observe_result(self, request: AdapterRequest) -> AdapterResponse: ...

    def verify_completion(self, request: AdapterRequest) -> AdapterResponse: ...

    def inspect(self, request: AdapterRequest) -> AdapterResponse: ...

    def cancel(self, request: AdapterRequest) -> AdapterResponse: ...

    def resume(self, request: AdapterRequest) -> AdapterResponse: ...
