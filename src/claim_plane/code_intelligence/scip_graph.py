"""Build evidence-bearing semantic dependency graphs from SCIP projections.

SCIP is a code-intelligence protocol, not a call-graph format.  This module therefore
preserves the distinctions SCIP actually proves: occurrence roles become file-to-symbol
defines/imports/reads/writes/reference edges, while SymbolInformation relationships
become references/implements/type-definition/definition-of edges.  Every emitted edge
carries revision-bound provenance suitable for later admission and audit stages.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Mapping

from claim_plane.code_intelligence.scip_ir import (
    SCIP_ROLE_DEFINITION,
    SCIP_ROLE_FORWARD_DEFINITION,
    SCIP_ROLE_IMPORT,
    SCIP_ROLE_READ,
    SCIP_ROLE_TEST,
    SCIP_ROLE_WRITE,
    ScipOccurrence,
    ScipRelationship,
    ScipSemanticResourceIndex,
    ScipSymbolResource,
)
from claim_plane.core.dependency_graph import (
    DependencyEdge,
    DependencyEvidence,
    DependencyNode,
    DependencyRelation,
    DependencyResolution,
    SemanticDependencyGraph,
)
from claim_plane.core.models import ResourceKind, ResourceRef
from claim_plane.core.resource_ir import SemanticResource, normalize_resource_ref

SCIP_DEPENDENCY_GRAPH_PROTOCOL = "claim-plane.scip-dependency-graph.v1"


class ScipDependencyGraphError(RuntimeError):
    """Raised when SCIP evidence cannot be safely projected into the graph."""


def _is_test_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    name = candidate.name
    return (
        "tests" in candidate.parts
        or "test" in candidate.parts
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _unresolved_resource(symbol: str) -> SemanticResource:
    digest = hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:24]
    return normalize_resource_ref(
        ResourceRef(
            kind=ResourceKind.SYMBOL,
            identifier=f"scip-unresolved-{digest}",
            concept_id=f"scip-unresolved:{digest}",
            metadata={
                "code_intelligence_provider": "scip",
                "scip_symbol": symbol,
                "scip_unresolved": True,
                "qualified_identifier": f"scip-unresolved:{digest}",
            },
        )
    )


def _range_tuple(occurrence: ScipOccurrence) -> tuple[int, int, int, int] | None:
    value = occurrence.source_range
    if value is None:
        return None
    return (
        value.start_line,
        value.start_character,
        value.end_line,
        value.end_character,
    )


def _evidence(
    index: ScipSemanticResourceIndex,
    *,
    evidence_type: str,
    path: str | None = None,
    source_range: tuple[int, int, int, int] | None = None,
    metadata: Mapping[str, object] | None = None,
) -> DependencyEvidence:
    payload = dict(metadata or {})
    if source_range is not None:
        payload.setdefault("coordinate_base", 0)
        payload.setdefault("range_semantics", "half_open")
    if index.tool_name is not None:
        payload.setdefault("tool_name", index.tool_name)
    if index.tool_version is not None:
        payload.setdefault("tool_version", index.tool_version)
    return DependencyEvidence(
        provider_id="scip",
        evidence_type=evidence_type,
        revision=index.revision,
        workspace_fingerprint=index.workspace_fingerprint,
        artifact_sha256=index.artifact_sha256,
        path=path,
        source_range=source_range,
        metadata=payload,
    )


@dataclass
class _GraphAccumulator:
    index: ScipSemanticResourceIndex

    def __post_init__(self) -> None:
        self.nodes: dict[str, DependencyNode] = {}
        self.edges: dict[tuple[str, str, str, str], DependencyEdge] = {}

    def node(
        self,
        resource: SemanticResource,
        *,
        external: bool = False,
        test: bool = False,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        current = self.nodes.get(resource.identity)
        next_metadata = dict(metadata or {})
        if current is None:
            self.nodes[resource.identity] = DependencyNode(
                resource=resource,
                public=False,
                test=test,
                external=external,
                metadata=next_metadata,
            )
            return
        if current.resource.to_dict() != resource.to_dict():
            raise ScipDependencyGraphError(
                f"conflicting SCIP graph resource identity: {resource.identity}"
            )
        self.nodes[resource.identity] = DependencyNode(
            resource=current.resource,
            public=current.public,
            test=current.test or test,
            external=current.external or external,
            metadata={**current.metadata, **next_metadata},
        )

    def edge(
        self,
        source: SemanticResource,
        target: SemanticResource,
        relation: DependencyRelation,
        *,
        resolution: DependencyResolution,
        evidence: DependencyEvidence,
        line: int | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        self.node(source)
        self.node(target, external=resolution is DependencyResolution.EXTERNAL)
        edge = DependencyEdge(
            source_identity=source.identity,
            target_identity=target.identity,
            relation=relation,
            resolution=resolution,
            locations=() if line is None else (line,),
            evidence=(evidence,),
            metadata=dict(metadata or {}),
        )
        previous = self.edges.get(edge.key)
        if previous is None:
            self.edges[edge.key] = edge
            return
        self.edges[edge.key] = DependencyEdge(
            source_identity=edge.source_identity,
            target_identity=edge.target_identity,
            relation=edge.relation,
            resolution=edge.resolution,
            locations=(*previous.locations, *edge.locations),
            evidence=(*previous.evidence, *edge.evidence),
            metadata={**previous.metadata, **edge.metadata},
        )


def _symbol_maps(
    index: ScipSemanticResourceIndex,
) -> tuple[dict[str, ScipSymbolResource], dict[str, SemanticResource]]:
    by_symbol: dict[str, ScipSymbolResource] = {}
    by_stable_id: dict[str, SemanticResource] = {}
    for item in index.symbols:
        previous = by_symbol.get(item.scip_symbol)
        if (
            previous is not None
            and previous.resource.identity != item.resource.identity
        ):
            raise ScipDependencyGraphError(
                f"conflicting SCIP symbol binding: {item.scip_symbol}"
            )
        by_symbol[item.scip_symbol] = item
        stable_id = item.resource.stable_id
        bound = by_stable_id.get(stable_id)
        if bound is not None and bound.identity != item.resource.identity:
            raise ScipDependencyGraphError(
                f"conflicting SCIP stable resource id: {stable_id}"
            )
        by_stable_id[stable_id] = item.resource
    return by_symbol, by_stable_id


def _resolve_target(
    *,
    symbol: str,
    stable_id: str | None,
    by_symbol: Mapping[str, ScipSymbolResource],
    by_stable_id: Mapping[str, SemanticResource],
) -> tuple[SemanticResource, DependencyResolution]:
    symbol_binding = by_symbol.get(symbol)
    stable_binding = None if stable_id is None else by_stable_id.get(stable_id)
    if stable_id is not None and stable_binding is None:
        raise ScipDependencyGraphError(
            f"SCIP evidence references unknown stable resource id: {stable_id}"
        )
    if symbol_binding is not None and stable_binding is not None:
        if symbol_binding.resource.identity != stable_binding.identity:
            raise ScipDependencyGraphError(
                f"SCIP symbol/stable-id binding mismatch for {symbol}"
            )
    binding = symbol_binding
    if binding is not None:
        return (
            binding.resource,
            DependencyResolution.EXTERNAL
            if binding.external
            else DependencyResolution.INTERNAL,
        )
    if stable_binding is not None:
        return stable_binding, DependencyResolution.INTERNAL
    return _unresolved_resource(symbol), DependencyResolution.UNRESOLVED


def _occurrence_relations(occurrence: ScipOccurrence) -> tuple[DependencyRelation, ...]:
    roles = occurrence.symbol_roles
    if roles & (SCIP_ROLE_DEFINITION | SCIP_ROLE_FORWARD_DEFINITION):
        return (DependencyRelation.DEFINES,)
    values: list[DependencyRelation] = []
    if roles & SCIP_ROLE_IMPORT:
        values.append(DependencyRelation.IMPORTS)
    if roles & SCIP_ROLE_WRITE:
        values.append(DependencyRelation.WRITES)
    if roles & SCIP_ROLE_READ:
        values.append(DependencyRelation.READS)
    if not values:
        values.append(DependencyRelation.REFERENCES)
    return tuple(values)


def _relationship_relations(
    relationship: ScipRelationship,
) -> tuple[DependencyRelation, ...]:
    values: list[DependencyRelation] = []
    if relationship.is_reference:
        values.append(DependencyRelation.REFERENCES)
    if relationship.is_implementation:
        values.append(DependencyRelation.IMPLEMENTS)
    if relationship.is_type_definition:
        values.append(DependencyRelation.TYPES)
    if relationship.is_definition:
        values.append(DependencyRelation.DEFINITION_OF)
    return tuple(values)


def build_scip_dependency_graph(
    index: ScipSemanticResourceIndex,
) -> SemanticDependencyGraph:
    """Project one SCIP semantic-resource index into an evidence-bearing graph.

    The projection intentionally does not invent call edges.  Ordinary occurrences
    have document-level ownership because SCIP does not encode the enclosing caller
    for every reference.  Later graph enrichment may combine this evidence with a
    language frontend that can prove finer source ownership.
    """

    if not isinstance(index, ScipSemanticResourceIndex):
        raise TypeError("index must be a ScipSemanticResourceIndex")

    accumulator = _GraphAccumulator(index)
    by_symbol, by_stable_id = _symbol_maps(index)
    file_by_path = {resource.path: resource for resource in index.file_resources}
    if None in file_by_path:
        raise ScipDependencyGraphError("SCIP file resources require repository paths")

    test_paths = {
        occurrence.path
        for occurrence in index.occurrences
        if occurrence.symbol_roles & SCIP_ROLE_TEST
    }
    for resource in index.file_resources:
        assert resource.path is not None
        accumulator.node(
            resource,
            test=resource.path in test_paths or _is_test_path(resource.path),
            metadata={
                "evidence_provider": "scip",
                "revision": index.revision,
            },
        )
    for item in index.symbols:
        accumulator.node(
            item.resource,
            external=item.external,
            test=(
                item.resource.path in test_paths
                if item.resource.path is not None
                else False
            ),
            metadata={
                "evidence_provider": "scip",
                "scip_symbol": item.scip_symbol,
                "scip_kind": item.scip_kind_name,
                "revision": index.revision,
            },
        )

    unresolved_occurrences = 0
    for occurrence in index.occurrences:
        source = file_by_path.get(occurrence.path)
        if source is None:
            raise ScipDependencyGraphError(
                f"SCIP occurrence path has no file resource: {occurrence.path}"
            )
        # Local SCIP symbols are deliberately not authority resources.  Keep them out
        # of the graph instead of manufacturing cross-document semantics for them.
        if (
            occurrence.scip_symbol.startswith("local ")
            and occurrence.resource_stable_id is None
        ):
            continue
        target, resolution = _resolve_target(
            symbol=occurrence.scip_symbol,
            stable_id=occurrence.resource_stable_id,
            by_symbol=by_symbol,
            by_stable_id=by_stable_id,
        )
        if resolution is DependencyResolution.UNRESOLVED:
            unresolved_occurrences += 1
            accumulator.node(
                target,
                metadata={
                    "evidence_provider": "scip",
                    "scip_symbol": occurrence.scip_symbol,
                    "unresolved": True,
                },
            )
        source_range = _range_tuple(occurrence)
        line = None if source_range is None else source_range[0] + 1
        evidence = _evidence(
            index,
            evidence_type="occurrence",
            path=occurrence.path,
            source_range=source_range,
            metadata={
                "scip_symbol": occurrence.scip_symbol,
                "symbol_roles": occurrence.symbol_roles,
                "role_names": list(occurrence.role_names),
                "resource_stable_id": occurrence.resource_stable_id,
            },
        )
        for relation in _occurrence_relations(occurrence):
            accumulator.edge(
                source,
                target,
                relation,
                resolution=resolution,
                evidence=evidence,
                line=line,
                metadata={"evidence_provider": "scip"},
            )

    unresolved_relationships = 0
    for relationship in index.relationships:
        source, source_resolution = _resolve_target(
            symbol=relationship.source_symbol,
            stable_id=relationship.source_resource_stable_id,
            by_symbol=by_symbol,
            by_stable_id=by_stable_id,
        )
        if source_resolution is DependencyResolution.UNRESOLVED:
            raise ScipDependencyGraphError(
                "SCIP relationship source must resolve to an indexed resource"
            )
        target, target_resolution = _resolve_target(
            symbol=relationship.target_symbol,
            stable_id=relationship.target_resource_stable_id,
            by_symbol=by_symbol,
            by_stable_id=by_stable_id,
        )
        if target_resolution is DependencyResolution.UNRESOLVED:
            unresolved_relationships += 1
            accumulator.node(
                target,
                metadata={
                    "evidence_provider": "scip",
                    "scip_symbol": relationship.target_symbol,
                    "unresolved": True,
                },
            )
        relations = _relationship_relations(relationship)
        if not relations:
            continue
        source_range = None
        raw_range = source.metadata.get("scip_definition_range")
        if isinstance(raw_range, Mapping):
            try:
                source_range = (
                    int(raw_range["start_line"]),
                    int(raw_range["start_character"]),
                    int(raw_range["end_line"]),
                    int(raw_range["end_character"]),
                )
            except (KeyError, TypeError, ValueError):
                source_range = None
        line = None if source_range is None else source_range[0] + 1
        evidence = _evidence(
            index,
            evidence_type="relationship",
            path=source.path,
            source_range=source_range,
            metadata={
                "source_symbol": relationship.source_symbol,
                "target_symbol": relationship.target_symbol,
                "is_reference": relationship.is_reference,
                "is_implementation": relationship.is_implementation,
                "is_type_definition": relationship.is_type_definition,
                "is_definition": relationship.is_definition,
                "source_resource_stable_id": relationship.source_resource_stable_id,
                "target_resource_stable_id": relationship.target_resource_stable_id,
            },
        )
        for relation in relations:
            accumulator.edge(
                source,
                target,
                relation,
                resolution=target_resolution,
                evidence=evidence,
                line=line,
                metadata={"evidence_provider": "scip"},
            )

    return SemanticDependencyGraph(
        nodes=tuple(accumulator.nodes.values()),
        edges=tuple(accumulator.edges.values()),
        source_digests={},
        metadata={
            "code_intelligence_provider": "scip",
            "scip_graph_protocol": SCIP_DEPENDENCY_GRAPH_PROTOCOL,
            "revision": index.revision,
            "workspace_fingerprint": index.workspace_fingerprint,
            "artifact_sha256": index.artifact_sha256,
            "resource_index_fingerprint": index.fingerprint,
            "project_name": index.project_name,
            "tool_name": index.tool_name,
            "tool_version": index.tool_version,
            "unresolved_occurrence_count": unresolved_occurrences,
            "unresolved_relationship_count": unresolved_relationships,
        },
    )
