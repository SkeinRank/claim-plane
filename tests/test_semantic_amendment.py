from __future__ import annotations

from dataclasses import replace

from claim_plane import (
    AccessMode,
    ChangeIntent,
    IntentOperation,
    Plane,
    ResourceKind,
    ResourceRef,
    ScopeCommitment,
    SemanticAmendmentBounds,
    SemanticAmendmentDisposition,
    SemanticAmendmentReason,
    assess_semantic_amendment,
    build_python_dependency_graph,
)

BASE = "a" * 40
SOURCE = {
    "app.py": """\
def produce():
    return 1


def consume():
    return produce()


def side():
    return 2
"""
}


def _symbol(name: str, *, commitment: ScopeCommitment = ScopeCommitment.COMMITTED):
    return IntentOperation(
        AccessMode.WRITE,
        ResourceRef(
            ResourceKind.SYMBOL,
            name,
            metadata={"path": "app.py", "qualified_identifier": name},
        ),
        commitment=commitment,
    )


def _intent(intent_id: str, *operations: IntentOperation) -> ChangeIntent:
    return ChangeIntent(
        intent_id=intent_id,
        task_id=intent_id,
        owner=f"agent-{intent_id}",
        base_revision=BASE,
        base_commit=BASE,
        operations=operations,
    )


def test_disjoint_symbol_expansion_is_bounded_and_safe() -> None:
    graph = build_python_dependency_graph(SOURCE)
    current = _intent("left", _symbol("produce"))
    candidate = replace(current, operations=(*current.operations, _symbol("side")))
    active = _intent("right", _symbol("consume"))

    assessment = assess_semantic_amendment(current, candidate, graph, (active,))

    assert assessment.disposition is SemanticAmendmentDisposition.APPROVE
    assert assessment.reason is SemanticAmendmentReason.BOUNDED_SAFE
    assert assessment.allowed is True
    assert assessment.new_paths == ("app.py",)
    assert [item.identity for item in assessment.semantic_changes] == [
        "symbol:app.py#side"
    ]
    assert assessment.impact is not None
    assert assessment.fingerprint == assessment.from_dict(assessment.to_dict()).fingerprint


def test_producer_expansion_requires_ordering_with_active_consumer() -> None:
    graph = build_python_dependency_graph(SOURCE)
    current = _intent("left", _symbol("side"))
    candidate = replace(current, operations=(*current.operations, _symbol("produce")))
    active = _intent("right", _symbol("consume"))

    assessment = assess_semantic_amendment(current, candidate, graph, (active,))

    assert assessment.disposition is SemanticAmendmentDisposition.ORDER
    assert assessment.reason is SemanticAmendmentReason.ACTIVE_ORDERING_REQUIRED
    assert assessment.allowed is False
    assert assessment.requires_ordering is True
    assert assessment.active_relations[0].decision.kind.value == "ordered"
    assert assessment.active_relations[0].decision.order.value == "left_before_right"


def test_non_monotonic_amendment_is_denied() -> None:
    graph = build_python_dependency_graph(SOURCE)
    current = _intent("left", _symbol("produce"), _symbol("side"))
    candidate = replace(current, operations=(_symbol("side"),))

    assessment = assess_semantic_amendment(current, candidate, graph)

    assert assessment.disposition is SemanticAmendmentDisposition.DENY
    assert assessment.reason is SemanticAmendmentReason.NON_MONOTONIC


def test_contingent_promotion_counts_as_new_authority() -> None:
    graph = build_python_dependency_graph(SOURCE)
    current = _intent(
        "left",
        _symbol("produce"),
        _symbol("side", commitment=ScopeCommitment.CONTINGENT),
    )
    candidate = replace(
        current,
        operations=(
            _symbol("produce"),
            _symbol("side", commitment=ScopeCommitment.COMMITTED),
        ),
    )

    assessment = assess_semantic_amendment(current, candidate, graph)

    assert assessment.allowed is True
    assert len(assessment.new_operations) == 1
    assert assessment.new_operations[0].resource.identifier == "side"


def test_hard_bounds_reject_large_expansion() -> None:
    graph = build_python_dependency_graph(SOURCE)
    current = _intent("left", _symbol("produce"))
    candidate = replace(
        current,
        operations=(*current.operations, _symbol("consume"), _symbol("side")),
    )

    assessment = assess_semantic_amendment(
        current,
        candidate,
        graph,
        bounds=SemanticAmendmentBounds(max_new_operations=1),
    )

    assert assessment.allowed is False
    assert assessment.reason is SemanticAmendmentReason.BOUND_EXCEEDED
    assert assessment.metadata["failed_bounds"] == ["new_operations"]


def test_plane_amend_bounded_commits_preflight_atomically(tmp_path) -> None:
    graph = build_python_dependency_graph(SOURCE)
    current = _intent("left", _symbol("produce"))
    candidate = replace(current, operations=(*current.operations, _symbol("side")))
    plane = Plane.open(tmp_path / "plane.db")
    try:
        assert plane.admit(current).allowed
        plane.activate(current.intent_id)

        result = plane.amend_bounded(candidate, graph, expected_version=2)

        assert result.allowed is True
        stored = plane.intent(current.intent_id)
        assert stored is not None
        assert len(stored.operations) == 2
        # Audit payload is persisted by the authoritative registry transaction.
        events = plane._registry.coordination_events()
        amended = [item for item in events if item["event_type"] == "intent_amended"][-1]
        assert amended["payload_json"]["amendment_preflight"]["protocol"] == (
            "claim-plane.semantic-amendment.v2"
        )
    finally:
        plane.close()


def test_plane_rejects_ordered_expansion_without_mutating_old_authority(tmp_path) -> None:
    graph = build_python_dependency_graph(SOURCE)
    current = _intent("left", _symbol("side"))
    candidate = replace(current, operations=(*current.operations, _symbol("produce")))
    active = _intent("right", _symbol("consume"))
    plane = Plane.open(tmp_path / "plane.db")
    try:
        assert plane.admit(current).allowed
        plane.activate(current.intent_id)
        assert plane.admit(active).allowed
        plane.activate(active.intent_id)

        result = plane.amend_bounded(candidate, graph, expected_version=2)

        assert result.allowed is False
        assert result.assessment.requires_ordering is True
        stored = plane.intent(current.intent_id)
        assert stored is not None
        assert [item.resource.identifier for item in stored.operations] == ["side"]
    finally:
        plane.close()
