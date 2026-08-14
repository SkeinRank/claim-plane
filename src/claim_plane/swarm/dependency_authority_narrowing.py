"""Dependency-aware authority narrowing for graph-aware swarm admission.

This stage sits after Symbol-Scoped Authority Projection v2.  It never grants new
mutation authority and never rewrites the worker intent used for execution or final
verification.  Instead it proves a smaller *analysis envelope* for work items whose
mutation roots are already exact symbols.

The proof is intentionally conservative: every root must resolve in the pinned
semantic dependency graph, traversal is limited to internal dependency relations,
and any unresolved boundary reachable from a root fences the item back to the 9B
analysis surface.  A closed envelope may drop redundant broad file carriers from
semantic/candidate analysis while preserving the exact projected mutation roots.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping

from claim_plane.core import (
    DependencyRelation,
    DependencyResolution,
    IntentOperation,
    ResourceKind,
    SemanticDependencyGraph,
    normalize_resource_ref,
)
from claim_plane.swarm.authority_projection import (
    SymbolScopedAuthorityProjectionReport,
)
from claim_plane.swarm.models import WorkGraph

DEPENDENCY_AWARE_AUTHORITY_NARROWING_PROTOCOL = (
    "claim-plane.dependency-aware-authority-narrowing.v1"
)

# Ownership/carrier edges do not prove that a sibling symbol belongs to mutation
# authority.  The remaining relations describe semantic dependencies that can form
# the context envelope around exact mutation roots.
_NARROWING_RELATIONS = frozenset(
    relation
    for relation in DependencyRelation
    if relation not in {DependencyRelation.DEFINES, DependencyRelation.PUBLIC_API}
)


class DependencyNarrowingState(str, Enum):
    CLOSED = "closed"
    FAIL_CLOSED = "fail_closed"
    NOT_APPLICABLE = "not_applicable"


class DependencyNarrowingReason(str, Enum):
    CLOSED_DEPENDENCY_ENVELOPE = "closed_dependency_envelope"
    NO_SEMANTIC_GRAPH = "no_semantic_graph"
    NO_SYMBOL_ROOT = "no_symbol_root"
    MISSING_GRAPH_ROOT = "missing_graph_root"
    UNRESOLVED_BOUNDARY = "unresolved_boundary"
    DESTRUCTIVE_AUTHORITY = "destructive_authority"
    PATTERN_AUTHORITY = "pattern_authority"


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _operation_key(operation: IntentOperation) -> str:
    return hashlib.sha256(_canonical_json(operation.to_dict())).hexdigest()


def _dedupe_operations(
    operations: tuple[IntentOperation, ...] | list[IntentOperation],
) -> tuple[IntentOperation, ...]:
    by_key = {_operation_key(item): item for item in operations}
    return tuple(by_key[key] for key in sorted(by_key))


def _operation_identity(operation: IntentOperation) -> str | None:
    if operation.resource.kind not in {ResourceKind.SYMBOL, ResourceKind.CONTRACT}:
        return None
    return normalize_resource_ref(operation.resource).identity


@dataclass(frozen=True, slots=True)
class DependencyAuthorityEdgeEvidence:
    source_identity: str
    target_identity: str
    relation: DependencyRelation
    evidence_fingerprints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("source_identity", "target_identity"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"{name} must not be empty")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "relation", DependencyRelation(self.relation))
        object.__setattr__(
            self,
            "evidence_fingerprints",
            tuple(
                sorted(
                    {str(item) for item in self.evidence_fingerprints if str(item)}
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_identity": self.source_identity,
            "target_identity": self.target_identity,
            "relation": self.relation.value,
            "evidence_fingerprints": list(self.evidence_fingerprints),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DependencyAuthorityEdgeEvidence":
        return cls(
            source_identity=str(data["source_identity"]),
            target_identity=str(data["target_identity"]),
            relation=DependencyRelation(data["relation"]),
            evidence_fingerprints=tuple(data.get("evidence_fingerprints") or ()),
        )


@dataclass(frozen=True, slots=True)
class WorkItemDependencyAuthorityNarrowing:
    work_id: str
    state: DependencyNarrowingState
    reasons: tuple[DependencyNarrowingReason, ...]
    mutation_root_identities: tuple[str, ...]
    dependency_context_identities: tuple[str, ...]
    unresolved_targets: tuple[str, ...]
    external_targets: tuple[str, ...]
    excluded_same_file_sibling_identities: tuple[str, ...]
    analysis_operations: tuple[IntentOperation, ...]
    edge_evidence: tuple[DependencyAuthorityEdgeEvidence, ...] = ()

    def __post_init__(self) -> None:
        work_id = self.work_id.strip()
        if not work_id:
            raise ValueError("work_id must not be empty")
        object.__setattr__(self, "work_id", work_id)
        object.__setattr__(self, "state", DependencyNarrowingState(self.state))
        object.__setattr__(
            self,
            "reasons",
            tuple(
                sorted(
                    {DependencyNarrowingReason(item) for item in self.reasons},
                    key=lambda item: item.value,
                )
            ),
        )
        for name in (
            "mutation_root_identities",
            "dependency_context_identities",
            "unresolved_targets",
            "external_targets",
            "excluded_same_file_sibling_identities",
        ):
            object.__setattr__(
                self,
                name,
                tuple(sorted({str(item) for item in getattr(self, name)})),
            )
        object.__setattr__(
            self,
            "analysis_operations",
            tuple(
                item
                if isinstance(item, IntentOperation)
                else IntentOperation.from_dict(item)
                for item in self.analysis_operations
            ),
        )
        object.__setattr__(
            self,
            "edge_evidence",
            tuple(
                sorted(
                    (
                        item
                        if isinstance(item, DependencyAuthorityEdgeEvidence)
                        else DependencyAuthorityEdgeEvidence.from_dict(item)
                        for item in self.edge_evidence
                    ),
                    key=lambda item: (
                        item.source_identity,
                        item.target_identity,
                        item.relation.value,
                    ),
                )
            ),
        )

    @property
    def narrowed(self) -> bool:
        return self.state is DependencyNarrowingState.CLOSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "state": self.state.value,
            "narrowed": self.narrowed,
            "reasons": [item.value for item in self.reasons],
            "mutation_root_identities": list(self.mutation_root_identities),
            "dependency_context_identities": list(self.dependency_context_identities),
            "unresolved_targets": list(self.unresolved_targets),
            "external_targets": list(self.external_targets),
            "excluded_same_file_sibling_identities": list(
                self.excluded_same_file_sibling_identities
            ),
            "analysis_operations": [
                item.to_dict() for item in self.analysis_operations
            ],
            "edge_evidence": [item.to_dict() for item in self.edge_evidence],
        }

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any]
    ) -> "WorkItemDependencyAuthorityNarrowing":
        result = cls(
            work_id=str(data["work_id"]),
            state=DependencyNarrowingState(data["state"]),
            reasons=tuple(
                DependencyNarrowingReason(item)
                for item in data.get("reasons") or ()
            ),
            mutation_root_identities=tuple(data.get("mutation_root_identities") or ()),
            dependency_context_identities=tuple(
                data.get("dependency_context_identities") or ()
            ),
            unresolved_targets=tuple(data.get("unresolved_targets") or ()),
            external_targets=tuple(data.get("external_targets") or ()),
            excluded_same_file_sibling_identities=tuple(
                data.get("excluded_same_file_sibling_identities") or ()
            ),
            analysis_operations=tuple(
                IntentOperation.from_dict(item)
                for item in data.get("analysis_operations") or ()
            ),
            edge_evidence=tuple(
                DependencyAuthorityEdgeEvidence.from_dict(item)
                for item in data.get("edge_evidence") or ()
            ),
        )
        if "narrowed" in data and bool(data["narrowed"]) != result.narrowed:
            raise ValueError("dependency authority narrowing narrowed flag mismatch")
        return result


@dataclass(frozen=True, slots=True)
class DependencyAwareAuthorityNarrowingReport:
    work_graph_fingerprint: str
    symbol_projection_fingerprint: str
    items: tuple[WorkItemDependencyAuthorityNarrowing, ...]
    semantic_graph_fingerprint: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = DEPENDENCY_AWARE_AUTHORITY_NARROWING_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != DEPENDENCY_AWARE_AUTHORITY_NARROWING_PROTOCOL:
            raise ValueError(
                f"unsupported dependency authority narrowing {self.protocol!r}"
            )
        for name in (
            "work_graph_fingerprint",
            "symbol_projection_fingerprint",
            "semantic_graph_fingerprint",
        ):
            value = getattr(self, name)
            if value is None and name == "semantic_graph_fingerprint":
                continue
            text = str(value).lower()
            if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
            object.__setattr__(self, name, text)
        items = tuple(
            item
            if isinstance(item, WorkItemDependencyAuthorityNarrowing)
            else WorkItemDependencyAuthorityNarrowing.from_dict(item)
            for item in self.items
        )
        if len({item.work_id for item in items}) != len(items):
            raise ValueError("dependency authority narrowing work ids must be unique")
        object.__setattr__(
            self, "items", tuple(sorted(items, key=lambda item: item.work_id))
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def item_map(self) -> dict[str, WorkItemDependencyAuthorityNarrowing]:
        return {item.work_id: item for item in self.items}

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_dict(include_fingerprint=False))

    def _summary_core(self) -> dict[str, Any]:
        return {
            "work_item_count": len(self.items),
            "closed_work_items": sum(
                item.state is DependencyNarrowingState.CLOSED for item in self.items
            ),
            "fail_closed_work_items": sum(
                item.state is DependencyNarrowingState.FAIL_CLOSED
                for item in self.items
            ),
            "not_applicable_work_items": sum(
                item.state is DependencyNarrowingState.NOT_APPLICABLE
                for item in self.items
            ),
            "mutation_root_count": len(
                {
                    identity
                    for item in self.items
                    for identity in item.mutation_root_identities
                }
            ),
            "dependency_context_count": len(
                {
                    identity
                    for item in self.items
                    for identity in item.dependency_context_identities
                }
            ),
            "excluded_same_file_sibling_count": len(
                {
                    identity
                    for item in self.items
                    for identity in item.excluded_same_file_sibling_identities
                }
            ),
            "unresolved_target_count": len(
                {
                    identity
                    for item in self.items
                    for identity in item.unresolved_targets
                }
            ),
        }

    def summary(self) -> dict[str, Any]:
        return {**self._summary_core(), "fingerprint": self.fingerprint}

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload = {
            "protocol": self.protocol,
            "work_graph_fingerprint": self.work_graph_fingerprint,
            "symbol_projection_fingerprint": self.symbol_projection_fingerprint,
            "semantic_graph_fingerprint": self.semantic_graph_fingerprint,
            "items": [item.to_dict() for item in self.items],
            "summary": self.summary() if include_fingerprint else self._summary_core(),
            "metadata": dict(self.metadata),
        }
        if include_fingerprint:
            return {"fingerprint": self.fingerprint, **payload}
        return payload

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any]
    ) -> "DependencyAwareAuthorityNarrowingReport":
        report = cls(
            protocol=str(
                data.get("protocol")
                or DEPENDENCY_AWARE_AUTHORITY_NARROWING_PROTOCOL
            ),
            work_graph_fingerprint=str(data["work_graph_fingerprint"]),
            symbol_projection_fingerprint=str(data["symbol_projection_fingerprint"]),
            semantic_graph_fingerprint=(
                str(data["semantic_graph_fingerprint"])
                if data.get("semantic_graph_fingerprint") is not None
                else None
            ),
            items=tuple(
                WorkItemDependencyAuthorityNarrowing.from_dict(item)
                for item in data.get("items") or ()
            ),
            metadata=dict(data.get("metadata") or {}),
        )
        supplied = data.get("fingerprint")
        if supplied is not None and str(supplied) != report.fingerprint:
            raise ValueError("dependency authority narrowing fingerprint mismatch")
        summary = data.get("summary")
        if isinstance(summary, Mapping):
            expected = report.summary()
            for key, value in expected.items():
                if key in summary and summary[key] != value:
                    raise ValueError("dependency authority narrowing summary mismatch")
        return report


def _root_identities(
    projected_operations: tuple[IntentOperation, ...],
) -> tuple[str, ...]:
    roots = []
    for operation in projected_operations:
        if not operation.committed or not operation.mutating:
            continue
        identity = _operation_identity(operation)
        if identity is not None:
            roots.append(identity)
    return tuple(sorted(set(roots)))


def _narrow_item(
    work_id: str,
    *,
    projected_operations: tuple[IntentOperation, ...],
    fallback_analysis_operations: tuple[IntentOperation, ...],
    semantic_graph: SemanticDependencyGraph | None,
) -> WorkItemDependencyAuthorityNarrowing:
    if semantic_graph is None:
        return WorkItemDependencyAuthorityNarrowing(
            work_id=work_id,
            state=DependencyNarrowingState.NOT_APPLICABLE,
            reasons=(DependencyNarrowingReason.NO_SEMANTIC_GRAPH,),
            mutation_root_identities=(),
            dependency_context_identities=(),
            unresolved_targets=(),
            external_targets=(),
            excluded_same_file_sibling_identities=(),
            analysis_operations=fallback_analysis_operations,
        )

    roots = _root_identities(projected_operations)
    reasons: set[DependencyNarrowingReason] = set()
    if not roots:
        reasons.add(DependencyNarrowingReason.NO_SYMBOL_ROOT)
    if any(
        op.committed and op.mutating and op.access.destructive
        for op in projected_operations
    ):
        reasons.add(DependencyNarrowingReason.DESTRUCTIVE_AUTHORITY)
    if any(
        op.committed and op.mutating and op.resource.is_pattern
        for op in projected_operations
    ):
        reasons.add(DependencyNarrowingReason.PATTERN_AUTHORITY)

    nodes = {node.identity: node for node in semantic_graph.nodes}
    missing = tuple(identity for identity in roots if identity not in nodes)
    if missing:
        reasons.add(DependencyNarrowingReason.MISSING_GRAPH_ROOT)

    outgoing: dict[str, list[Any]] = {}
    for edge in semantic_graph.edges:
        if edge.relation in _NARROWING_RELATIONS:
            outgoing.setdefault(edge.source_identity, []).append(edge)
    for edges in outgoing.values():
        edges.sort(
            key=lambda edge: (
                edge.target_identity,
                edge.relation.value,
                edge.resolution.value,
            )
        )

    context: set[str] = set()
    unresolved: set[str] = set()
    external: set[str] = set()
    edge_evidence: dict[tuple[str, str, str], DependencyAuthorityEdgeEvidence] = {}
    queue = [identity for identity in roots if identity in nodes]
    seen = set(queue)
    while queue:
        current = queue.pop(0)
        for edge in outgoing.get(current, ()):
            evidence = DependencyAuthorityEdgeEvidence(
                source_identity=edge.source_identity,
                target_identity=edge.target_identity,
                relation=edge.relation,
                evidence_fingerprints=tuple(item.fingerprint for item in edge.evidence),
            )
            edge_evidence[
                (edge.source_identity, edge.target_identity, edge.relation.value)
            ] = evidence
            if edge.resolution is DependencyResolution.UNRESOLVED:
                unresolved.add(edge.target_identity)
                continue
            if edge.resolution is DependencyResolution.EXTERNAL:
                external.add(edge.target_identity)
                continue
            context.add(edge.target_identity)
            if edge.target_identity not in seen:
                seen.add(edge.target_identity)
                queue.append(edge.target_identity)

    if unresolved:
        reasons.add(DependencyNarrowingReason.UNRESOLVED_BOUNDARY)

    blocking = reasons & {
        DependencyNarrowingReason.NO_SYMBOL_ROOT,
        DependencyNarrowingReason.MISSING_GRAPH_ROOT,
        DependencyNarrowingReason.UNRESOLVED_BOUNDARY,
        DependencyNarrowingReason.DESTRUCTIVE_AUTHORITY,
        DependencyNarrowingReason.PATTERN_AUTHORITY,
    }
    if blocking:
        state = DependencyNarrowingState.FAIL_CLOSED
        analysis_operations = fallback_analysis_operations
    else:
        state = DependencyNarrowingState.CLOSED
        reasons.add(DependencyNarrowingReason.CLOSED_DEPENDENCY_ENVELOPE)
        # 9B's projected surface is already a subset of worker authority.  A closed
        # dependency envelope proves it is safe to use that exact surface for
        # semantic/candidate analysis instead of retaining redundant file carriers.
        analysis_operations = _dedupe_operations(projected_operations)

    root_paths = {
        nodes[identity].resource.path
        for identity in roots
        if identity in nodes and nodes[identity].resource.path
    }
    in_envelope = set(roots) | context
    excluded_siblings = {
        node.identity
        for node in semantic_graph.nodes
        if (
            not node.external
            and node.resource.kind is ResourceKind.SYMBOL
            and node.resource.path in root_paths
            and node.identity not in in_envelope
        )
    }

    return WorkItemDependencyAuthorityNarrowing(
        work_id=work_id,
        state=state,
        reasons=tuple(reasons),
        mutation_root_identities=roots,
        dependency_context_identities=tuple(context),
        unresolved_targets=tuple(unresolved),
        external_targets=tuple(external),
        excluded_same_file_sibling_identities=tuple(excluded_siblings),
        analysis_operations=analysis_operations,
        edge_evidence=tuple(edge_evidence.values()),
    )


def build_dependency_aware_authority_narrowing(
    graph: WorkGraph,
    symbol_projection: SymbolScopedAuthorityProjectionReport,
    semantic_graph: SemanticDependencyGraph | None,
) -> DependencyAwareAuthorityNarrowingReport:
    """Build a source-bound dependency envelope around exact projected roots."""

    if symbol_projection.work_graph_fingerprint != graph.fingerprint():
        raise ValueError("symbol authority projection is stale for the work graph")
    if (
        semantic_graph is not None
        and symbol_projection.semantic_graph_fingerprint != semantic_graph.fingerprint
    ):
        raise ValueError("symbol authority projection is stale for the semantic graph")

    projection_map = symbol_projection.item_map
    items = tuple(
        _narrow_item(
            item.work_id,
            projected_operations=projection_map[item.work_id].projected_operations,
            fallback_analysis_operations=(
                projection_map[item.work_id].analysis_operations
            ),
            semantic_graph=semantic_graph,
        )
        for item in graph.work_items
    )
    return DependencyAwareAuthorityNarrowingReport(
        work_graph_fingerprint=graph.fingerprint(),
        symbol_projection_fingerprint=symbol_projection.fingerprint,
        semantic_graph_fingerprint=(
            semantic_graph.fingerprint if semantic_graph is not None else None
        ),
        items=items,
        metadata={
            "scope": "admission-analysis-only",
            "mutation_authority_preserved": True,
            "worker_intent_unchanged": True,
            "traversal_direction": "outgoing_dependency",
            "relations": sorted(item.value for item in _NARROWING_RELATIONS),
            "unresolved_boundary_policy": "fail_closed",
        },
    )


def dependency_narrowed_analysis_graph(
    graph: WorkGraph,
    report: DependencyAwareAuthorityNarrowingReport,
) -> WorkGraph:
    """Return internal work graph using the proven analysis envelope per item."""

    if report.work_graph_fingerprint != graph.fingerprint():
        raise ValueError("dependency authority narrowing is stale for the work graph")
    item_map = report.item_map
    items = tuple(
        replace(item, operations=item_map[item.work_id].analysis_operations)
        for item in graph.work_items
    )
    return WorkGraph(
        work_items=items, metadata=dict(graph.metadata), protocol=graph.protocol
    )


def narrowed_operations_for_work(
    report: DependencyAwareAuthorityNarrowingReport,
    work_id: str,
) -> tuple[IntentOperation, ...]:
    try:
        return report.item_map[work_id].analysis_operations
    except KeyError as exc:
        raise KeyError(
            f"dependency authority narrowing has no work item {work_id!r}"
        ) from exc


__all__ = [
    "DEPENDENCY_AWARE_AUTHORITY_NARROWING_PROTOCOL",
    "DependencyNarrowingState",
    "DependencyNarrowingReason",
    "DependencyAuthorityEdgeEvidence",
    "WorkItemDependencyAuthorityNarrowing",
    "DependencyAwareAuthorityNarrowingReport",
    "build_dependency_aware_authority_narrowing",
    "dependency_narrowed_analysis_graph",
    "narrowed_operations_for_work",
]
