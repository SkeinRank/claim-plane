from __future__ import annotations

import json
from textwrap import dedent

from claim_plane import (
    AffectedSubgraphCandidateBlockingPlan,
    CandidateBlockingReason,
    CandidateFailClosedReason,
    DependencyEdge,
    DependencyNode,
    DependencyRelation,
    DependencyResolution,
    ResourceKind,
    ResourceRef,
    SemanticChange,
    SemanticChangeKind,
    SemanticDependencyGraph,
    SemanticMutationCandidate,
    build_affected_subgraph_candidate_blocking,
    build_python_dependency_graph,
    classify_semantic_conflict,
    normalize_resource_ref,
)


def _change(graph, identity: str, kind=SemanticChangeKind.IMPLEMENTATION):
    node = graph.node(identity)
    assert node is not None
    return SemanticChange(
        identity=identity,
        kind=kind,
        before_resource=node.resource,
        after_resource=node.resource,
    )


def _candidate(
    graph,
    candidate_id: str,
    identity: str,
    kind=SemanticChangeKind.IMPLEMENTATION,
):
    return SemanticMutationCandidate(candidate_id, (_change(graph, identity, kind),))


def test_disjoint_subgraphs_are_pruned_before_conflict_classification() -> None:
    graph = build_python_dependency_graph(
        {
            "app.py": dedent(
                """
                def left():
                    return 1

                def right():
                    return 2
                """
            ).lstrip()
        }
    )
    left = _candidate(graph, "left", "symbol:app.py#left")
    right = _candidate(graph, "right", "symbol:app.py#right")

    plan = build_affected_subgraph_candidate_blocking(graph, (left, right))

    assert plan.total_pair_count == 1
    assert plan.selected_pair_count == 0
    assert plan.pruned_pair_count == 1
    assert plan.pruning_ratio == 1.0
    assert plan.candidates_for("left") == ()
    decision = classify_semantic_conflict(graph, left.changes, right.changes)
    assert decision.parallel_safe is True


def test_dependency_path_retains_pair_without_pairwise_graph_walks() -> None:
    graph = build_python_dependency_graph(
        {
            "app.py": dedent(
                """
                def parse(value: str) -> str:
                    return value

                def consume(value: str) -> str:
                    return parse(value)
                """
            ).lstrip()
        }
    )
    producer = _candidate(
        graph, "producer", "symbol:app.py#parse", SemanticChangeKind.CONTRACT
    )
    consumer = _candidate(graph, "consumer", "symbol:app.py#consume")

    plan = build_affected_subgraph_candidate_blocking(graph, (producer, consumer))

    pair = plan.pair("producer", "consumer")
    assert pair is not None
    assert CandidateBlockingReason.AFFECTED_SUBGRAPH_OVERLAP in pair.reasons
    assert "symbol:app.py#consume" in pair.shared_identities
    assert plan.metadata["pairwise_graph_walks"] == 0


def test_shared_downstream_impact_is_conservatively_retained() -> None:
    graph = build_python_dependency_graph(
        {
            "app.py": dedent(
                """
                def left():
                    return 1

                def right():
                    return 2

                def combined():
                    return left() + right()
                """
            ).lstrip()
        }
    )
    left = _candidate(graph, "left", "symbol:app.py#left")
    right = _candidate(graph, "right", "symbol:app.py#right")

    plan = build_affected_subgraph_candidate_blocking(graph, (left, right))

    pair = plan.pair("left", "right")
    assert pair is not None
    assert "symbol:app.py#combined" in pair.shared_identities


def test_unresolved_boundary_fails_closed_against_every_candidate() -> None:
    a = normalize_resource_ref(
        ResourceRef(ResourceKind.SYMBOL, "a", metadata={"path": "a.py"})
    )
    b = normalize_resource_ref(
        ResourceRef(ResourceKind.SYMBOL, "b", metadata={"path": "b.py"})
    )
    unresolved = normalize_resource_ref(
        ResourceRef(ResourceKind.SYMBOL, "missing", concept_id="unresolved:missing")
    )
    graph = SemanticDependencyGraph(
        nodes=(
            DependencyNode(a),
            DependencyNode(b),
            DependencyNode(unresolved, external=True),
        ),
        edges=(
            DependencyEdge(
                source_identity=a.identity,
                target_identity=unresolved.identity,
                relation=DependencyRelation.REFERENCES,
                resolution=DependencyResolution.UNRESOLVED,
            ),
        ),
    )
    left = SemanticMutationCandidate(
        "left",
        (SemanticChange(a.identity, SemanticChangeKind.IMPLEMENTATION, a, a),),
    )
    right = SemanticMutationCandidate(
        "right",
        (SemanticChange(b.identity, SemanticChangeKind.IMPLEMENTATION, b, b),),
    )

    plan = build_affected_subgraph_candidate_blocking(graph, (left, right))

    left_subgraph = next(item for item in plan.subgraphs if item.candidate_id == "left")
    assert (
        CandidateFailClosedReason.UNRESOLVED_BOUNDARY
        in left_subgraph.fail_closed_reasons
    )
    pair = plan.pair("left", "right")
    assert pair is not None
    assert CandidateBlockingReason.FAIL_CLOSED_CANDIDATE in pair.reasons
    assert pair.fail_closed_candidates == ("left",)


def test_missing_root_and_unknown_change_are_never_pruned() -> None:
    graph = build_python_dependency_graph({"app.py": "def known():\n    return 1\n"})
    known = _candidate(graph, "known", "symbol:app.py#known")
    missing_resource = normalize_resource_ref(
        ResourceRef(ResourceKind.SYMBOL, "missing", metadata={"path": "app.py"})
    )
    missing = SemanticMutationCandidate(
        "missing",
        (
            SemanticChange(
                missing_resource.identity,
                SemanticChangeKind.IMPLEMENTATION,
                missing_resource,
                missing_resource,
            ),
        ),
    )
    unknown = SemanticMutationCandidate(
        "unknown",
        (
            SemanticChange(
                "symbol:app.py#known",
                SemanticChangeKind.UNKNOWN,
                graph.node("symbol:app.py#known").resource,
                graph.node("symbol:app.py#known").resource,
            ),
        ),
    )

    plan = build_affected_subgraph_candidate_blocking(graph, (known, missing, unknown))

    assert plan.selected_pair_count == 3
    reasons = {
        item.candidate_id: set(item.fail_closed_reasons) for item in plan.subgraphs
    }
    assert CandidateFailClosedReason.MISSING_GRAPH_ROOT in reasons["missing"]
    assert CandidateFailClosedReason.UNKNOWN_CHANGE in reasons["unknown"]


def test_bounded_candidate_blocking_is_fail_closed() -> None:
    graph = build_python_dependency_graph(
        {"app.py": "def left():\n    return 1\n\ndef right():\n    return 2\n"}
    )
    left = _candidate(graph, "left", "symbol:app.py#left")
    right = _candidate(graph, "right", "symbol:app.py#right")

    plan = build_affected_subgraph_candidate_blocking(
        graph, (left, right), max_depth=0
    )

    assert plan.selected_pair_count == 1
    assert all(
        CandidateFailClosedReason.BOUNDED_TRAVERSAL in item.fail_closed_reasons
        for item in plan.subgraphs
    )


def test_file_root_expands_to_defined_resources() -> None:
    graph = build_python_dependency_graph(
        {"app.py": "def left():\n    return 1\n\ndef right():\n    return 2\n"}
    )
    file_node = graph.node("file:app.py")
    assert file_node is not None
    broad = SemanticMutationCandidate(
        "broad",
        (
            SemanticChange(
                file_node.identity,
                SemanticChangeKind.STRUCTURE,
                file_node.resource,
                file_node.resource,
            ),
        ),
    )
    symbol = _candidate(graph, "symbol", "symbol:app.py#left")

    plan = build_affected_subgraph_candidate_blocking(graph, (broad, symbol))

    pair = plan.pair("broad", "symbol")
    assert pair is not None
    assert "symbol:app.py#left" in pair.shared_identities


def test_plan_is_deterministic_and_round_trips() -> None:
    graph = build_python_dependency_graph(
        {
            "app.py": "def base():\n    return 1\n\ndef use():\n    return base()\n",
            "other.py": "def other():\n    return 2\n",
        }
    )
    candidates = (
        _candidate(graph, "z", "symbol:other.py#other"),
        _candidate(graph, "a", "symbol:app.py#base", SemanticChangeKind.CONTRACT),
        _candidate(graph, "m", "symbol:app.py#use"),
    )

    first = build_affected_subgraph_candidate_blocking(graph, candidates)
    second = build_affected_subgraph_candidate_blocking(
        graph, tuple(reversed(candidates))
    )

    assert first.fingerprint == second.fingerprint
    assert first.to_dict() == second.to_dict()
    restored = AffectedSubgraphCandidateBlockingPlan.from_dict(
        json.loads(json.dumps(first.to_dict()))
    )
    assert restored == first
    assert restored.fingerprint == first.fingerprint


def test_reference_relation_is_retained_for_future_graph_aware_admission() -> None:
    producer = normalize_resource_ref(
        ResourceRef(ResourceKind.SYMBOL, "producer", metadata={"path": "a.py"})
    )
    consumer = normalize_resource_ref(
        ResourceRef(ResourceKind.SYMBOL, "consumer", metadata={"path": "b.py"})
    )
    graph = SemanticDependencyGraph(
        nodes=(DependencyNode(producer), DependencyNode(consumer)),
        edges=(
            DependencyEdge(
                source_identity=consumer.identity,
                target_identity=producer.identity,
                relation=DependencyRelation.REFERENCES,
            ),
        ),
    )
    left = SemanticMutationCandidate(
        "producer",
        (
            SemanticChange(
                producer.identity,
                SemanticChangeKind.IMPLEMENTATION,
                producer,
                producer,
            ),
        ),
    )
    right = SemanticMutationCandidate(
        "consumer",
        (
            SemanticChange(
                consumer.identity,
                SemanticChangeKind.IMPLEMENTATION,
                consumer,
                consumer,
            ),
        ),
    )

    plan = build_affected_subgraph_candidate_blocking(graph, (left, right))

    pair = plan.pair("producer", "consumer")
    assert pair is not None
    assert consumer.identity in pair.shared_identities


def test_inverted_index_prunes_large_disjoint_candidate_set() -> None:
    resources = tuple(
        normalize_resource_ref(
            ResourceRef(
                ResourceKind.SYMBOL,
                f"symbol_{index}",
                metadata={"path": f"module_{index}.py"},
            )
        )
        for index in range(30)
    )
    graph = SemanticDependencyGraph(
        nodes=tuple(DependencyNode(resource) for resource in resources),
        edges=(),
    )
    candidates = tuple(
        SemanticMutationCandidate(
            f"candidate-{index:02d}",
            (
                SemanticChange(
                    resource.identity,
                    SemanticChangeKind.IMPLEMENTATION,
                    resource,
                    resource,
                ),
            ),
        )
        for index, resource in enumerate(resources)
    )

    plan = build_affected_subgraph_candidate_blocking(graph, candidates)

    assert plan.total_pair_count == 435
    assert plan.selected_pair_count == 0
    assert plan.pruned_pair_count == 435
    assert plan.metadata["graph_index_builds"] == 1
    assert plan.metadata["pairwise_graph_walks"] == 0
