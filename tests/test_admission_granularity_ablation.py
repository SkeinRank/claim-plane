from __future__ import annotations

import json
from pathlib import Path

import pytest

from claim_plane import (
    ADMISSION_GRANULARITY_ABLATION_PROTOCOL,
    AdmissionGranularityAblationReport,
    AdmissionGranularityProfile,
    build_python_dependency_graph,
    run_admission_granularity_ablation,
)
from claim_plane.swarm import SwarmBudgetPolicy, WorkGraph, compute_concurrency_plan


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


def _item(work_id: str, path: str, qualified: str) -> dict[str, object]:
    return {
        "work_id": work_id,
        "title": work_id,
        "goal": work_id,
        "operations": [
            {"access": "write", "resource": {"kind": "file", "identifier": path}},
            {
                "access": "write",
                "resource": {
                    "kind": "symbol",
                    "identifier": qualified,
                    "metadata": {
                        "path": path,
                        "language": "python",
                        "qualified_identifier": qualified,
                    },
                },
            },
        ],
    }


def _graph(*items: dict[str, object]) -> WorkGraph:
    return WorkGraph.from_dict(
        {"protocol": "claim-plane.swarm-work-graph.v1", "work_items": list(items)}
    )


def test_ablation_attributes_refined_policy_parallel_gain() -> None:
    semantic = build_python_dependency_graph(
        {
            "a.py": "def handle():\n    return 1\n",
            "b.py": "def handle():\n    return 2\n",
        }
    )
    graph = _graph(_item("left", "a.py", "handle"), _item("right", "b.py", "handle"))

    report = run_admission_granularity_ablation(
        graph, _policy(), semantic_graph=semantic
    )

    assert report.protocol == ADMISSION_GRANULARITY_ABLATION_PROTOCOL
    assert [item.profile for item in report.profiles] == [
        AdmissionGranularityProfile.BROAD_DECLARED,
        AdmissionGranularityProfile.SYMBOL_PROJECTION,
        AdmissionGranularityProfile.DEPENDENCY_NARROWING,
        AdmissionGranularityProfile.REFINED_POLICY,
    ]
    assert [item.serialized_pairs for item in report.profiles] == [1, 1, 1, 0]
    assert [item.parallel_eligible_pairs for item in report.profiles] == [0, 0, 0, 1]
    assert [item.peak_concurrency for item in report.profiles] == [1, 1, 1, 2]
    final_transition = report.transitions[-1]
    assert final_transition.released_serializations == 1
    assert final_transition.newly_parallel_pairs == 1
    assert final_transition.ordering_changes == 0
    assert report.summary()["parallel_pair_gain"] == 1
    assert report.metadata["candidate_blocking_enabled"] is False
    assert report.metadata["worker_mutation_authority_preserved"] is True


def test_ablation_preserves_explicit_same_file_serialization() -> None:
    semantic = build_python_dependency_graph(
        {"app.py": "def first():\n    return 1\n\ndef second():\n    return 2\n"}
    )
    graph = _graph(
        _item("first", "app.py", "first"),
        _item("second", "app.py", "second"),
    )

    report = run_admission_granularity_ablation(
        graph,
        _policy(same_file="serialize"),
        semantic_graph=semantic,
    )

    assert all(item.serialized_pairs == 1 for item in report.profiles)
    assert all(item.parallel_eligible_pairs == 0 for item in report.profiles)
    assert report.summary()["parallel_pair_gain"] == 0
    assert report.summary()["released_serializations"] == 0


def test_production_default_matches_refined_policy_stage() -> None:
    semantic = build_python_dependency_graph(
        {
            "a.py": "def handle():\n    return 1\n",
            "b.py": "def handle():\n    return 2\n",
        }
    )
    graph = _graph(_item("left", "a.py", "handle"), _item("right", "b.py", "handle"))
    policy = _policy()

    default = compute_concurrency_plan(
        graph,
        policy,
        semantic_graph=semantic,
        candidate_blocking_enabled=False,
    )
    explicit = compute_concurrency_plan(
        graph,
        policy,
        semantic_graph=semantic,
        candidate_blocking_enabled=False,
        admission_granularity_stage="refined_policy",
    )

    assert default.fingerprint() == explicit.fingerprint()
    assert default.waves == explicit.waves
    assert default.constraints == explicit.constraints


def test_ablation_is_deterministic_round_trips_and_keeps_source_bindings() -> None:
    semantic = build_python_dependency_graph(
        {
            "a.py": "def handle():\n    return 1\n",
            "b.py": "def handle():\n    return 2\n",
        }
    )
    graph = _graph(_item("left", "a.py", "handle"), _item("right", "b.py", "handle"))
    policy = _policy()

    first = run_admission_granularity_ablation(graph, policy, semantic_graph=semantic)
    second = run_admission_granularity_ablation(graph, policy, semantic_graph=semantic)
    restored = AdmissionGranularityAblationReport.from_dict(first.to_dict())

    assert first.fingerprint == second.fingerprint == restored.fingerprint
    assert first.work_graph_fingerprint == graph.fingerprint()
    assert first.budget_fingerprint == policy.fingerprint()
    assert first.semantic_graph_fingerprint == semantic.fingerprint
    assert len({item.plan_fingerprint for item in first.profiles}) == 4
    assert len({item.analysis_graph_fingerprint for item in first.profiles}) == 2
    assert [item.narrowed_work_items for item in first.profiles] == [0, 2, 2, 2]
    assert [item.closed_dependency_work_items for item in first.profiles] == [
        0,
        0,
        2,
        2,
    ]
    assert [item.policy_released_serializations for item in first.profiles] == [
        0,
        0,
        0,
        1,
    ]


def test_invalid_ablation_stage_fails_before_planning() -> None:
    graph = _graph(_item("left", "a.py", "handle"), _item("right", "b.py", "handle"))
    with pytest.raises(ValueError, match="unsupported admission granularity stage"):
        compute_concurrency_plan(
            graph,
            _policy(),
            admission_granularity_stage="unknown-stage",
        )


def test_ablation_schema_is_packaged_and_protocol_bound() -> None:
    root = Path("schemas/admission-granularity-ablation.schema.json")
    packaged = Path(
        "src/claim_plane/resources/schemas/admission-granularity-ablation.schema.json"
    )
    assert root.read_bytes() == packaged.read_bytes()
    schema = json.loads(root.read_text(encoding="utf-8"))
    assert schema["properties"]["protocol"]["const"] == (
        ADMISSION_GRANULARITY_ABLATION_PROTOCOL
    )
    assert schema["properties"]["profiles"]["minItems"] == 4
