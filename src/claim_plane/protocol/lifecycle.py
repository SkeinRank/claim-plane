"""Normalized append-only lifecycle events for coding-agent adapters.

The lifecycle layer is runtime-neutral. Adapters translate native callbacks into a
small event vocabulary while Claim Plane owns ordering, duplicate suppression,
redaction, replay, and deterministic validation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from claim_plane.protocol.adapter import (
    AdapterOperation,
    AdapterProtocolError,
    AdapterRequest,
    AdapterResponse,
    AdapterStatus,
)

LIFECYCLE_EVENT_PROTOCOL = "claim-plane.lifecycle-event.v1"
LIFECYCLE_EVENT_PROTOCOL_VERSION = "1.0"
LIFECYCLE_STORE_PROTOCOL = "claim-plane.lifecycle-store.v1"


class LifecycleEventType(str, Enum):
    """Portable lifecycle events emitted by every adapter."""

    SESSION_STARTED = "SessionStarted"
    TASK_SUBMITTED = "TaskSubmitted"
    INTENT_PROPOSED = "IntentProposed"
    ADMISSION_REQUESTED = "AdmissionRequested"
    ADMISSION_GRANTED = "AdmissionGranted"
    ADMISSION_DENIED = "AdmissionDenied"
    MUTATION_REQUESTED = "MutationRequested"
    MUTATION_ALLOWED = "MutationAllowed"
    MUTATION_DENIED = "MutationDenied"
    MUTATION_OBSERVED = "MutationObserved"
    SCOPE_EXPANSION_REQUESTED = "ScopeExpansionRequested"
    SCOPE_EXPANSION_GRANTED = "ScopeExpansionGranted"
    SCOPE_EXPANSION_DENIED = "ScopeExpansionDenied"
    VERIFICATION_STARTED = "VerificationStarted"
    VERIFICATION_COMPLETED = "VerificationCompleted"
    AGENT_STOPPED = "AgentStopped"
    SESSION_ENDED = "SessionEnded"


class LifecycleValidationCode(str, Enum):
    """Machine-readable reasons why an event stream is invalid."""

    EMPTY = "empty"
    MIXED_SESSION = "mixed_session"
    MIXED_ADAPTER = "mixed_adapter"
    NON_CONTIGUOUS_SEQUENCE = "non_contiguous_sequence"
    DUPLICATE_EVENT_ID = "duplicate_event_id"
    CAUSAL_LINK_MISMATCH = "causal_link_mismatch"
    INVALID_TRANSITION = "invalid_transition"
    INVALID_EVENT_DIGEST = "invalid_event_digest"
    INVALID_PROTOCOL = "invalid_protocol"


class LifecycleStoreError(RuntimeError):
    """Base failure for normalized lifecycle persistence."""


class LifecycleConflictError(LifecycleStoreError):
    """An event identity or expected sequence conflicts with durable state."""


class LifecycleCorruptError(LifecycleStoreError):
    """Durable lifecycle state cannot be validated safely."""


def _required_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _optional_text(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name=field_name)



def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _safe_digest(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _event_identity(
    *,
    adapter: str,
    session_id: str,
    request_id: str,
    event_type: LifecycleEventType,
    ordinal: int,
) -> str:
    material = "|".join(
        (
            LIFECYCLE_EVENT_PROTOCOL,
            adapter,
            session_id,
            request_id,
            event_type.value,
            str(ordinal),
        )
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"evt_{digest[:40]}"


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """One immutable event in a session-local causal chain."""

    event_id: str
    event_type: LifecycleEventType
    adapter: str
    session_id: str
    sequence: int
    timestamp: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = LIFECYCLE_EVENT_PROTOCOL
    protocol_version: str = LIFECYCLE_EVENT_PROTOCOL_VERSION
    run_id: str | None = None
    intent_id: str | None = None
    intent_version: int | None = None
    caused_by: str | None = None
    digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "event_id",
            _required_text(self.event_id, field_name="event_id"),
        )
        object.__setattr__(self, "event_type", LifecycleEventType(self.event_type))
        object.__setattr__(
            self, "adapter", _required_text(self.adapter, field_name="adapter")
        )
        object.__setattr__(
            self,
            "session_id",
            _required_text(self.session_id, field_name="session_id"),
        )
        object.__setattr__(
            self, "run_id", _optional_text(self.run_id, field_name="run_id")
        )
        object.__setattr__(
            self,
            "intent_id",
            _optional_text(self.intent_id, field_name="intent_id"),
        )
        object.__setattr__(
            self,
            "caused_by",
            _optional_text(self.caused_by, field_name="caused_by"),
        )
        if self.sequence <= 0:
            raise ValueError("sequence must be positive")
        if self.intent_version is not None and self.intent_version < 0:
            raise ValueError("intent_version must be non-negative")
        if self.protocol != LIFECYCLE_EVENT_PROTOCOL:
            raise ValueError(
                f"lifecycle protocol must be {LIFECYCLE_EVENT_PROTOCOL!r}"
            )
        if self.protocol_version != LIFECYCLE_EVENT_PROTOCOL_VERSION:
            raise ValueError(
                "unsupported lifecycle protocol version: "
                f"{self.protocol_version!r}"
            )
        object.__setattr__(self, "payload", dict(self.payload))
        expected = self.compute_digest()
        if self.digest and self.digest != expected:
            raise ValueError("lifecycle event digest does not match its envelope")
        object.__setattr__(self, "digest", expected)

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "protocol_version": self.protocol_version,
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "adapter": self.adapter,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "intent_id": self.intent_id,
            "intent_version": self.intent_version,
            "sequence": self.sequence,
            "caused_by": self.caused_by,
            "timestamp": self.timestamp,
            "payload": dict(self.payload),
        }

    def compute_digest(self) -> str:
        return _sha256(self.unsigned_dict())

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "digest": self.digest}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LifecycleEvent":
        return cls(
            protocol=str(data.get("protocol") or ""),
            protocol_version=str(data.get("protocol_version") or ""),
            event_id=str(data.get("event_id") or ""),
            event_type=LifecycleEventType(str(data.get("event_type") or "")),
            adapter=str(data.get("adapter") or ""),
            session_id=str(data.get("session_id") or ""),
            run_id=data.get("run_id"),
            intent_id=data.get("intent_id"),
            intent_version=(
                int(data["intent_version"])
                if data.get("intent_version") is not None
                else None
            ),
            sequence=int(data.get("sequence") or 0),
            caused_by=data.get("caused_by"),
            timestamp=str(data.get("timestamp") or ""),
            payload=dict(data.get("payload") or {}),
            digest=str(data.get("digest") or ""),
        )


@dataclass(frozen=True, slots=True)
class LifecycleEventDraft:
    """Event data before the store assigns sequence and causal identity."""

    event_type: LifecycleEventType
    payload: Mapping[str, Any] = field(default_factory=dict)
    intent_id: str | None = None
    intent_version: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", LifecycleEventType(self.event_type))
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(
            self,
            "intent_id",
            _optional_text(self.intent_id, field_name="intent_id"),
        )
        if self.intent_version is not None and self.intent_version < 0:
            raise ValueError("intent_version must be non-negative")


@dataclass(frozen=True, slots=True)
class LifecycleFinding:
    code: LifecycleValidationCode
    message: str
    sequence: int | None = None
    event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "sequence": self.sequence,
            "event_id": self.event_id,
        }


@dataclass(frozen=True, slots=True)
class LifecycleReport:
    """Deterministic projection shared by reports, replay, and recovery."""

    adapter: str | None
    session_id: str | None
    run_id: str | None
    valid: bool
    outcome: str
    verified: bool
    event_count: int
    head_event_id: str | None
    head_digest: str | None
    intent_id: str | None
    intent_version: int | None
    denied_mutations: int
    granted_amendments: int
    findings: tuple[LifecycleFinding, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": "claim-plane.lifecycle-report.v1",
            "adapter": self.adapter,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "valid": self.valid,
            "outcome": self.outcome,
            "verified": self.verified,
            "event_count": self.event_count,
            "head_event_id": self.head_event_id,
            "head_digest": self.head_digest,
            "intent_id": self.intent_id,
            "intent_version": self.intent_version,
            "denied_mutations": self.denied_mutations,
            "granted_amendments": self.granted_amendments,
            "findings": [item.to_dict() for item in self.findings],
        }


_ALLOWED_PREVIOUS: dict[LifecycleEventType, frozenset[LifecycleEventType | None]] = {
    LifecycleEventType.SESSION_STARTED: frozenset(
        {
            None,
            LifecycleEventType.SESSION_STARTED,
            LifecycleEventType.TASK_SUBMITTED,
            LifecycleEventType.ADMISSION_GRANTED,
            LifecycleEventType.ADMISSION_DENIED,
            LifecycleEventType.MUTATION_ALLOWED,
            LifecycleEventType.MUTATION_DENIED,
            LifecycleEventType.MUTATION_OBSERVED,
            LifecycleEventType.SCOPE_EXPANSION_GRANTED,
            LifecycleEventType.SCOPE_EXPANSION_DENIED,
            LifecycleEventType.VERIFICATION_COMPLETED,
            LifecycleEventType.AGENT_STOPPED,
        }
    ),
    LifecycleEventType.TASK_SUBMITTED: frozenset(
        {
            LifecycleEventType.SESSION_STARTED,
            LifecycleEventType.VERIFICATION_COMPLETED,
        }
    ),
    LifecycleEventType.INTENT_PROPOSED: frozenset(
        {
            LifecycleEventType.TASK_SUBMITTED,
            LifecycleEventType.ADMISSION_DENIED,
        }
    ),
    LifecycleEventType.ADMISSION_REQUESTED: frozenset(
        {LifecycleEventType.INTENT_PROPOSED}
    ),
    LifecycleEventType.ADMISSION_GRANTED: frozenset(
        {LifecycleEventType.ADMISSION_REQUESTED}
    ),
    LifecycleEventType.ADMISSION_DENIED: frozenset(
        {LifecycleEventType.ADMISSION_REQUESTED}
    ),
    LifecycleEventType.MUTATION_REQUESTED: frozenset(
        {
            LifecycleEventType.SESSION_STARTED,
            LifecycleEventType.ADMISSION_GRANTED,
            LifecycleEventType.MUTATION_ALLOWED,
            LifecycleEventType.MUTATION_DENIED,
            LifecycleEventType.MUTATION_OBSERVED,
            LifecycleEventType.SCOPE_EXPANSION_GRANTED,
            LifecycleEventType.SCOPE_EXPANSION_DENIED,
        }
    ),
    LifecycleEventType.MUTATION_ALLOWED: frozenset(
        {LifecycleEventType.MUTATION_REQUESTED}
    ),
    LifecycleEventType.MUTATION_DENIED: frozenset(
        {LifecycleEventType.MUTATION_REQUESTED}
    ),
    LifecycleEventType.MUTATION_OBSERVED: frozenset(
        {
            LifecycleEventType.MUTATION_ALLOWED,
            LifecycleEventType.MUTATION_OBSERVED,
        }
    ),
    LifecycleEventType.SCOPE_EXPANSION_REQUESTED: frozenset(
        {
            LifecycleEventType.SESSION_STARTED,
            LifecycleEventType.ADMISSION_GRANTED,
            LifecycleEventType.MUTATION_ALLOWED,
            LifecycleEventType.MUTATION_DENIED,
            LifecycleEventType.MUTATION_OBSERVED,
            LifecycleEventType.SCOPE_EXPANSION_GRANTED,
            LifecycleEventType.SCOPE_EXPANSION_DENIED,
        }
    ),
    LifecycleEventType.SCOPE_EXPANSION_GRANTED: frozenset(
        {LifecycleEventType.SCOPE_EXPANSION_REQUESTED}
    ),
    LifecycleEventType.SCOPE_EXPANSION_DENIED: frozenset(
        {LifecycleEventType.SCOPE_EXPANSION_REQUESTED}
    ),
    LifecycleEventType.VERIFICATION_STARTED: frozenset(
        {
            LifecycleEventType.SESSION_STARTED,
            LifecycleEventType.ADMISSION_GRANTED,
            LifecycleEventType.MUTATION_ALLOWED,
            LifecycleEventType.MUTATION_DENIED,
            LifecycleEventType.MUTATION_OBSERVED,
            LifecycleEventType.SCOPE_EXPANSION_GRANTED,
            LifecycleEventType.SCOPE_EXPANSION_DENIED,
        }
    ),
    LifecycleEventType.VERIFICATION_COMPLETED: frozenset(
        {LifecycleEventType.VERIFICATION_STARTED}
    ),
    LifecycleEventType.AGENT_STOPPED: frozenset(
        {
            LifecycleEventType.SESSION_STARTED,
            LifecycleEventType.TASK_SUBMITTED,
            LifecycleEventType.ADMISSION_GRANTED,
            LifecycleEventType.ADMISSION_DENIED,
            LifecycleEventType.MUTATION_ALLOWED,
            LifecycleEventType.MUTATION_DENIED,
            LifecycleEventType.MUTATION_OBSERVED,
            LifecycleEventType.SCOPE_EXPANSION_GRANTED,
            LifecycleEventType.SCOPE_EXPANSION_DENIED,
            LifecycleEventType.VERIFICATION_COMPLETED,
        }
    ),
    LifecycleEventType.SESSION_ENDED: frozenset(
        {
            LifecycleEventType.SESSION_STARTED,
            LifecycleEventType.TASK_SUBMITTED,
            LifecycleEventType.ADMISSION_GRANTED,
            LifecycleEventType.ADMISSION_DENIED,
            LifecycleEventType.MUTATION_ALLOWED,
            LifecycleEventType.MUTATION_DENIED,
            LifecycleEventType.MUTATION_OBSERVED,
            LifecycleEventType.SCOPE_EXPANSION_GRANTED,
            LifecycleEventType.SCOPE_EXPANSION_DENIED,
            LifecycleEventType.VERIFICATION_COMPLETED,
            LifecycleEventType.AGENT_STOPPED,
        }
    ),
}


def validate_lifecycle_events(
    events: Sequence[LifecycleEvent],
) -> tuple[LifecycleFinding, ...]:
    """Validate ordering, identity, causal links, and state transitions."""

    if not events:
        return (
            LifecycleFinding(
                LifecycleValidationCode.EMPTY,
                "the lifecycle stream contains no events",
            ),
        )

    findings: list[LifecycleFinding] = []
    first = events[0]
    event_ids: set[str] = set()
    previous: LifecycleEvent | None = None
    for expected_sequence, event in enumerate(events, start=1):
        if event.protocol != LIFECYCLE_EVENT_PROTOCOL:
            findings.append(
                LifecycleFinding(
                    LifecycleValidationCode.INVALID_PROTOCOL,
                    "unsupported lifecycle event protocol",
                    event.sequence,
                    event.event_id,
                )
            )
        if event.adapter != first.adapter:
            findings.append(
                LifecycleFinding(
                    LifecycleValidationCode.MIXED_ADAPTER,
                    "one stream cannot contain multiple adapters",
                    event.sequence,
                    event.event_id,
                )
            )
        if event.session_id != first.session_id:
            findings.append(
                LifecycleFinding(
                    LifecycleValidationCode.MIXED_SESSION,
                    "one stream cannot contain multiple sessions",
                    event.sequence,
                    event.event_id,
                )
            )
        if event.sequence != expected_sequence:
            findings.append(
                LifecycleFinding(
                    LifecycleValidationCode.NON_CONTIGUOUS_SEQUENCE,
                    (
                        f"expected sequence {expected_sequence}, "
                        f"received {event.sequence}"
                    ),
                    event.sequence,
                    event.event_id,
                )
            )
        if event.event_id in event_ids:
            findings.append(
                LifecycleFinding(
                    LifecycleValidationCode.DUPLICATE_EVENT_ID,
                    "event_id is duplicated in the lifecycle stream",
                    event.sequence,
                    event.event_id,
                )
            )
        event_ids.add(event.event_id)
        expected_cause = previous.event_id if previous is not None else None
        if event.caused_by != expected_cause:
            findings.append(
                LifecycleFinding(
                    LifecycleValidationCode.CAUSAL_LINK_MISMATCH,
                    "caused_by does not match the previous durable event",
                    event.sequence,
                    event.event_id,
                )
            )
        if event.digest != event.compute_digest():
            findings.append(
                LifecycleFinding(
                    LifecycleValidationCode.INVALID_EVENT_DIGEST,
                    "event digest does not match the canonical envelope",
                    event.sequence,
                    event.event_id,
                )
            )
        previous_type = previous.event_type if previous is not None else None
        allowed_previous = _ALLOWED_PREVIOUS[event.event_type]
        if previous_type not in allowed_previous:
            findings.append(
                LifecycleFinding(
                    LifecycleValidationCode.INVALID_TRANSITION,
                    (
                        f"{event.event_type.value} cannot follow "
                        f"{previous_type.value if previous_type else 'stream start'}"
                    ),
                    event.sequence,
                    event.event_id,
                )
            )
        previous = event
    return tuple(findings)


def build_lifecycle_report(events: Sequence[LifecycleEvent]) -> LifecycleReport:
    """Project a validated stream into one runtime-neutral report."""

    findings = validate_lifecycle_events(events)
    if not events:
        return LifecycleReport(
            adapter=None,
            session_id=None,
            run_id=None,
            valid=False,
            outcome="corrupt",
            verified=False,
            event_count=0,
            head_event_id=None,
            head_digest=None,
            intent_id=None,
            intent_version=None,
            denied_mutations=0,
            granted_amendments=0,
            findings=findings,
        )

    head = events[-1]
    intent_id: str | None = None
    intent_version: int | None = None
    denied_mutations = 0
    granted_amendments = 0
    verified = False
    outcome = "in_progress"
    for event in events:
        if event.intent_id is not None:
            intent_id = event.intent_id
        if event.intent_version is not None:
            intent_version = max(intent_version or 0, event.intent_version)
        if event.event_type is LifecycleEventType.MUTATION_DENIED:
            denied_mutations += 1
        if event.event_type is LifecycleEventType.SCOPE_EXPANSION_GRANTED:
            granted_amendments += 1
        if event.event_type is LifecycleEventType.ADMISSION_DENIED:
            outcome = "denied"
        elif event.event_type is LifecycleEventType.AGENT_STOPPED:
            outcome = "stopped"
        elif event.event_type is LifecycleEventType.SESSION_ENDED:
            outcome = "ended"
        elif event.event_type is LifecycleEventType.VERIFICATION_COMPLETED:
            verified = bool(event.payload.get("verified"))
            outcome = "verified" if verified else "unverified"

    if findings:
        verified = False
        outcome = "corrupt"
    return LifecycleReport(
        adapter=head.adapter,
        session_id=head.session_id,
        run_id=next((item.run_id for item in reversed(events) if item.run_id), None),
        valid=not findings,
        outcome=outcome,
        verified=verified,
        event_count=len(events),
        head_event_id=head.event_id,
        head_digest=head.digest,
        intent_id=intent_id,
        intent_version=intent_version,
        denied_mutations=denied_mutations,
        granted_amendments=granted_amendments,
        findings=findings,
    )


def render_lifecycle_replay(events: Sequence[LifecycleEvent]) -> tuple[str, ...]:
    """Render a deterministic chronology without replaying provider calls."""

    report = build_lifecycle_report(events)
    if not report.valid:
        details = "; ".join(
            f"{item.code.value}: {item.message}" for item in report.findings
        )
        return (f"INVALID LIFECYCLE: {details}",)
    return tuple(
        (
            f"{event.sequence:04d} {event.timestamp} "
            f"{event.event_type.value}"
            + (
                f" intent={event.intent_id}@{event.intent_version}"
                if event.intent_id is not None
                else ""
            )
        )
        for event in events
    )


class LifecycleEventStore:
    """SQLite-backed append-only lifecycle storage with atomic batch append."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=30.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._initialize()

    @classmethod
    def for_project(cls, project_root: str | Path) -> "LifecycleEventStore":
        return cls(
            Path(project_root)
            / ".claim-plane"
            / "lifecycle"
            / "events.sqlite3"
        )

    def __enter__(self) -> "LifecycleEventStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS lifecycle_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lifecycle_events (
                event_id TEXT PRIMARY KEY,
                protocol TEXT NOT NULL,
                protocol_version TEXT NOT NULL,
                event_type TEXT NOT NULL,
                adapter TEXT NOT NULL,
                session_id TEXT NOT NULL,
                run_id TEXT,
                intent_id TEXT,
                intent_version INTEGER,
                sequence INTEGER NOT NULL,
                caused_by TEXT,
                timestamp TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                digest TEXT NOT NULL,
                UNIQUE(adapter, session_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS lifecycle_events_session
                ON lifecycle_events(adapter, session_id, sequence);
            """
        )
        row = self._connection.execute(
            "SELECT value FROM lifecycle_metadata WHERE key = 'protocol'"
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO lifecycle_metadata(key, value) VALUES ('protocol', ?)",
                (LIFECYCLE_STORE_PROTOCOL,),
            )
            self._connection.commit()
        elif str(row["value"]) != LIFECYCLE_STORE_PROTOCOL:
            raise LifecycleCorruptError("unsupported lifecycle store protocol")

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> LifecycleEvent:
        return LifecycleEvent(
            event_id=str(row["event_id"]),
            protocol=str(row["protocol"]),
            protocol_version=str(row["protocol_version"]),
            event_type=LifecycleEventType(str(row["event_type"])),
            adapter=str(row["adapter"]),
            session_id=str(row["session_id"]),
            run_id=row["run_id"],
            intent_id=row["intent_id"],
            intent_version=(
                int(row["intent_version"])
                if row["intent_version"] is not None
                else None
            ),
            sequence=int(row["sequence"]),
            caused_by=row["caused_by"],
            timestamp=str(row["timestamp"]),
            payload=json.loads(str(row["payload_json"])),
            digest=str(row["digest"]),
        )

    def list_events(
        self, *, adapter: str, session_id: str
    ) -> tuple[LifecycleEvent, ...]:
        rows = self._connection.execute(
            "SELECT * FROM lifecycle_events "
            "WHERE adapter = ? AND session_id = ? ORDER BY sequence",
            (adapter, session_id),
        ).fetchall()
        try:
            return tuple(self._row_to_event(row) for row in rows)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LifecycleCorruptError(
                f"lifecycle event storage is corrupt: {exc}"
            ) from exc

    def report(self, *, adapter: str, session_id: str) -> LifecycleReport:
        return build_lifecycle_report(
            self.list_events(adapter=adapter, session_id=session_id)
        )

    def replay(self, *, adapter: str, session_id: str) -> tuple[str, ...]:
        return render_lifecycle_replay(
            self.list_events(adapter=adapter, session_id=session_id)
        )

    def export_ndjson(
        self,
        *,
        adapter: str,
        session_id: str,
        destination: str | Path,
    ) -> Path:
        events = self.list_events(adapter=adapter, session_id=session_id)
        report = build_lifecycle_report(events)
        if not report.valid:
            raise LifecycleCorruptError(
                "invalid lifecycle stream cannot be exported as evidence"
            )
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for event in events:
                handle.write(_canonical_json(event.to_dict()))
                handle.write("\n")
        return path

    def append_batch(
        self,
        *,
        adapter: str,
        session_id: str,
        request_id: str,
        drafts: Sequence[LifecycleEventDraft],
        run_id: str | None = None,
        default_intent_id: str | None = None,
        default_intent_version: int | None = None,
        expected_sequence: int | None = None,
    ) -> tuple[LifecycleEvent, ...]:
        if not drafts:
            return ()
        adapter = _required_text(adapter, field_name="adapter")
        session_id = _required_text(session_id, field_name="session_id")
        request_id = _required_text(request_id, field_name="request_id")
        run_id = _optional_text(run_id, field_name="run_id")
        default_intent_id = _optional_text(
            default_intent_id, field_name="default_intent_id"
        )
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM lifecycle_events "
                "WHERE adapter = ? AND session_id = ? ORDER BY sequence",
                (adapter, session_id),
            ).fetchall()
            existing = tuple(self._row_to_event(row) for row in rows)
            existing_report = (
                build_lifecycle_report(existing) if existing else None
            )
            if existing_report is not None and not existing_report.valid:
                raise LifecycleCorruptError(
                    "cannot append to an invalid lifecycle stream"
                )
            current_sequence = len(existing)
            existing_by_id = {event.event_id: event for event in existing}
            duplicate_events: list[LifecycleEvent] = []
            for ordinal, draft in enumerate(drafts):
                event_id = _event_identity(
                    adapter=adapter,
                    session_id=session_id,
                    request_id=request_id,
                    event_type=draft.event_type,
                    ordinal=ordinal,
                )
                previous = existing_by_id.get(event_id)
                if previous is None:
                    duplicate_events = []
                    break
                expected_intent_id = draft.intent_id or default_intent_id
                expected_intent_version = (
                    draft.intent_version
                    if draft.intent_version is not None
                    else default_intent_version
                )
                if (
                    previous.event_type is not draft.event_type
                    or previous.run_id != run_id
                    or previous.intent_id != expected_intent_id
                    or previous.intent_version != expected_intent_version
                    or dict(previous.payload) != dict(draft.payload)
                ):
                    raise LifecycleConflictError(
                        "event_id already exists with different lifecycle data"
                    )
                duplicate_events.append(previous)
            if len(duplicate_events) == len(drafts):
                connection.rollback()
                return tuple(duplicate_events)
            if any(
                _event_identity(
                    adapter=adapter,
                    session_id=session_id,
                    request_id=request_id,
                    event_type=draft.event_type,
                    ordinal=ordinal,
                )
                in existing_by_id
                for ordinal, draft in enumerate(drafts)
            ):
                raise LifecycleCorruptError(
                    "lifecycle request is only partially persisted"
                )
            if (
                expected_sequence is not None
                and expected_sequence != current_sequence
            ):
                raise LifecycleConflictError(
                    "lifecycle expected sequence does not match durable head"
                )
            head = existing[-1] if existing else None
            base_time = datetime.now(timezone.utc)
            candidates: list[LifecycleEvent] = []
            for ordinal, draft in enumerate(drafts):
                sequence = current_sequence + ordinal + 1
                caused_by = (
                    candidates[-1].event_id
                    if candidates
                    else head.event_id if head is not None else None
                )
                event = LifecycleEvent(
                    event_id=_event_identity(
                        adapter=adapter,
                        session_id=session_id,
                        request_id=request_id,
                        event_type=draft.event_type,
                        ordinal=ordinal,
                    ),
                    event_type=draft.event_type,
                    adapter=adapter,
                    session_id=session_id,
                    run_id=run_id,
                    intent_id=draft.intent_id or default_intent_id,
                    intent_version=(
                        draft.intent_version
                        if draft.intent_version is not None
                        else default_intent_version
                    ),
                    sequence=sequence,
                    caused_by=caused_by,
                    timestamp=(base_time + timedelta(microseconds=ordinal))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    payload=draft.payload,
                )
                candidates.append(event)

            combined = (*existing, *candidates)
            findings = validate_lifecycle_events(combined)
            if findings:
                detail = "; ".join(
                    f"{item.code.value}: {item.message}" for item in findings
                )
                raise LifecycleConflictError(
                    f"lifecycle events violate the normalized state machine: {detail}"
                )

            stored: list[LifecycleEvent] = []
            for event in candidates:
                previous_row = connection.execute(
                    "SELECT * FROM lifecycle_events WHERE event_id = ?",
                    (event.event_id,),
                ).fetchone()
                if previous_row is not None:
                    previous = self._row_to_event(previous_row)
                    if (
                        previous.event_type is not event.event_type
                        or previous.adapter != event.adapter
                        or previous.session_id != event.session_id
                        or previous.run_id != event.run_id
                        or previous.intent_id != event.intent_id
                        or previous.intent_version != event.intent_version
                        or dict(previous.payload) != dict(event.payload)
                    ):
                        raise LifecycleConflictError(
                            "event_id already exists with different lifecycle data"
                        )
                    stored.append(previous)
                    continue
                connection.execute(
                    "INSERT INTO lifecycle_events ("
                    "event_id, protocol, protocol_version, event_type, adapter, "
                    "session_id, run_id, intent_id, intent_version, sequence, "
                    "caused_by, timestamp, payload_json, digest"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.event_id,
                        event.protocol,
                        event.protocol_version,
                        event.event_type.value,
                        event.adapter,
                        event.session_id,
                        event.run_id,
                        event.intent_id,
                        event.intent_version,
                        event.sequence,
                        event.caused_by,
                        event.timestamp,
                        _canonical_json(dict(event.payload)),
                        event.digest,
                    ),
                )
                stored.append(event)
            connection.commit()
            return tuple(stored)
        except Exception:
            connection.rollback()
            raise


_SECRET_KEYS = (
    "authorization",
    "cookie",
    "credential",
    "key",
    "password",
    "prompt",
    "secret",
    "token",
)


def _is_secret_key(key: str) -> bool:
    lowered = key.casefold().replace("-", "_")
    return any(item in lowered for item in _SECRET_KEYS)


def _safe_paths(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value and len(value) <= 512 else ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(
            item
            for item in value
            if isinstance(item, str) and item and len(item) <= 512
        )[:100]
    return ()


def _proposal_summary(request: AdapterRequest) -> dict[str, Any]:
    proposal = request.payload.get("proposal")
    if not isinstance(proposal, Mapping):
        return {"proposal_digest": _safe_digest(None)}
    operations = proposal.get("operations")
    preserves = proposal.get("preserves")
    acceptance = proposal.get("acceptance")
    return {
        "proposal_digest": _safe_digest(dict(proposal)),
        "operation_count": len(operations) if isinstance(operations, Sequence) else 0,
        "preserve_count": len(preserves) if isinstance(preserves, Sequence) else 0,
        "acceptance_count": (
            len(acceptance) if isinstance(acceptance, Sequence) else 0
        ),
    }


def _request_summary(request: AdapterRequest) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "request_id": request.request_id,
        "operation": request.operation.value,
        "request_digest": request.fingerprint(),
    }
    if request.operation is AdapterOperation.START_SESSION:
        summary["resume"] = False
        summary["source"] = str(request.payload.get("source") or "startup")
    elif request.operation is AdapterOperation.RESUME:
        summary["resume"] = True
        summary["source"] = "resume"
    elif request.operation is AdapterOperation.SUBMIT_TASK:
        prompt = request.payload.get("prompt")
        summary["task_digest"] = _safe_digest(prompt)
        summary["task_length"] = len(prompt) if isinstance(prompt, str) else 0
    elif request.operation is AdapterOperation.PROPOSE_INTENT:
        summary.update(_proposal_summary(request))
    elif request.operation in {
        AdapterOperation.REQUEST_MUTATION,
        AdapterOperation.OBSERVE_RESULT,
    }:
        summary["tool_name"] = str(request.payload.get("tool_name") or "unknown")
        summary["mutation_digest"] = _safe_digest(
            {
                key: value
                for key, value in request.payload.items()
                if not _is_secret_key(str(key))
                and key not in {"tool_input", "hook_output", "hook_result"}
            }
        )
        for key in ("paths", "affected_paths", "changed_paths"):
            paths = _safe_paths(request.payload.get(key))
            if paths:
                summary["paths"] = list(paths)
                break
    elif request.operation is AdapterOperation.REQUEST_AMENDMENT:
        summary["ticket_id"] = str(request.payload.get("ticket_id") or "")
        reason = request.payload.get("reason")
        summary["reason_digest"] = _safe_digest(reason)
        summary["reason_length"] = len(reason) if isinstance(reason, str) else 0
    elif request.operation is AdapterOperation.VERIFY_COMPLETION:
        summary["timeout_seconds"] = request.timeout_seconds
    return summary


def _response_summary(
    response: AdapterResponse | None,
    error: AdapterProtocolError | None,
) -> dict[str, Any]:
    if error is not None:
        return {
            "status": "failed",
            "error_code": error.code.value,
            "retryable": error.retryable,
        }
    if response is None:
        return {"status": "failed", "error_code": "missing_response"}
    payload = response.payload
    response_document = response.to_dict()
    response_document.pop("replayed", None)
    summary: dict[str, Any] = {
        "status": response.status.value,
        "response_digest": _safe_digest(response_document),
    }
    for key in ("allowed", "verified", "exit_code", "state"):
        value = payload.get(key)
        if isinstance(value, (str, bool, int, float)) or value is None:
            if key in payload:
                summary[key] = value
    for key in ("changed_paths", "authorized_paths", "denied_paths"):
        paths = _safe_paths(payload.get(key))
        if paths:
            summary[key] = list(paths)
    return summary


def lifecycle_event_drafts(
    request: AdapterRequest,
    *,
    response: AdapterResponse | None = None,
    error: AdapterProtocolError | None = None,
) -> tuple[LifecycleEventDraft, ...]:
    """Translate one adapter operation into normalized, redacted events."""

    request_summary = _request_summary(request)
    response_summary = _response_summary(response, error)
    intent_id = response.intent_id if response is not None else request.intent_id
    intent_version = (
        response.intent_version if response is not None else request.intent_version
    )
    denied = error is not None or (
        response is not None and response.status is not AdapterStatus.SUCCEEDED
    )

    def draft(
        event_type: LifecycleEventType,
        payload: Mapping[str, Any],
    ) -> LifecycleEventDraft:
        return LifecycleEventDraft(
            event_type,
            payload,
            intent_id=intent_id,
            intent_version=intent_version,
        )

    operation = request.operation
    if operation in {AdapterOperation.START_SESSION, AdapterOperation.RESUME}:
        return (draft(LifecycleEventType.SESSION_STARTED, request_summary),)
    if operation is AdapterOperation.SUBMIT_TASK:
        return (draft(LifecycleEventType.TASK_SUBMITTED, request_summary),)
    if operation is AdapterOperation.PROPOSE_INTENT:
        return (
            draft(LifecycleEventType.INTENT_PROPOSED, request_summary),
            draft(LifecycleEventType.ADMISSION_REQUESTED, request_summary),
            draft(
                LifecycleEventType.ADMISSION_DENIED
                if denied
                else LifecycleEventType.ADMISSION_GRANTED,
                response_summary,
            ),
        )
    if operation is AdapterOperation.REQUEST_MUTATION:
        return (
            draft(LifecycleEventType.MUTATION_REQUESTED, request_summary),
            draft(
                LifecycleEventType.MUTATION_DENIED
                if denied
                else LifecycleEventType.MUTATION_ALLOWED,
                response_summary,
            ),
        )
    if operation is AdapterOperation.OBSERVE_RESULT:
        return (
            draft(
                LifecycleEventType.MUTATION_OBSERVED,
                {**request_summary, **response_summary},
            ),
        )
    if operation is AdapterOperation.REQUEST_AMENDMENT:
        return (
            draft(LifecycleEventType.SCOPE_EXPANSION_REQUESTED, request_summary),
            draft(
                LifecycleEventType.SCOPE_EXPANSION_DENIED
                if denied
                else LifecycleEventType.SCOPE_EXPANSION_GRANTED,
                response_summary,
            ),
        )
    if operation is AdapterOperation.VERIFY_COMPLETION:
        verified = bool(
            response is not None
            and response.status is AdapterStatus.SUCCEEDED
            and response.payload.get("verified", True)
        )
        return (
            draft(LifecycleEventType.VERIFICATION_STARTED, request_summary),
            draft(
                LifecycleEventType.VERIFICATION_COMPLETED,
                {**response_summary, "verified": verified},
            ),
        )
    if operation is AdapterOperation.CANCEL:
        return (
            draft(
                LifecycleEventType.AGENT_STOPPED,
                {**request_summary, **response_summary},
            ),
        )
    if operation is AdapterOperation.STOP_SESSION:
        return (
            draft(
                LifecycleEventType.SESSION_ENDED,
                {**request_summary, **response_summary},
            ),
        )
    return ()


def record_adapter_lifecycle(
    *,
    project_root: str | Path,
    request: AdapterRequest,
    response: AdapterResponse | None = None,
    error: AdapterProtocolError | None = None,
) -> tuple[LifecycleEvent, ...]:
    """Persist normalized events for one adapter request."""

    if request.session_id is None:
        return ()
    drafts = lifecycle_event_drafts(request, response=response, error=error)
    if not drafts:
        return ()
    try:
        with LifecycleEventStore.for_project(project_root) as store:
            return store.append_batch(
                adapter=request.adapter,
                session_id=request.session_id,
                request_id=request.request_id,
                drafts=drafts,
                run_id=request.run_id,
                default_intent_id=(
                    response.intent_id
                    if response is not None
                    else request.intent_id
                ),
                default_intent_version=(
                    response.intent_version
                    if response is not None
                    else request.intent_version
                ),
            )
    except LifecycleStoreError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LifecycleCorruptError(
            f"failed to persist normalized lifecycle events: {exc}"
        ) from exc


def load_lifecycle_ndjson(source: str | Path) -> tuple[LifecycleEvent, ...]:
    """Load and validate an exported lifecycle stream."""

    events: list[LifecycleEvent] = []
    with Path(source).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if not isinstance(payload, Mapping):
                    raise ValueError("line must contain a JSON object")
                events.append(LifecycleEvent.from_dict(payload))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise LifecycleCorruptError(
                    f"invalid lifecycle event at line {line_number}: {exc}"
                ) from exc
    report = build_lifecycle_report(events)
    if not report.valid:
        detail = "; ".join(
            f"{item.code.value}: {item.message}" for item in report.findings
        )
        raise LifecycleCorruptError(f"invalid lifecycle stream: {detail}")
    return tuple(events)


def iter_event_payload_values(events: Iterable[LifecycleEvent]) -> Iterable[Any]:
    """Expose payload values for conformance and redaction tests."""

    for event in events:
        yield from event.payload.values()
