"""Deterministic semantic conflict taxonomy for repository mutations.

The taxonomy consumes Semantic Dependency Graph v2 plus the semantic impact surface
of two mutation sets and classifies their relationship as independent, commutative,
ordered, conflicting, or unknown.  It deliberately does not grant execution authority:
later admission stages may consume the decision, policy, and evidence to decide whether
parallel execution is allowed.

The classifier is fail-closed.  Missing graph roots, explicitly unknown changes, and
shared unresolved dependency boundaries are not treated as proof of independence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from claim_plane.core.dependency_graph import (
    DependencyRelation,
    DependencyResolution,
    SemanticDependencyGraph,
)
from claim_plane.core.impact import (
    ImpactPath,
    SemanticChange,
    SemanticChangeKind,
    SemanticImpactReport,
    analyze_semantic_impact,
)

SEMANTIC_CONFLICT_TAXONOMY_PROTOCOL = "claim-plane.semantic-conflict-taxonomy.v2"


class SemanticConflictKind(str, Enum):
    """Deterministic relationship between two semantic mutation sets."""

    INDEPENDENT = "independent"
    COMMUTATIVE = "commutative"
    ORDERED = "ordered"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


class SemanticConflictOrder(str, Enum):
    """Required order when a pair is safe only after deterministic serialization."""

    LEFT_BEFORE_RIGHT = "left_before_right"
    RIGHT_BEFORE_LEFT = "right_before_left"


class SemanticConflictReason(str, Enum):
    """Machine-readable evidence categories emitted by the taxonomy."""

    DIRECT_RESOURCE_OVERLAP = "direct_resource_overlap"
    SEMANTIC_DEPENDENCY = "semantic_dependency"
    STABLE_CONTRACT_DEPENDENCY = "stable_contract_dependency"
    MUTUAL_DEPENDENCY = "mutual_dependency"
    EXPLICIT_COMMUTATIVITY = "explicit_commutativity"
    UNKNOWN_CHANGE = "unknown_change"
    MISSING_GRAPH_ROOT = "missing_graph_root"
    BOUNDED_IMPACT = "bounded_impact"
    UNRESOLVED_DEPENDENCY = "unresolved_dependency"
    DISJOINT_SEMANTIC_SURFACE = "disjoint_semantic_surface"


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _pair_key(left_identity: str, right_identity: str) -> tuple[str, str]:
    if left_identity <= right_identity:
        return left_identity, right_identity
    return right_identity, left_identity


@dataclass(frozen=True, slots=True)
class CommutativityProof:
    """Explicit deterministic evidence that one semantic change pair commutes.

    The taxonomy never infers commutativity from naming similarity or text distance.
    A caller must provide a proof produced by a trusted deterministic rule.  The proof
    is evidence only; policy/admission code remains responsible for deciding whether a
    proof source is acceptable for mutation authority.
    """

    left_identity: str
    right_identity: str
    basis: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        left = self.left_identity.strip()
        right = self.right_identity.strip()
        basis = self.basis.strip()
        if not left or not right:
            raise ValueError("commutativity proof identities must not be empty")
        if not basis:
            raise ValueError("commutativity proof basis must not be empty")
        object.__setattr__(self, "left_identity", left)
        object.__setattr__(self, "right_identity", right)
        object.__setattr__(self, "basis", basis)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def pair_key(self) -> tuple[str, str]:
        return _pair_key(self.left_identity, self.right_identity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "left_identity": self.left_identity,
            "right_identity": self.right_identity,
            "basis": self.basis,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CommutativityProof":
        return cls(
            left_identity=str(data["left_identity"]),
            right_identity=str(data["right_identity"]),
            basis=str(data["basis"]),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class SemanticConflictEvidence:
    """One deterministic reason contributing to a conflict classification."""

    reason: SemanticConflictReason
    left_identity: str | None = None
    right_identity: str | None = None
    order: SemanticConflictOrder | None = None
    path: tuple[str, ...] = ()
    relations: tuple[DependencyRelation, ...] = ()
    boundary_target: str | None = None
    detail: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", SemanticConflictReason(self.reason))
        if self.order is not None:
            object.__setattr__(self, "order", SemanticConflictOrder(self.order))
        object.__setattr__(self, "path", tuple(str(item) for item in self.path))
        object.__setattr__(
            self,
            "relations",
            tuple(DependencyRelation(item) for item in self.relations),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason.value,
            "left_identity": self.left_identity,
            "right_identity": self.right_identity,
            "order": self.order.value if self.order is not None else None,
            "path": list(self.path),
            "relations": [item.value for item in self.relations],
            "boundary_target": self.boundary_target,
            "detail": self.detail,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SemanticConflictEvidence":
        return cls(
            reason=SemanticConflictReason(data["reason"]),
            left_identity=(
                str(data["left_identity"])
                if data.get("left_identity") is not None
                else None
            ),
            right_identity=(
                str(data["right_identity"])
                if data.get("right_identity") is not None
                else None
            ),
            order=(
                SemanticConflictOrder(data["order"])
                if data.get("order") is not None
                else None
            ),
            path=tuple(str(item) for item in data.get("path") or ()),
            relations=tuple(
                DependencyRelation(item) for item in data.get("relations") or ()
            ),
            boundary_target=(
                str(data["boundary_target"])
                if data.get("boundary_target") is not None
                else None
            ),
            detail=str(data.get("detail") or ""),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class SemanticConflictDecision:
    """Immutable pair classification with source-bound impact evidence."""

    graph_fingerprint: str
    left_id: str
    right_id: str
    kind: SemanticConflictKind
    order: SemanticConflictOrder | None
    left_changes: tuple[str, ...]
    right_changes: tuple[str, ...]
    left_impact_fingerprint: str
    right_impact_fingerprint: str
    evidence: tuple[SemanticConflictEvidence, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SEMANTIC_CONFLICT_TAXONOMY_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SEMANTIC_CONFLICT_TAXONOMY_PROTOCOL:
            raise ValueError(
                f"unsupported semantic conflict protocol {self.protocol!r}"
            )
        for name in (
            "graph_fingerprint",
            "left_impact_fingerprint",
            "right_impact_fingerprint",
        ):
            value = str(getattr(self, name))
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        left_id = self.left_id.strip()
        right_id = self.right_id.strip()
        if not left_id or not right_id or left_id == right_id:
            raise ValueError("semantic conflict decision requires distinct side ids")
        object.__setattr__(self, "left_id", left_id)
        object.__setattr__(self, "right_id", right_id)
        object.__setattr__(self, "kind", SemanticConflictKind(self.kind))
        if self.order is not None:
            object.__setattr__(self, "order", SemanticConflictOrder(self.order))
        if self.kind is SemanticConflictKind.ORDERED and self.order is None:
            raise ValueError("ordered conflict classification requires an order")
        if self.kind is not SemanticConflictKind.ORDERED and self.order is not None:
            raise ValueError("only ordered classifications may carry an order")
        left_changes = tuple(sorted({str(item) for item in self.left_changes}))
        right_changes = tuple(sorted({str(item) for item in self.right_changes}))
        if not left_changes or not right_changes:
            raise ValueError(
                "semantic conflict decision requires changes on both sides"
            )
        object.__setattr__(self, "left_changes", left_changes)
        object.__setattr__(self, "right_changes", right_changes)
        evidence = tuple(
            sorted(
                (
                    item
                    if isinstance(item, SemanticConflictEvidence)
                    else SemanticConflictEvidence.from_dict(item)
                    for item in self.evidence
                ),
                key=lambda item: (
                    item.reason.value,
                    item.left_identity or "",
                    item.right_identity or "",
                    item.order.value if item.order is not None else "",
                    item.boundary_target or "",
                    item.path,
                    tuple(relation.value for relation in item.relations),
                    item.detail,
                ),
            )
        )
        if not evidence:
            raise ValueError("semantic conflict decision requires evidence")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def parallel_safe(self) -> bool:
        return self.kind in {
            SemanticConflictKind.INDEPENDENT,
            SemanticConflictKind.COMMUTATIVE,
        }

    @property
    def requires_ordering(self) -> bool:
        return self.kind is SemanticConflictKind.ORDERED

    @property
    def fail_closed(self) -> bool:
        return self.kind in {
            SemanticConflictKind.CONFLICTING,
            SemanticConflictKind.UNKNOWN,
        }

    @property
    def fingerprint(self) -> str:
        payload = self.to_dict(include_fingerprint=False)
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        result = {
            "protocol": self.protocol,
            "graph_fingerprint": self.graph_fingerprint,
            "left_id": self.left_id,
            "right_id": self.right_id,
            "kind": self.kind.value,
            "order": self.order.value if self.order is not None else None,
            "left_changes": list(self.left_changes),
            "right_changes": list(self.right_changes),
            "left_impact_fingerprint": self.left_impact_fingerprint,
            "right_impact_fingerprint": self.right_impact_fingerprint,
            "parallel_safe": self.parallel_safe,
            "requires_ordering": self.requires_ordering,
            "fail_closed": self.fail_closed,
            "evidence": [item.to_dict() for item in self.evidence],
            "metadata": dict(self.metadata),
        }
        if include_fingerprint:
            return {"fingerprint": self.fingerprint, **result}
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SemanticConflictDecision":
        decision = cls(
            protocol=str(data.get("protocol") or SEMANTIC_CONFLICT_TAXONOMY_PROTOCOL),
            graph_fingerprint=str(data["graph_fingerprint"]),
            left_id=str(data["left_id"]),
            right_id=str(data["right_id"]),
            kind=SemanticConflictKind(data["kind"]),
            order=(
                SemanticConflictOrder(data["order"])
                if data.get("order") is not None
                else None
            ),
            left_changes=tuple(str(item) for item in data.get("left_changes") or ()),
            right_changes=tuple(str(item) for item in data.get("right_changes") or ()),
            left_impact_fingerprint=str(data["left_impact_fingerprint"]),
            right_impact_fingerprint=str(data["right_impact_fingerprint"]),
            evidence=tuple(
                SemanticConflictEvidence.from_dict(item)
                for item in data.get("evidence") or ()
            ),
            metadata=dict(data.get("metadata") or {}),
        )
        expected = data.get("fingerprint")
        if expected is not None and str(expected) != decision.fingerprint:
            raise ValueError("semantic conflict fingerprint mismatch")
        for derived_name, derived_value in (
            ("parallel_safe", decision.parallel_safe),
            ("requires_ordering", decision.requires_ordering),
            ("fail_closed", decision.fail_closed),
        ):
            if derived_name in data and bool(data[derived_name]) != derived_value:
                raise ValueError(f"semantic conflict {derived_name} mismatch")
        return decision


def _normalize_changes(changes: Sequence[SemanticChange]) -> tuple[SemanticChange, ...]:
    normalized = tuple(
        item if isinstance(item, SemanticChange) else SemanticChange.from_dict(item)
        for item in changes
    )
    if not normalized:
        raise ValueError("semantic conflict classification requires non-empty changes")
    identities = [item.identity for item in normalized]
    if len(set(identities)) != len(identities):
        raise ValueError(
            "semantic conflict changes must have unique identities per side"
        )
    return tuple(sorted(normalized, key=lambda item: item.identity))


def _normalize_proofs(
    proofs: Iterable[CommutativityProof],
) -> dict[tuple[str, str], CommutativityProof]:
    result: dict[tuple[str, str], CommutativityProof] = {}
    for raw in proofs:
        proof = (
            raw
            if isinstance(raw, CommutativityProof)
            else CommutativityProof.from_dict(raw)
        )
        previous = result.get(proof.pair_key)
        if previous is not None and previous.to_dict() != proof.to_dict():
            raise ValueError(
                "conflicting commutativity proofs for " + " / ".join(proof.pair_key)
            )
        result[proof.pair_key] = proof
    return result


def _safe_impact(
    graph: SemanticDependencyGraph,
    changes: tuple[SemanticChange, ...],
    *,
    max_depth: int | None,
) -> SemanticImpactReport:
    incomplete = tuple(
        item.identity
        for item in changes
        if graph.node(item.identity) is None and item.resource is None
    )
    if incomplete:
        return SemanticImpactReport(
            graph_fingerprint=graph.fingerprint,
            changes=changes,
            impacted=(),
            boundaries=(),
            metadata={
                "direction": "reverse_dependency",
                "max_depth": max_depth,
                "incomplete_roots": list(incomplete),
            },
        )
    return analyze_semantic_impact(graph, changes, max_depth=max_depth)


def _impact_path(
    report: SemanticImpactReport,
    *,
    root_identity: str,
    target_identity: str,
) -> ImpactPath | None:
    impacted = report.impacted_resource(target_identity)
    if impacted is None:
        return None
    candidates = tuple(
        path for path in impacted.paths if path.root_identity == root_identity
    )
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda path: (
            path.distance,
            path.identities,
            tuple(relation.value for relation in path.relations),
        ),
    )


def _unresolved_targets(
    report: SemanticImpactReport,
    *,
    root_identity: str,
) -> set[str]:
    return {
        boundary.target_identity
        for boundary in report.boundaries
        if boundary.resolution is DependencyResolution.UNRESOLVED
        and root_identity in boundary.root_identities
    }


def _path_evidence(
    *,
    reason: SemanticConflictReason,
    left_identity: str,
    right_identity: str,
    order: SemanticConflictOrder | None,
    path: ImpactPath,
    detail: str,
) -> SemanticConflictEvidence:
    return SemanticConflictEvidence(
        reason=reason,
        left_identity=left_identity,
        right_identity=right_identity,
        order=order,
        path=path.identities,
        relations=path.relations,
        detail=detail,
    )


_ORDER_SENSITIVE_CHANGE_KINDS = frozenset(
    {
        SemanticChangeKind.CONTRACT,
        SemanticChangeKind.STATE,
        SemanticChangeKind.STRUCTURE,
        SemanticChangeKind.ADDED,
        SemanticChangeKind.REMOVED,
    }
)


def _dependency_requires_order(change: SemanticChange) -> bool:
    """Return whether a dependency path constrains mutation execution order.

    Dependency Graph v2 describes repository coupling, not temporal authority. An
    implementation-only edit keeps the existing callable/resource contract stable,
    so an existing caller/callee edge alone is not proof that the dependent writer
    must observe the implementation edit first. Contract, state, structural, added,
    and removed roots can invalidate a dependent mutation premise and therefore retain
    producer-before-consumer ordering. Unknown changes are handled fail-closed before
    this helper is called.
    """

    return change.kind in _ORDER_SENSITIVE_CHANGE_KINDS


def classify_semantic_conflict(
    graph: SemanticDependencyGraph,
    left_changes: Sequence[SemanticChange],
    right_changes: Sequence[SemanticChange],
    *,
    left_id: str = "left",
    right_id: str = "right",
    commutativity_proofs: Iterable[CommutativityProof] = (),
    max_depth: int | None = None,
    mutation_sensitive_ordering: bool = False,
) -> SemanticConflictDecision:
    """Classify two mutation sets using graph and impact evidence.

    ``independent`` means the current deterministic graph proves no ordering-sensitive
    coupling between the mutation roots. ``commutative`` is emitted only when an
    explicit deterministic proof covers a coupling that would otherwise constrain
    execution. ``ordered`` records one producer-before-consumer direction.
    ``conflicting`` means both directions are required or the same semantic resource is
    mutated without a commutativity proof. ``unknown`` preserves incomplete semantic
    evidence.

    By default every dependency path remains ordering-sensitive for compatibility with
    runtime premise and amendment checks. Concurrency/integration callers may enable
    ``mutation_sensitive_ordering`` so an implementation-only producer change does not
    create an execution order solely from an existing dependency edge. Contract, state,
    structural, added, and removed changes remain order-sensitive in that mode.
    """

    if max_depth is not None and max_depth < 0:
        raise ValueError("max_depth must be non-negative or None")
    left = _normalize_changes(left_changes)
    right = _normalize_changes(right_changes)
    proofs = _normalize_proofs(commutativity_proofs)
    left_impact = _safe_impact(graph, left, max_depth=max_depth)
    right_impact = _safe_impact(graph, right, max_depth=max_depth)

    evidence: list[SemanticConflictEvidence] = []
    pair_kinds: list[SemanticConflictKind] = []
    orders: set[SemanticConflictOrder] = set()

    for left_change in left:
        for right_change in right:
            pair = _pair_key(left_change.identity, right_change.identity)
            proof = proofs.get(pair)

            if left_change.identity == right_change.identity:
                if proof is not None:
                    pair_kinds.append(SemanticConflictKind.COMMUTATIVE)
                    evidence.append(
                        SemanticConflictEvidence(
                            reason=SemanticConflictReason.EXPLICIT_COMMUTATIVITY,
                            left_identity=left_change.identity,
                            right_identity=right_change.identity,
                            detail=(
                                "same semantic resource is covered by explicit "
                                "deterministic commutativity evidence"
                            ),
                            metadata={"basis": proof.basis, **dict(proof.metadata)},
                        )
                    )
                else:
                    pair_kinds.append(SemanticConflictKind.CONFLICTING)
                    evidence.append(
                        SemanticConflictEvidence(
                            reason=SemanticConflictReason.DIRECT_RESOURCE_OVERLAP,
                            left_identity=left_change.identity,
                            right_identity=right_change.identity,
                            detail="both mutation sets change the same semantic resource",
                        )
                    )
                continue

            missing = tuple(
                identity
                for identity in (left_change.identity, right_change.identity)
                if graph.node(identity) is None
            )
            if missing:
                pair_kinds.append(SemanticConflictKind.UNKNOWN)
                evidence.append(
                    SemanticConflictEvidence(
                        reason=SemanticConflictReason.MISSING_GRAPH_ROOT,
                        left_identity=left_change.identity,
                        right_identity=right_change.identity,
                        detail="one or more mutation roots are absent from the graph snapshot",
                        metadata={"missing": list(missing)},
                    )
                )
                continue

            if (
                left_change.kind is SemanticChangeKind.UNKNOWN
                or right_change.kind is SemanticChangeKind.UNKNOWN
            ):
                pair_kinds.append(SemanticConflictKind.UNKNOWN)
                evidence.append(
                    SemanticConflictEvidence(
                        reason=SemanticConflictReason.UNKNOWN_CHANGE,
                        left_identity=left_change.identity,
                        right_identity=right_change.identity,
                        detail="change semantics are explicitly unknown",
                    )
                )
                continue

            left_to_right = _impact_path(
                left_impact,
                root_identity=left_change.identity,
                target_identity=right_change.identity,
            )
            right_to_left = _impact_path(
                right_impact,
                root_identity=right_change.identity,
                target_identity=left_change.identity,
            )

            if left_to_right is not None or right_to_left is not None:
                if proof is not None:
                    pair_kinds.append(SemanticConflictKind.COMMUTATIVE)
                    evidence.append(
                        SemanticConflictEvidence(
                            reason=SemanticConflictReason.EXPLICIT_COMMUTATIVITY,
                            left_identity=left_change.identity,
                            right_identity=right_change.identity,
                            detail=(
                                "semantic coupling is covered by explicit deterministic "
                                "commutativity evidence"
                            ),
                            metadata={"basis": proof.basis, **dict(proof.metadata)},
                        )
                    )
                    continue

                left_requires_order = left_to_right is not None and (
                    not mutation_sensitive_ordering
                    or _dependency_requires_order(left_change)
                )
                right_requires_order = right_to_left is not None and (
                    not mutation_sensitive_ordering
                    or _dependency_requires_order(right_change)
                )

                if left_requires_order and right_requires_order:
                    assert left_to_right is not None
                    assert right_to_left is not None
                    pair_kinds.append(SemanticConflictKind.CONFLICTING)
                    evidence.append(
                        _path_evidence(
                            reason=SemanticConflictReason.MUTUAL_DEPENDENCY,
                            left_identity=left_change.identity,
                            right_identity=right_change.identity,
                            order=None,
                            path=left_to_right,
                            detail=(
                                "left mutation changes an order-sensitive semantic premise "
                                "of the right mutation while the reverse dependency is also "
                                "order-sensitive"
                            ),
                        )
                    )
                    evidence.append(
                        _path_evidence(
                            reason=SemanticConflictReason.MUTUAL_DEPENDENCY,
                            left_identity=left_change.identity,
                            right_identity=right_change.identity,
                            order=None,
                            path=right_to_left,
                            detail=(
                                "reverse order-sensitive semantic impact closes a mutation "
                                "dependency cycle"
                            ),
                        )
                    )
                    continue

                if left_requires_order:
                    assert left_to_right is not None
                    pair_order = SemanticConflictOrder.LEFT_BEFORE_RIGHT
                    pair_kinds.append(SemanticConflictKind.ORDERED)
                    orders.add(pair_order)
                    evidence.append(
                        _path_evidence(
                            reason=SemanticConflictReason.SEMANTIC_DEPENDENCY,
                            left_identity=left_change.identity,
                            right_identity=right_change.identity,
                            order=pair_order,
                            path=left_to_right,
                            detail=(
                                "the left mutation changes an order-sensitive semantic "
                                "premise consumed by the right mutation root"
                            ),
                        )
                    )
                    continue

                if right_requires_order:
                    assert right_to_left is not None
                    pair_order = SemanticConflictOrder.RIGHT_BEFORE_LEFT
                    pair_kinds.append(SemanticConflictKind.ORDERED)
                    orders.add(pair_order)
                    evidence.append(
                        _path_evidence(
                            reason=SemanticConflictReason.SEMANTIC_DEPENDENCY,
                            left_identity=left_change.identity,
                            right_identity=right_change.identity,
                            order=pair_order,
                            path=right_to_left,
                            detail=(
                                "the right mutation changes an order-sensitive semantic "
                                "premise consumed by the left mutation root"
                            ),
                        )
                    )
                    continue

                pair_kinds.append(SemanticConflictKind.INDEPENDENT)
                for path in (left_to_right, right_to_left):
                    if path is None:
                        continue
                    evidence.append(
                        _path_evidence(
                            reason=SemanticConflictReason.STABLE_CONTRACT_DEPENDENCY,
                            left_identity=left_change.identity,
                            right_identity=right_change.identity,
                            order=None,
                            path=path,
                            detail=(
                                "an existing semantic dependency connects the mutation "
                                "roots, but the producer-side change is implementation-only "
                                "and preserves the dependency contract"
                            ),
                        )
                    )
                continue

            left_unresolved = _unresolved_targets(
                left_impact, root_identity=left_change.identity
            )
            right_unresolved = _unresolved_targets(
                right_impact, root_identity=right_change.identity
            )
            ambiguous_targets = sorted(
                (left_unresolved & right_unresolved)
                | ({right_change.identity} & left_unresolved)
                | ({left_change.identity} & right_unresolved)
            )
            if ambiguous_targets:
                pair_kinds.append(SemanticConflictKind.UNKNOWN)
                for target in ambiguous_targets:
                    evidence.append(
                        SemanticConflictEvidence(
                            reason=SemanticConflictReason.UNRESOLVED_DEPENDENCY,
                            left_identity=left_change.identity,
                            right_identity=right_change.identity,
                            boundary_target=target,
                            detail=(
                                "unresolved dependency evidence prevents proof of semantic "
                                "independence"
                            ),
                        )
                    )
                continue

            if max_depth is not None:
                pair_kinds.append(SemanticConflictKind.UNKNOWN)
                evidence.append(
                    SemanticConflictEvidence(
                        reason=SemanticConflictReason.BOUNDED_IMPACT,
                        left_identity=left_change.identity,
                        right_identity=right_change.identity,
                        detail=(
                            "bounded impact traversal cannot prove semantic independence "
                            "when no coupling was found within the configured depth"
                        ),
                        metadata={"max_depth": max_depth},
                    )
                )
                continue

            pair_kinds.append(SemanticConflictKind.INDEPENDENT)

    decision_order: SemanticConflictOrder | None
    if SemanticConflictKind.CONFLICTING in pair_kinds:
        kind = SemanticConflictKind.CONFLICTING
        decision_order = None
    elif len(orders) > 1:
        kind = SemanticConflictKind.CONFLICTING
        decision_order = None
        evidence.append(
            SemanticConflictEvidence(
                reason=SemanticConflictReason.MUTUAL_DEPENDENCY,
                detail=(
                    "different semantic change pairs require opposite execution orders"
                ),
                metadata={"orders": sorted(item.value for item in orders)},
            )
        )
    elif SemanticConflictKind.UNKNOWN in pair_kinds:
        kind = SemanticConflictKind.UNKNOWN
        decision_order = None
    elif SemanticConflictKind.ORDERED in pair_kinds:
        kind = SemanticConflictKind.ORDERED
        decision_order = next(iter(orders))
    elif SemanticConflictKind.COMMUTATIVE in pair_kinds:
        kind = SemanticConflictKind.COMMUTATIVE
        decision_order = None
    else:
        kind = SemanticConflictKind.INDEPENDENT
        decision_order = None
        if not any(
            item.reason is SemanticConflictReason.STABLE_CONTRACT_DEPENDENCY
            for item in evidence
        ):
            evidence.append(
                SemanticConflictEvidence(
                    reason=SemanticConflictReason.DISJOINT_SEMANTIC_SURFACE,
                    detail=(
                        "mutation roots have no direct overlap, ordering-sensitive "
                        "dependency path, or shared unresolved boundary in the current "
                        "graph snapshot"
                    ),
                    metadata={
                        "left_roots": [item.identity for item in left],
                        "right_roots": [item.identity for item in right],
                    },
                )
            )

    return SemanticConflictDecision(
        graph_fingerprint=graph.fingerprint,
        left_id=left_id,
        right_id=right_id,
        kind=kind,
        order=decision_order,
        left_changes=tuple(item.identity for item in left),
        right_changes=tuple(item.identity for item in right),
        left_impact_fingerprint=left_impact.fingerprint,
        right_impact_fingerprint=right_impact.fingerprint,
        evidence=tuple(evidence),
        metadata={
            "max_depth": max_depth,
            "mutation_sensitive_ordering": mutation_sensitive_ordering,
            "commutativity_proof_count": len(proofs),
            "classification_order": [
                SemanticConflictKind.CONFLICTING.value,
                SemanticConflictKind.UNKNOWN.value,
                SemanticConflictKind.ORDERED.value,
                SemanticConflictKind.COMMUTATIVE.value,
                SemanticConflictKind.INDEPENDENT.value,
            ],
        },
    )
