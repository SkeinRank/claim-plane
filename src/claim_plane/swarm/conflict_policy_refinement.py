"""Evidence-backed refinement of conservative swarm conflict policy.

Conflict Policy Refinement consumes the exact symbol projection (9B) and the
closed dependency envelope (9C).  It may remove a conservative *serialization*
constraint only when both work items have closed, exact mutation surfaces and
the pinned semantic graph classifies those mutation roots as independent or
commutative.  Unknown, stale, destructive, patterned, schema/contract policy,
and explicit deny/serialize cases remain fail-closed.

The layer never expands worker mutation authority.  It changes only pairwise
admission constraints and records a source-bound explanation for every attempted
refinement.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping

from claim_plane.core import (
    CommutativityProof,
    ResourceKind,
    SemanticConflictDecision,
    SemanticConflictKind,
    SemanticConflictOrder,
    SemanticDependencyGraph,
    classify_semantic_conflict,
)
from claim_plane.swarm.budget import SameFilePolicy
from claim_plane.swarm.dependency_authority_narrowing import (
    DependencyAwareAuthorityNarrowingReport,
    DependencyNarrowingState,
    WorkItemDependencyAuthorityNarrowing,
)
from claim_plane.swarm.models import WorkGraph, WorkItem
from claim_plane.swarm.same_file_admission import semantic_changes_for_item

CONFLICT_POLICY_REFINEMENT_PROTOCOL = "claim-plane.conflict-policy-refinement.v1"


class ConflictPolicyClass(str, Enum):
    MUST_CONFLICT = "must_conflict"
    ORDERED = "ordered"
    COMMUTATIVE = "commutative"
    PROVABLY_INDEPENDENT = "provably_independent"
    CONSERVATIVE_UNKNOWN = "conservative_unknown"
    NOT_APPLICABLE = "not_applicable"


class ConflictPolicyEffect(str, Enum):
    PRESERVE = "preserve"
    RELEASE_SERIALIZATION = "release_serialization"
    REPLACE_WITH_SEMANTIC_ORDER = "replace_with_semantic_order"
    REPLACE_WITH_SEMANTIC_CONFLICT = "replace_with_semantic_conflict"


class ConflictPolicyRefinementReason(str, Enum):
    NO_BASE_CONSTRAINT = "no_base_constraint"
    EXPLICIT_DENY = "explicit_deny"
    EXPLICIT_SAME_FILE_POLICY = "explicit_same_file_policy"
    NON_REFINABLE_POLICY_CONSTRAINT = "non_refinable_policy_constraint"
    NARROWING_NOT_CLOSED = "narrowing_not_closed"
    NON_EXACT_MUTATION_SURFACE = "non_exact_mutation_surface"
    NO_SEMANTIC_GRAPH = "no_semantic_graph"
    NO_SEMANTIC_MUTATION_ROOT = "no_semantic_mutation_root"
    SEMANTIC_INDEPENDENT = "semantic_independent"
    SEMANTIC_COMMUTATIVE = "semantic_commutative"
    SEMANTIC_ORDERED = "semantic_ordered"
    SEMANTIC_CONFLICT = "semantic_conflict"
    SEMANTIC_UNKNOWN = "semantic_unknown"


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _decision_summary(
    decision: SemanticConflictDecision | None,
) -> Mapping[str, Any] | None:
    if decision is None:
        return None
    return {
        "fingerprint": decision.fingerprint,
        "kind": decision.kind.value,
        "order": decision.order.value if decision.order is not None else None,
        "left_changes": list(decision.left_changes),
        "right_changes": list(decision.right_changes),
        "evidence_reasons": sorted({item.reason.value for item in decision.evidence}),
        "evidence_count": len(decision.evidence),
    }


def _exact_mutation_surface(item: WorkItemDependencyAuthorityNarrowing) -> bool:
    """Return true only when every committed mutation is semantic and exact.

    A CLOSED 9C envelope can legitimately preserve a broad carrier when 9B could
    not prove that carrier maps to one symbol.  9D must not use such an envelope
    to release a file-level conflict.
    """

    exact_kinds = {ResourceKind.SYMBOL, ResourceKind.CONTRACT}
    mutations = [
        operation
        for operation in item.analysis_operations
        if operation.committed and operation.mutating
    ]
    return bool(mutations) and all(
        operation.resource.kind in exact_kinds and not operation.resource.is_pattern
        for operation in mutations
    )


@dataclass(frozen=True, slots=True)
class ConflictPolicyPairRefinement:
    left_id: str
    right_id: str
    classification: ConflictPolicyClass
    effect: ConflictPolicyEffect
    reasons: tuple[ConflictPolicyRefinementReason, ...]
    base_action: str | None
    base_reasons: tuple[str, ...]
    before_id: str | None = None
    after_id: str | None = None
    semantic_decision: Mapping[str, Any] | None = None
    left_narrowing_fingerprint: str | None = None
    right_narrowing_fingerprint: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        left = self.left_id.strip()
        right = self.right_id.strip()
        if not left or not right or left == right:
            raise ValueError("conflict policy refinement requires distinct work ids")
        object.__setattr__(self, "left_id", left)
        object.__setattr__(self, "right_id", right)
        object.__setattr__(
            self, "classification", ConflictPolicyClass(self.classification)
        )
        object.__setattr__(self, "effect", ConflictPolicyEffect(self.effect))
        reasons = tuple(
            sorted(
                {ConflictPolicyRefinementReason(item) for item in self.reasons},
                key=lambda item: item.value,
            )
        )
        if not reasons:
            raise ValueError("conflict policy refinement requires at least one reason")
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "base_reasons", tuple(sorted(set(self.base_reasons))))
        if (self.before_id is None) != (self.after_id is None):
            raise ValueError("semantic ordering requires both before_id and after_id")
        if self.before_id is not None and self.before_id == self.after_id:
            raise ValueError("semantic ordering must reference distinct work ids")
        if self.effect is ConflictPolicyEffect.REPLACE_WITH_SEMANTIC_ORDER:
            if self.classification is not ConflictPolicyClass.ORDERED:
                raise ValueError(
                    "semantic-order effect requires ordered classification"
                )
            if self.before_id is None:
                raise ValueError("ordered refinement requires before_id and after_id")
        if (
            self.effect is ConflictPolicyEffect.RELEASE_SERIALIZATION
            and self.classification
            not in {
            ConflictPolicyClass.PROVABLY_INDEPENDENT,
            ConflictPolicyClass.COMMUTATIVE,
            }
        ):
            raise ValueError(
                "serialization release requires independent/commutative proof"
            )
        object.__setattr__(
            self,
            "semantic_decision",
            (
                dict(self.semantic_decision)
                if self.semantic_decision is not None
                else None
            ),
        )

    @property
    def changed(self) -> bool:
        return self.effect is not ConflictPolicyEffect.PRESERVE

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload = {
            "left_id": self.left_id,
            "right_id": self.right_id,
            "classification": self.classification.value,
            "effect": self.effect.value,
            "changed": self.changed,
            "reasons": [item.value for item in self.reasons],
            "base_action": self.base_action,
            "base_reasons": list(self.base_reasons),
            "before_id": self.before_id,
            "after_id": self.after_id,
            "semantic_decision": self.semantic_decision,
            "left_narrowing_fingerprint": self.left_narrowing_fingerprint,
            "right_narrowing_fingerprint": self.right_narrowing_fingerprint,
            "detail": self.detail,
        }
        if include_fingerprint:
            return {"fingerprint": self.fingerprint, **payload}
        return payload

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_dict(include_fingerprint=False))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ConflictPolicyPairRefinement":
        item = cls(
            left_id=str(data["left_id"]),
            right_id=str(data["right_id"]),
            classification=ConflictPolicyClass(data["classification"]),
            effect=ConflictPolicyEffect(data["effect"]),
            reasons=tuple(
                ConflictPolicyRefinementReason(value)
                for value in data.get("reasons") or ()
            ),
            base_action=(
                str(data["base_action"])
                if data.get("base_action") is not None
                else None
            ),
            base_reasons=tuple(str(value) for value in data.get("base_reasons") or ()),
            before_id=(
                str(data["before_id"])
                if data.get("before_id") is not None
                else None
            ),
            after_id=(
                str(data["after_id"])
                if data.get("after_id") is not None
                else None
            ),
            semantic_decision=(
                dict(data["semantic_decision"])
                if isinstance(data.get("semantic_decision"), Mapping)
                else None
            ),
            left_narrowing_fingerprint=(
                str(data["left_narrowing_fingerprint"])
                if data.get("left_narrowing_fingerprint") is not None
                else None
            ),
            right_narrowing_fingerprint=(
                str(data["right_narrowing_fingerprint"])
                if data.get("right_narrowing_fingerprint") is not None
                else None
            ),
            detail=str(data.get("detail") or ""),
        )
        supplied = data.get("fingerprint")
        if supplied is not None and str(supplied) != item.fingerprint:
            raise ValueError("conflict policy pair refinement fingerprint mismatch")
        if "changed" in data and bool(data["changed"]) != item.changed:
            raise ValueError("conflict policy pair refinement changed flag mismatch")
        return item


@dataclass(frozen=True, slots=True)
class ConflictPolicyRefinementReport:
    work_graph_fingerprint: str
    budget_fingerprint: str
    dependency_narrowing_fingerprint: str
    pairs: tuple[ConflictPolicyPairRefinement, ...]
    semantic_graph_fingerprint: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = CONFLICT_POLICY_REFINEMENT_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != CONFLICT_POLICY_REFINEMENT_PROTOCOL:
            raise ValueError(
                f"unsupported conflict policy refinement {self.protocol!r}"
            )
        for name in (
            "work_graph_fingerprint",
            "budget_fingerprint",
            "dependency_narrowing_fingerprint",
            "semantic_graph_fingerprint",
        ):
            value = getattr(self, name)
            if value is None and name == "semantic_graph_fingerprint":
                continue
            text = str(value).lower()
            if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
            object.__setattr__(self, name, text)
        pairs = tuple(
            (
                item
                if isinstance(item, ConflictPolicyPairRefinement)
                else ConflictPolicyPairRefinement.from_dict(item)
            )
            for item in self.pairs
        )
        keys = [(item.left_id, item.right_id) for item in pairs]
        if len(set(keys)) != len(keys):
            raise ValueError("conflict policy refinement pair ids must be unique")
        object.__setattr__(
            self,
            "pairs",
            tuple(sorted(pairs, key=lambda item: (item.left_id, item.right_id))),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def pair_map(self) -> dict[frozenset[str], ConflictPolicyPairRefinement]:
        return {frozenset((item.left_id, item.right_id)): item for item in self.pairs}

    def _summary_core(self) -> dict[str, Any]:
        return {
            "pair_count": len(self.pairs),
            "changed_pairs": sum(item.changed for item in self.pairs),
            "preserved_pairs": sum(not item.changed for item in self.pairs),
            "released_serializations": sum(
                item.effect is ConflictPolicyEffect.RELEASE_SERIALIZATION
                for item in self.pairs
            ),
            "semantic_orders": sum(
                item.classification is ConflictPolicyClass.ORDERED
                for item in self.pairs
            ),
            "must_conflict": sum(
                item.classification is ConflictPolicyClass.MUST_CONFLICT
                for item in self.pairs
            ),
            "commutative": sum(
                item.classification is ConflictPolicyClass.COMMUTATIVE
                for item in self.pairs
            ),
            "provably_independent": sum(
                item.classification is ConflictPolicyClass.PROVABLY_INDEPENDENT
                for item in self.pairs
            ),
            "conservative_unknown": sum(
                item.classification is ConflictPolicyClass.CONSERVATIVE_UNKNOWN
                for item in self.pairs
            ),
            "not_applicable": sum(
                item.classification is ConflictPolicyClass.NOT_APPLICABLE
                for item in self.pairs
            ),
        }

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_dict(include_fingerprint=False))

    def summary(self) -> dict[str, Any]:
        return {**self._summary_core(), "fingerprint": self.fingerprint}

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload = {
            "protocol": self.protocol,
            "work_graph_fingerprint": self.work_graph_fingerprint,
            "budget_fingerprint": self.budget_fingerprint,
            "dependency_narrowing_fingerprint": self.dependency_narrowing_fingerprint,
            "semantic_graph_fingerprint": self.semantic_graph_fingerprint,
            "pairs": [item.to_dict() for item in self.pairs],
            "summary": self.summary() if include_fingerprint else self._summary_core(),
            "metadata": dict(self.metadata),
        }
        if include_fingerprint:
            return {"fingerprint": self.fingerprint, **payload}
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ConflictPolicyRefinementReport":
        report = cls(
            protocol=str(data.get("protocol") or CONFLICT_POLICY_REFINEMENT_PROTOCOL),
            work_graph_fingerprint=str(data["work_graph_fingerprint"]),
            budget_fingerprint=str(data["budget_fingerprint"]),
            dependency_narrowing_fingerprint=str(
                data["dependency_narrowing_fingerprint"]
            ),
            semantic_graph_fingerprint=(
                str(data["semantic_graph_fingerprint"])
                if data.get("semantic_graph_fingerprint") is not None
                else None
            ),
            pairs=tuple(
                ConflictPolicyPairRefinement.from_dict(item)
                for item in data.get("pairs") or ()
            ),
            metadata=dict(data.get("metadata") or {}),
        )
        supplied = data.get("fingerprint")
        if supplied is not None and str(supplied) != report.fingerprint:
            raise ValueError("conflict policy refinement fingerprint mismatch")
        summary = data.get("summary")
        if isinstance(summary, Mapping):
            expected = report.summary()
            for key, value in expected.items():
                if key in summary and summary[key] != value:
                    raise ValueError("conflict policy refinement summary mismatch")
        return report


def _preserved(
    left_id: str,
    right_id: str,
    *,
    classification: ConflictPolicyClass,
    reason: ConflictPolicyRefinementReason,
    base_action: str | None,
    base_reasons: tuple[str, ...],
    left_narrowing: WorkItemDependencyAuthorityNarrowing | None,
    right_narrowing: WorkItemDependencyAuthorityNarrowing | None,
    detail: str,
) -> ConflictPolicyPairRefinement:
    return ConflictPolicyPairRefinement(
        left_id=left_id,
        right_id=right_id,
        classification=classification,
        effect=ConflictPolicyEffect.PRESERVE,
        reasons=(reason,),
        base_action=base_action,
        base_reasons=base_reasons,
        left_narrowing_fingerprint=(
            _sha256(left_narrowing.to_dict()) if left_narrowing is not None else None
        ),
        right_narrowing_fingerprint=(
            _sha256(right_narrowing.to_dict()) if right_narrowing is not None else None
        ),
        detail=detail,
    )


def evaluate_conflict_policy_refinement(
    left: WorkItem,
    right: WorkItem,
    *,
    dependency_narrowing: DependencyAwareAuthorityNarrowingReport,
    semantic_graph: SemanticDependencyGraph | None,
    same_file_policy: SameFilePolicy,
    base_action: str | None,
    base_reasons: Iterable[str],
    commutativity_proofs: Iterable[CommutativityProof] = (),
) -> tuple[ConflictPolicyPairRefinement, SemanticConflictDecision | None]:
    """Refine one existing conservative pair constraint, fail-closed by default."""

    reasons = tuple(sorted({str(item) for item in base_reasons}))
    left_narrowing = dependency_narrowing.item_map.get(left.work_id)
    right_narrowing = dependency_narrowing.item_map.get(right.work_id)

    if base_action is None:
        return (
            _preserved(
                left.work_id,
                right.work_id,
                classification=ConflictPolicyClass.NOT_APPLICABLE,
                reason=ConflictPolicyRefinementReason.NO_BASE_CONSTRAINT,
                base_action=None,
                base_reasons=reasons,
                left_narrowing=left_narrowing,
                right_narrowing=right_narrowing,
                detail="no conservative pair constraint needs refinement",
            ),
            None,
        )
    if base_action == "deny":
        return (
            _preserved(
                left.work_id,
                right.work_id,
                classification=ConflictPolicyClass.MUST_CONFLICT,
                reason=ConflictPolicyRefinementReason.EXPLICIT_DENY,
                base_action=base_action,
                base_reasons=reasons,
                left_narrowing=left_narrowing,
                right_narrowing=right_narrowing,
                detail=(
                    "deny constraints are authoritative and cannot be released "
                    "by narrowing"
                ),
            ),
            None,
        )
    if "schema_change" in reasons or "shared_contract" in reasons:
        return (
            _preserved(
                left.work_id,
                right.work_id,
                classification=ConflictPolicyClass.MUST_CONFLICT,
                reason=ConflictPolicyRefinementReason.NON_REFINABLE_POLICY_CONSTRAINT,
                base_action=base_action,
                base_reasons=reasons,
                left_narrowing=left_narrowing,
                right_narrowing=right_narrowing,
                detail="schema/shared-contract policy remains authoritative",
            ),
            None,
        )
    if "semantic_conflict" in reasons:
        return (
            _preserved(
                left.work_id,
                right.work_id,
                classification=ConflictPolicyClass.MUST_CONFLICT,
                reason=ConflictPolicyRefinementReason.SEMANTIC_CONFLICT,
                base_action=base_action,
                base_reasons=reasons,
                left_narrowing=left_narrowing,
                right_narrowing=right_narrowing,
                detail="existing semantic conflict remains authoritative",
            ),
            None,
        )
    if "same_file" in reasons and same_file_policy is not SameFilePolicy.REGION_SAFE:
        return (
            _preserved(
                left.work_id,
                right.work_id,
                classification=ConflictPolicyClass.MUST_CONFLICT,
                reason=ConflictPolicyRefinementReason.EXPLICIT_SAME_FILE_POLICY,
                base_action=base_action,
                base_reasons=reasons,
                left_narrowing=left_narrowing,
                right_narrowing=right_narrowing,
                detail="explicit same-file serialize/deny policy cannot be overridden",
            ),
            None,
        )
    refinable_reasons = {"same_file", "unknown_overlap", "semantic_order"}
    if not set(reasons).issubset(refinable_reasons):
        return (
            _preserved(
                left.work_id,
                right.work_id,
                classification=ConflictPolicyClass.CONSERVATIVE_UNKNOWN,
                reason=ConflictPolicyRefinementReason.NON_REFINABLE_POLICY_CONSTRAINT,
                base_action=base_action,
                base_reasons=reasons,
                left_narrowing=left_narrowing,
                right_narrowing=right_narrowing,
                detail=(
                    "constraint includes a policy reason that 9D is not allowed "
                    "to relax"
                ),
            ),
            None,
        )
    if semantic_graph is None:
        return (
            _preserved(
                left.work_id,
                right.work_id,
                classification=ConflictPolicyClass.CONSERVATIVE_UNKNOWN,
                reason=ConflictPolicyRefinementReason.NO_SEMANTIC_GRAPH,
                base_action=base_action,
                base_reasons=reasons,
                left_narrowing=left_narrowing,
                right_narrowing=right_narrowing,
                detail="semantic graph is required to refine conservative overlap",
            ),
            None,
        )
    if (
        left_narrowing is None
        or right_narrowing is None
        or left_narrowing.state is not DependencyNarrowingState.CLOSED
        or right_narrowing.state is not DependencyNarrowingState.CLOSED
    ):
        return (
            _preserved(
                left.work_id,
                right.work_id,
                classification=ConflictPolicyClass.CONSERVATIVE_UNKNOWN,
                reason=ConflictPolicyRefinementReason.NARROWING_NOT_CLOSED,
                base_action=base_action,
                base_reasons=reasons,
                left_narrowing=left_narrowing,
                right_narrowing=right_narrowing,
                detail="both work items require a closed dependency authority envelope",
            ),
            None,
        )
    if (
        not _exact_mutation_surface(left_narrowing)
        or not _exact_mutation_surface(right_narrowing)
    ):
        return (
            _preserved(
                left.work_id,
                right.work_id,
                classification=ConflictPolicyClass.CONSERVATIVE_UNKNOWN,
                reason=ConflictPolicyRefinementReason.NON_EXACT_MUTATION_SURFACE,
                base_action=base_action,
                base_reasons=reasons,
                left_narrowing=left_narrowing,
                right_narrowing=right_narrowing,
                detail=(
                    "broad or non-semantic mutation authority remains after narrowing"
                ),
            ),
            None,
        )

    narrowed_left = replace(left, operations=left_narrowing.analysis_operations)
    narrowed_right = replace(right, operations=right_narrowing.analysis_operations)
    left_changes = semantic_changes_for_item(narrowed_left, semantic_graph)
    right_changes = semantic_changes_for_item(narrowed_right, semantic_graph)
    if not left_changes or not right_changes:
        return (
            _preserved(
                left.work_id,
                right.work_id,
                classification=ConflictPolicyClass.CONSERVATIVE_UNKNOWN,
                reason=ConflictPolicyRefinementReason.NO_SEMANTIC_MUTATION_ROOT,
                base_action=base_action,
                base_reasons=reasons,
                left_narrowing=left_narrowing,
                right_narrowing=right_narrowing,
                detail=(
                    "exact narrowed authority did not resolve to semantic "
                    "mutation roots"
                ),
            ),
            None,
        )

    decision = classify_semantic_conflict(
        semantic_graph,
        left_changes,
        right_changes,
        left_id=left.work_id,
        right_id=right.work_id,
        commutativity_proofs=tuple(commutativity_proofs),
        mutation_sensitive_ordering=True,
    )
    common = {
        "base_action": base_action,
        "base_reasons": reasons,
        "left_narrowing_fingerprint": _sha256(left_narrowing.to_dict()),
        "right_narrowing_fingerprint": _sha256(right_narrowing.to_dict()),
        "semantic_decision": _decision_summary(decision),
    }
    if decision.kind is SemanticConflictKind.INDEPENDENT:
        return (
            ConflictPolicyPairRefinement(
                left_id=left.work_id,
                right_id=right.work_id,
                classification=ConflictPolicyClass.PROVABLY_INDEPENDENT,
                effect=ConflictPolicyEffect.RELEASE_SERIALIZATION,
                reasons=(ConflictPolicyRefinementReason.SEMANTIC_INDEPENDENT,),
                detail="closed exact authority has no blocking semantic coupling",
                **common,
            ),
            decision,
        )
    if decision.kind is SemanticConflictKind.COMMUTATIVE:
        return (
            ConflictPolicyPairRefinement(
                left_id=left.work_id,
                right_id=right.work_id,
                classification=ConflictPolicyClass.COMMUTATIVE,
                effect=ConflictPolicyEffect.RELEASE_SERIALIZATION,
                reasons=(ConflictPolicyRefinementReason.SEMANTIC_COMMUTATIVE,),
                detail="closed exact authority has an explicit commutativity proof",
                **common,
            ),
            decision,
        )
    if decision.kind is SemanticConflictKind.ORDERED:
        before_id, after_id = (
            (left.work_id, right.work_id)
            if decision.order is SemanticConflictOrder.LEFT_BEFORE_RIGHT
            else (right.work_id, left.work_id)
        )
        return (
            ConflictPolicyPairRefinement(
                left_id=left.work_id,
                right_id=right.work_id,
                classification=ConflictPolicyClass.ORDERED,
                effect=ConflictPolicyEffect.REPLACE_WITH_SEMANTIC_ORDER,
                reasons=(ConflictPolicyRefinementReason.SEMANTIC_ORDERED,),
                before_id=before_id,
                after_id=after_id,
                detail=(
                    "closed exact authority requires one deterministic semantic order"
                ),
                **common,
            ),
            decision,
        )
    if decision.kind is SemanticConflictKind.CONFLICTING:
        return (
            ConflictPolicyPairRefinement(
                left_id=left.work_id,
                right_id=right.work_id,
                classification=ConflictPolicyClass.MUST_CONFLICT,
                effect=ConflictPolicyEffect.REPLACE_WITH_SEMANTIC_CONFLICT,
                reasons=(ConflictPolicyRefinementReason.SEMANTIC_CONFLICT,),
                detail="closed exact authority proves a semantic mutation conflict",
                **common,
            ),
            decision,
        )
    return (
        ConflictPolicyPairRefinement(
            left_id=left.work_id,
            right_id=right.work_id,
            classification=ConflictPolicyClass.CONSERVATIVE_UNKNOWN,
            effect=ConflictPolicyEffect.PRESERVE,
            reasons=(ConflictPolicyRefinementReason.SEMANTIC_UNKNOWN,),
            detail=(
                "semantic evidence remains incomplete; conservative serialization "
                "is preserved"
            ),
            **common,
        ),
        decision,
    )


def build_conflict_policy_refinement_report(
    graph: WorkGraph,
    *,
    budget_fingerprint: str,
    dependency_narrowing: DependencyAwareAuthorityNarrowingReport,
    semantic_graph: SemanticDependencyGraph | None,
    pairs: Iterable[ConflictPolicyPairRefinement],
) -> ConflictPolicyRefinementReport:
    if dependency_narrowing.work_graph_fingerprint != graph.fingerprint():
        raise ValueError(
            "dependency authority narrowing is stale for conflict refinement"
        )
    if (
        semantic_graph is not None
        and dependency_narrowing.semantic_graph_fingerprint
        != semantic_graph.fingerprint
    ):
        raise ValueError("dependency authority narrowing is stale for semantic graph")
    return ConflictPolicyRefinementReport(
        work_graph_fingerprint=graph.fingerprint(),
        budget_fingerprint=budget_fingerprint,
        dependency_narrowing_fingerprint=dependency_narrowing.fingerprint,
        semantic_graph_fingerprint=(
            semantic_graph.fingerprint if semantic_graph is not None else None
        ),
        pairs=tuple(pairs),
        metadata={
            "scope": "pairwise-admission-policy",
            "worker_mutation_authority_preserved": True,
            "unknown_policy": "fail_closed",
            "explicit_deny_preserved": True,
            "explicit_same_file_policy_preserved": True,
            "release_requires": (
                "closed_exact_authority_plus_semantic_independent_or_commutative"
            ),
        },
    )


__all__ = [
    "CONFLICT_POLICY_REFINEMENT_PROTOCOL",
    "ConflictPolicyClass",
    "ConflictPolicyEffect",
    "ConflictPolicyRefinementReason",
    "ConflictPolicyPairRefinement",
    "ConflictPolicyRefinementReport",
    "evaluate_conflict_policy_refinement",
    "build_conflict_policy_refinement_report",
]
