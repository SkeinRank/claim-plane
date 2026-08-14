"""Deterministic attribution for swarm admission and concurrency decisions.

This layer is intentionally observational. It does not change scheduling,
conflict classification, or mutation authority. Instead it records why every
work-item pair is parallel-eligible, dependency-ordered, serialized, or denied,
together with the declared authority surfaces and source-bound evidence that led
to that result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from itertools import combinations
from typing import Any, Iterable, Mapping

from claim_plane.core import (
    AffectedSubgraphCandidateBlockingPlan,
    SemanticConflictDecision,
)
from claim_plane.swarm.authority_projection import (
    SymbolScopedAuthorityProjectionReport,
)
from claim_plane.swarm.dependency_authority_narrowing import (
    DependencyAwareAuthorityNarrowingReport,
)
from claim_plane.swarm.conflict_policy_refinement import (
    ConflictPolicyClass,
    ConflictPolicyRefinementReport,
)
from claim_plane.swarm.models import WorkGraph, WorkItem
from claim_plane.swarm.same_file_admission import (
    SameFileAdmissionAction,
    SameFileAdmissionDecision,
    SameFileAdmissionReason,
)

ADMISSION_DECISION_ATTRIBUTION_PROTOCOL = "claim-plane.admission-decision-attribution.v1"


class AdmissionPairDisposition(str, Enum):
    """Stable pair-level outcome before worker execution."""

    PARALLEL_ELIGIBLE = "parallel_eligible"
    ORDERED_BY_DEPENDENCY = "ordered_by_dependency"
    SERIALIZED = "serialized"
    DENIED = "denied"


class AdmissionAttributionReason(str, Enum):
    """Stable machine-readable reasons used by pair attribution."""

    DECLARED_DEPENDENCY = "declared_dependency"
    SAME_FILE = "same_file"
    UNKNOWN_OVERLAP = "unknown_overlap"
    SHARED_CONTRACT = "shared_contract"
    SCHEMA_CHANGE = "schema_change"
    SEMANTIC_ORDER = "semantic_order"
    SEMANTIC_CONFLICT = "semantic_conflict"
    AFFECTED_SUBGRAPH_DISJOINT = "affected_subgraph_disjoint"
    SEMANTIC_INDEPENDENT = "semantic_independent"
    SEMANTIC_COMMUTATIVE = "semantic_commutative"
    NO_BLOCKING_EVIDENCE = "no_blocking_evidence"


_CONSTRAINT_REASON_ORDER = {
    AdmissionAttributionReason.SEMANTIC_CONFLICT: 0,
    AdmissionAttributionReason.SCHEMA_CHANGE: 1,
    AdmissionAttributionReason.SHARED_CONTRACT: 2,
    AdmissionAttributionReason.SEMANTIC_ORDER: 3,
    AdmissionAttributionReason.SAME_FILE: 4,
    AdmissionAttributionReason.UNKNOWN_OVERLAP: 5,
}


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _normal_path(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/") or None


def _operation_path(operation: Any) -> str | None:
    resource = operation.resource
    if resource.kind.value in {"file", "document"}:
        return _normal_path(resource.identifier)
    return _normal_path(
        resource.metadata.get("path")
        or resource.metadata.get("file")
        or resource.metadata.get("source_path")
        or resource.metadata.get("repository_path")
    )


@dataclass(frozen=True, slots=True)
class DeclaredAuthoritySurface:
    """One committed operation as seen by concurrency admission."""

    access: str
    kind: str
    identifier: str
    semantic_key: str
    path: str | None = None
    region: str | None = None
    commitment: str = "committed"
    mutating: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "access": self.access,
            "kind": self.kind,
            "identifier": self.identifier,
            "semantic_key": self.semantic_key,
            "path": self.path,
            "region": self.region,
            "commitment": self.commitment,
            "mutating": self.mutating,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DeclaredAuthoritySurface":
        return cls(
            access=str(data["access"]),
            kind=str(data["kind"]),
            identifier=str(data["identifier"]),
            semantic_key=str(data["semantic_key"]),
            path=(str(data["path"]) if data.get("path") is not None else None),
            region=(str(data["region"]) if data.get("region") is not None else None),
            commitment=str(data.get("commitment") or "committed"),
            mutating=bool(data.get("mutating")),
        )


def authority_surfaces_for_item(item: WorkItem) -> tuple[DeclaredAuthoritySurface, ...]:
    """Return deterministic committed authority surfaces for attribution only."""

    surfaces = [
        DeclaredAuthoritySurface(
            access=operation.access.value,
            kind=operation.resource.kind.value,
            identifier=operation.resource.identifier,
            semantic_key=operation.resource.semantic_key,
            path=_operation_path(operation),
            region=operation.resource.region,
            commitment=operation.commitment.value,
            mutating=operation.mutating,
        )
        for operation in item.operations
        if operation.committed
    ]
    return tuple(
        sorted(
            surfaces,
            key=lambda item: (
                item.path or "",
                item.kind,
                item.semantic_key,
                item.access,
                item.region or "",
                item.identifier,
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class AdmissionPairAttribution:
    """Evidence-backed explanation for one work-item pair."""

    left_id: str
    right_id: str
    disposition: AdmissionPairDisposition
    primary_reason: AdmissionAttributionReason
    reasons: tuple[AdmissionAttributionReason, ...]
    left_authority: tuple[DeclaredAuthoritySurface, ...]
    right_authority: tuple[DeclaredAuthoritySurface, ...]
    before_id: str | None = None
    after_id: str | None = None
    resources: tuple[str, ...] = ()
    detail: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        left = self.left_id.strip()
        right = self.right_id.strip()
        if not left or not right or left == right:
            raise ValueError("admission attribution requires distinct work ids")
        object.__setattr__(self, "left_id", left)
        object.__setattr__(self, "right_id", right)
        object.__setattr__(
            self, "disposition", AdmissionPairDisposition(self.disposition)
        )
        primary = AdmissionAttributionReason(self.primary_reason)
        object.__setattr__(self, "primary_reason", primary)
        reasons = tuple(
            dict.fromkeys(AdmissionAttributionReason(item) for item in self.reasons)
        )
        if not reasons or primary not in reasons:
            raise ValueError("primary attribution reason must be present in reasons")
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(
            self,
            "left_authority",
            tuple(
                item
                if isinstance(item, DeclaredAuthoritySurface)
                else DeclaredAuthoritySurface.from_dict(item)
                for item in self.left_authority
            ),
        )
        object.__setattr__(
            self,
            "right_authority",
            tuple(
                item
                if isinstance(item, DeclaredAuthoritySurface)
                else DeclaredAuthoritySurface.from_dict(item)
                for item in self.right_authority
            ),
        )
        if self.disposition in {
            AdmissionPairDisposition.ORDERED_BY_DEPENDENCY,
            AdmissionPairDisposition.SERIALIZED,
        }:
            if not self.before_id or not self.after_id or self.before_id == self.after_id:
                raise ValueError("ordered attribution requires before_id and after_id")
        object.__setattr__(self, "resources", tuple(sorted(set(self.resources))))
        object.__setattr__(self, "evidence", dict(self.evidence))

    @property
    def parallel_eligible(self) -> bool:
        return self.disposition is AdmissionPairDisposition.PARALLEL_ELIGIBLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "left_id": self.left_id,
            "right_id": self.right_id,
            "disposition": self.disposition.value,
            "parallel_eligible": self.parallel_eligible,
            "primary_reason": self.primary_reason.value,
            "reasons": [item.value for item in self.reasons],
            "before_id": self.before_id,
            "after_id": self.after_id,
            "resources": list(self.resources),
            "detail": self.detail,
            "left_authority": [item.to_dict() for item in self.left_authority],
            "right_authority": [item.to_dict() for item in self.right_authority],
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdmissionPairAttribution":
        result = cls(
            left_id=str(data["left_id"]),
            right_id=str(data["right_id"]),
            disposition=AdmissionPairDisposition(data["disposition"]),
            primary_reason=AdmissionAttributionReason(data["primary_reason"]),
            reasons=tuple(
                AdmissionAttributionReason(item) for item in data.get("reasons") or ()
            ),
            before_id=(str(data["before_id"]) if data.get("before_id") else None),
            after_id=(str(data["after_id"]) if data.get("after_id") else None),
            resources=tuple(str(item) for item in data.get("resources") or ()),
            detail=str(data.get("detail") or ""),
            left_authority=tuple(
                DeclaredAuthoritySurface.from_dict(item)
                for item in data.get("left_authority") or ()
            ),
            right_authority=tuple(
                DeclaredAuthoritySurface.from_dict(item)
                for item in data.get("right_authority") or ()
            ),
            evidence=dict(data.get("evidence") or {}),
        )
        if (
            "parallel_eligible" in data
            and bool(data["parallel_eligible"]) != result.parallel_eligible
        ):
            raise ValueError("admission attribution parallel_eligible mismatch")
        return result


@dataclass(frozen=True, slots=True)
class AdmissionDecisionAttributionReport:
    """Source-bound attribution report covering every work-item pair."""

    graph_fingerprint: str
    budget_fingerprint: str
    pairs: tuple[AdmissionPairAttribution, ...]
    semantic_graph_fingerprint: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = ADMISSION_DECISION_ATTRIBUTION_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != ADMISSION_DECISION_ATTRIBUTION_PROTOCOL:
            raise ValueError(f"unsupported attribution protocol {self.protocol!r}")
        for name in ("graph_fingerprint", "budget_fingerprint"):
            value = str(getattr(self, name)).lower()
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
            object.__setattr__(self, name, value)
        if self.semantic_graph_fingerprint is not None:
            value = str(self.semantic_graph_fingerprint).lower()
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise ValueError("semantic_graph_fingerprint must be a SHA-256 digest")
            object.__setattr__(self, "semantic_graph_fingerprint", value)
        pairs = tuple(
            item
            if isinstance(item, AdmissionPairAttribution)
            else AdmissionPairAttribution.from_dict(item)
            for item in self.pairs
        )
        keys = [(item.left_id, item.right_id) for item in pairs]
        if len(set(frozenset(key) for key in keys)) != len(keys):
            raise ValueError("admission attribution pairs must be unique")
        object.__setattr__(self, "pairs", pairs)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_dict(include_fingerprint=False))

    def _summary_core(self) -> dict[str, Any]:
        disposition_counts = {
            disposition.value: sum(
                1 for item in self.pairs if item.disposition is disposition
            )
            for disposition in AdmissionPairDisposition
        }
        reason_counts = {
            reason.value: sum(1 for item in self.pairs if reason in item.reasons)
            for reason in AdmissionAttributionReason
        }
        primary_reason_counts = {
            reason.value: sum(1 for item in self.pairs if item.primary_reason is reason)
            for reason in AdmissionAttributionReason
        }
        blocking_pairs = tuple(
            item
            for item in self.pairs
            if item.disposition
            in {AdmissionPairDisposition.SERIALIZED, AdmissionPairDisposition.DENIED}
        )
        blocking_reason_counts = {
            reason.value: sum(1 for item in blocking_pairs if reason in item.reasons)
            for reason in AdmissionAttributionReason
        }
        return {
            "pair_count": len(self.pairs),
            "parallel_eligible_pairs": disposition_counts[
                AdmissionPairDisposition.PARALLEL_ELIGIBLE.value
            ],
            "dependency_ordered_pairs": disposition_counts[
                AdmissionPairDisposition.ORDERED_BY_DEPENDENCY.value
            ],
            "serialized_pairs": disposition_counts[
                AdmissionPairDisposition.SERIALIZED.value
            ],
            "denied_pairs": disposition_counts[AdmissionPairDisposition.DENIED.value],
            "disposition_counts": disposition_counts,
            "reason_counts": {
                key: value for key, value in reason_counts.items() if value
            },
            "primary_reason_counts": {
                key: value for key, value in primary_reason_counts.items() if value
            },
            "blocking_reason_counts": {
                key: value for key, value in blocking_reason_counts.items() if value
            },
        }

    def summary(self) -> dict[str, Any]:
        return {**self._summary_core(), "fingerprint": self.fingerprint}

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload = {
            "protocol": self.protocol,
            "graph_fingerprint": self.graph_fingerprint,
            "budget_fingerprint": self.budget_fingerprint,
            "semantic_graph_fingerprint": self.semantic_graph_fingerprint,
            "pairs": [item.to_dict() for item in self.pairs],
            "summary": self.summary() if include_fingerprint else self._summary_core(),
            "metadata": dict(self.metadata),
        }
        if include_fingerprint:
            return {"fingerprint": self.fingerprint, **payload}
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdmissionDecisionAttributionReport":
        report = cls(
            protocol=str(data.get("protocol") or ADMISSION_DECISION_ATTRIBUTION_PROTOCOL),
            graph_fingerprint=str(data["graph_fingerprint"]),
            budget_fingerprint=str(data["budget_fingerprint"]),
            semantic_graph_fingerprint=(
                str(data["semantic_graph_fingerprint"])
                if data.get("semantic_graph_fingerprint") is not None
                else None
            ),
            pairs=tuple(
                AdmissionPairAttribution.from_dict(item)
                for item in data.get("pairs") or ()
            ),
            metadata=dict(data.get("metadata") or {}),
        )
        expected = data.get("fingerprint")
        if expected is not None and str(expected) != report.fingerprint:
            raise ValueError("admission attribution fingerprint mismatch")
        supplied_summary = data.get("summary")
        if supplied_summary is not None and dict(supplied_summary) != report.summary():
            raise ValueError("admission attribution summary mismatch")
        return report


def _ancestor_map(graph: WorkGraph) -> dict[str, set[str]]:
    ancestors: dict[str, set[str]] = {}
    for work_id in graph.topological_order():
        current = set(graph.item_map[work_id].depends_on)
        for dependency in graph.item_map[work_id].depends_on:
            current.update(ancestors[dependency])
        ancestors[work_id] = current
    return ancestors


def _pair_key(left_id: str, right_id: str) -> frozenset[str]:
    return frozenset((left_id, right_id))


def _constraint_reasons(constraint: Any) -> tuple[AdmissionAttributionReason, ...]:
    mapped = [AdmissionAttributionReason(reason.value) for reason in constraint.reasons]
    return tuple(
        sorted(
            set(mapped),
            key=lambda item: (_CONSTRAINT_REASON_ORDER.get(item, 999), item.value),
        )
    )


def _same_file_parallel_reason(
    decisions: Iterable[SameFileAdmissionDecision],
) -> AdmissionAttributionReason | None:
    reasons = {
        item.reason
        for item in decisions
        if item.action is SameFileAdmissionAction.PARALLEL
    }
    if SameFileAdmissionReason.SEMANTIC_COMMUTATIVE in reasons:
        return AdmissionAttributionReason.SEMANTIC_COMMUTATIVE
    if SameFileAdmissionReason.SEMANTIC_INDEPENDENT in reasons:
        return AdmissionAttributionReason.SEMANTIC_INDEPENDENT
    return None


def _semantic_parallel_reason(
    decisions: Iterable[SemanticConflictDecision],
) -> AdmissionAttributionReason | None:
    kinds = {item.kind.value for item in decisions}
    if "commutative" in kinds:
        return AdmissionAttributionReason.SEMANTIC_COMMUTATIVE
    if "independent" in kinds:
        return AdmissionAttributionReason.SEMANTIC_INDEPENDENT
    return None


def build_admission_decision_attribution(
    graph: WorkGraph,
    *,
    graph_fingerprint: str,
    budget_fingerprint: str,
    constraints: Iterable[Any],
    candidate_blocking: AffectedSubgraphCandidateBlockingPlan | None = None,
    same_file_admissions: Iterable[SameFileAdmissionDecision] = (),
    semantic_decisions: Iterable[SemanticConflictDecision] = (),
    semantic_graph_fingerprint: str | None = None,
    authority_projection: SymbolScopedAuthorityProjectionReport | None = None,
    dependency_narrowing: DependencyAwareAuthorityNarrowingReport | None = None,
    conflict_policy_refinement: ConflictPolicyRefinementReport | None = None,
) -> AdmissionDecisionAttributionReport:
    """Build deterministic pair-level decision attribution without changing policy."""

    order = graph.topological_order()
    rank = {work_id: index for index, work_id in enumerate(order)}
    ancestors = _ancestor_map(graph)
    constraint_map = {
        _pair_key(item.before, item.after): item for item in constraints
    }
    same_file_map: dict[frozenset[str], list[SameFileAdmissionDecision]] = {}
    for item in same_file_admissions:
        same_file_map.setdefault(_pair_key(item.left_id, item.right_id), []).append(item)
    semantic_map: dict[frozenset[str], list[SemanticConflictDecision]] = {}
    for item in semantic_decisions:
        semantic_map.setdefault(_pair_key(item.left_id, item.right_id), []).append(item)

    projection_map = (
        authority_projection.item_map if authority_projection is not None else {}
    )
    narrowing_map = (
        dependency_narrowing.item_map if dependency_narrowing is not None else {}
    )
    refinement_map = (
        conflict_policy_refinement.pair_map
        if conflict_policy_refinement is not None
        else {}
    )
    candidate_ids = (
        {item.candidate_id for item in candidate_blocking.subgraphs}
        if candidate_blocking is not None
        else set()
    )
    attributions: list[AdmissionPairAttribution] = []
    for first, second in combinations(order, 2):
        left_id, right_id = sorted((first, second), key=rank.__getitem__)
        pair_key = _pair_key(left_id, right_id)
        left = graph.item_map[left_id]
        right = graph.item_map[right_id]
        left_authority = authority_surfaces_for_item(left)
        right_authority = authority_surfaces_for_item(right)
        pair_same_file = tuple(
            sorted(
                same_file_map.get(pair_key, ()),
                key=lambda item: (item.path, item.reason.value, item.fingerprint),
            )
        )
        pair_semantic = tuple(
            sorted(
                semantic_map.get(pair_key, ()),
                key=lambda item: item.fingerprint,
            )
        )

        evidence: dict[str, Any] = {
            "same_file_admissions": [item.to_dict() for item in pair_same_file],
            "symbol_authority_projection": {
                "left": (
                    projection_map[left_id].to_dict()
                    if left_id in projection_map
                    else None
                ),
                "right": (
                    projection_map[right_id].to_dict()
                    if right_id in projection_map
                    else None
                ),
            },
            "dependency_authority_narrowing": {
                "left": narrowing_map[left_id].to_dict() if left_id in narrowing_map else None,
                "right": narrowing_map[right_id].to_dict() if right_id in narrowing_map else None,
            },
            "conflict_policy_refinement": (
                refinement_map[pair_key].to_dict()
                if pair_key in refinement_map
                else None
            ),
            "semantic_classifications": [
                {
                    "fingerprint": item.fingerprint,
                    "kind": item.kind.value,
                    "order": item.order.value if item.order is not None else None,
                    "left_changes": list(item.left_changes),
                    "right_changes": list(item.right_changes),
                    "evidence_reasons": sorted({e.reason.value for e in item.evidence}),
                    "evidence_count": len(item.evidence),
                }
                for item in pair_semantic
            ],
        }

        if candidate_blocking is None or not {left_id, right_id}.issubset(candidate_ids):
            evidence["candidate_blocking"] = {"state": "not_applicable"}
        else:
            candidate_pair = candidate_blocking.pair(left_id, right_id)
            if candidate_pair is None:
                evidence["candidate_blocking"] = {
                    "state": "pruned",
                    "plan_fingerprint": candidate_blocking.fingerprint,
                }
            else:
                evidence["candidate_blocking"] = {
                    "state": "selected",
                    "plan_fingerprint": candidate_blocking.fingerprint,
                    "reasons": [item.value for item in candidate_pair.reasons],
                    "shared_identities": list(candidate_pair.shared_identities),
                    "shared_unresolved_targets": list(
                        candidate_pair.shared_unresolved_targets
                    ),
                    "fail_closed_candidates": list(candidate_pair.fail_closed_candidates),
                }

        if left_id in ancestors[right_id] or right_id in ancestors[left_id]:
            before_id, after_id = (
                (left_id, right_id)
                if left_id in ancestors[right_id]
                else (right_id, left_id)
            )
            direct = before_id in graph.item_map[after_id].depends_on
            evidence["dependency"] = {
                "kind": "direct" if direct else "transitive",
                "before_id": before_id,
                "after_id": after_id,
            }
            attributions.append(
                AdmissionPairAttribution(
                    left_id=left_id,
                    right_id=right_id,
                    disposition=AdmissionPairDisposition.ORDERED_BY_DEPENDENCY,
                    primary_reason=AdmissionAttributionReason.DECLARED_DEPENDENCY,
                    reasons=(AdmissionAttributionReason.DECLARED_DEPENDENCY,),
                    before_id=before_id,
                    after_id=after_id,
                    left_authority=left_authority,
                    right_authority=right_authority,
                    detail=(
                        "work items are explicitly dependency-ordered"
                        if direct
                        else "work items are transitively dependency-ordered"
                    ),
                    evidence=evidence,
                )
            )
            continue

        constraint = constraint_map.get(pair_key)
        if constraint is not None:
            reasons = _constraint_reasons(constraint)
            disposition = (
                AdmissionPairDisposition.DENIED
                if constraint.action.value == "deny"
                else AdmissionPairDisposition.SERIALIZED
            )
            evidence["concurrency_constraint"] = constraint.to_dict()
            attributions.append(
                AdmissionPairAttribution(
                    left_id=left_id,
                    right_id=right_id,
                    disposition=disposition,
                    primary_reason=reasons[0],
                    reasons=reasons,
                    before_id=(
                        constraint.before
                        if disposition is AdmissionPairDisposition.SERIALIZED
                        else None
                    ),
                    after_id=(
                        constraint.after
                        if disposition is AdmissionPairDisposition.SERIALIZED
                        else None
                    ),
                    resources=tuple(constraint.resources),
                    detail=constraint.detail,
                    left_authority=left_authority,
                    right_authority=right_authority,
                    evidence=evidence,
                )
            )
            continue

        pair_refinement = refinement_map.get(pair_key)
        parallel_reason = None
        if pair_refinement is not None:
            if pair_refinement.classification is ConflictPolicyClass.COMMUTATIVE:
                parallel_reason = AdmissionAttributionReason.SEMANTIC_COMMUTATIVE
            elif (
                pair_refinement.classification
                is ConflictPolicyClass.PROVABLY_INDEPENDENT
            ):
                parallel_reason = AdmissionAttributionReason.SEMANTIC_INDEPENDENT
        if parallel_reason is None:
            parallel_reason = _same_file_parallel_reason(pair_same_file)
        if parallel_reason is None:
            candidate_state = evidence["candidate_blocking"]["state"]
            if candidate_state == "pruned":
                parallel_reason = AdmissionAttributionReason.AFFECTED_SUBGRAPH_DISJOINT
        if parallel_reason is None:
            parallel_reason = _semantic_parallel_reason(pair_semantic)
        if parallel_reason is None:
            parallel_reason = AdmissionAttributionReason.NO_BLOCKING_EVIDENCE
        attributions.append(
            AdmissionPairAttribution(
                left_id=left_id,
                right_id=right_id,
                disposition=AdmissionPairDisposition.PARALLEL_ELIGIBLE,
                primary_reason=parallel_reason,
                reasons=(parallel_reason,),
                left_authority=left_authority,
                right_authority=right_authority,
                detail="no admission blocker requires ordering for this pair",
                evidence=evidence,
            )
        )

    return AdmissionDecisionAttributionReport(
        graph_fingerprint=graph_fingerprint,
        budget_fingerprint=budget_fingerprint,
        semantic_graph_fingerprint=semantic_graph_fingerprint,
        pairs=tuple(attributions),
        metadata={
            "observational_only": True,
            "work_item_count": len(graph.work_items),
            "expected_pair_count": (
                len(graph.work_items) * (len(graph.work_items) - 1) // 2
            ),
            "authority_projection": (
                authority_projection.protocol
                if authority_projection is not None
                else "declared-committed-operations-v1"
            ),
            "authority_projection_fingerprint": (
                authority_projection.fingerprint
                if authority_projection is not None
                else None
            ),
            "dependency_authority_narrowing": (
                dependency_narrowing.protocol if dependency_narrowing is not None else None
            ),
            "dependency_authority_narrowing_fingerprint": (
                dependency_narrowing.fingerprint if dependency_narrowing is not None else None
            ),
            "conflict_policy_refinement": (
                conflict_policy_refinement.protocol
                if conflict_policy_refinement is not None
                else None
            ),
            "conflict_policy_refinement_fingerprint": (
                conflict_policy_refinement.fingerprint
                if conflict_policy_refinement is not None
                else None
            ),
        },
    )


__all__ = [
    "ADMISSION_DECISION_ATTRIBUTION_PROTOCOL",
    "AdmissionAttributionReason",
    "AdmissionDecisionAttributionReport",
    "AdmissionPairAttribution",
    "AdmissionPairDisposition",
    "DeclaredAuthoritySurface",
    "authority_surfaces_for_item",
    "build_admission_decision_attribution",
]
