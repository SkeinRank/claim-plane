"""Deterministic same-file admission using semantic resource evidence.

Same-file Admission v2 upgrades the conservative line-region fallback used by the
swarm concurrency controller.  When a policy permits region-safe concurrency and
both work items expose semantic mutation roots for the same repository path, Claim
Plane classifies those roots with Dependency Graph v2 and Conflict Taxonomy v2.

The layer is deliberately fail-closed.  Missing semantic evidence never unlocks
parallel execution; it falls back to the existing path/region policy.  Explicit
same-file deny/serialize policy remains authoritative over semantic evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from claim_plane.core import (
    AccessMode,
    CommutativityProof,
    ResourceKind,
    SemanticChange,
    SemanticChangeKind,
    SemanticConflictDecision,
    SemanticConflictKind,
    SemanticConflictOrder,
    SemanticDependencyGraph,
    classify_semantic_conflict,
    normalize_resource_ref,
)
from claim_plane.swarm.budget import (
    ConflictPolicy,
    SameFilePolicy,
    SwarmBudgetPolicy,
)
from claim_plane.swarm.models import WorkItem

SAME_FILE_ADMISSION_PROTOCOL = "claim-plane.same-file-admission.v2"


class SameFileAdmissionAction(str, Enum):
    """Action emitted for one same-file work pair."""

    PARALLEL = "parallel"
    SERIALIZE = "serialize"
    DENY = "deny"
    FALLBACK = "fallback"


class SameFileAdmissionReason(str, Enum):
    """Stable machine-readable reason for the same-file decision."""

    POLICY_DENY = "policy_deny"
    POLICY_SERIALIZE = "policy_serialize"
    MISSING_SEMANTIC_GRAPH = "missing_semantic_graph"
    MISSING_SEMANTIC_ROOTS = "missing_semantic_roots"
    SEMANTIC_INDEPENDENT = "semantic_independent"
    SEMANTIC_COMMUTATIVE = "semantic_commutative"
    SEMANTIC_ORDERED = "semantic_ordered"
    SEMANTIC_CONFLICTING = "semantic_conflicting"
    SEMANTIC_UNKNOWN = "semantic_unknown"


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _normal_path(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/") or None


def _operation_path(operation: Any) -> str | None:
    resource = operation.resource
    if resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}:
        return _normal_path(resource.identifier)
    return _normal_path(
        resource.metadata.get("path")
        or resource.metadata.get("file")
        or resource.metadata.get("source_path")
        or resource.metadata.get("repository_path")
    )


def _change_kind(operation: Any) -> SemanticChangeKind:
    explicit = operation.metadata.get(
        "semantic_change_kind"
    ) or operation.resource.metadata.get("semantic_change_kind")
    if explicit is not None:
        return SemanticChangeKind(str(explicit))
    if operation.access is AccessMode.DELETE:
        return SemanticChangeKind.REMOVED
    if operation.access is AccessMode.RENAME:
        return SemanticChangeKind.STRUCTURE
    if operation.resource.kind in {ResourceKind.CONTRACT, ResourceKind.SCHEMA}:
        return SemanticChangeKind.CONTRACT
    if operation.resource.metadata.get("state") is True:
        return SemanticChangeKind.STATE
    if (
        operation.metadata.get("contract_change") is True
        or operation.resource.metadata.get("contract_change") is True
    ):
        return SemanticChangeKind.CONTRACT
    return SemanticChangeKind.IMPLEMENTATION


def _kind_rank(kind: SemanticChangeKind) -> int:
    return {
        SemanticChangeKind.UNKNOWN: 0,
        SemanticChangeKind.IMPLEMENTATION: 1,
        SemanticChangeKind.STATE: 2,
        SemanticChangeKind.ADDED: 3,
        SemanticChangeKind.REMOVED: 4,
        SemanticChangeKind.STRUCTURE: 5,
        SemanticChangeKind.CONTRACT: 6,
    }[kind]


def _graph_root_for_operation(
    operation: Any,
    *,
    path: str,
    graph: SemanticDependencyGraph,
) -> tuple[str, Any] | None:
    resource = operation.resource
    if resource.kind not in {
        ResourceKind.SYMBOL,
        ResourceKind.CONTRACT,
        ResourceKind.SCHEMA,
    }:
        return None
    normalized = normalize_resource_ref(resource)
    if graph.node(normalized.identity) is not None:
        return normalized.identity, normalized

    # Dependency Graph v2 represents Python callable contracts on their owning symbol.
    # Prefer an explicit IR parent when available; otherwise derive the symbol identity
    # from the qualified contract coordinate for this repository path.
    candidates: list[str] = []
    if normalized.parent_identity:
        candidates.append(normalized.parent_identity)
    qualified = normalized.qualified_name or resource.metadata.get(
        "qualified_identifier"
    )
    subject = (
        resource.metadata.get("subject_qualified_identifier")
        or resource.metadata.get("subject_qualified_name")
        or resource.subject_concept_id
    )
    for value in (subject, qualified):
        if value:
            candidates.append(f"symbol:{path}#{str(value).strip()}")
    for identity in candidates:
        node = graph.node(identity)
        if node is not None:
            return identity, node.resource
    return normalized.identity, normalized


def semantic_changes_for_path(
    item: WorkItem,
    path: str,
    graph: SemanticDependencyGraph,
) -> tuple[SemanticChange, ...]:
    """Project one work item's committed semantic mutations onto graph roots.

    Multiple declarations for the same root are collapsed deterministically.  A
    stronger change kind (for example ``contract`` over ``implementation``) wins.
    """

    target_path = _normal_path(path)
    assert target_path is not None
    by_identity: dict[str, SemanticChange] = {}
    for operation in item.operations:
        if not operation.committed or not operation.mutating:
            continue
        if _operation_path(operation) != target_path:
            continue
        resolved = _graph_root_for_operation(operation, path=target_path, graph=graph)
        if resolved is None:
            continue
        identity, resource = resolved
        kind = _change_kind(operation)
        change = SemanticChange(
            identity=identity,
            kind=kind,
            before_resource=resource,
            after_resource=resource,
            metadata={
                "work_id": item.work_id,
                "path": target_path,
                "access": operation.access.value,
                "declared_kind": operation.resource.kind.value,
            },
        )
        previous = by_identity.get(identity)
        if previous is None or _kind_rank(change.kind) > _kind_rank(previous.kind):
            by_identity[identity] = change
    return tuple(by_identity[key] for key in sorted(by_identity))


def semantic_changes_for_item(
    item: WorkItem,
    graph: SemanticDependencyGraph,
) -> tuple[SemanticChange, ...]:
    """Project all committed semantic mutation roots for one work item.

    The projection is intentionally limited to graph-backed repository resources.
    File-only declarations remain outside this helper so incomplete semantic scope
    cannot be mistaken for proof of independence.
    """

    paths = sorted(
        {
            path
            for operation in item.operations
            if operation.committed and operation.mutating
            if operation.resource.kind
            in {ResourceKind.SYMBOL, ResourceKind.CONTRACT, ResourceKind.SCHEMA}
            if (path := _operation_path(operation)) is not None
        }
    )
    by_identity: dict[str, SemanticChange] = {}
    for path in paths:
        for change in semantic_changes_for_path(item, path, graph):
            previous = by_identity.get(change.identity)
            if previous is None or _kind_rank(change.kind) > _kind_rank(previous.kind):
                by_identity[change.identity] = change
    return tuple(by_identity[key] for key in sorted(by_identity))


@dataclass(frozen=True, slots=True)
class SameFileAdmissionDecision:
    """Source-bound same-file admission evidence for one work-item pair."""

    left_id: str
    right_id: str
    path: str
    action: SameFileAdmissionAction
    reason: SameFileAdmissionReason
    semantic_kind: SemanticConflictKind | None = None
    order: SemanticConflictOrder | None = None
    graph_fingerprint: str | None = None
    semantic_decision_fingerprint: str | None = None
    left_changes: tuple[str, ...] = ()
    right_changes: tuple[str, ...] = ()
    detail: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SAME_FILE_ADMISSION_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SAME_FILE_ADMISSION_PROTOCOL:
            raise ValueError(
                f"unsupported same-file admission protocol {self.protocol!r}"
            )
        if not self.left_id or not self.right_id or self.left_id == self.right_id:
            raise ValueError("same-file admission requires distinct work ids")
        path = _normal_path(self.path)
        if path is None:
            raise ValueError("same-file admission path must not be empty")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "action", SameFileAdmissionAction(self.action))
        object.__setattr__(self, "reason", SameFileAdmissionReason(self.reason))
        if self.semantic_kind is not None:
            object.__setattr__(
                self, "semantic_kind", SemanticConflictKind(self.semantic_kind)
            )
        if self.order is not None:
            object.__setattr__(self, "order", SemanticConflictOrder(self.order))
        if (
            self.order is not None
            and self.semantic_kind is not SemanticConflictKind.ORDERED
        ):
            raise ValueError(
                "same-file admission order requires ordered semantic classification"
            )
        for name in ("graph_fingerprint", "semantic_decision_fingerprint"):
            value = getattr(self, name)
            if value is not None and (
                len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        object.__setattr__(self, "left_changes", tuple(sorted(set(self.left_changes))))
        object.__setattr__(
            self, "right_changes", tuple(sorted(set(self.right_changes)))
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def parallel_safe(self) -> bool:
        return self.action is SameFileAdmissionAction.PARALLEL

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            _canonical_json(self.to_dict(include_fingerprint=False))
        ).hexdigest()

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload = {
            "protocol": self.protocol,
            "left_id": self.left_id,
            "right_id": self.right_id,
            "path": self.path,
            "action": self.action.value,
            "reason": self.reason.value,
            "semantic_kind": self.semantic_kind.value if self.semantic_kind else None,
            "order": self.order.value if self.order else None,
            "graph_fingerprint": self.graph_fingerprint,
            "semantic_decision_fingerprint": self.semantic_decision_fingerprint,
            "left_changes": list(self.left_changes),
            "right_changes": list(self.right_changes),
            "parallel_safe": self.parallel_safe,
            "detail": self.detail,
            "metadata": dict(self.metadata),
        }
        return (
            {"fingerprint": self.fingerprint, **payload}
            if include_fingerprint
            else payload
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SameFileAdmissionDecision":
        result = cls(
            protocol=str(data.get("protocol") or SAME_FILE_ADMISSION_PROTOCOL),
            left_id=str(data["left_id"]),
            right_id=str(data["right_id"]),
            path=str(data["path"]),
            action=SameFileAdmissionAction(data["action"]),
            reason=SameFileAdmissionReason(data["reason"]),
            semantic_kind=(
                SemanticConflictKind(data["semantic_kind"])
                if data.get("semantic_kind") is not None
                else None
            ),
            order=(
                SemanticConflictOrder(data["order"])
                if data.get("order") is not None
                else None
            ),
            graph_fingerprint=data.get("graph_fingerprint"),
            semantic_decision_fingerprint=data.get("semantic_decision_fingerprint"),
            left_changes=tuple(str(item) for item in data.get("left_changes") or ()),
            right_changes=tuple(str(item) for item in data.get("right_changes") or ()),
            detail=str(data.get("detail") or ""),
            metadata=dict(data.get("metadata") or {}),
        )
        supplied = data.get("fingerprint")
        if supplied is not None and str(supplied) != result.fingerprint:
            raise ValueError("same-file admission fingerprint mismatch")
        if (
            "parallel_safe" in data
            and bool(data["parallel_safe"]) != result.parallel_safe
        ):
            raise ValueError("same-file admission parallel_safe mismatch")
        return result


def _decision_from_semantic(
    semantic: SemanticConflictDecision,
    *,
    path: str,
    policy: SwarmBudgetPolicy,
) -> SameFileAdmissionDecision:
    if semantic.kind is SemanticConflictKind.INDEPENDENT:
        action = SameFileAdmissionAction.PARALLEL
        reason = SameFileAdmissionReason.SEMANTIC_INDEPENDENT
        detail = "same-file semantic mutation roots are independent"
    elif semantic.kind is SemanticConflictKind.COMMUTATIVE:
        action = SameFileAdmissionAction.PARALLEL
        reason = SameFileAdmissionReason.SEMANTIC_COMMUTATIVE
        detail = "same-file semantic mutation roots have explicit commutativity proof"
    elif semantic.kind is SemanticConflictKind.ORDERED:
        action = SameFileAdmissionAction.SERIALIZE
        reason = SameFileAdmissionReason.SEMANTIC_ORDERED
        detail = "same-file semantic dependency requires deterministic ordering"
    elif semantic.kind is SemanticConflictKind.CONFLICTING:
        action = SameFileAdmissionAction.SERIALIZE
        reason = SameFileAdmissionReason.SEMANTIC_CONFLICTING
        detail = "same-file semantic mutation roots conflict"
    else:
        action = (
            SameFileAdmissionAction.DENY
            if policy.concurrency.unknown_overlap is ConflictPolicy.DENY
            else SameFileAdmissionAction.SERIALIZE
        )
        reason = SameFileAdmissionReason.SEMANTIC_UNKNOWN
        detail = "same-file semantic evidence is incomplete or unresolved"
    return SameFileAdmissionDecision(
        left_id=semantic.left_id,
        right_id=semantic.right_id,
        path=path,
        action=action,
        reason=reason,
        semantic_kind=semantic.kind,
        order=semantic.order,
        graph_fingerprint=semantic.graph_fingerprint,
        semantic_decision_fingerprint=semantic.fingerprint,
        left_changes=semantic.left_changes,
        right_changes=semantic.right_changes,
        detail=detail,
        metadata={"semantic_evidence_count": len(semantic.evidence)},
    )


def evaluate_same_file_admission(
    left: WorkItem,
    right: WorkItem,
    path: str,
    policy: SwarmBudgetPolicy,
    *,
    semantic_graph: SemanticDependencyGraph | None = None,
    commutativity_proofs: Iterable[CommutativityProof] = (),
    max_depth: int | None = None,
) -> SameFileAdmissionDecision:
    """Evaluate whether coarse same-file work can execute concurrently.

    Unknown or overlapping planner regions may be refined by source-bound semantic
    mutation roots. Only ``region_safe`` policy may be upgraded to semantic
    parallelism. Explicit ``deny`` and ``serialize`` settings remain hard policy
    choices.
    """

    if policy.concurrency.same_file is SameFilePolicy.DENY:
        return SameFileAdmissionDecision(
            left.work_id,
            right.work_id,
            path,
            SameFileAdmissionAction.DENY,
            SameFileAdmissionReason.POLICY_DENY,
            detail="same-file concurrency is denied by policy",
        )
    if policy.concurrency.same_file is SameFilePolicy.SERIALIZE:
        return SameFileAdmissionDecision(
            left.work_id,
            right.work_id,
            path,
            SameFileAdmissionAction.SERIALIZE,
            SameFileAdmissionReason.POLICY_SERIALIZE,
            detail="same-file work is serialized by policy",
        )
    if semantic_graph is None:
        return SameFileAdmissionDecision(
            left.work_id,
            right.work_id,
            path,
            SameFileAdmissionAction.FALLBACK,
            SameFileAdmissionReason.MISSING_SEMANTIC_GRAPH,
            detail="semantic graph is unavailable; preserve region-based fallback",
        )

    left_changes = semantic_changes_for_path(left, path, semantic_graph)
    right_changes = semantic_changes_for_path(right, path, semantic_graph)
    if not left_changes or not right_changes:
        return SameFileAdmissionDecision(
            left.work_id,
            right.work_id,
            path,
            SameFileAdmissionAction.FALLBACK,
            SameFileAdmissionReason.MISSING_SEMANTIC_ROOTS,
            graph_fingerprint=semantic_graph.fingerprint,
            left_changes=tuple(item.identity for item in left_changes),
            right_changes=tuple(item.identity for item in right_changes),
            detail="both work items must declare same-file semantic mutation roots",
        )

    semantic = classify_semantic_conflict(
        semantic_graph,
        left_changes,
        right_changes,
        left_id=left.work_id,
        right_id=right.work_id,
        commutativity_proofs=commutativity_proofs,
        max_depth=max_depth,
        mutation_sensitive_ordering=True,
    )
    return _decision_from_semantic(semantic, path=path, policy=policy)


__all__ = [
    "SAME_FILE_ADMISSION_PROTOCOL",
    "SameFileAdmissionAction",
    "SameFileAdmissionDecision",
    "SameFileAdmissionReason",
    "evaluate_same_file_admission",
    "semantic_changes_for_item",
    "semantic_changes_for_path",
]
