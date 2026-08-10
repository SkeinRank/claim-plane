"""Bounded semantic scope amendments over immutable repository evidence.

Amendment Protocol v2 treats scope growth as a new authority decision rather than a
textual append to an intent.  The planner proves that the candidate is monotonic,
projects newly granted mutation authority onto Semantic Resource IR v2, propagates
its impact through Dependency Graph v2, and checks the additional authority against
other active intents before the existing admission engine is allowed to commit it.

The layer is deterministic and fail-closed.  It does not pause or resume workers;
ordered overlap is surfaced as an explicit result for the runtime-fencing layer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from claim_plane.core.conflict import (
    CommutativityProof,
    SemanticConflictDecision,
    SemanticConflictKind,
    classify_semantic_conflict,
)
from claim_plane.core.dependency_graph import (
    DependencyResolution,
    SemanticDependencyGraph,
)
from claim_plane.core.impact import (
    SemanticChange,
    SemanticChangeKind,
    SemanticImpactReport,
    analyze_semantic_impact,
)
from claim_plane.core.models import (
    AccessMode,
    AdmissionDecision,
    ChangeIntent,
    IntentOperation,
    ResourceKind,
)
from claim_plane.core.resource_ir import SemanticResource, normalize_resource_ref

SEMANTIC_AMENDMENT_PROTOCOL = "claim-plane.semantic-amendment.v2"


class SemanticAmendmentDisposition(str, Enum):
    """Deterministic disposition for one requested authority expansion."""

    APPROVE = "approve"
    ORDER = "order"
    DENY = "deny"


class SemanticAmendmentReason(str, Enum):
    """Stable reason codes for amendment decisions."""

    BOUNDED_SAFE = "bounded_safe"
    NO_NEW_AUTHORITY = "no_new_authority"
    NON_MONOTONIC = "non_monotonic"
    BOUND_EXCEEDED = "bound_exceeded"
    MISSING_SEMANTIC_ROOT = "missing_semantic_root"
    UNRESOLVED_BOUNDARY = "unresolved_boundary"
    ACTIVE_CONFLICT = "active_conflict"
    ACTIVE_UNKNOWN = "active_unknown"
    ACTIVE_ORDERING_REQUIRED = "active_ordering_required"


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


def _operation_path(operation: IntentOperation) -> str | None:
    resource = operation.resource
    if resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}:
        return _normal_path(resource.identifier)
    return _normal_path(
        resource.metadata.get("path")
        or resource.metadata.get("file")
        or resource.metadata.get("source_path")
        or resource.metadata.get("repository_path")
    )


def _rename_target(operation: IntentOperation) -> str | None:
    if operation.access is not AccessMode.RENAME:
        return None
    for source in (operation.metadata, operation.resource.metadata):
        for key in ("rename_to", "target", "to"):
            value = _normal_path(source.get(key))
            if value is not None:
                return value
    return None


def _authority_key(operation: IntentOperation) -> tuple[object, ...]:
    resource = normalize_resource_ref(operation.resource)
    return (
        operation.access.value,
        resource.identity,
        operation.resource.region,
        operation.resource.signature,
        operation.resource.concept_id,
        operation.resource.subject_concept_id,
        _rename_target(operation),
    )


def _control_surface(intent: ChangeIntent) -> tuple[object, ...]:
    return (
        intent.intent_id,
        intent.task_id,
        intent.owner,
        intent.base_revision,
        intent.base_commit,
        intent.preserves,
        intent.acceptance,
        intent.dependencies,
        intent.lease_seconds,
    )


def _is_monotonic(current: ChangeIntent, candidate: ChangeIntent) -> tuple[bool, str]:
    if _control_surface(current) != _control_surface(candidate):
        return (
            False,
            "scope amendment cannot change task, owner, base, guarantees, dependencies, or lease",
        )

    candidate_by_key = {_authority_key(item): item for item in candidate.operations}
    for existing in current.operations:
        replacement = candidate_by_key.get(_authority_key(existing))
        if replacement is None:
            return False, "scope amendment cannot remove existing operations"
        if existing.committed and replacement.contingent:
            return False, "scope amendment cannot revoke committed authority"
        if existing.committed and existing.required != replacement.required:
            return (
                False,
                "scope amendment cannot change required status of committed operations",
            )
        if (
            existing.contingent
            and replacement.contingent
            and existing.required != replacement.required
        ):
            return (
                False,
                "scope amendment cannot rewrite unchanged contingent operations",
            )
    return True, ""


def _new_authority(
    current: ChangeIntent, candidate: ChangeIntent
) -> tuple[IntentOperation, ...]:
    committed = {
        _authority_key(item)
        for item in current.operations
        if item.committed and item.mutating
    }
    additions = [
        item
        for item in candidate.operations
        if item.committed and item.mutating and _authority_key(item) not in committed
    ]
    return tuple(
        sorted(
            additions,
            key=lambda item: (
                _operation_path(item) or "",
                item.resource.kind.value,
                normalize_resource_ref(item.resource).identity,
                item.access.value,
            ),
        )
    )


def _change_kind(operation: IntentOperation) -> SemanticChangeKind:
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


def _resource_nodes_for_path(
    graph: SemanticDependencyGraph, path: str
) -> tuple[SemanticResource, ...]:
    nodes = [
        node.resource
        for node in graph.nodes
        if not node.external and _normal_path(node.resource.path) == path
    ]
    return tuple(sorted(nodes, key=lambda item: item.identity))


def _operation_roots(
    operation: IntentOperation,
    graph: SemanticDependencyGraph,
    *,
    allow_new_files: bool,
) -> tuple[SemanticChange, ...]:
    normalized = normalize_resource_ref(operation.resource)
    path = _operation_path(operation)
    explicit_node = graph.node(normalized.identity)
    kind = _change_kind(operation)
    if explicit_node is not None:
        return (
            SemanticChange(
                identity=explicit_node.identity,
                kind=kind,
                before_resource=explicit_node.resource,
                after_resource=explicit_node.resource,
                metadata={"path": path, "access": operation.access.value},
            ),
        )

    # Callable contract coordinates are projected onto their owning symbol in the
    # Python graph.  Prefer the explicit IR parent and then a qualified symbol id.
    candidates: list[str] = []
    if normalized.parent_identity:
        candidates.append(normalized.parent_identity)
    if path is not None:
        qualified = normalized.qualified_name or operation.resource.metadata.get(
            "qualified_identifier"
        )
        subject = (
            operation.resource.metadata.get("subject_qualified_identifier")
            or operation.resource.metadata.get("subject_qualified_name")
            or operation.resource.subject_concept_id
        )
        for value in (subject, qualified):
            if value:
                candidates.append(f"symbol:{path}#{str(value).strip()}")
    for identity in candidates:
        node = graph.node(identity)
        if node is not None:
            return (
                SemanticChange(
                    identity=node.identity,
                    kind=kind,
                    before_resource=node.resource,
                    after_resource=node.resource,
                    metadata={"path": path, "access": operation.access.value},
                ),
            )

    if operation.resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT} and path:
        resources = _resource_nodes_for_path(graph, path)
        if resources:
            # Exact-file authority can mutate any semantic resource in that file.
            # Model that breadth explicitly instead of pretending the file is one
            # implementation-only root.
            return tuple(
                SemanticChange(
                    identity=resource.identity,
                    kind=SemanticChangeKind.STRUCTURE,
                    before_resource=resource,
                    after_resource=resource,
                    metadata={
                        "path": path,
                        "access": operation.access.value,
                        "broad_file_authority": True,
                    },
                )
                for resource in resources
            )
        declared_new_file = bool(
            operation.metadata.get("new_file")
            or operation.resource.metadata.get("new_file")
        )
        if (
            allow_new_files
            and declared_new_file
            and operation.access
            in {AccessMode.WRITE, AccessMode.DOCUMENT, AccessMode.TEST}
        ):
            synthetic = normalized
            return (
                SemanticChange(
                    identity=synthetic.identity,
                    kind=SemanticChangeKind.ADDED,
                    before_resource=None,
                    after_resource=synthetic,
                    metadata={
                        "path": path,
                        "access": operation.access.value,
                        "synthetic_new_file_root": True,
                    },
                ),
            )
    return ()


def _changes_for_operations(
    operations: Iterable[IntentOperation],
    graph: SemanticDependencyGraph,
    *,
    allow_new_files: bool,
) -> tuple[SemanticChange, ...]:
    by_identity: dict[str, SemanticChange] = {}
    rank = {
        SemanticChangeKind.IMPLEMENTATION: 1,
        SemanticChangeKind.STATE: 2,
        SemanticChangeKind.ADDED: 3,
        SemanticChangeKind.REMOVED: 4,
        SemanticChangeKind.STRUCTURE: 5,
        SemanticChangeKind.CONTRACT: 6,
        SemanticChangeKind.UNKNOWN: 7,
    }
    for operation in operations:
        for change in _operation_roots(
            operation, graph, allow_new_files=allow_new_files
        ):
            previous = by_identity.get(change.identity)
            if previous is None or rank[change.kind] > rank[previous.kind]:
                by_identity[change.identity] = change
    return tuple(by_identity[key] for key in sorted(by_identity))


@dataclass(frozen=True, slots=True)
class SemanticAmendmentBounds:
    """Hard deterministic limits for one scope expansion."""

    max_new_operations: int = 4
    max_new_paths: int = 2
    max_semantic_roots: int = 24
    max_impact_resources: int = 64
    max_impact_depth: int = 4
    max_contract_changes: int = 2
    allow_new_files: bool = False
    deny_unresolved_boundaries: bool = True

    def __post_init__(self) -> None:
        for name in (
            "max_new_operations",
            "max_new_paths",
            "max_semantic_roots",
            "max_impact_resources",
            "max_impact_depth",
            "max_contract_changes",
        ):
            value = int(getattr(self, name))
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_new_operations": self.max_new_operations,
            "max_new_paths": self.max_new_paths,
            "max_semantic_roots": self.max_semantic_roots,
            "max_impact_resources": self.max_impact_resources,
            "max_impact_depth": self.max_impact_depth,
            "max_contract_changes": self.max_contract_changes,
            "allow_new_files": self.allow_new_files,
            "deny_unresolved_boundaries": self.deny_unresolved_boundaries,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SemanticAmendmentBounds":
        return cls(
            max_new_operations=int(data.get("max_new_operations", 4)),
            max_new_paths=int(data.get("max_new_paths", 2)),
            max_semantic_roots=int(data.get("max_semantic_roots", 24)),
            max_impact_resources=int(data.get("max_impact_resources", 64)),
            max_impact_depth=int(data.get("max_impact_depth", 4)),
            max_contract_changes=int(data.get("max_contract_changes", 2)),
            allow_new_files=bool(data.get("allow_new_files", False)),
            deny_unresolved_boundaries=bool(
                data.get("deny_unresolved_boundaries", True)
            ),
        )


@dataclass(frozen=True, slots=True)
class ActiveAmendmentRelation:
    """Semantic relationship between new authority and one active intent."""

    intent_id: str
    owner: str
    decision: SemanticConflictDecision

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "owner": self.owner,
            "decision": self.decision.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ActiveAmendmentRelation":
        return cls(
            intent_id=str(data["intent_id"]),
            owner=str(data["owner"]),
            decision=SemanticConflictDecision.from_dict(data["decision"]),
        )


@dataclass(frozen=True, slots=True)
class SemanticAmendmentAssessment:
    """Source-bound proof describing whether additional authority is bounded."""

    intent_id: str
    current_fingerprint: str
    candidate_fingerprint: str
    graph_fingerprint: str
    disposition: SemanticAmendmentDisposition
    reason: SemanticAmendmentReason
    new_operations: tuple[IntentOperation, ...]
    new_paths: tuple[str, ...]
    semantic_changes: tuple[SemanticChange, ...]
    impact: SemanticImpactReport | None
    active_relations: tuple[ActiveAmendmentRelation, ...] = ()
    bounds: SemanticAmendmentBounds = field(default_factory=SemanticAmendmentBounds)
    detail: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SEMANTIC_AMENDMENT_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SEMANTIC_AMENDMENT_PROTOCOL:
            raise ValueError(
                f"unsupported semantic amendment protocol {self.protocol!r}"
            )
        object.__setattr__(
            self, "disposition", SemanticAmendmentDisposition(self.disposition)
        )
        object.__setattr__(self, "reason", SemanticAmendmentReason(self.reason))
        for name in ("current_fingerprint", "candidate_fingerprint"):
            value = str(getattr(self, name))
            if not value or any(ch not in "0123456789abcdef" for ch in value):
                raise ValueError(f"{name} must be a lowercase hexadecimal fingerprint")
        if len(self.graph_fingerprint) != 64 or any(
            ch not in "0123456789abcdef" for ch in self.graph_fingerprint
        ):
            raise ValueError("graph_fingerprint must be a lowercase SHA-256 digest")
        object.__setattr__(
            self,
            "new_operations",
            tuple(
                item
                if isinstance(item, IntentOperation)
                else IntentOperation.from_dict(item)
                for item in self.new_operations
            ),
        )
        object.__setattr__(self, "new_paths", tuple(sorted(set(self.new_paths))))
        object.__setattr__(
            self,
            "semantic_changes",
            tuple(
                item
                if isinstance(item, SemanticChange)
                else SemanticChange.from_dict(item)
                for item in self.semantic_changes
            ),
        )
        object.__setattr__(
            self,
            "active_relations",
            tuple(
                item
                if isinstance(item, ActiveAmendmentRelation)
                else ActiveAmendmentRelation.from_dict(item)
                for item in self.active_relations
            ),
        )
        if not isinstance(self.bounds, SemanticAmendmentBounds):
            object.__setattr__(
                self,
                "bounds",
                SemanticAmendmentBounds.from_dict(self.bounds),
            )
        if self.impact is not None and not isinstance(
            self.impact, SemanticImpactReport
        ):
            object.__setattr__(
                self,
                "impact",
                SemanticImpactReport.from_dict(self.impact),
            )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def allowed(self) -> bool:
        return self.disposition is SemanticAmendmentDisposition.APPROVE

    @property
    def requires_ordering(self) -> bool:
        return self.disposition is SemanticAmendmentDisposition.ORDER

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            _canonical_json(self.to_dict(include_fingerprint=False))
        ).hexdigest()

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload = {
            "protocol": self.protocol,
            "intent_id": self.intent_id,
            "current_fingerprint": self.current_fingerprint,
            "candidate_fingerprint": self.candidate_fingerprint,
            "graph_fingerprint": self.graph_fingerprint,
            "disposition": self.disposition.value,
            "reason": self.reason.value,
            "allowed": self.allowed,
            "requires_ordering": self.requires_ordering,
            "new_operations": [item.to_dict() for item in self.new_operations],
            "new_paths": list(self.new_paths),
            "semantic_changes": [item.to_dict() for item in self.semantic_changes],
            "impact": self.impact.to_dict() if self.impact is not None else None,
            "active_relations": [item.to_dict() for item in self.active_relations],
            "bounds": self.bounds.to_dict(),
            "detail": self.detail,
            "metadata": dict(self.metadata),
        }
        if include_fingerprint:
            return {"fingerprint": self.fingerprint, **payload}
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SemanticAmendmentAssessment":
        assessment = cls(
            protocol=str(data.get("protocol") or SEMANTIC_AMENDMENT_PROTOCOL),
            intent_id=str(data["intent_id"]),
            current_fingerprint=str(data["current_fingerprint"]),
            candidate_fingerprint=str(data["candidate_fingerprint"]),
            graph_fingerprint=str(data["graph_fingerprint"]),
            disposition=SemanticAmendmentDisposition(data["disposition"]),
            reason=SemanticAmendmentReason(data["reason"]),
            new_operations=tuple(
                IntentOperation.from_dict(item)
                for item in data.get("new_operations") or ()
            ),
            new_paths=tuple(str(item) for item in data.get("new_paths") or ()),
            semantic_changes=tuple(
                SemanticChange.from_dict(item)
                for item in data.get("semantic_changes") or ()
            ),
            impact=(
                SemanticImpactReport.from_dict(data["impact"])
                if data.get("impact") is not None
                else None
            ),
            active_relations=tuple(
                ActiveAmendmentRelation.from_dict(item)
                for item in data.get("active_relations") or ()
            ),
            bounds=SemanticAmendmentBounds.from_dict(data.get("bounds") or {}),
            detail=str(data.get("detail") or ""),
            metadata=dict(data.get("metadata") or {}),
        )
        supplied = data.get("fingerprint")
        if supplied is not None and str(supplied) != assessment.fingerprint:
            raise ValueError("semantic amendment fingerprint mismatch")
        if "allowed" in data and bool(data["allowed"]) != assessment.allowed:
            raise ValueError("semantic amendment allowed flag mismatch")
        if (
            "requires_ordering" in data
            and bool(data["requires_ordering"]) != assessment.requires_ordering
        ):
            raise ValueError("semantic amendment requires_ordering flag mismatch")
        return assessment


@dataclass(frozen=True, slots=True)
class SemanticAmendmentExecution:
    """Atomic bounded-amendment result returned by :class:`Plane`."""

    assessment: SemanticAmendmentAssessment
    admission: AdmissionDecision

    @property
    def allowed(self) -> bool:
        return self.assessment.allowed and self.admission.allowed

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": SEMANTIC_AMENDMENT_PROTOCOL,
            "allowed": self.allowed,
            "assessment": self.assessment.to_dict(),
            "admission": self.admission.to_dict(),
        }


def _assessment(
    current: ChangeIntent,
    candidate: ChangeIntent,
    graph: SemanticDependencyGraph,
    *,
    disposition: SemanticAmendmentDisposition,
    reason: SemanticAmendmentReason,
    bounds: SemanticAmendmentBounds,
    new_operations: Sequence[IntentOperation] = (),
    semantic_changes: Sequence[SemanticChange] = (),
    impact: SemanticImpactReport | None = None,
    active_relations: Sequence[ActiveAmendmentRelation] = (),
    detail: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> SemanticAmendmentAssessment:
    return SemanticAmendmentAssessment(
        intent_id=current.intent_id,
        current_fingerprint=current.fingerprint(),
        candidate_fingerprint=candidate.fingerprint(),
        graph_fingerprint=graph.fingerprint,
        disposition=disposition,
        reason=reason,
        new_operations=tuple(new_operations),
        new_paths=tuple(
            path for path in (_operation_path(item) for item in new_operations) if path
        ),
        semantic_changes=tuple(semantic_changes),
        impact=impact,
        active_relations=tuple(active_relations),
        bounds=bounds,
        detail=detail,
        metadata=dict(metadata or {}),
    )


def assess_semantic_amendment(
    current: ChangeIntent,
    candidate: ChangeIntent,
    graph: SemanticDependencyGraph,
    active_intents: Iterable[ChangeIntent] = (),
    *,
    bounds: SemanticAmendmentBounds | None = None,
    commutativity_proofs: Iterable[CommutativityProof] = (),
) -> SemanticAmendmentAssessment:
    """Prove whether one intent expansion is bounded against active repository work."""

    limits = bounds or SemanticAmendmentBounds()
    monotonic, detail = _is_monotonic(current, candidate)
    if not monotonic:
        return _assessment(
            current,
            candidate,
            graph,
            disposition=SemanticAmendmentDisposition.DENY,
            reason=SemanticAmendmentReason.NON_MONOTONIC,
            bounds=limits,
            detail=detail,
        )

    additions = _new_authority(current, candidate)
    if not additions:
        return _assessment(
            current,
            candidate,
            graph,
            disposition=SemanticAmendmentDisposition.DENY,
            reason=SemanticAmendmentReason.NO_NEW_AUTHORITY,
            bounds=limits,
            detail="candidate grants no additional committed mutation authority",
        )
    paths = {path for path in (_operation_path(item) for item in additions) if path}
    contract_changes = sum(
        1 for item in additions if _change_kind(item) is SemanticChangeKind.CONTRACT
    )
    limit_failures: list[str] = []
    if len(additions) > limits.max_new_operations:
        limit_failures.append("new_operations")
    if len(paths) > limits.max_new_paths:
        limit_failures.append("new_paths")
    if contract_changes > limits.max_contract_changes:
        limit_failures.append("contract_changes")
    if limit_failures:
        return _assessment(
            current,
            candidate,
            graph,
            disposition=SemanticAmendmentDisposition.DENY,
            reason=SemanticAmendmentReason.BOUND_EXCEEDED,
            bounds=limits,
            new_operations=additions,
            detail="amendment exceeds hard bound(s): " + ", ".join(limit_failures),
            metadata={"failed_bounds": limit_failures},
        )

    changes = _changes_for_operations(
        additions, graph, allow_new_files=limits.allow_new_files
    )
    if not changes:
        return _assessment(
            current,
            candidate,
            graph,
            disposition=SemanticAmendmentDisposition.DENY,
            reason=SemanticAmendmentReason.MISSING_SEMANTIC_ROOT,
            bounds=limits,
            new_operations=additions,
            detail="new mutation authority cannot be projected onto the semantic graph",
        )
    if len(changes) > limits.max_semantic_roots:
        return _assessment(
            current,
            candidate,
            graph,
            disposition=SemanticAmendmentDisposition.DENY,
            reason=SemanticAmendmentReason.BOUND_EXCEEDED,
            bounds=limits,
            new_operations=additions,
            semantic_changes=changes,
            detail="amendment expands to too many semantic roots",
            metadata={"failed_bounds": ["semantic_roots"]},
        )

    # Compute complete impact first. ``max_impact_depth`` is an authority bound, not
    # a traversal shortcut: truncating the proof at that depth could hide a deeper
    # consumer and accidentally approve an under-scoped expansion.
    impact = analyze_semantic_impact(graph, changes, max_depth=None)
    max_distance = max((item.min_distance for item in impact.impacted), default=0)
    if max_distance > limits.max_impact_depth:
        return _assessment(
            current,
            candidate,
            graph,
            disposition=SemanticAmendmentDisposition.DENY,
            reason=SemanticAmendmentReason.BOUND_EXCEEDED,
            bounds=limits,
            new_operations=additions,
            semantic_changes=changes,
            impact=impact,
            detail="semantic impact exceeds the configured dependency-depth bound",
            metadata={"failed_bounds": ["impact_depth"], "max_distance": max_distance},
        )
    if len(impact.impacted) > limits.max_impact_resources:
        return _assessment(
            current,
            candidate,
            graph,
            disposition=SemanticAmendmentDisposition.DENY,
            reason=SemanticAmendmentReason.BOUND_EXCEEDED,
            bounds=limits,
            new_operations=additions,
            semantic_changes=changes,
            impact=impact,
            detail="semantic impact exceeds the configured resource bound",
            metadata={"failed_bounds": ["impact_resources"]},
        )
    unresolved = tuple(
        item
        for item in impact.boundaries
        if item.resolution is DependencyResolution.UNRESOLVED
    )
    if limits.deny_unresolved_boundaries and unresolved:
        return _assessment(
            current,
            candidate,
            graph,
            disposition=SemanticAmendmentDisposition.DENY,
            reason=SemanticAmendmentReason.UNRESOLVED_BOUNDARY,
            bounds=limits,
            new_operations=additions,
            semantic_changes=changes,
            impact=impact,
            detail="semantic impact reaches unresolved dependency boundaries",
            metadata={"unresolved_boundary_count": len(unresolved)},
        )

    relations: list[ActiveAmendmentRelation] = []
    proofs = tuple(commutativity_proofs)
    for active in sorted(active_intents, key=lambda item: item.intent_id):
        if active.intent_id == current.intent_id:
            continue
        active_operations = tuple(
            item for item in active.operations if item.committed and item.mutating
        )
        active_changes = _changes_for_operations(
            active_operations, graph, allow_new_files=False
        )
        if not active_changes:
            # Opaque, disjoint resources remain subject to the ordinary admission
            # engine.  Exact overlap cannot be silently ignored.
            active_paths = {
                path
                for path in (_operation_path(item) for item in active_operations)
                if path
            }
            if paths & active_paths:
                return _assessment(
                    current,
                    candidate,
                    graph,
                    disposition=SemanticAmendmentDisposition.DENY,
                    reason=SemanticAmendmentReason.ACTIVE_UNKNOWN,
                    bounds=limits,
                    new_operations=additions,
                    semantic_changes=changes,
                    impact=impact,
                    detail=f"active intent {active.intent_id} overlaps without semantic roots",
                    metadata={"active_intent_id": active.intent_id},
                )
            continue
        semantic = classify_semantic_conflict(
            graph,
            changes,
            active_changes,
            left_id=current.intent_id,
            right_id=active.intent_id,
            commutativity_proofs=proofs,
            # Independence must be proved against the complete immutable graph.
            # The impact-report depth bound controls amendment breadth; using that
            # same finite depth here would intentionally degrade every disjoint pair
            # to ``unknown`` in Conflict Taxonomy v2.
            max_depth=None,
        )
        relation = ActiveAmendmentRelation(active.intent_id, active.owner, semantic)
        relations.append(relation)
        if semantic.kind is SemanticConflictKind.CONFLICTING:
            return _assessment(
                current,
                candidate,
                graph,
                disposition=SemanticAmendmentDisposition.DENY,
                reason=SemanticAmendmentReason.ACTIVE_CONFLICT,
                bounds=limits,
                new_operations=additions,
                semantic_changes=changes,
                impact=impact,
                active_relations=relations,
                detail=f"new authority conflicts with active intent {active.intent_id}",
            )
        if semantic.kind is SemanticConflictKind.UNKNOWN:
            return _assessment(
                current,
                candidate,
                graph,
                disposition=SemanticAmendmentDisposition.DENY,
                reason=SemanticAmendmentReason.ACTIVE_UNKNOWN,
                bounds=limits,
                new_operations=additions,
                semantic_changes=changes,
                impact=impact,
                active_relations=relations,
                detail=(
                    "new authority has unresolved relationship with active intent "
                    f"{active.intent_id}"
                ),
            )
        if semantic.kind is SemanticConflictKind.ORDERED:
            return _assessment(
                current,
                candidate,
                graph,
                disposition=SemanticAmendmentDisposition.ORDER,
                reason=SemanticAmendmentReason.ACTIVE_ORDERING_REQUIRED,
                bounds=limits,
                new_operations=additions,
                semantic_changes=changes,
                impact=impact,
                active_relations=relations,
                detail=(
                    f"new authority requires deterministic ordering with active intent "
                    f"{active.intent_id}; runtime fencing must establish that order before retry"
                ),
            )

    return _assessment(
        current,
        candidate,
        graph,
        disposition=SemanticAmendmentDisposition.APPROVE,
        reason=SemanticAmendmentReason.BOUNDED_SAFE,
        bounds=limits,
        new_operations=additions,
        semantic_changes=changes,
        impact=impact,
        active_relations=relations,
        detail=(
            "additional authority is monotonic, bounded, and semantically safe "
            "against active work"
        ),
        metadata={
            "impact_resource_count": len(impact.impacted),
            "external_boundary_count": sum(
                1
                for item in impact.boundaries
                if item.resolution is DependencyResolution.EXTERNAL
            ),
        },
    )
