from __future__ import annotations

import json
from pathlib import Path

from claim_plane import (
    SYMBOL_SCOPED_AUTHORITY_PROJECTION_PROTOCOL,
    IntegrationTarget,
    RootTask,
    SwarmBudgetPolicy,
    SwarmSession,
    SwarmSessionState,
    SymbolProjectionReason,
    SymbolProjectionSource,
    SymbolScopedAuthorityProjectionReport,
    WorkGraph,
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


def _file_region_item(work_id: str, region: str) -> dict[str, object]:
    return {
        "work_id": work_id,
        "title": work_id,
        "goal": work_id,
        "operations": [
            {
                "access": "write",
                "resource": {
                    "kind": "file",
                    "identifier": "app.py",
                    "region": region,
                },
            }
        ],
    }


def _declared_symbol_item(work_id: str, qualified: str) -> dict[str, object]:
    return {
        "work_id": work_id,
        "title": work_id,
        "goal": work_id,
        "operations": [
            {
                "access": "write",
                "resource": {"kind": "file", "identifier": "app.py"},
            },
            {
                "access": "write",
                "resource": {
                    "kind": "symbol",
                    "identifier": qualified,
                    "metadata": {
                        "path": "app.py",
                        "language": "python",
                        "qualified_identifier": qualified,
                    },
                },
            },
        ],
    }


def _semantic_graph():
    return build_python_dependency_graph(
        {
            "app.py": (
                "def first():\n"
                "    return 1\n"
                "\n"
                "def second():\n"
                "    return 2\n"
            )
        }
    )


def test_declared_graph_backed_symbol_removes_redundant_file_carrier() -> None:
    semantic = _semantic_graph()
    graph = _graph(_declared_symbol_item("first", "first"))

    report = build_symbol_scoped_authority_projection(graph, semantic)
    item = report.items[0]

    assert report.protocol == SYMBOL_SCOPED_AUTHORITY_PROJECTION_PROTOCOL
    assert item.narrowed is True
    assert item.removed_file_operation_count == 1
    assert item.synthesized_symbol_operation_count == 0
    assert [op.resource.kind.value for op in item.projected_operations] == ["symbol"]
    assert item.projected_symbol_identities == ("symbol:app.py#first",)
    assert item.evidence[0].source is SymbolProjectionSource.DECLARED_SYMBOL
    assert item.evidence[0].reason is SymbolProjectionReason.EXACT_DECLARED_SYMBOL


def test_bounded_file_region_projects_to_unique_enclosing_symbol() -> None:
    semantic = _semantic_graph()
    graph = _graph(_file_region_item("first", "lines:1-2"))

    report = build_symbol_scoped_authority_projection(graph, semantic)
    item = report.items[0]

    assert item.narrowed is True
    assert item.removed_file_operation_count == 1
    assert item.synthesized_symbol_operation_count == 1
    assert item.projected_symbol_identities == ("symbol:app.py#first",)
    assert [op.resource.kind.value for op in item.projected_operations] == ["symbol"]
    assert {op.resource.kind.value for op in item.analysis_operations} == {
        "file",
        "symbol",
    }
    assert item.evidence[0].source is SymbolProjectionSource.REGION_ENCLOSED_SYMBOL
    assert item.evidence[0].source_region == "lines:1-2"


def test_region_projection_unlocks_existing_same_file_semantic_admission() -> None:
    semantic = _semantic_graph()
    graph = _graph(
        _file_region_item("first", "lines:1-2"),
        _file_region_item("second", "lines:4-5"),
    )

    without_semantic = compute_concurrency_plan(graph, _policy())
    with_semantic = compute_concurrency_plan(graph, _policy(), semantic_graph=semantic)

    assert [wave.work_ids for wave in without_semantic.waves] == [("first", "second")]
    # Regions are already textually disjoint, so the scheduler is allowed to keep
    # the same wave even without semantic evidence.  The important distinction is
    # that 9B now records symbol-level proof and feeds candidate/same-file analysis.
    summary = with_semantic.metadata["symbol_authority_projection_summary"]
    assert summary["narrowed_work_items"] == 2
    assert summary["synthesized_symbol_operations"] == 2
    assert with_semantic.metadata["same_file_admissions"][0]["reason"] == (
        "semantic_independent"
    )
    attribution = with_semantic.metadata["admission_attribution"]["pairs"][0]
    assert attribution["evidence"]["symbol_authority_projection"]["left"][
        "narrowed"
    ] is True
    assert attribution["evidence"]["symbol_authority_projection"]["right"][
        "narrowed"
    ] is True


def test_same_file_serialize_policy_remains_authoritative_after_projection() -> None:
    semantic = _semantic_graph()
    graph = _graph(
        _file_region_item("first", "lines:1-2"),
        _file_region_item("second", "lines:4-5"),
    )

    plan = compute_concurrency_plan(
        graph,
        _policy(same_file="serialize"),
        semantic_graph=semantic,
    )

    assert plan.summary()["serialized_pairs"] == 1
    assert [wave.work_ids for wave in plan.waves] == [("first",), ("second",)]
    assert plan.constraints[0].reasons[0].value == "same_file"


def test_unbounded_file_without_exact_symbol_evidence_fails_closed() -> None:
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

    report = build_symbol_scoped_authority_projection(graph, semantic)
    item = report.items[0]

    assert item.narrowed is False
    assert [op.resource.kind.value for op in item.projected_operations] == ["file"]
    assert SymbolProjectionReason.NO_EXACT_SYMBOL_EVIDENCE in item.preserved_reasons


def test_destructive_file_authority_is_never_narrowed() -> None:
    semantic = _semantic_graph()
    graph = _graph(
        {
            "work_id": "delete",
            "title": "delete",
            "goal": "delete",
            "operations": [
                {
                    "access": "delete",
                    "resource": {
                        "kind": "file",
                        "identifier": "app.py",
                        "region": "lines:1-2",
                    },
                }
            ],
        }
    )

    report = build_symbol_scoped_authority_projection(graph, semantic)
    item = report.items[0]

    assert item.narrowed is False
    assert SymbolProjectionReason.DESTRUCTIVE_ACCESS in item.preserved_reasons


def test_projection_round_trip_is_source_bound_and_tamper_evident() -> None:
    semantic = _semantic_graph()
    graph = _graph(_file_region_item("first", "lines:1-2"))
    report = build_symbol_scoped_authority_projection(graph, semantic)
    payload = report.to_dict()

    restored = SymbolScopedAuthorityProjectionReport.from_dict(payload)
    assert restored.fingerprint == report.fingerprint
    assert restored.work_graph_fingerprint == graph.fingerprint()
    assert restored.semantic_graph_fingerprint == semantic.fingerprint

    payload["items"][0]["removed_file_operation_count"] = 0
    try:
        SymbolScopedAuthorityProjectionReport.from_dict(payload)
    except ValueError as exc:
        assert (
            "fingerprint mismatch" in str(exc)
            or "summary mismatch" in str(exc)
            or "narrowed flag mismatch" in str(exc)
        )
    else:
        raise AssertionError("tampered projection must fail validation")


def test_shared_admission_uses_projected_surface_but_keeps_worker_authority() -> None:
    semantic = _semantic_graph()
    graph = _graph(
        _file_region_item("first", "lines:1-2"),
        _file_region_item("second", "lines:4-5"),
    )
    policy = _policy()
    plan = compute_concurrency_plan(graph, policy, semantic_graph=semantic)
    session = SwarmSession(
        session_id="symbol-projection",
        repository_root=".",
        repository_identity="a" * 64,
        base_commit="b" * 40,
        base_branch="main",
        root_task=RootTask("Projection", "Projection"),
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
    # Execution/final verification keep the planner-declared file authority. Only
    # pairwise conflict admission sees the graph-proven symbol projection.
    assert all(
        [op.resource.kind.value for op in item.intent.operations] == ["file"]
        for item in shared.admissions
    )
    assert shared.metadata["symbol_authority_projection_summary"][
        "narrowed_work_items"
    ] == 2


def test_projection_schema_is_packaged_and_protocol_bound() -> None:
    root = Path("schemas/symbol-scoped-authority-projection.schema.json")
    packaged = Path(
        "src/claim_plane/resources/schemas/"
        "symbol-scoped-authority-projection.schema.json"
    )
    assert root.read_bytes() == packaged.read_bytes()
    schema = json.loads(root.read_text(encoding="utf-8"))
    assert schema["properties"]["protocol"]["const"] == (
        SYMBOL_SCOPED_AUTHORITY_PROJECTION_PROTOCOL
    )
