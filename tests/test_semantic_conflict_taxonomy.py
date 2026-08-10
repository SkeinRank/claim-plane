from __future__ import annotations

import json
from textwrap import dedent

from claim_plane import (
    SEMANTIC_CONFLICT_TAXONOMY_PROTOCOL,
    CommutativityProof,
    SemanticChange,
    SemanticChangeKind,
    SemanticConflictDecision,
    SemanticConflictKind,
    SemanticConflictOrder,
    SemanticConflictReason,
    build_python_dependency_graph,
    classify_semantic_conflict,
)


def _change(graph, identity: str, kind: SemanticChangeKind) -> SemanticChange:
    node = graph.node(identity)
    assert node is not None
    return SemanticChange(
        identity=identity,
        kind=kind,
        before_resource=node.resource,
        after_resource=node.resource,
    )


def test_same_file_different_methods_are_semantically_independent() -> None:
    graph = build_python_dependency_graph(
        {
            "parser.py": dedent(
                """
                class Parser:
                    def parse(self, value: str) -> str:
                        return value.strip()

                    def validate(self, value: str) -> bool:
                        return bool(value)
                """
            ).lstrip()
        }
    )

    decision = classify_semantic_conflict(
        graph,
        (
            _change(
                graph,
                "symbol:parser.py#Parser.parse",
                SemanticChangeKind.IMPLEMENTATION,
            ),
        ),
        (
            _change(
                graph,
                "symbol:parser.py#Parser.validate",
                SemanticChangeKind.IMPLEMENTATION,
            ),
        ),
        left_id="parse-change",
        right_id="validate-change",
    )

    assert decision.kind is SemanticConflictKind.INDEPENDENT
    assert decision.parallel_safe is True
    assert decision.order is None
    assert (
        decision.evidence[0].reason is SemanticConflictReason.DISJOINT_SEMANTIC_SURFACE
    )


def test_contract_producer_change_orders_consumer_after_producer() -> None:
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

    decision = classify_semantic_conflict(
        graph,
        (_change(graph, "symbol:app.py#parse", SemanticChangeKind.CONTRACT),),
        (_change(graph, "symbol:app.py#consume", SemanticChangeKind.IMPLEMENTATION),),
        left_id="producer",
        right_id="consumer",
    )

    assert decision.kind is SemanticConflictKind.ORDERED
    assert decision.order is SemanticConflictOrder.LEFT_BEFORE_RIGHT
    assert decision.requires_ordering is True
    assert decision.parallel_safe is False
    dependency = next(
        item
        for item in decision.evidence
        if item.reason is SemanticConflictReason.SEMANTIC_DEPENDENCY
    )
    assert dependency.path == (
        "symbol:app.py#parse",
        "symbol:app.py#consume",
    )
    assert tuple(item.value for item in dependency.relations) == ("calls",)


def test_order_direction_reverses_with_classification_sides() -> None:
    graph = build_python_dependency_graph(
        {"app.py": "def base():\n    return 1\n\ndef use():\n    return base()\n"}
    )

    decision = classify_semantic_conflict(
        graph,
        (_change(graph, "symbol:app.py#use", SemanticChangeKind.IMPLEMENTATION),),
        (_change(graph, "symbol:app.py#base", SemanticChangeKind.CONTRACT),),
    )

    assert decision.kind is SemanticConflictKind.ORDERED
    assert decision.order is SemanticConflictOrder.RIGHT_BEFORE_LEFT


def test_same_semantic_resource_conflicts_without_proof() -> None:
    graph = build_python_dependency_graph({"app.py": "def value():\n    return 1\n"})
    change = _change(graph, "symbol:app.py#value", SemanticChangeKind.IMPLEMENTATION)

    decision = classify_semantic_conflict(graph, (change,), (change,))

    assert decision.kind is SemanticConflictKind.CONFLICTING
    assert decision.fail_closed is True
    assert decision.evidence[0].reason is SemanticConflictReason.DIRECT_RESOURCE_OVERLAP


def test_explicit_commutativity_proof_is_required_to_emit_commutative() -> None:
    graph = build_python_dependency_graph({"app.py": "STATE = set()\n"})
    change = _change(graph, "symbol:app.py#STATE", SemanticChangeKind.STATE)
    proof = CommutativityProof(
        left_identity=change.identity,
        right_identity=change.identity,
        basis="set-additions-on-distinct-literals",
        metadata={"rule": "fixture"},
    )

    decision = classify_semantic_conflict(
        graph,
        (change,),
        (change,),
        commutativity_proofs=(proof,),
    )

    assert decision.kind is SemanticConflictKind.COMMUTATIVE
    assert decision.parallel_safe is True
    assert decision.evidence[0].reason is SemanticConflictReason.EXPLICIT_COMMUTATIVITY
    assert decision.evidence[0].metadata["basis"] == proof.basis


def test_mutual_semantic_dependency_is_conflicting() -> None:
    graph = build_python_dependency_graph(
        {
            "cycle.py": dedent(
                """
                def left(value: int) -> int:
                    if value <= 0:
                        return 0
                    return right(value - 1)

                def right(value: int) -> int:
                    if value <= 0:
                        return 0
                    return left(value - 1)
                """
            ).lstrip()
        }
    )

    decision = classify_semantic_conflict(
        graph,
        (_change(graph, "symbol:cycle.py#left", SemanticChangeKind.CONTRACT),),
        (_change(graph, "symbol:cycle.py#right", SemanticChangeKind.CONTRACT),),
    )

    assert decision.kind is SemanticConflictKind.CONFLICTING
    assert any(
        item.reason is SemanticConflictReason.MUTUAL_DEPENDENCY
        for item in decision.evidence
    )


def test_unknown_change_kind_fails_closed() -> None:
    graph = build_python_dependency_graph(
        {"app.py": "def a():\n    return 1\n\ndef b():\n    return 2\n"}
    )

    decision = classify_semantic_conflict(
        graph,
        (_change(graph, "symbol:app.py#a", SemanticChangeKind.UNKNOWN),),
        (_change(graph, "symbol:app.py#b", SemanticChangeKind.IMPLEMENTATION),),
    )

    assert decision.kind is SemanticConflictKind.UNKNOWN
    assert decision.fail_closed is True
    assert decision.evidence[0].reason is SemanticConflictReason.UNKNOWN_CHANGE


def test_missing_graph_root_is_not_treated_as_independent() -> None:
    graph = build_python_dependency_graph({"app.py": "def known():\n    return 1\n"})
    known = _change(graph, "symbol:app.py#known", SemanticChangeKind.IMPLEMENTATION)
    missing = SemanticChange(
        identity="symbol:app.py#missing",
        kind=SemanticChangeKind.IMPLEMENTATION,
    )

    decision = classify_semantic_conflict(graph, (known,), (missing,))

    assert decision.kind is SemanticConflictKind.UNKNOWN
    assert decision.evidence[0].reason is SemanticConflictReason.MISSING_GRAPH_ROOT


def test_shared_unresolved_dependency_prevents_independence() -> None:
    graph = build_python_dependency_graph(
        {
            "app.py": dedent(
                """
                def first(value):
                    return missing(value)

                def second(value):
                    return missing(value + 1)
                """
            ).lstrip()
        }
    )

    decision = classify_semantic_conflict(
        graph,
        (_change(graph, "symbol:app.py#first", SemanticChangeKind.IMPLEMENTATION),),
        (_change(graph, "symbol:app.py#second", SemanticChangeKind.IMPLEMENTATION),),
    )

    assert decision.kind is SemanticConflictKind.UNKNOWN
    unresolved = next(
        item
        for item in decision.evidence
        if item.reason is SemanticConflictReason.UNRESOLVED_DEPENDENCY
    )
    assert unresolved.boundary_target == "symbol:missing"


def test_shared_external_dependency_does_not_create_false_conflict() -> None:
    graph = build_python_dependency_graph(
        {
            "app.py": dedent(
                """
                import json

                def first(value: str):
                    return json.loads(value)

                def second(value: str):
                    return json.loads(value)
                """
            ).lstrip()
        }
    )

    decision = classify_semantic_conflict(
        graph,
        (_change(graph, "symbol:app.py#first", SemanticChangeKind.IMPLEMENTATION),),
        (_change(graph, "symbol:app.py#second", SemanticChangeKind.IMPLEMENTATION),),
    )

    assert decision.kind is SemanticConflictKind.INDEPENDENT


def test_opposite_order_constraints_across_multi_change_sets_conflict() -> None:
    graph = build_python_dependency_graph(
        {
            "app.py": dedent(
                """
                def a():
                    return 1

                def b():
                    return a()

                def c():
                    return 1

                def d():
                    return c()
                """
            ).lstrip()
        }
    )

    decision = classify_semantic_conflict(
        graph,
        (
            _change(graph, "symbol:app.py#a", SemanticChangeKind.CONTRACT),
            _change(graph, "symbol:app.py#d", SemanticChangeKind.IMPLEMENTATION),
        ),
        (
            _change(graph, "symbol:app.py#b", SemanticChangeKind.IMPLEMENTATION),
            _change(graph, "symbol:app.py#c", SemanticChangeKind.CONTRACT),
        ),
    )

    assert decision.kind is SemanticConflictKind.CONFLICTING
    assert any(
        item.reason is SemanticConflictReason.MUTUAL_DEPENDENCY
        and item.left_identity is None
        for item in decision.evidence
    )


def test_decision_roundtrip_and_fingerprint_are_deterministic() -> None:
    graph = build_python_dependency_graph(
        {"app.py": "def a():\n    return 1\n\ndef b():\n    return 2\n"}
    )
    left = (_change(graph, "symbol:app.py#a", SemanticChangeKind.IMPLEMENTATION),)
    right = (_change(graph, "symbol:app.py#b", SemanticChangeKind.IMPLEMENTATION),)

    first = classify_semantic_conflict(graph, left, right, left_id="a", right_id="b")
    second = classify_semantic_conflict(graph, left, right, left_id="a", right_id="b")

    assert first.protocol == SEMANTIC_CONFLICT_TAXONOMY_PROTOCOL
    assert first.to_dict() == second.to_dict()
    assert first.fingerprint == second.fingerprint
    restored = SemanticConflictDecision.from_dict(
        json.loads(json.dumps(first.to_dict()))
    )
    assert restored == first
    assert restored.fingerprint == first.fingerprint


def test_bounded_impact_does_not_claim_independence() -> None:
    graph = build_python_dependency_graph(
        {"app.py": "def a():\n    return 1\n\ndef b():\n    return 2\n"}
    )
    decision = classify_semantic_conflict(
        graph,
        (_change(graph, "symbol:app.py#a", SemanticChangeKind.IMPLEMENTATION),),
        (_change(graph, "symbol:app.py#b", SemanticChangeKind.IMPLEMENTATION),),
        max_depth=0,
    )

    assert decision.kind is SemanticConflictKind.UNKNOWN
    assert any(
        item.reason is SemanticConflictReason.BOUNDED_IMPACT
        for item in decision.evidence
    )
