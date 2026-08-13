from __future__ import annotations

import json

import pytest

from claim_plane import (
    DependencyEdge,
    DependencyEvidence,
    DependencyRelation,
    DependencyResolution,
    ResourceKind,
    ResourceRef,
    ScipDependencyGraphError,
    ScipOccurrence,
    ScipRelationship,
    ScipSemanticResourceIndex,
    ScipSourceRange,
    ScipSymbolResource,
    SemanticDependencyGraph,
    build_scip_dependency_graph,
    normalize_resource_ref,
)


def _resources():
    file_resource = normalize_resource_ref(
        ResourceRef(
            ResourceKind.FILE,
            "src/app.py",
            metadata={"language": "python"},
        )
    )
    run_resource = normalize_resource_ref(
        ResourceRef(
            ResourceKind.SYMBOL,
            "run",
            signature="def run() -> str",
            concept_id="demo/app/run().",
            metadata={
                "path": "src/app.py",
                "language": "python",
                "qualified_name": "demo/app/run().",
                "scip_definition_range": {
                    "start_line": 2,
                    "start_character": 4,
                    "end_line": 2,
                    "end_character": 7,
                },
            },
        )
    )
    external_resource = normalize_resource_ref(
        ResourceRef(
            ResourceKind.SYMBOL,
            "render",
            concept_id="external:scip-python:pip:dep:dep/render().",
            metadata={
                "qualified_name": "external:scip-python:pip:dep:dep/render().",
                "external": True,
                "language": "python",
            },
        )
    )
    return file_resource, run_resource, external_resource


def _index(*, unresolved_relationship: bool = False) -> ScipSemanticResourceIndex:
    file_resource, run_resource, external_resource = _resources()
    run_symbol = "scip-python pip demo rev demo/app/run()."
    external_symbol = "scip-python pip dep 1 dep/render()."
    symbols = (
        ScipSymbolResource(
            scip_symbol=run_symbol,
            resource=run_resource,
            display_name="run",
            scip_kind=17,
            external=False,
        ),
        ScipSymbolResource(
            scip_symbol=external_symbol,
            resource=external_resource,
            display_name="render",
            scip_kind=17,
            external=True,
        ),
    )
    target_stable_id = (
        None if unresolved_relationship else external_resource.stable_id
    )
    target_symbol = (
        "scip-python pip missing 1 missing/value()."
        if unresolved_relationship
        else external_symbol
    )
    return ScipSemanticResourceIndex(
        revision="a" * 40,
        workspace_fingerprint="b" * 64,
        artifact_sha256="c" * 64,
        project_name="demo",
        project_root="file:///repo",
        tool_name="scip-python",
        tool_version="0.6.6",
        file_resources=(file_resource,),
        symbols=symbols,
        occurrences=(
            ScipOccurrence(
                path="src/app.py",
                scip_symbol=run_symbol,
                symbol_roles=0x1,
                source_range=ScipSourceRange(2, 4, 2, 7),
                resource_stable_id=run_resource.stable_id,
            ),
            ScipOccurrence(
                path="src/app.py",
                scip_symbol=external_symbol,
                symbol_roles=0x8,
                source_range=ScipSourceRange(5, 11, 5, 17),
                resource_stable_id=external_resource.stable_id,
            ),
            ScipOccurrence(
                path="src/app.py",
                scip_symbol="local 7",
                symbol_roles=0x4,
                source_range=ScipSourceRange(6, 4, 6, 9),
                resource_stable_id=None,
            ),
        ),
        relationships=(
            ScipRelationship(
                source_symbol=run_symbol,
                target_symbol=target_symbol,
                is_reference=True,
                is_implementation=True,
                source_resource_stable_id=run_resource.stable_id,
                target_resource_stable_id=target_stable_id,
            ),
        ),
    )


def test_dependency_evidence_is_deterministic_and_backward_compatible() -> None:
    evidence = DependencyEvidence(
        provider_id="SCIP",
        evidence_type="occurrence",
        revision="a" * 40,
        workspace_fingerprint="b" * 64,
        artifact_sha256="c" * 64,
        path="src/app.py",
        source_range=(3, 4, 3, 7),
        metadata={"coordinate_base": 0},
    )
    edge = DependencyEdge(
        source_identity="file:src/app.py",
        target_identity="symbol:src/app.py#run",
        relation=DependencyRelation.REFERENCES,
        evidence=(evidence, evidence),
    )
    assert len(edge.evidence) == 1
    payload = edge.to_dict()
    assert payload["evidence"][0]["provider_id"] == "scip"
    assert DependencyEdge.from_dict(json.loads(json.dumps(payload))) == edge

    legacy = DependencyEdge(
        source_identity="file:src/app.py",
        target_identity="symbol:src/app.py#run",
        relation=DependencyRelation.DEFINES,
    )
    assert "evidence" not in legacy.to_dict()


def test_scip_graph_emits_occurrence_and_relationship_evidence() -> None:
    index = _index()
    graph = build_scip_dependency_graph(index)
    file_resource, run_resource, external_resource = _resources()

    assert graph.metadata["code_intelligence_provider"] == "scip"
    assert graph.metadata["revision"] == index.revision
    assert graph.metadata["resource_index_fingerprint"] == index.fingerprint

    defines = next(
        edge
        for edge in graph.edges
        if edge.relation is DependencyRelation.DEFINES
        and edge.target_identity == run_resource.identity
    )
    assert defines.source_identity == file_resource.identity
    assert defines.locations == (3,)
    assert defines.evidence[0].evidence_type == "occurrence"
    assert defines.evidence[0].source_range == (2, 4, 2, 7)
    assert defines.evidence[0].metadata["coordinate_base"] == 0

    reads = next(
        edge
        for edge in graph.edges
        if edge.relation is DependencyRelation.READS
        and edge.target_identity == external_resource.identity
    )
    assert reads.resolution is DependencyResolution.EXTERNAL
    assert reads.locations == (6,)

    relationship_edges = {
        edge.relation: edge
        for edge in graph.edges
        if edge.source_identity == run_resource.identity
        and edge.target_identity == external_resource.identity
    }
    assert DependencyRelation.REFERENCES in relationship_edges
    assert DependencyRelation.IMPLEMENTS in relationship_edges
    assert relationship_edges[DependencyRelation.IMPLEMENTS].resolution is (
        DependencyResolution.EXTERNAL
    )
    evidence = relationship_edges[DependencyRelation.IMPLEMENTS].evidence[0]
    assert evidence.evidence_type == "relationship"
    assert evidence.metadata["source_symbol"].endswith("demo/app/run().")
    assert evidence.metadata["is_implementation"] is True

    # Document-local SCIP symbols remain occurrence evidence only and are not
    # manufactured into graph authority resources.
    assert all("local 7" not in node.identity for node in graph.nodes)


def test_scip_graph_keeps_unresolved_relationships_explicit() -> None:
    graph = build_scip_dependency_graph(_index(unresolved_relationship=True))

    unresolved = [
        edge
        for edge in graph.edges
        if edge.resolution is DependencyResolution.UNRESOLVED
    ]
    assert {edge.relation for edge in unresolved} == {
        DependencyRelation.REFERENCES,
        DependencyRelation.IMPLEMENTS,
    }
    assert graph.metadata["unresolved_relationship_count"] == 1
    assert all(graph.node(edge.target_identity) is not None for edge in unresolved)


def test_scip_graph_fails_closed_on_stable_id_binding_mismatch() -> None:
    index = _index()
    relation = index.relationships[0]
    broken = ScipRelationship(
        source_symbol=relation.source_symbol,
        target_symbol=relation.target_symbol,
        is_reference=True,
        source_resource_stable_id="sr2_deadbeefdeadbeefdeadbeef",
        target_resource_stable_id=relation.target_resource_stable_id,
    )
    object.__setattr__(index, "relationships", (broken,))

    with pytest.raises(ScipDependencyGraphError, match="unknown stable resource id"):
        build_scip_dependency_graph(index)


def test_scip_graph_serialization_is_deterministic() -> None:
    first = build_scip_dependency_graph(_index())
    second = build_scip_dependency_graph(_index())

    assert first.to_dict() == second.to_dict()
    restored = SemanticDependencyGraph.from_dict(
        json.loads(json.dumps(first.to_dict()))
    )
    assert restored == first
    assert restored.fingerprint == first.fingerprint
