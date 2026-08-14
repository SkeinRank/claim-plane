from __future__ import annotations

import json
from pathlib import Path

from claim_plane import (
    DEPENDENCY_AWARE_AUTHORITY_NARROWING_PROTOCOL,
    DependencyAwareAuthorityNarrowingReport,
    DependencyNarrowingReason,
    DependencyNarrowingState,
    IntegrationTarget,
    RootTask,
    SwarmBudgetPolicy,
    SwarmSession,
    SwarmSessionState,
    WorkGraph,
    build_dependency_aware_authority_narrowing,
    build_python_dependency_graph,
    build_symbol_scoped_authority_projection,
)
from claim_plane.swarm import compute_concurrency_plan, compute_shared_admission


def _policy(*, same_file: str = "region_safe") -> SwarmBudgetPolicy:
    return SwarmBudgetPolicy.from_dict(
        {
            "workers": {
                "max_active": 2,
                "max_active_per_work_item": 1,
                "max_work_items": 8,
                "max_total_launches": 16,
            },
            "concurrency": {
                "same_file": same_file,
                "unknown_overlap": "serialize",
                "shared_contract": "serialize",
                "schema_change": "serialize",
            },
        }
    )


def _graph(*items: dict[str, object]) -> WorkGraph:
    return WorkGraph.from_dict(
        {"protocol": "claim-plane.swarm-work-graph.v1", "work_items": list(items)}
    )


def _symbol_item(work_id: str, symbol: str) -> dict[str, object]:
    return {
        "work_id": work_id,
        "title": work_id,
        "goal": work_id,
        "operations": [
            {"access": "write", "resource": {"kind": "file", "identifier": "app.py"}},
            {
                "access": "write",
                "resource": {
                    "kind": "symbol",
                    "identifier": symbol,
                    "metadata": {
                        "path": "app.py",
                        "language": "python",
                        "qualified_identifier": symbol,
                    },
                },
            },
        ],
    }


def _semantic_graph():
    return build_python_dependency_graph(
        {
            "app.py": (
                "def helper():\n"
                "    return 1\n\n"
                "def main():\n"
                "    return helper()\n\n"
                "def unused():\n"
                "    return 3\n"
            )
        }
    )


def test_closed_dependency_envelope_tracks_context_and_excludes_sibling() -> None:
    semantic = _semantic_graph()
    graph = _graph(_symbol_item("main", "main"))
    projection = build_symbol_scoped_authority_projection(graph, semantic)

    report = build_dependency_aware_authority_narrowing(graph, projection, semantic)
    item = report.items[0]

    assert report.protocol == DEPENDENCY_AWARE_AUTHORITY_NARROWING_PROTOCOL
    assert item.state is DependencyNarrowingState.CLOSED
    assert item.mutation_root_identities == ("symbol:app.py#main",)
    assert "symbol:app.py#helper" in item.dependency_context_identities
    assert "symbol:app.py#unused" in item.excluded_same_file_sibling_identities
    assert item.unresolved_targets == ()
    assert DependencyNarrowingReason.CLOSED_DEPENDENCY_ENVELOPE in item.reasons
    assert [op.resource.kind.value for op in item.analysis_operations] == ["symbol"]
    assert any(edge.relation.value == "calls" for edge in item.edge_evidence)


def test_unresolved_dependency_boundary_fails_closed_to_9b_analysis_surface() -> None:
    semantic = build_python_dependency_graph(
        {"app.py": "def main():\n    return missing_name()\n"}
    )
    graph = _graph(_symbol_item("main", "main"))
    projection = build_symbol_scoped_authority_projection(graph, semantic)

    report = build_dependency_aware_authority_narrowing(graph, projection, semantic)
    item = report.items[0]

    assert item.state is DependencyNarrowingState.FAIL_CLOSED
    assert item.unresolved_targets
    assert DependencyNarrowingReason.UNRESOLVED_BOUNDARY in item.reasons
    # Fail closed restores 9B's conservative analysis surface, including the
    # declared file carrier retained for semantic/same-file reasoning.
    assert {op.resource.kind.value for op in item.analysis_operations} == {
        "file",
        "symbol",
    }


def test_broad_file_without_symbol_root_cannot_be_narrowed_from_dependencies() -> None:
    semantic = _semantic_graph()
    graph = _graph(
        {
            "work_id": "broad",
            "title": "broad",
            "goal": "broad",
            "operations": [
                {
                    "access": "write",
                    "resource": {"kind": "file", "identifier": "app.py"},
                }
            ],
        }
    )
    projection = build_symbol_scoped_authority_projection(graph, semantic)

    report = build_dependency_aware_authority_narrowing(graph, projection, semantic)
    item = report.items[0]

    assert item.state is DependencyNarrowingState.FAIL_CLOSED
    assert DependencyNarrowingReason.NO_SYMBOL_ROOT in item.reasons
    assert [op.resource.kind.value for op in item.analysis_operations] == ["file"]


def test_explicit_same_file_serialize_policy_remains_authoritative() -> None:
    semantic = _semantic_graph()
    graph = _graph(_symbol_item("main", "main"), _symbol_item("unused", "unused"))

    plan = compute_concurrency_plan(
        graph,
        _policy(same_file="serialize"),
        semantic_graph=semantic,
    )

    assert plan.summary()["serialized_pairs"] == 1
    assert [wave.work_ids for wave in plan.waves] == [("main",), ("unused",)]


def test_plan_and_attribution_carry_dependency_narrowing_evidence() -> None:
    semantic = _semantic_graph()
    graph = _graph(_symbol_item("main", "main"), _symbol_item("unused", "unused"))

    plan = compute_concurrency_plan(graph, _policy(), semantic_graph=semantic)

    summary = plan.metadata["dependency_authority_narrowing_summary"]
    assert summary["closed_work_items"] == 2
    attribution = plan.metadata["admission_attribution"]
    pair = attribution["pairs"][0]
    assert pair["evidence"]["dependency_authority_narrowing"]["left"] is not None
    assert pair["evidence"]["dependency_authority_narrowing"]["right"] is not None
    assert attribution["metadata"]["dependency_authority_narrowing_fingerprint"] == (
        plan.metadata["dependency_authority_narrowing"]["fingerprint"]
    )


def test_shared_admission_uses_narrowed_analysis_but_keeps_worker_intent() -> None:
    semantic = _semantic_graph()
    graph = _graph(_symbol_item("main", "main"), _symbol_item("unused", "unused"))
    policy = _policy()
    plan = compute_concurrency_plan(graph, policy, semantic_graph=semantic)
    session = SwarmSession(
        session_id="dependency-narrowing",
        repository_root=".",
        repository_identity="a" * 64,
        base_commit="b" * 40,
        base_branch="main",
        root_task=RootTask("Narrowing", "Narrowing"),
        integration_target=IntegrationTarget("main"),
        work_graph=graph,
        budget_policy=policy,
        graph_version=1,
        budget_version=1,
        state=SwarmSessionState.PLANNED,
        created_at="2026-08-13T00:00:00Z",
        updated_at="2026-08-13T00:00:00Z",
    )

    shared = compute_shared_admission(session, plan)

    assert shared.status.value == "ready"
    assert all(item.allowed for item in shared.admissions)
    assert all(
        {op.resource.kind.value for op in item.intent.operations} == {"file", "symbol"}
        for item in shared.admissions
    )
    assert (
        shared.metadata["dependency_authority_narrowing_summary"]["closed_work_items"]
        == 2
    )


def test_dependency_narrowing_round_trip_and_schema_are_source_bound() -> None:
    semantic = _semantic_graph()
    graph = _graph(_symbol_item("main", "main"))
    projection = build_symbol_scoped_authority_projection(graph, semantic)
    report = build_dependency_aware_authority_narrowing(graph, projection, semantic)
    payload = report.to_dict()

    restored = DependencyAwareAuthorityNarrowingReport.from_dict(payload)
    assert restored.fingerprint == report.fingerprint
    assert restored.symbol_projection_fingerprint == projection.fingerprint

    payload["items"][0]["mutation_root_identities"] = ["symbol:app.py#tampered"]
    try:
        DependencyAwareAuthorityNarrowingReport.from_dict(payload)
    except ValueError as exc:
        assert "fingerprint mismatch" in str(exc) or "summary mismatch" in str(exc)
    else:
        raise AssertionError(
            "tampered dependency narrowing report must fail validation"
        )

    root = Path("schemas/dependency-aware-authority-narrowing.schema.json")
    packaged = Path(
        "src/claim_plane/resources/schemas/"
        "dependency-aware-authority-narrowing.schema.json"
    )
    assert root.read_bytes() == packaged.read_bytes()
    schema = json.loads(root.read_text(encoding="utf-8"))
    assert schema["properties"]["protocol"]["const"] == (
        DEPENDENCY_AWARE_AUTHORITY_NARROWING_PROTOCOL
    )
