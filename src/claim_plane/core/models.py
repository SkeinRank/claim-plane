"""Transport-neutral protocol models for agentic change coordination.

Claim Plane treats a change as an intent, not a lock. Intents declare
resources, semantic concepts, contracts, dependencies, bounded regions,
acceptance commands, and invariants before implementation.  Manifests then
record the concrete Git changes and verification evidence produced by workers.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import posixpath
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping


def _require_fields(
    data: Mapping[str, Any], fields: Iterable[str], *, model_name: str
) -> None:
    """Raise a stable validation error instead of leaking raw mapping KeyErrors."""

    missing = [field for field in fields if field not in data]
    if not missing:
        return
    suffix = "field" if len(missing) == 1 else "fields"
    raise ValueError(
        f"missing required {model_name} {suffix}: {', '.join(sorted(missing))}"
    )


class ClaimType(str, Enum):
    NAME = "name"
    CONTRACT = "contract"
    SCOPE = "scope"


class ClaimState(str, Enum):
    REQUESTED = "requested"
    GRANTED = "granted"
    CONFLICTED = "conflicted"
    RELEASED = "released"
    EXPIRED = "expired"


class VerdictKind(str, Enum):
    GRANTED = "granted"
    CONFLICT = "conflict"
    DUPLICATE = "duplicate"
    CONTRACT_MISMATCH = "contract_mismatch"


class ResourceKind(str, Enum):
    FILE = "file"
    SYMBOL = "symbol"
    CONCEPT = "concept"
    CONTRACT = "contract"
    DOCUMENT = "document"
    CONFIG = "config"
    ROUTE = "route"
    SCHEMA = "schema"


class AccessMode(str, Enum):
    READ = "read"
    WRITE = "write"
    EXTEND = "extend"
    DELETE = "delete"
    RENAME = "rename"
    DOCUMENT = "document"
    TEST = "test"

    @property
    def mutating(self) -> bool:
        return self is not AccessMode.READ

    @property
    def destructive(self) -> bool:
        return self in {AccessMode.DELETE, AccessMode.RENAME}


class IntentState(str, Enum):
    REQUESTED = "requested"
    ADMITTED = "admitted"
    BLOCKED = "blocked"
    ACTIVE = "active"
    COMPLETED = "completed"
    RELEASED = "released"
    EXPIRED = "expired"
    STALE = "stale"


class AdmissionKind(str, Enum):
    INDEPENDENT = "independent"
    COMPATIBLE_OVERLAP = "compatible_overlap"
    CONTRACT_DEPENDENCY = "contract_dependency"
    PARALLEL_WITH_CONSTRAINT = "parallel_with_constraint"
    REQUIRES_STUB = "requires_stub"
    NOTIFY_ON_CHANGE = "notify_on_change"
    SERIALIZE = "serialize"
    REPLAN = "replan"
    REJECT = "reject"


class FindingSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class FindingCode(str, Enum):
    UNDECLARED_CHANGE = "undeclared_change"
    MISSING_DECLARED_CHANGE = "missing_declared_change"
    REGION_VIOLATION = "region_violation"
    CONTRACT_MISSING = "contract_missing"
    CONTRACT_MISMATCH = "contract_mismatch"
    STALE_BASE = "stale_base"
    SEMANTIC_DRIFT = "semantic_drift"
    SEMANTIC_UNAVAILABLE = "semantic_unavailable"
    CROSS_INTENT_COLLISION = "cross_intent_collision"
    OWNER_MISMATCH = "owner_mismatch"
    UNKNOWN_INTENT = "unknown_intent"
    DEPENDENCY_MISSING = "dependency_missing"
    DEPENDENCY_STALE = "dependency_stale"
    ACCEPTANCE_FAILED = "acceptance_failed"
    PRESERVE_VIOLATION = "preserve_violation"
    SNAPSHOT_MUTATION = "snapshot_mutation"
    UNDECLARED_READ = "undeclared_read"
    OBSERVED_DEPENDENCY_MISSING = "observed_dependency_missing"
    OBSERVATION_TRACE_INVALID = "observation_trace_invalid"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    EVIDENCE_SIGNATURE_INVALID = "evidence_signature_invalid"


class WorkerTier(str, Enum):
    ECONOMY = "economy"
    STANDARD = "standard"
    FRONTIER = "frontier"


class RepairActionKind(str, Enum):
    REVERT_UNDECLARED = "revert_undeclared"
    REDECLARE_SCOPE = "redeclare_scope"
    ALIGN_CONTRACT = "align_contract"
    REBASE = "rebase"
    NOTIFY_DEPENDENTS = "notify_dependents"
    SERIALIZE = "serialize"
    ADD_MISSING_ARTIFACT = "add_missing_artifact"
    RERUN_ACCEPTANCE = "rerun_acceptance"
    REPAIR_REGION = "repair_region"
    REFRESH_DEPENDENCY = "refresh_dependency"
    HUMAN_REVIEW = "human_review"
    DECLARE_READ = "declare_read"
    PIN_BASE = "pin_base"


def _clean(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _norm_identifier(value: str) -> str:
    return "".join(ch for ch in value.casefold() if ch.isalnum())


def _norm_path(value: str) -> str:
    value = value.replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    if any(ch in value for ch in "*?["):
        return value.rstrip("/")
    normalized = posixpath.normpath(value)
    return "" if normalized == "." else normalized.rstrip("/")


def _norm_signature(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.split())


def _json_fingerprint(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True, slots=True)
class Claim:
    claim_type: ClaimType
    identifier: str
    owner: str
    signature: str | None = None
    task_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    lease_seconds: int = 900

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_type", ClaimType(self.claim_type))
        object.__setattr__(
            self, "identifier", _clean(self.identifier, field_name="identifier")
        )
        object.__setattr__(self, "owner", _clean(self.owner, field_name="owner"))
        object.__setattr__(self, "signature", _norm_signature(self.signature))
        object.__setattr__(self, "metadata", dict(self.metadata))
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

    @property
    def key(self) -> str:
        return _norm_identifier(self.identifier)

    @property
    def signature_key(self) -> str | None:
        return _norm_signature(self.signature)

    def fingerprint(self) -> str:
        return _json_fingerprint(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_type": self.claim_type.value,
            "identifier": self.identifier,
            "owner": self.owner,
            "signature": self.signature,
            "task_id": self.task_id,
            "metadata": dict(self.metadata),
            "lease_seconds": self.lease_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Claim":
        return cls(
            claim_type=ClaimType(data["claim_type"]),
            identifier=str(data["identifier"]),
            owner=str(data["owner"]),
            signature=data.get("signature"),
            task_id=data.get("task_id"),
            metadata=dict(data.get("metadata") or {}),
            lease_seconds=int(data.get("lease_seconds", 900)),
        )


@dataclass(frozen=True, slots=True)
class Verdict:
    kind: VerdictKind
    claim: Claim
    incumbent: str | None = None
    incumbent_signature: str | None = None
    guidance: str = ""

    @property
    def granted(self) -> bool:
        return self.kind is VerdictKind.GRANTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "granted": self.granted,
            "claim": self.claim.to_dict(),
            "incumbent": self.incumbent,
            "incumbent_signature": self.incumbent_signature,
            "guidance": self.guidance,
        }


@dataclass(frozen=True, slots=True)
class ResourceRef:
    """A normalized resource addressed by an intent operation.

    ``concept_id`` identifies the resource itself.  ``subject_concept_id`` binds
    a contract to the domain/code concept whose behavior it constrains.  Keeping
    these identities separate prevents an unrelated shared contract from
    admitting two writers to the same concept.
    """

    kind: ResourceKind
    identifier: str
    signature: str | None = None
    region: str | None = None
    concept_id: str | None = None
    subject_concept_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ResourceKind(self.kind))
        object.__setattr__(
            self,
            "identifier",
            _clean(self.identifier, field_name="resource identifier"),
        )
        object.__setattr__(self, "signature", _norm_signature(self.signature))
        if self.region is not None:
            object.__setattr__(self, "region", _clean(self.region, field_name="region"))
        if self.concept_id is not None:
            object.__setattr__(
                self, "concept_id", _clean(self.concept_id, field_name="concept_id")
            )
        if self.subject_concept_id is not None:
            object.__setattr__(
                self,
                "subject_concept_id",
                _clean(self.subject_concept_id, field_name="subject_concept_id"),
            )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def key(self) -> str:
        if self.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}:
            return _norm_path(self.identifier)
        if self.kind is ResourceKind.ROUTE:
            return " ".join(self.identifier.casefold().split())
        return _norm_identifier(self.concept_id or self.identifier)

    @property
    def semantic_key(self) -> str:
        return _norm_identifier(self.concept_id or self.identifier)

    @property
    def subject_key(self) -> str | None:
        value = self.subject_concept_id or self.metadata.get("subject_concept_id")
        return _norm_identifier(str(value)) if value else None

    @property
    def is_pattern(self) -> bool:
        return any(ch in self.identifier for ch in "*?[")

    def covers_path(self, path: str) -> bool:
        if self.kind not in {ResourceKind.FILE, ResourceKind.DOCUMENT}:
            return False
        target = _norm_path(path)
        pattern = self.key
        if self.is_pattern:
            return fnmatch.fnmatchcase(target, pattern)
        return target == pattern

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "identifier": self.identifier,
            "signature": self.signature,
            "region": self.region,
            "concept_id": self.concept_id,
            "subject_concept_id": self.subject_concept_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResourceRef":
        _require_fields(data, ("kind", "identifier"), model_name="ResourceRef")
        return cls(
            kind=ResourceKind(data["kind"]),
            identifier=str(data["identifier"]),
            signature=data.get("signature"),
            region=data.get("region"),
            concept_id=data.get("concept_id"),
            subject_concept_id=data.get("subject_concept_id"),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class IntentOperation:
    access: AccessMode
    resource: ResourceRef
    required: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "access", AccessMode(self.access))
        if not isinstance(self.resource, ResourceRef):
            object.__setattr__(self, "resource", ResourceRef.from_dict(self.resource))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def mutating(self) -> bool:
        return self.access.mutating

    def to_dict(self) -> dict[str, Any]:
        return {
            "access": self.access.value,
            "resource": self.resource.to_dict(),
            "required": self.required,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "IntentOperation":
        _require_fields(data, ("access",), model_name="IntentOperation")
        if "resource" in data:
            resource = ResourceRef.from_dict(data["resource"])
        else:
            _require_fields(data, ("kind", "identifier"), model_name="IntentOperation")
            resource = ResourceRef(
                kind=ResourceKind(data["kind"]),
                identifier=str(data["identifier"]),
                signature=data.get("signature"),
                region=data.get("region"),
                concept_id=data.get("concept_id"),
                subject_concept_id=data.get("subject_concept_id"),
                metadata=dict(data.get("resource_metadata") or {}),
            )
        return cls(
            access=AccessMode(data["access"]),
            resource=resource,
            required=bool(data.get("required", True)),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class ChangeIntent:
    intent_id: str
    task_id: str
    owner: str
    base_revision: str
    operations: tuple[IntentOperation, ...]
    base_commit: str | None = None
    preserves: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    lease_seconds: int = 900
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "intent_id", _clean(self.intent_id, field_name="intent_id")
        )
        object.__setattr__(self, "task_id", _clean(self.task_id, field_name="task_id"))
        object.__setattr__(self, "owner", _clean(self.owner, field_name="owner"))
        object.__setattr__(
            self,
            "base_revision",
            _clean(self.base_revision, field_name="base_revision"),
        )
        if self.base_commit is not None:
            pinned = _clean(self.base_commit, field_name="base_commit")
            if not re.fullmatch(r"[0-9a-fA-F]{40,64}", pinned):
                raise ValueError("base_commit must be a full hexadecimal object id")
            object.__setattr__(self, "base_commit", pinned.lower())
        operations = tuple(
            op if isinstance(op, IntentOperation) else IntentOperation.from_dict(op)
            for op in self.operations
        )
        if not operations:
            raise ValueError("operations must not be empty")
        object.__setattr__(self, "operations", operations)
        object.__setattr__(
            self,
            "preserves",
            tuple(_clean(v, field_name="preserve") for v in self.preserves),
        )
        object.__setattr__(
            self,
            "acceptance",
            tuple(_clean(v, field_name="acceptance") for v in self.acceptance),
        )
        object.__setattr__(
            self,
            "dependencies",
            tuple(
                dict.fromkeys(
                    _clean(v, field_name="dependency") for v in self.dependencies
                )
            ),
        )
        if self.intent_id in self.dependencies:
            raise ValueError("an intent cannot depend on itself")
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def mutating_operations(self) -> tuple[IntentOperation, ...]:
        return tuple(op for op in self.operations if op.mutating)

    def fingerprint(self) -> str:
        return _json_fingerprint(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "task_id": self.task_id,
            "owner": self.owner,
            "base_revision": self.base_revision,
            "base_commit": self.base_commit,
            "operations": [op.to_dict() for op in self.operations],
            "preserves": list(self.preserves),
            "acceptance": list(self.acceptance),
            "dependencies": list(self.dependencies),
            "lease_seconds": self.lease_seconds,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChangeIntent":
        _require_fields(
            data,
            ("intent_id", "owner", "base_revision", "operations"),
            model_name="ChangeIntent",
        )
        raw_operations = data["operations"]
        if raw_operations is None:
            raise ValueError("ChangeIntent operations must not be null")
        if isinstance(raw_operations, (str, bytes, Mapping)):
            raise ValueError("ChangeIntent operations must be a sequence of objects")
        return cls(
            intent_id=str(data["intent_id"]),
            task_id=str(data.get("task_id") or data["intent_id"]),
            owner=str(data["owner"]),
            base_revision=str(data["base_revision"]),
            base_commit=(str(data["base_commit"]) if data.get("base_commit") else None),
            operations=tuple(
                IntentOperation.from_dict(item) for item in raw_operations
            ),
            preserves=tuple(data.get("preserves") or ()),
            acceptance=tuple(data.get("acceptance") or ()),
            dependencies=tuple(data.get("dependencies") or ()),
            lease_seconds=int(data.get("lease_seconds", 900)),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class AdmissionConflict:
    existing_intent_id: str
    existing_owner: str
    kind: AdmissionKind
    incoming_operation: IntentOperation
    existing_operation: IntentOperation
    reason: str
    blocking: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "existing_intent_id": self.existing_intent_id,
            "existing_owner": self.existing_owner,
            "kind": self.kind.value,
            "incoming_operation": self.incoming_operation.to_dict(),
            "existing_operation": self.existing_operation.to_dict(),
            "reason": self.reason,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    kind: AdmissionKind
    intent: ChangeIntent
    allowed: bool
    conflicts: tuple[AdmissionConflict, ...] = ()
    constraints: tuple[str, ...] = ()
    notifications: tuple[str, ...] = ()
    guidance: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "allowed": self.allowed,
            "intent": self.intent.to_dict(),
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "constraints": list(self.constraints),
            "notifications": list(self.notifications),
            "guidance": self.guidance,
        }


@dataclass(frozen=True, slots=True)
class ChangedRegion:
    path: str
    start_line: int
    end_line: int
    old_start_line: int | None = None
    old_end_line: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "path", _norm_path(_clean(self.path, field_name="region path"))
        )
        if self.start_line < 0 or self.end_line < self.start_line:
            raise ValueError("invalid changed region")

    def overlaps(self, start: int, end: int) -> bool:
        return not (self.end_line < start or end < self.start_line)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "old_start_line": self.old_start_line,
            "old_end_line": self.old_end_line,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChangedRegion":
        return cls(
            path=str(data["path"]),
            start_line=int(data["start_line"]),
            end_line=int(data["end_line"]),
            old_start_line=int(data["old_start_line"])
            if data.get("old_start_line") is not None
            else None,
            old_end_line=int(data["old_end_line"])
            if data.get("old_end_line") is not None
            else None,
        )


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    command: str
    returncode: int
    duration_ms: int = 0
    stdout_tail: str = ""
    stderr_tail: str = ""
    sandbox_backend: str = "tree"
    sandbox_enforced: bool = False

    @property
    def passed(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "returncode": self.returncode,
            "passed": self.passed,
            "duration_ms": self.duration_ms,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "sandbox_backend": self.sandbox_backend,
            "sandbox_enforced": self.sandbox_enforced,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AcceptanceResult":
        return cls(
            command=str(data["command"]),
            returncode=int(data["returncode"]),
            duration_ms=int(data.get("duration_ms", 0)),
            stdout_tail=str(data.get("stdout_tail") or ""),
            stderr_tail=str(data.get("stderr_tail") or ""),
            sandbox_backend=str(data.get("sandbox_backend") or "tree"),
            sandbox_enforced=bool(data.get("sandbox_enforced", False)),
        )


@dataclass(frozen=True, slots=True)
class ObservedAccess:
    mode: AccessMode
    resource: ResourceRef
    tool: str | None = None
    timestamp: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", AccessMode(self.mode))
        if not isinstance(self.resource, ResourceRef):
            object.__setattr__(self, "resource", ResourceRef.from_dict(self.resource))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "resource": self.resource.to_dict(),
            "tool": self.tool,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ObservedAccess":
        return cls(
            mode=AccessMode(data["mode"]),
            resource=ResourceRef.from_dict(data["resource"]),
            tool=str(data["tool"]) if data.get("tool") is not None else None,
            timestamp=(
                str(data["timestamp"]) if data.get("timestamp") is not None else None
            ),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class ObservedArtifact:
    kind: ResourceKind
    identifier: str
    path: str
    signature: str | None = None
    concept_id: str | None = None
    subject_concept_id: str | None = None
    qualified_identifier: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ResourceKind(self.kind))
        object.__setattr__(
            self,
            "identifier",
            _clean(self.identifier, field_name="artifact identifier"),
        )
        object.__setattr__(
            self, "path", _norm_path(_clean(self.path, field_name="artifact path"))
        )
        object.__setattr__(self, "signature", _norm_signature(self.signature))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def key(self) -> str:
        return _norm_identifier(self.concept_id or self.identifier)

    @property
    def subject_key(self) -> str | None:
        return (
            _norm_identifier(self.subject_concept_id)
            if self.subject_concept_id
            else None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "identifier": self.identifier,
            "path": self.path,
            "signature": self.signature,
            "concept_id": self.concept_id,
            "subject_concept_id": self.subject_concept_id,
            "qualified_identifier": self.qualified_identifier,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ObservedArtifact":
        return cls(
            kind=ResourceKind(data["kind"]),
            identifier=str(data["identifier"]),
            path=str(data["path"]),
            signature=data.get("signature"),
            concept_id=data.get("concept_id"),
            subject_concept_id=data.get("subject_concept_id"),
            qualified_identifier=data.get("qualified_identifier"),
            line_start=int(data["line_start"])
            if data.get("line_start") is not None
            else None,
            line_end=int(data["line_end"])
            if data.get("line_end") is not None
            else None,
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class ChangeManifest:
    intent_id: str
    owner: str
    base_revision: str
    changed_files: tuple[str, ...]
    base_commit: str | None = None
    artifacts: tuple[ObservedArtifact, ...] = ()
    observed_accesses: tuple[ObservedAccess, ...] = ()
    changed_regions: tuple[ChangedRegion, ...] = ()
    acceptance_results: tuple[AcceptanceResult, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "intent_id", _clean(self.intent_id, field_name="manifest intent_id")
        )
        object.__setattr__(
            self, "owner", _clean(self.owner, field_name="manifest owner")
        )
        object.__setattr__(
            self,
            "base_revision",
            _clean(self.base_revision, field_name="manifest base_revision"),
        )
        if self.base_commit is not None:
            object.__setattr__(
                self,
                "base_commit",
                _clean(self.base_commit, field_name="manifest base_commit").lower(),
            )
        object.__setattr__(
            self,
            "changed_files",
            tuple(dict.fromkeys(_norm_path(v) for v in self.changed_files)),
        )
        object.__setattr__(
            self,
            "artifacts",
            tuple(
                a if isinstance(a, ObservedArtifact) else ObservedArtifact.from_dict(a)
                for a in self.artifacts
            ),
        )
        object.__setattr__(
            self,
            "observed_accesses",
            tuple(
                item
                if isinstance(item, ObservedAccess)
                else ObservedAccess.from_dict(item)
                for item in self.observed_accesses
            ),
        )
        object.__setattr__(
            self,
            "changed_regions",
            tuple(
                r if isinstance(r, ChangedRegion) else ChangedRegion.from_dict(r)
                for r in self.changed_regions
            ),
        )
        object.__setattr__(
            self,
            "acceptance_results",
            tuple(
                r if isinstance(r, AcceptanceResult) else AcceptanceResult.from_dict(r)
                for r in self.acceptance_results
            ),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "owner": self.owner,
            "base_revision": self.base_revision,
            "base_commit": self.base_commit,
            "changed_files": list(self.changed_files),
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "observed_accesses": [item.to_dict() for item in self.observed_accesses],
            "changed_regions": [region.to_dict() for region in self.changed_regions],
            "acceptance_results": [
                result.to_dict() for result in self.acceptance_results
            ],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChangeManifest":
        return cls(
            intent_id=str(data["intent_id"]),
            owner=str(data["owner"]),
            base_revision=str(data["base_revision"]),
            base_commit=(str(data["base_commit"]) if data.get("base_commit") else None),
            changed_files=tuple(data.get("changed_files") or ()),
            artifacts=tuple(
                ObservedArtifact.from_dict(a) for a in data.get("artifacts") or ()
            ),
            observed_accesses=tuple(
                ObservedAccess.from_dict(item)
                for item in data.get("observed_accesses") or ()
            ),
            changed_regions=tuple(
                ChangedRegion.from_dict(r) for r in data.get("changed_regions") or ()
            ),
            acceptance_results=tuple(
                AcceptanceResult.from_dict(r)
                for r in data.get("acceptance_results") or ()
            ),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class IntegrationFinding:
    code: FindingCode
    severity: FindingSeverity
    message: str
    path: str | None = None
    identifier: str | None = None
    related_intent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "message": self.message,
            "path": self.path,
            "identifier": self.identifier,
            "related_intent_id": self.related_intent_id,
        }


@dataclass(frozen=True, slots=True)
class IntegrationReport:
    intent_id: str
    findings: tuple[IntegrationFinding, ...]
    metrics: Mapping[str, Any] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not any(f.severity is FindingSeverity.ERROR for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "clean": self.clean,
            "findings": [finding.to_dict() for finding in self.findings],
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True, slots=True)
class RepairAction:
    kind: RepairActionKind
    instruction: str
    paths: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()
    related_intents: tuple[str, ...] = ()
    priority: int = 50

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "instruction": self.instruction,
            "paths": list(self.paths),
            "identifiers": list(self.identifiers),
            "related_intents": list(self.related_intents),
            "priority": self.priority,
        }


@dataclass(frozen=True, slots=True)
class RepairPlan:
    intent_id: str
    actions: tuple[RepairAction, ...]
    rerun_checks: tuple[str, ...] = ("claim-plane verify-git",)

    @property
    def requires_replan(self) -> bool:
        return any(
            action.kind
            in {
                RepairActionKind.REBASE,
                RepairActionKind.SERIALIZE,
                RepairActionKind.REDECLARE_SCOPE,
                RepairActionKind.REFRESH_DEPENDENCY,
                RepairActionKind.DECLARE_READ,
                RepairActionKind.PIN_BASE,
            }
            for action in self.actions
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "requires_replan": self.requires_replan,
            "actions": [action.to_dict() for action in self.actions],
            "rerun_checks": list(self.rerun_checks),
        }


@dataclass(frozen=True, slots=True)
class RouteRecommendation:
    tier: WorkerTier
    risk_score: int
    reasons: tuple[str, ...]
    fallback_tier: WorkerTier | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier.value,
            "risk_score": self.risk_score,
            "reasons": list(self.reasons),
            "fallback_tier": self.fallback_tier.value if self.fallback_tier else None,
        }


def highest_admission_kind(kinds: Iterable[AdmissionKind]) -> AdmissionKind:
    order = {
        AdmissionKind.INDEPENDENT: 0,
        AdmissionKind.COMPATIBLE_OVERLAP: 1,
        AdmissionKind.CONTRACT_DEPENDENCY: 2,
        AdmissionKind.NOTIFY_ON_CHANGE: 3,
        AdmissionKind.PARALLEL_WITH_CONSTRAINT: 4,
        AdmissionKind.REQUIRES_STUB: 5,
        AdmissionKind.SERIALIZE: 6,
        AdmissionKind.REPLAN: 7,
        AdmissionKind.REJECT: 8,
    }
    return max(kinds, key=order.__getitem__, default=AdmissionKind.INDEPENDENT)
