"""Affected-subgraph candidate blocking for semantic conflict analysis.

This layer is a conservative pre-filter. It narrows the pairs that need the more
expensive semantic conflict classifier without making an admission decision itself.
A pair is pruned only when the pinned dependency graph proves that the candidates'
affected subgraphs are disjoint and neither side carries incomplete evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from itertools import combinations
from typing import Any, Mapping, Sequence

from claim_plane.core.dependency_graph import (
    DependencyNode,
    DependencyRelation,
    DependencyResolution,
    SemanticDependencyGraph,
)
from claim_plane.core.impact import SemanticChange, SemanticChangeKind

AFFECTED_SUBGRAPH_CANDIDATE_BLOCKING_PROTOCOL = (
    "claim-plane.affected-subgraph-candidate-blocking.v1"
)
CANDIDATE_AFFECTED_SUBGRAPH_PROTOCOL = "claim-plane.candidate-affected-subgraph.v1"
CANDIDATE_PAIR_PROTOCOL = "claim-plane.semantic-conflict-candidate-pair.v1"

# Candidate blocking intentionally uses a broader relation surface than impact
# classification. It is allowed to retain false-positive pairs, but it must not prune
# a pair that a later graph-aware classifier could need. Ownership is included so a
# broad file-level mutation remains coupled to resources defined by that file.
_CANDIDATE_RELATIONS = frozenset(
    item
    for item in DependencyRelation
    if item not in {DependencyRelation.DEFINES, DependencyRelation.PUBLIC_API}
)


@dataclass(frozen=True, slots=True)
class _CandidateGraphIndex:
    graph_fingerprint: str
    nodes: Mapping[str, DependencyNode]
    incoming_internal: Mapping[str, tuple[str, ...]]
    outgoing_boundaries: Mapping[str, tuple[tuple[str, DependencyResolution], ...]]
    defined_children: Mapping[str, tuple[str, ...]]


def _index_graph(graph: SemanticDependencyGraph) -> _CandidateGraphIndex:
    nodes = {node.identity: node for node in graph.nodes}
    incoming_internal: dict[str, set[str]] = {}
    outgoing_boundaries: dict[
        str, set[tuple[str, DependencyResolution]]
    ] = {}
    defined_children: dict[str, set[str]] = {}
    for edge in graph.edges:
        if (
            edge.relation is DependencyRelation.DEFINES
            and edge.resolution is DependencyResolution.INTERNAL
        ):
            defined_children.setdefault(edge.source_identity, set()).add(
                edge.target_identity
            )
        if edge.relation not in _CANDIDATE_RELATIONS:
            continue
        if edge.resolution is DependencyResolution.INTERNAL:
            incoming_internal.setdefault(edge.target_identity, set()).add(
                edge.source_identity
            )
        elif edge.resolution in {
            DependencyResolution.EXTERNAL,
            DependencyResolution.UNRESOLVED,
        }:
            outgoing_boundaries.setdefault(edge.source_identity, set()).add(
                (edge.target_identity, edge.resolution)
            )
    return _CandidateGraphIndex(
        graph_fingerprint=graph.fingerprint,
        nodes=nodes,
        incoming_internal={
            key: tuple(sorted(values))
            for key, values in incoming_internal.items()
        },
        outgoing_boundaries={
            key: tuple(sorted(values, key=lambda item: (item[0], item[1].value)))
            for key, values in outgoing_boundaries.items()
        },
        defined_children={
            key: tuple(sorted(values)) for key, values in defined_children.items()
        },
    )


class CandidateBlockingReason(str, Enum):
    """Why a pair must continue to semantic conflict classification."""

    AFFECTED_SUBGRAPH_OVERLAP = "affected_subgraph_overlap"
    SHARED_UNRESOLVED_BOUNDARY = "shared_unresolved_boundary"
    FAIL_CLOSED_CANDIDATE = "fail_closed_candidate"


class CandidateFailClosedReason(str, Enum):
    """Evidence conditions that prevent candidate pruning."""

    MISSING_GRAPH_ROOT = "missing_graph_root"
    UNKNOWN_CHANGE = "unknown_change"
    UNRESOLVED_BOUNDARY = "unresolved_boundary"
    BOUNDED_TRAVERSAL = "bounded_traversal"


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class SemanticMutationCandidate:
    """One mutation set that may need pairwise semantic conflict analysis."""

    candidate_id: str
    changes: tuple[SemanticChange, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        candidate_id = self.candidate_id.strip()
        if not candidate_id:
            raise ValueError("semantic mutation candidate id must not be empty")
        object.__setattr__(self, "candidate_id", candidate_id)
        changes = tuple(
            item if isinstance(item, SemanticChange) else SemanticChange.from_dict(item)
            for item in self.changes
        )
        if not changes:
            raise ValueError("semantic mutation candidate requires at least one change")
        identities = [item.identity for item in changes]
        if len(set(identities)) != len(identities):
            raise ValueError(
                "semantic mutation candidate change identities must be unique"
            )
        object.__setattr__(
            self, "changes", tuple(sorted(changes, key=lambda item: item.identity))
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "changes": [item.to_dict() for item in self.changes],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SemanticMutationCandidate":
        return cls(
            candidate_id=str(data["candidate_id"]),
            changes=tuple(
                SemanticChange.from_dict(item) for item in data.get("changes") or ()
            ),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class CandidateAffectedSubgraph:
    """Deterministic affected-subgraph projection for one mutation candidate."""

    candidate_id: str
    graph_fingerprint: str
    root_identities: tuple[str, ...]
    affected_identities: tuple[str, ...]
    unresolved_targets: tuple[str, ...] = ()
    external_targets: tuple[str, ...] = ()
    fail_closed_reasons: tuple[CandidateFailClosedReason, ...] = ()
    max_depth: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = CANDIDATE_AFFECTED_SUBGRAPH_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != CANDIDATE_AFFECTED_SUBGRAPH_PROTOCOL:
            raise ValueError(
                f"unsupported affected-subgraph protocol {self.protocol!r}"
            )
        candidate_id = self.candidate_id.strip()
        if not candidate_id:
            raise ValueError("candidate affected subgraph requires an id")
        object.__setattr__(self, "candidate_id", candidate_id)
        if len(self.graph_fingerprint) != 64:
            raise ValueError(
                "candidate affected subgraph graph fingerprint must be SHA-256"
            )
        roots = tuple(sorted({str(item) for item in self.root_identities if str(item)}))
        affected = tuple(
            sorted({str(item) for item in self.affected_identities if str(item)})
        )
        if not roots:
            raise ValueError("candidate affected subgraph requires roots")
        if not set(roots).issubset(affected):
            raise ValueError("candidate affected subgraph must include every root")
        object.__setattr__(self, "root_identities", roots)
        object.__setattr__(self, "affected_identities", affected)
        object.__setattr__(
            self,
            "unresolved_targets",
            tuple(sorted({str(item) for item in self.unresolved_targets if str(item)})),
        )
        object.__setattr__(
            self,
            "external_targets",
            tuple(sorted({str(item) for item in self.external_targets if str(item)})),
        )
        reasons = tuple(
            sorted(
                {CandidateFailClosedReason(item) for item in self.fail_closed_reasons},
                key=lambda item: item.value,
            )
        )
        object.__setattr__(self, "fail_closed_reasons", reasons)
        if self.max_depth is not None and self.max_depth < 0:
            raise ValueError(
                "candidate blocking max_depth must be non-negative or None"
            )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def fail_closed(self) -> bool:
        return bool(self.fail_closed_reasons)

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_dict(include_fingerprint=False))

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload = {
            "protocol": self.protocol,
            "candidate_id": self.candidate_id,
            "graph_fingerprint": self.graph_fingerprint,
            "root_identities": list(self.root_identities),
            "affected_identities": list(self.affected_identities),
            "unresolved_targets": list(self.unresolved_targets),
            "external_targets": list(self.external_targets),
            "fail_closed": self.fail_closed,
            "fail_closed_reasons": [item.value for item in self.fail_closed_reasons],
            "max_depth": self.max_depth,
            "metadata": dict(self.metadata),
        }
        if include_fingerprint:
            return {"fingerprint": self.fingerprint, **payload}
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CandidateAffectedSubgraph":
        result = cls(
            protocol=str(
                data.get("protocol") or CANDIDATE_AFFECTED_SUBGRAPH_PROTOCOL
            ),
            candidate_id=str(data["candidate_id"]),
            graph_fingerprint=str(data["graph_fingerprint"]),
            root_identities=tuple(
                str(item) for item in data.get("root_identities") or ()
            ),
            affected_identities=tuple(
                str(item) for item in data.get("affected_identities") or ()
            ),
            unresolved_targets=tuple(
                str(item) for item in data.get("unresolved_targets") or ()
            ),
            external_targets=tuple(
                str(item) for item in data.get("external_targets") or ()
            ),
            fail_closed_reasons=tuple(
                CandidateFailClosedReason(item)
                for item in data.get("fail_closed_reasons") or ()
            ),
            max_depth=(
                None if data.get("max_depth") is None else int(data["max_depth"])
            ),
            metadata=dict(data.get("metadata") or {}),
        )
        if "fail_closed" in data and bool(data["fail_closed"]) != result.fail_closed:
            raise ValueError("candidate affected subgraph fail_closed mismatch")
        expected = data.get("fingerprint")
        if expected is not None and str(expected) != result.fingerprint:
            raise ValueError("candidate affected subgraph fingerprint mismatch")
        return result


@dataclass(frozen=True, slots=True)
class SemanticConflictCandidatePair:
    """One candidate pair retained for expensive semantic classification."""

    left_id: str
    right_id: str
    reasons: tuple[CandidateBlockingReason, ...]
    shared_identities: tuple[str, ...] = ()
    shared_unresolved_targets: tuple[str, ...] = ()
    fail_closed_candidates: tuple[str, ...] = ()
    protocol: str = CANDIDATE_PAIR_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != CANDIDATE_PAIR_PROTOCOL:
            raise ValueError(f"unsupported candidate pair protocol {self.protocol!r}")
        left = self.left_id.strip()
        right = self.right_id.strip()
        if not left or not right or left == right:
            raise ValueError("candidate pair requires distinct non-empty ids")
        if right < left:
            left, right = right, left
        object.__setattr__(self, "left_id", left)
        object.__setattr__(self, "right_id", right)
        reasons = tuple(
            sorted(
                {CandidateBlockingReason(item) for item in self.reasons},
                key=lambda item: item.value,
            )
        )
        if not reasons:
            raise ValueError("candidate pair requires at least one blocking reason")
        object.__setattr__(self, "reasons", reasons)
        for name in (
            "shared_identities",
            "shared_unresolved_targets",
            "fail_closed_candidates",
        ):
            object.__setattr__(
                self,
                name,
                tuple(sorted({str(item) for item in getattr(self, name) if str(item)})),
            )

    @property
    def key(self) -> tuple[str, str]:
        return (self.left_id, self.right_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "left_id": self.left_id,
            "right_id": self.right_id,
            "reasons": [item.value for item in self.reasons],
            "shared_identities": list(self.shared_identities),
            "shared_unresolved_targets": list(self.shared_unresolved_targets),
            "fail_closed_candidates": list(self.fail_closed_candidates),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SemanticConflictCandidatePair":
        return cls(
            protocol=str(data.get("protocol") or CANDIDATE_PAIR_PROTOCOL),
            left_id=str(data["left_id"]),
            right_id=str(data["right_id"]),
            reasons=tuple(
                CandidateBlockingReason(item) for item in data.get("reasons") or ()
            ),
            shared_identities=tuple(
                str(item) for item in data.get("shared_identities") or ()
            ),
            shared_unresolved_targets=tuple(
                str(item) for item in data.get("shared_unresolved_targets") or ()
            ),
            fail_closed_candidates=tuple(
                str(item) for item in data.get("fail_closed_candidates") or ()
            ),
        )


@dataclass(frozen=True, slots=True)
class AffectedSubgraphCandidateBlockingPlan:
    """Immutable conservative pair-selection plan for semantic conflict analysis."""

    graph_fingerprint: str
    subgraphs: tuple[CandidateAffectedSubgraph, ...]
    pairs: tuple[SemanticConflictCandidatePair, ...]
    max_depth: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = AFFECTED_SUBGRAPH_CANDIDATE_BLOCKING_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != AFFECTED_SUBGRAPH_CANDIDATE_BLOCKING_PROTOCOL:
            raise ValueError(
                f"unsupported candidate blocking protocol {self.protocol!r}"
            )
        if len(self.graph_fingerprint) != 64:
            raise ValueError("candidate blocking graph fingerprint must be SHA-256")
        subgraphs = tuple(sorted(self.subgraphs, key=lambda item: item.candidate_id))
        ids = [item.candidate_id for item in subgraphs]
        if len(set(ids)) != len(ids):
            raise ValueError("candidate blocking candidate ids must be unique")
        if any(item.graph_fingerprint != self.graph_fingerprint for item in subgraphs):
            raise ValueError("candidate blocking subgraph graph mismatch")
        object.__setattr__(self, "subgraphs", subgraphs)
        pairs = tuple(sorted(self.pairs, key=lambda item: item.key))
        if len({item.key for item in pairs}) != len(pairs):
            raise ValueError("candidate blocking pair keys must be unique")
        known = set(ids)
        if any(
            item.left_id not in known or item.right_id not in known for item in pairs
        ):
            raise ValueError("candidate blocking pair references unknown candidate")
        object.__setattr__(self, "pairs", pairs)
        if self.max_depth is not None and self.max_depth < 0:
            raise ValueError(
                "candidate blocking max_depth must be non-negative or None"
            )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def total_pair_count(self) -> int:
        count = len(self.subgraphs)
        return count * (count - 1) // 2

    @property
    def selected_pair_count(self) -> int:
        return len(self.pairs)

    @property
    def pruned_pair_count(self) -> int:
        return self.total_pair_count - self.selected_pair_count

    @property
    def pruning_ratio(self) -> float:
        if self.total_pair_count == 0:
            return 0.0
        return self.pruned_pair_count / self.total_pair_count

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_dict(include_fingerprint=False))

    def candidates_for(self, candidate_id: str) -> tuple[str, ...]:
        value = candidate_id.strip()
        if value not in {item.candidate_id for item in self.subgraphs}:
            raise KeyError(value)
        result = {
            item.right_id if item.left_id == value else item.left_id
            for item in self.pairs
            if value in item.key
        }
        return tuple(sorted(result))

    def pair(self, left_id: str, right_id: str) -> SemanticConflictCandidatePair | None:
        key = tuple(sorted((left_id.strip(), right_id.strip())))
        if len(key) != 2 or key[0] == key[1]:
            raise ValueError("candidate pair lookup requires distinct ids")
        return next((item for item in self.pairs if item.key == key), None)

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload = {
            "protocol": self.protocol,
            "graph_fingerprint": self.graph_fingerprint,
            "max_depth": self.max_depth,
            "subgraphs": [item.to_dict() for item in self.subgraphs],
            "pairs": [item.to_dict() for item in self.pairs],
            "metrics": {
                "candidate_count": len(self.subgraphs),
                "total_pair_count": self.total_pair_count,
                "selected_pair_count": self.selected_pair_count,
                "pruned_pair_count": self.pruned_pair_count,
                "pruning_ratio": self.pruning_ratio,
                "fail_closed_candidate_count": sum(
                    1 for item in self.subgraphs if item.fail_closed
                ),
            },
            "metadata": dict(self.metadata),
        }
        if include_fingerprint:
            return {"fingerprint": self.fingerprint, **payload}
        return payload

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any]
    ) -> "AffectedSubgraphCandidateBlockingPlan":
        result = cls(
            protocol=str(
                data.get("protocol") or AFFECTED_SUBGRAPH_CANDIDATE_BLOCKING_PROTOCOL
            ),
            graph_fingerprint=str(data["graph_fingerprint"]),
            max_depth=(
                None if data.get("max_depth") is None else int(data["max_depth"])
            ),
            subgraphs=tuple(
                CandidateAffectedSubgraph.from_dict(item)
                for item in data.get("subgraphs") or ()
            ),
            pairs=tuple(
                SemanticConflictCandidatePair.from_dict(item)
                for item in data.get("pairs") or ()
            ),
            metadata=dict(data.get("metadata") or {}),
        )
        metrics = data.get("metrics")
        if isinstance(metrics, Mapping):
            expected_metrics = result.to_dict(include_fingerprint=False)["metrics"]
            for key, value in expected_metrics.items():
                if key in metrics and metrics[key] != value:
                    raise ValueError(f"candidate blocking metric mismatch: {key}")
        expected = data.get("fingerprint")
        if expected is not None and str(expected) != result.fingerprint:
            raise ValueError("candidate blocking fingerprint mismatch")
        return result


def _normalize_candidates(
    candidates: Sequence[SemanticMutationCandidate | Mapping[str, Any]],
) -> tuple[SemanticMutationCandidate, ...]:
    normalized = tuple(
        item
        if isinstance(item, SemanticMutationCandidate)
        else SemanticMutationCandidate.from_dict(item)
        for item in candidates
    )
    ids = [item.candidate_id for item in normalized]
    if len(set(ids)) != len(ids):
        raise ValueError("semantic mutation candidate ids must be unique")
    return tuple(sorted(normalized, key=lambda item: item.candidate_id))


def _affected_subgraph(
    index: _CandidateGraphIndex,
    candidate: SemanticMutationCandidate,
    *,
    max_depth: int | None,
) -> CandidateAffectedSubgraph:
    roots = tuple(item.identity for item in candidate.changes)
    missing = tuple(identity for identity in roots if identity not in index.nodes)
    fail_reasons: set[CandidateFailClosedReason] = set()
    if missing:
        fail_reasons.add(CandidateFailClosedReason.MISSING_GRAPH_ROOT)
    if any(item.kind is SemanticChangeKind.UNKNOWN for item in candidate.changes):
        fail_reasons.add(CandidateFailClosedReason.UNKNOWN_CHANGE)
    if max_depth is not None:
        fail_reasons.add(CandidateFailClosedReason.BOUNDED_TRAVERSAL)

    affected = set(roots)
    unresolved: set[str] = set()
    external: set[str] = set()
    queue: list[tuple[str, int]] = [
        (identity, 0) for identity in roots if identity in index.nodes
    ]
    seen = {identity for identity, _ in queue}

    while queue:
        current, depth = queue.pop(0)
        for target, resolution in index.outgoing_boundaries.get(current, ()):
            if resolution is DependencyResolution.UNRESOLVED:
                unresolved.add(target)
            elif resolution is DependencyResolution.EXTERNAL:
                external.add(target)

        if max_depth is not None and depth >= max_depth:
            continue
        for neighbor in index.incoming_internal.get(current, ()):
            affected.add(neighbor)
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append((neighbor, depth + 1))

        # A file/document mutation covers the semantic resources it defines. Avoid
        # traversing this ownership edge from symbol roots, which would otherwise
        # collapse every symbol in one file into a single candidate bucket.
        node = index.nodes.get(current)
        if node is not None and current in roots and node.resource.kind.value in {
            "file",
            "document",
        }:
            for child in index.defined_children.get(current, ()):
                affected.add(child)
                if child not in seen:
                    seen.add(child)
                    queue.append((child, depth + 1))

    if unresolved:
        fail_reasons.add(CandidateFailClosedReason.UNRESOLVED_BOUNDARY)

    return CandidateAffectedSubgraph(
        candidate_id=candidate.candidate_id,
        graph_fingerprint=index.graph_fingerprint,
        root_identities=roots,
        affected_identities=tuple(affected),
        unresolved_targets=tuple(unresolved),
        external_targets=tuple(external),
        fail_closed_reasons=tuple(fail_reasons),
        max_depth=max_depth,
        metadata={
            "candidate_relations": sorted(item.value for item in _CANDIDATE_RELATIONS),
            "missing_graph_roots": list(missing),
            "change_kinds": sorted({item.kind.value for item in candidate.changes}),
        },
    )


def build_affected_subgraph_candidate_blocking(
    graph: SemanticDependencyGraph,
    candidates: Sequence[SemanticMutationCandidate | Mapping[str, Any]],
    *,
    max_depth: int | None = None,
) -> AffectedSubgraphCandidateBlockingPlan:
    """Build a conservative conflict-candidate plan without pairwise graph walks.

    The implementation indexes affected resource identities once and derives candidate
    pairs from inverted buckets. Incomplete candidates are paired with every other
    candidate. Therefore absence from ``plan.pairs`` is a proof obligation: both sides
    had complete unbounded graph evidence and disjoint affected subgraphs.
    """

    if max_depth is not None and max_depth < 0:
        raise ValueError("candidate blocking max_depth must be non-negative or None")
    normalized = _normalize_candidates(candidates)
    index = _index_graph(graph)
    subgraphs = tuple(
        _affected_subgraph(index, candidate, max_depth=max_depth)
        for candidate in normalized
    )
    ids = tuple(item.candidate_id for item in subgraphs)

    affected_index: dict[str, set[str]] = {}
    unresolved_index: dict[str, set[str]] = {}
    by_id = {item.candidate_id: item for item in subgraphs}
    for subgraph in subgraphs:
        for identity in subgraph.affected_identities:
            affected_index.setdefault(identity, set()).add(subgraph.candidate_id)
        for target in subgraph.unresolved_targets:
            unresolved_index.setdefault(target, set()).add(subgraph.candidate_id)

    pair_reasons: dict[tuple[str, str], set[CandidateBlockingReason]] = {}
    shared_identities: dict[tuple[str, str], set[str]] = {}
    shared_unresolved: dict[tuple[str, str], set[str]] = {}
    fail_closed_ids: dict[tuple[str, str], set[str]] = {}

    def add_pair(
        left: str,
        right: str,
        reason: CandidateBlockingReason,
        *,
        identity: str | None = None,
        unresolved_target: str | None = None,
        fail_closed_candidate: str | None = None,
    ) -> None:
        key = tuple(sorted((left, right)))
        if key[0] == key[1]:
            return
        pair_reasons.setdefault(key, set()).add(reason)
        if identity is not None:
            shared_identities.setdefault(key, set()).add(identity)
        if unresolved_target is not None:
            shared_unresolved.setdefault(key, set()).add(unresolved_target)
        if fail_closed_candidate is not None:
            fail_closed_ids.setdefault(key, set()).add(fail_closed_candidate)

    for identity, owners in affected_index.items():
        for left, right in combinations(sorted(owners), 2):
            add_pair(
                left,
                right,
                CandidateBlockingReason.AFFECTED_SUBGRAPH_OVERLAP,
                identity=identity,
            )

    for target, owners in unresolved_index.items():
        for left, right in combinations(sorted(owners), 2):
            add_pair(
                left,
                right,
                CandidateBlockingReason.SHARED_UNRESOLVED_BOUNDARY,
                unresolved_target=target,
            )

    for candidate_id in ids:
        if not by_id[candidate_id].fail_closed:
            continue
        for other_id in ids:
            if other_id == candidate_id:
                continue
            add_pair(
                candidate_id,
                other_id,
                CandidateBlockingReason.FAIL_CLOSED_CANDIDATE,
                fail_closed_candidate=candidate_id,
            )

    pairs = tuple(
        SemanticConflictCandidatePair(
            left_id=key[0],
            right_id=key[1],
            reasons=tuple(pair_reasons[key]),
            shared_identities=tuple(shared_identities.get(key, ())),
            shared_unresolved_targets=tuple(shared_unresolved.get(key, ())),
            fail_closed_candidates=tuple(fail_closed_ids.get(key, ())),
        )
        for key in sorted(pair_reasons)
    )

    return AffectedSubgraphCandidateBlockingPlan(
        graph_fingerprint=index.graph_fingerprint,
        subgraphs=subgraphs,
        pairs=pairs,
        max_depth=max_depth,
        metadata={
            "algorithm": "inverted-affected-subgraph-index-v1",
            "graph_index_builds": 1,
            "pairwise_graph_walks": 0,
            "fail_closed_policy": (
                "missing-root-or-unknown-change-or-unresolved-boundary-"
                "or-bounded-traversal"
            ),
        },
    )
