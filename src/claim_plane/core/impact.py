"""Deterministic semantic impact and contract propagation.

The impact layer consumes Semantic Dependency Graph v2 and turns a bounded set of
known repository mutations into an explicit downstream impact report. It does not
make an admission decision and never executes repository code. Contract-sensitive
changes propagate through callers, type users, inheritance, importers, shared-state
consumers, and tests; implementation-only changes use a narrower dependency surface.
Unknown or external dependency boundaries stay visible instead of being treated as
proof of independence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from claim_plane.core.dependency_graph import (
    DependencyEdge,
    DependencyNode,
    DependencyRelation,
    DependencyResolution,
    SemanticDependencyGraph,
)
from claim_plane.core.models import ResourceKind, ResourceRef
from claim_plane.core.resource_ir import SemanticResource, normalize_resource_ref

SEMANTIC_IMPACT_PROTOCOL = "claim-plane.semantic-impact.v1"


class SemanticChangeKind(str, Enum):
    """How a mutated semantic resource changed."""

    IMPLEMENTATION = "implementation"
    CONTRACT = "contract"
    STATE = "state"
    STRUCTURE = "structure"
    ADDED = "added"
    REMOVED = "removed"
    UNKNOWN = "unknown"


_CONTRACT_RELATIONS = frozenset(
    {
        DependencyRelation.IMPORTS,
        DependencyRelation.CALLS,
        DependencyRelation.READS,
        DependencyRelation.WRITES,
        DependencyRelation.INHERITS,
        DependencyRelation.TYPES,
        DependencyRelation.TESTS,
    }
)
_IMPLEMENTATION_RELATIONS = frozenset(
    {
        DependencyRelation.CALLS,
        DependencyRelation.READS,
        DependencyRelation.TESTS,
    }
)
_STATE_RELATIONS = frozenset(
    {
        DependencyRelation.CALLS,
        DependencyRelation.READS,
        DependencyRelation.WRITES,
        DependencyRelation.TESTS,
    }
)
_STRUCTURE_RELATIONS = _CONTRACT_RELATIONS


_RELATION_PREFERENCE = {
    DependencyRelation.TESTS: 0,
    DependencyRelation.WRITES: 1,
    DependencyRelation.TYPES: 2,
    DependencyRelation.INHERITS: 3,
    DependencyRelation.CALLS: 4,
    DependencyRelation.READS: 5,
    DependencyRelation.IMPORTS: 6,
    DependencyRelation.PUBLIC_API: 7,
    DependencyRelation.DEFINES: 8,
}


def _path_preference(path: "ImpactPath") -> tuple[object, ...]:
    return (
        path.distance,
        tuple(_RELATION_PREFERENCE[item] for item in path.relations),
        path.identities,
        tuple(item.value for item in path.relations),
    )


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _resource_contract(resource: SemanticResource) -> SemanticResource | None:
    """Project one symbol signature onto a stable contract resource coordinate."""

    if resource.kind is not ResourceKind.SYMBOL or resource.signature is None:
        return None
    qualified = resource.qualified_name or resource.identifier
    metadata = {
        "language": resource.language,
        "path": resource.path,
        "qualified_identifier": qualified,
        "subject_qualified_identifier": qualified,
        "parent_identity": resource.identity,
        "source_resource_identity": resource.identity,
    }
    metadata = {key: value for key, value in metadata.items() if value is not None}
    return normalize_resource_ref(
        ResourceRef(
            ResourceKind.CONTRACT,
            resource.identifier,
            signature=resource.signature,
            concept_id=resource.concept_id,
            subject_concept_id=resource.concept_id,
            metadata=metadata,
        )
    )


def project_contract_resource(resource: SemanticResource) -> SemanticResource | None:
    """Return a stable contract coordinate for a signed symbol, if available."""

    return _resource_contract(resource)


@dataclass(frozen=True, slots=True)
class SemanticChange:
    """One known semantic mutation used as an impact root."""

    identity: str
    kind: SemanticChangeKind
    before_resource: SemanticResource | None = None
    after_resource: SemanticResource | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        identity = self.identity.strip()
        if not identity:
            raise ValueError("semantic change identity must not be empty")
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "kind", SemanticChangeKind(self.kind))
        for name in ("before_resource", "after_resource"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, SemanticResource):
                object.__setattr__(
                    self,
                    name,
                    SemanticResource.from_dict(value),  # type: ignore[arg-type]
                )
        if self.before_resource is not None and self.before_resource.identity != identity:
            raise ValueError("before_resource identity must match semantic change")
        if self.after_resource is not None and self.after_resource.identity != identity:
            raise ValueError("after_resource identity must match semantic change")
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def resource(self) -> SemanticResource | None:
        return self.after_resource or self.before_resource

    @property
    def contract_resource(self) -> SemanticResource | None:
        if self.kind is not SemanticChangeKind.CONTRACT:
            return None
        resource = self.resource
        return _resource_contract(resource) if resource is not None else None

    def to_dict(self) -> dict[str, Any]:
        contract = self.contract_resource
        return {
            "identity": self.identity,
            "kind": self.kind.value,
            "before_resource": (
                self.before_resource.to_dict() if self.before_resource is not None else None
            ),
            "after_resource": (
                self.after_resource.to_dict() if self.after_resource is not None else None
            ),
            "contract_resource": contract.to_dict() if contract is not None else None,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SemanticChange":
        return cls(
            identity=str(data["identity"]),
            kind=SemanticChangeKind(data["kind"]),
            before_resource=(
                SemanticResource.from_dict(data["before_resource"])
                if data.get("before_resource") is not None
                else None
            ),
            after_resource=(
                SemanticResource.from_dict(data["after_resource"])
                if data.get("after_resource") is not None
                else None
            ),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class ImpactPath:
    """One shortest deterministic dependency path from a change to an impact."""

    root_identity: str
    target_identity: str
    identities: tuple[str, ...]
    relations: tuple[DependencyRelation, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "identities", tuple(str(item) for item in self.identities))
        object.__setattr__(
            self,
            "relations",
            tuple(DependencyRelation(item) for item in self.relations),
        )
        if not self.identities:
            raise ValueError("impact path must contain at least one identity")
        if self.identities[0] != self.root_identity:
            raise ValueError("impact path must begin at root_identity")
        if self.identities[-1] != self.target_identity:
            raise ValueError("impact path must end at target_identity")
        if len(self.relations) + 1 != len(self.identities):
            raise ValueError("impact path relation count does not match identities")

    @property
    def distance(self) -> int:
        return len(self.relations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_identity": self.root_identity,
            "target_identity": self.target_identity,
            "identities": list(self.identities),
            "relations": [item.value for item in self.relations],
            "distance": self.distance,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ImpactPath":
        path = cls(
            root_identity=str(data["root_identity"]),
            target_identity=str(data["target_identity"]),
            identities=tuple(str(item) for item in data.get("identities") or ()),
            relations=tuple(
                DependencyRelation(item) for item in data.get("relations") or ()
            ),
        )
        supplied = data.get("distance")
        if supplied is not None and int(supplied) != path.distance:
            raise ValueError("impact path distance mismatch")
        return path


@dataclass(frozen=True, slots=True)
class ImpactedResource:
    """One repository resource reached from one or more semantic changes."""

    node: DependencyNode
    root_identities: tuple[str, ...]
    paths: tuple[ImpactPath, ...]
    contract_sensitive: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.node, DependencyNode):
            object.__setattr__(
                self,
                "node",
                DependencyNode.from_dict(self.node),  # type: ignore[arg-type]
            )
        roots = tuple(sorted({str(item) for item in self.root_identities}))
        paths = tuple(
            sorted(
                [
                    item
                    if isinstance(item, ImpactPath)
                    else ImpactPath.from_dict(item)  # type: ignore[arg-type]
                    for item in self.paths
                ],
                key=lambda item: (
                    item.root_identity,
                    item.distance,
                    item.identities,
                    tuple(relation.value for relation in item.relations),
                ),
            )
        )
        if roots != tuple(sorted({path.root_identity for path in paths})):
            raise ValueError("impacted resource roots must match impact paths")
        if any(path.target_identity != self.node.identity for path in paths):
            raise ValueError("impact paths must end at impacted resource")
        object.__setattr__(self, "root_identities", roots)
        object.__setattr__(self, "paths", paths)

    @property
    def min_distance(self) -> int:
        return min((path.distance for path in self.paths), default=0)

    @property
    def relations(self) -> tuple[DependencyRelation, ...]:
        values = {relation for path in self.paths for relation in path.relations}
        return tuple(sorted(values, key=lambda item: item.value))

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node.to_dict(),
            "root_identities": list(self.root_identities),
            "paths": [item.to_dict() for item in self.paths],
            "min_distance": self.min_distance,
            "relations": [item.value for item in self.relations],
            "contract_sensitive": self.contract_sensitive,
            "public": self.node.public,
            "test": self.node.test,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ImpactedResource":
        result = cls(
            node=DependencyNode.from_dict(data["node"]),
            root_identities=tuple(str(item) for item in data.get("root_identities") or ()),
            paths=tuple(ImpactPath.from_dict(item) for item in data.get("paths") or ()),
            contract_sensitive=bool(data.get("contract_sensitive", False)),
        )
        supplied_distance = data.get("min_distance")
        if supplied_distance is not None and int(supplied_distance) != result.min_distance:
            raise ValueError("impacted resource min_distance mismatch")
        supplied_relations = tuple(
            sorted(str(item) for item in data.get("relations") or ())
        )
        if supplied_relations and supplied_relations != tuple(
            item.value for item in result.relations
        ):
            raise ValueError("impacted resource relations mismatch")
        if "public" in data and bool(data["public"]) != result.node.public:
            raise ValueError("impacted resource public flag mismatch")
        if "test" in data and bool(data["test"]) != result.node.test:
            raise ValueError("impacted resource test flag mismatch")
        return result


@dataclass(frozen=True, slots=True)
class ImpactBoundary:
    """A non-internal dependency touching an impacted resource."""

    source_identity: str
    target_identity: str
    relation: DependencyRelation
    resolution: DependencyResolution
    root_identities: tuple[str, ...]
    locations: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "relation", DependencyRelation(self.relation))
        object.__setattr__(self, "resolution", DependencyResolution(self.resolution))
        if self.resolution is DependencyResolution.INTERNAL:
            raise ValueError("impact boundary must be external or unresolved")
        object.__setattr__(
            self,
            "root_identities",
            tuple(sorted({str(item) for item in self.root_identities})),
        )
        object.__setattr__(
            self,
            "locations",
            tuple(sorted({int(item) for item in self.locations})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_identity": self.source_identity,
            "target_identity": self.target_identity,
            "relation": self.relation.value,
            "resolution": self.resolution.value,
            "root_identities": list(self.root_identities),
            "locations": list(self.locations),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ImpactBoundary":
        return cls(
            source_identity=str(data["source_identity"]),
            target_identity=str(data["target_identity"]),
            relation=DependencyRelation(data["relation"]),
            resolution=DependencyResolution(data["resolution"]),
            root_identities=tuple(str(item) for item in data.get("root_identities") or ()),
            locations=tuple(int(item) for item in data.get("locations") or ()),
        )


@dataclass(frozen=True, slots=True)
class SemanticImpactReport:
    """Immutable downstream impact evidence for a semantic graph snapshot."""

    graph_fingerprint: str
    changes: tuple[SemanticChange, ...]
    impacted: tuple[ImpactedResource, ...]
    boundaries: tuple[ImpactBoundary, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SEMANTIC_IMPACT_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SEMANTIC_IMPACT_PROTOCOL:
            raise ValueError(f"unsupported semantic impact protocol {self.protocol!r}")
        if len(self.graph_fingerprint) != 64:
            raise ValueError("semantic impact graph fingerprint must be SHA-256")
        changes = tuple(
            sorted(
                [
                    item
                    if isinstance(item, SemanticChange)
                    else SemanticChange.from_dict(item)  # type: ignore[arg-type]
                    for item in self.changes
                ],
                key=lambda item: item.identity,
            )
        )
        impacted = tuple(
            sorted(
                [
                    item
                    if isinstance(item, ImpactedResource)
                    else ImpactedResource.from_dict(item)  # type: ignore[arg-type]
                    for item in self.impacted
                ],
                key=lambda item: item.node.identity,
            )
        )
        boundaries = tuple(
            sorted(
                [
                    item
                    if isinstance(item, ImpactBoundary)
                    else ImpactBoundary.from_dict(item)  # type: ignore[arg-type]
                    for item in self.boundaries
                ],
                key=lambda item: (
                    item.source_identity,
                    item.target_identity,
                    item.relation.value,
                    item.resolution.value,
                ),
            )
        )
        object.__setattr__(self, "changes", changes)
        object.__setattr__(self, "impacted", impacted)
        object.__setattr__(self, "boundaries", boundaries)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def fingerprint(self) -> str:
        return _impact_fingerprint(self)

    @property
    def public_impacts(self) -> tuple[ImpactedResource, ...]:
        return tuple(item for item in self.impacted if item.node.public)

    @property
    def test_impacts(self) -> tuple[ImpactedResource, ...]:
        return tuple(item for item in self.impacted if item.node.test)

    def impacted_resource(self, identity: str) -> ImpactedResource | None:
        return next((item for item in self.impacted if item.node.identity == identity), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "fingerprint": self.fingerprint,
            "graph_fingerprint": self.graph_fingerprint,
            "changes": [item.to_dict() for item in self.changes],
            "impacted": [item.to_dict() for item in self.impacted],
            "boundaries": [item.to_dict() for item in self.boundaries],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SemanticImpactReport":
        report = cls(
            protocol=str(data.get("protocol") or SEMANTIC_IMPACT_PROTOCOL),
            graph_fingerprint=str(data["graph_fingerprint"]),
            changes=tuple(
                SemanticChange.from_dict(item) for item in data.get("changes") or ()
            ),
            impacted=tuple(
                ImpactedResource.from_dict(item) for item in data.get("impacted") or ()
            ),
            boundaries=tuple(
                ImpactBoundary.from_dict(item) for item in data.get("boundaries") or ()
            ),
            metadata=dict(data.get("metadata") or {}),
        )
        supplied = data.get("fingerprint")
        if supplied is not None and str(supplied) != report.fingerprint:
            raise ValueError("semantic impact fingerprint mismatch")
        return report


def _impact_fingerprint(report: SemanticImpactReport) -> str:
    payload = {
        "protocol": report.protocol,
        "graph_fingerprint": report.graph_fingerprint,
        "changes": [item.to_dict() for item in report.changes],
        "impacted": [item.to_dict() for item in report.impacted],
        "boundaries": [item.to_dict() for item in report.boundaries],
        "metadata": dict(report.metadata),
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _relations_for_change(kind: SemanticChangeKind) -> frozenset[DependencyRelation]:
    if kind is SemanticChangeKind.IMPLEMENTATION:
        return _IMPLEMENTATION_RELATIONS
    if kind is SemanticChangeKind.STATE:
        return _STATE_RELATIONS
    if kind is SemanticChangeKind.CONTRACT:
        return _CONTRACT_RELATIONS
    return _STRUCTURE_RELATIONS


def _incident_signature(graph: SemanticDependencyGraph, identity: str) -> tuple[tuple[str, ...], ...]:
    return tuple(
        sorted(
            (
                edge.source_identity,
                edge.target_identity,
                edge.relation.value,
                edge.resolution.value,
            )
            for edge in graph.edges
            if edge.source_identity == identity or edge.target_identity == identity
        )
    )


def compare_semantic_graphs(
    before: SemanticDependencyGraph,
    after: SemanticDependencyGraph,
    *,
    changed_identities: Iterable[str] = (),
) -> tuple[SemanticChange, ...]:
    """Classify stable semantic resources across two graph snapshots.

    ``changed_identities`` is the authoritative mutation surface from Git hunk/owner
    mapping. It lets the comparison distinguish an implementation edit even when the
    AST dependency shape and callable signature are unchanged. Independently visible
    graph additions/removals, signature changes, and relationship changes are detected
    without requiring the explicit set.
    """

    explicit = {str(item).strip() for item in changed_identities if str(item).strip()}
    before_nodes = {node.identity: node for node in before.nodes}
    after_nodes = {node.identity: node for node in after.nodes}
    candidates = set(explicit)
    candidates.update(before_nodes.keys() ^ after_nodes.keys())
    for identity in before_nodes.keys() & after_nodes.keys():
        left = before_nodes[identity]
        right = after_nodes[identity]
        if left.resource.signature != right.resource.signature:
            candidates.add(identity)
            continue
        if (left.public, left.test, left.external) != (
            right.public,
            right.test,
            right.external,
        ):
            candidates.add(identity)
            continue
        if _incident_signature(before, identity) != _incident_signature(after, identity):
            candidates.add(identity)

    changes: list[SemanticChange] = []
    for identity in sorted(candidates):
        left = before_nodes.get(identity)
        right = after_nodes.get(identity)
        before_resource = left.resource if left is not None else None
        after_resource = right.resource if right is not None else None
        if left is None and right is not None:
            kind = SemanticChangeKind.ADDED
        elif left is not None and right is None:
            kind = SemanticChangeKind.REMOVED
        elif left is None and right is None:
            raise ValueError(f"changed semantic identity is absent from both graphs: {identity}")
        elif before_resource.signature != after_resource.signature:  # type: ignore[union-attr]
            kind = SemanticChangeKind.CONTRACT
        elif bool((right or left).metadata.get("symbol_kind") == "state"):
            kind = SemanticChangeKind.STATE
        elif _incident_signature(before, identity) != _incident_signature(after, identity):
            kind = SemanticChangeKind.STRUCTURE
        else:
            kind = SemanticChangeKind.IMPLEMENTATION
        changes.append(
            SemanticChange(
                identity=identity,
                kind=kind,
                before_resource=before_resource,
                after_resource=after_resource,
                metadata={
                    "explicit_mutation": identity in explicit,
                    "before_graph": before.fingerprint,
                    "after_graph": after.fingerprint,
                },
            )
        )
    return tuple(changes)


def _change_node(graph: SemanticDependencyGraph, change: SemanticChange) -> DependencyNode:
    existing = graph.node(change.identity)
    if existing is not None:
        return existing
    resource = change.resource
    if resource is None:
        raise ValueError(f"semantic change has no resource evidence: {change.identity}")
    return DependencyNode(
        resource=resource,
        public=bool(resource.metadata.get("is_public", False)),
        test=False,
        external=False,
        metadata={"synthetic_change_root": True},
    )


def _collect_boundaries(
    graph: SemanticDependencyGraph,
    *,
    source_identity: str,
    root_identity: str,
    relations: frozenset[DependencyRelation],
) -> tuple[ImpactBoundary, ...]:
    values: list[ImpactBoundary] = []
    for edge in graph.outgoing(source_identity, relations=relations):
        if edge.resolution is DependencyResolution.INTERNAL:
            continue
        values.append(
            ImpactBoundary(
                source_identity=edge.source_identity,
                target_identity=edge.target_identity,
                relation=edge.relation,
                resolution=edge.resolution,
                root_identities=(root_identity,),
                locations=edge.locations,
            )
        )
    return tuple(values)


def analyze_semantic_impact(
    graph: SemanticDependencyGraph,
    changes: Sequence[SemanticChange],
    *,
    max_depth: int | None = None,
) -> SemanticImpactReport:
    """Propagate known mutations to semantic dependents in a graph snapshot."""

    if max_depth is not None and max_depth < 0:
        raise ValueError("max_depth must be non-negative or None")
    normalized = tuple(
        item if isinstance(item, SemanticChange) else SemanticChange.from_dict(item)  # type: ignore[arg-type]
        for item in changes
    )
    if len({item.identity for item in normalized}) != len(normalized):
        raise ValueError("semantic impact changes must have unique identities")

    paths_by_target: dict[str, dict[str, ImpactPath]] = {}
    nodes: dict[str, DependencyNode] = {}
    contract_roots = {
        item.identity for item in normalized if item.kind is SemanticChangeKind.CONTRACT
    }
    contract_sensitive_targets: set[str] = set()
    boundary_groups: dict[
        tuple[str, str, str, str], tuple[ImpactBoundary, set[str]]
    ] = {}

    for change in sorted(normalized, key=lambda item: item.identity):
        root_node = _change_node(graph, change)
        nodes[root_node.identity] = root_node
        root_path = ImpactPath(
            root_identity=change.identity,
            target_identity=change.identity,
            identities=(change.identity,),
            relations=(),
        )
        paths_by_target.setdefault(change.identity, {})[change.identity] = root_path
        if change.identity in contract_roots:
            contract_sensitive_targets.add(change.identity)

        allowed = _relations_for_change(change.kind)
        queue: list[ImpactPath] = [root_path]
        best_distance: dict[str, int] = {change.identity: 0}
        while queue:
            current_path = queue.pop(0)
            current = current_path.target_identity
            for boundary in _collect_boundaries(
                graph,
                source_identity=current,
                root_identity=change.identity,
                relations=allowed,
            ):
                key = (
                    boundary.source_identity,
                    boundary.target_identity,
                    boundary.relation.value,
                    boundary.resolution.value,
                )
                existing = boundary_groups.get(key)
                if existing is None:
                    boundary_groups[key] = (boundary, {change.identity})
                else:
                    existing[1].add(change.identity)

            if max_depth is not None and current_path.distance >= max_depth:
                continue
            incoming = sorted(
                graph.incoming(current, relations=allowed),
                key=lambda edge: (
                    edge.source_identity,
                    edge.relation.value,
                    edge.target_identity,
                ),
            )
            for edge in incoming:
                if edge.resolution is not DependencyResolution.INTERNAL:
                    continue
                target = edge.source_identity
                distance = current_path.distance + 1
                previous = best_distance.get(target)
                if previous is not None and previous < distance:
                    continue
                path = ImpactPath(
                    root_identity=change.identity,
                    target_identity=target,
                    identities=(*current_path.identities, target),
                    relations=(*current_path.relations, edge.relation),
                )
                stored = paths_by_target.setdefault(target, {}).get(change.identity)
                if stored is None or _path_preference(path) < _path_preference(stored):
                    paths_by_target[target][change.identity] = path
                if previous is None or distance < previous:
                    best_distance[target] = distance
                    queue.append(path)
                node = graph.node(target)
                if node is not None:
                    nodes[target] = node
                if change.identity in contract_roots:
                    contract_sensitive_targets.add(target)

    impacted: list[ImpactedResource] = []
    for identity in sorted(paths_by_target):
        node = nodes.get(identity) or graph.node(identity)
        if node is None:
            # Only a removed change root can be absent from the selected graph.
            change = next(item for item in normalized if item.identity == identity)
            node = _change_node(graph, change)
        root_paths = tuple(paths_by_target[identity][key] for key in sorted(paths_by_target[identity]))
        impacted.append(
            ImpactedResource(
                node=node,
                root_identities=tuple(path.root_identity for path in root_paths),
                paths=root_paths,
                contract_sensitive=identity in contract_sensitive_targets,
            )
        )

    boundaries = tuple(
        ImpactBoundary(
            source_identity=boundary.source_identity,
            target_identity=boundary.target_identity,
            relation=boundary.relation,
            resolution=boundary.resolution,
            root_identities=tuple(sorted(roots)),
            locations=boundary.locations,
        )
        for boundary, roots in boundary_groups.values()
    )
    return SemanticImpactReport(
        graph_fingerprint=graph.fingerprint,
        changes=normalized,
        impacted=tuple(impacted),
        boundaries=boundaries,
        metadata={
            "direction": "reverse_dependency",
            "max_depth": max_depth,
            "contract_relations": sorted(item.value for item in _CONTRACT_RELATIONS),
            "implementation_relations": sorted(
                item.value for item in _IMPLEMENTATION_RELATIONS
            ),
            "state_relations": sorted(item.value for item in _STATE_RELATIONS),
        },
    )


def analyze_graph_change_impact(
    before: SemanticDependencyGraph,
    after: SemanticDependencyGraph,
    *,
    changed_identities: Iterable[str] = (),
    max_depth: int | None = None,
) -> SemanticImpactReport:
    """Compare graph snapshots and propagate the resulting semantic changes.

    Removed resources are propagated against the before graph because their consumers
    no longer have an after-graph target. Mixed add/remove changes are represented in
    one report by merging the deterministic shortest paths from both snapshots.
    """

    changes = compare_semantic_graphs(
        before, after, changed_identities=changed_identities
    )
    removed = tuple(item for item in changes if item.kind is SemanticChangeKind.REMOVED)
    current = tuple(item for item in changes if item.kind is not SemanticChangeKind.REMOVED)
    reports: list[SemanticImpactReport] = []
    if current:
        reports.append(analyze_semantic_impact(after, current, max_depth=max_depth))
    if removed:
        reports.append(analyze_semantic_impact(before, removed, max_depth=max_depth))
    if not reports:
        return SemanticImpactReport(
            graph_fingerprint=after.fingerprint,
            changes=(),
            impacted=(),
            metadata={
                "direction": "reverse_dependency",
                "max_depth": max_depth,
                "comparison_before_graph": before.fingerprint,
                "comparison_after_graph": after.fingerprint,
            },
        )
    if len(reports) == 1 and not removed:
        report = reports[0]
        return SemanticImpactReport(
            graph_fingerprint=after.fingerprint,
            changes=changes,
            impacted=report.impacted,
            boundaries=report.boundaries,
            metadata={
                **report.metadata,
                "comparison_before_graph": before.fingerprint,
                "comparison_after_graph": after.fingerprint,
            },
        )

    # Merge current and removed impact evidence. The after fingerprint remains the
    # report anchor while removed paths explicitly carry their before resources.
    path_groups: dict[str, dict[str, ImpactPath]] = {}
    node_by_identity: dict[str, DependencyNode] = {}
    contract_sensitive: set[str] = set()
    boundary_groups: dict[
        tuple[str, str, str, str], tuple[ImpactBoundary, set[str]]
    ] = {}
    for report in reports:
        for item in report.impacted:
            node_by_identity.setdefault(item.node.identity, item.node)
            if item.contract_sensitive:
                contract_sensitive.add(item.node.identity)
            group = path_groups.setdefault(item.node.identity, {})
            for path in item.paths:
                previous = group.get(path.root_identity)
                if previous is None or path.distance < previous.distance:
                    group[path.root_identity] = path
        for boundary in report.boundaries:
            key = (
                boundary.source_identity,
                boundary.target_identity,
                boundary.relation.value,
                boundary.resolution.value,
            )
            current_group = boundary_groups.get(key)
            if current_group is None:
                boundary_groups[key] = (boundary, set(boundary.root_identities))
            else:
                current_group[1].update(boundary.root_identities)

    impacted = tuple(
        ImpactedResource(
            node=node_by_identity[identity],
            root_identities=tuple(sorted(path_groups[identity])),
            paths=tuple(path_groups[identity][key] for key in sorted(path_groups[identity])),
            contract_sensitive=identity in contract_sensitive,
        )
        for identity in sorted(path_groups)
    )
    boundaries = tuple(
        ImpactBoundary(
            source_identity=item.source_identity,
            target_identity=item.target_identity,
            relation=item.relation,
            resolution=item.resolution,
            root_identities=tuple(sorted(roots)),
            locations=item.locations,
        )
        for item, roots in boundary_groups.values()
    )
    return SemanticImpactReport(
        graph_fingerprint=after.fingerprint,
        changes=changes,
        impacted=impacted,
        boundaries=boundaries,
        metadata={
            "direction": "reverse_dependency",
            "max_depth": max_depth,
            "comparison_before_graph": before.fingerprint,
            "comparison_after_graph": after.fingerprint,
            "removed_resources_propagated_on_before_graph": True,
        },
    )
