from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

from claim_plane import (
    CONFLICT_POLICY_REFINEMENT_PROTOCOL,
    ConflictPolicyClass,
    ConflictPolicyEffect,
    ConflictPolicyRefinementReport,
    IntegrationTarget,
    RootTask,
    SwarmSession,
    SwarmSessionState,
    build_python_dependency_graph,
)
from claim_plane.swarm import (
    ConcurrencyConstraintReason,
    SwarmBudgetPolicy,
    WorkGraph,
    compute_concurrency_plan,
    compute_shared_admission,
)


def _policy(
    *, same_file: str = "region_safe", unknown_overlap: str = "serialize"
) -> SwarmBudgetPolicy:
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
                "unknown_overlap": unknown_overlap,
                "shared_contract": "serialize",
                "schema_change": "serialize",
            },
        }
    )


def _symbol_op(
    path: str, qualified: str, *, change_kind: str = "implementation"
) -> dict[str, object]:
    return {
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
        "metadata": {"semantic_change_kind": change_kind},
    }


def _item(
    work_id: str,
    path: str,
    qualified: str,
    *,
    change_kind: str = "implementation",
) -> dict[str, object]:
    return {
        "work_id": work_id,
        "title": work_id,
        "goal": work_id,
        "operations": [
            {"access": "write", "resource": {"kind": "file", "identifier": path}},
            _symbol_op(path, qualified, change_kind=change_kind),
        ],
    }


def _graph(*items: dict[str, object]) -> WorkGraph:
    return WorkGraph.from_dict(
        {"protocol": "claim-plane.swarm-work-graph.v1", "work_items": list(items)}
    )




def _session(graph: WorkGraph, policy: SwarmBudgetPolicy) -> SwarmSession:
    return SwarmSession(
        session_id="conflict-policy-refinement",
        repository_root=".",
        repository_identity="a" * 64,
        base_commit="b" * 40,
        base_branch="main",
        root_task=RootTask("Policy refinement", "Policy refinement"),
        integration_target=IntegrationTarget("main"),
        work_graph=graph,
        budget_policy=policy,
        graph_version=1,
        budget_version=1,
        state=SwarmSessionState.PLANNED,
        created_at="2026-08-13T00:00:00Z",
        updated_at="2026-08-13T00:00:00Z",
    )


def test_closed_exact_symbols_release_false_same_name_overlap() -> None:
    semantic = build_python_dependency_graph(
        {
            "a.py": "def handle():\n    return 1\n",
            "b.py": "def handle():\n    return 2\n",
        }
    )
    graph = _graph(_item("left", "a.py", "handle"), _item("right", "b.py", "handle"))

    plan = compute_concurrency_plan(
        graph,
        _policy(),
        semantic_graph=semantic,
        candidate_blocking_enabled=False,
    )

    assert plan.constraints == ()
    assert [wave.work_ids for wave in plan.waves] == [("left", "right")]
    report = ConflictPolicyRefinementReport.from_dict(
        plan.metadata["conflict_policy_refinement"]
    )
    pair = report.pairs[0]
    assert report.protocol == CONFLICT_POLICY_REFINEMENT_PROTOCOL
    assert pair.classification is ConflictPolicyClass.PROVABLY_INDEPENDENT
    assert pair.effect is ConflictPolicyEffect.RELEASE_SERIALIZATION
    assert pair.base_action == "serialize"
    assert pair.base_reasons == ("unknown_overlap",)
    assert report.summary()["released_serializations"] == 1
    attribution = plan.metadata["admission_attribution"]["pairs"][0]
    assert attribution["primary_reason"] == "semantic_independent"
    assert attribution["evidence"]["conflict_policy_refinement"]["changed"] is True



def test_shared_admission_consumes_released_pair_proof() -> None:
    semantic = build_python_dependency_graph(
        {
            "a.py": "def handle():\n    return 1\n",
            "b.py": "def handle():\n    return 2\n",
        }
    )
    graph = _graph(_item("left", "a.py", "handle"), _item("right", "b.py", "handle"))
    policy = _policy()
    plan = compute_concurrency_plan(
        graph,
        policy,
        semantic_graph=semantic,
        candidate_blocking_enabled=False,
    )

    shared = compute_shared_admission(_session(graph, policy), plan)

    assert shared.status.value == "ready"
    assert all(item.allowed for item in shared.admissions)
    assert [wave.work_ids for wave in plan.waves] == [("left", "right")]
    assert shared.metadata["conflict_policy_refinement_summary"][
        "released_serializations"
    ] == 1


def test_semantic_order_is_preserved_and_normalized() -> None:
    semantic = build_python_dependency_graph(
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
    graph = _graph(
        _item("consumer", "app.py", "consume"),
        _item("producer", "app.py", "parse", change_kind="contract"),
    )

    plan = compute_concurrency_plan(graph, _policy(), semantic_graph=semantic)

    assert [wave.work_ids for wave in plan.waves] == [("producer",), ("consumer",)]
    assert len(plan.constraints) == 1
    assert plan.constraints[0].reasons == (ConcurrencyConstraintReason.SEMANTIC_ORDER,)
    report = ConflictPolicyRefinementReport.from_dict(
        plan.metadata["conflict_policy_refinement"]
    )
    pair = report.pairs[0]
    assert pair.classification is ConflictPolicyClass.ORDERED
    assert pair.effect is ConflictPolicyEffect.REPLACE_WITH_SEMANTIC_ORDER
    assert (pair.before_id, pair.after_id) == ("producer", "consumer")


def test_explicit_same_file_serialize_remains_authoritative() -> None:
    semantic = build_python_dependency_graph(
        {"app.py": "def first():\n    return 1\n\ndef second():\n    return 2\n"}
    )
    graph = _graph(
        _item("first", "app.py", "first"),
        _item("second", "app.py", "second"),
    )

    plan = compute_concurrency_plan(
        graph,
        _policy(same_file="serialize"),
        semantic_graph=semantic,
    )

    assert [wave.work_ids for wave in plan.waves] == [("first",), ("second",)]
    report = ConflictPolicyRefinementReport.from_dict(
        plan.metadata["conflict_policy_refinement"]
    )
    pair = report.pairs[0]
    assert pair.classification is ConflictPolicyClass.MUST_CONFLICT
    assert pair.effect is ConflictPolicyEffect.PRESERVE
    assert "explicit_same_file_policy" in [reason.value for reason in pair.reasons]


def test_unresolved_dependency_boundary_cannot_release_serialization() -> None:
    semantic = build_python_dependency_graph(
        {
            "a.py": "def handle():\n    return missing_name()\n",
            "b.py": "def handle():\n    return 2\n",
        }
    )
    graph = _graph(_item("left", "a.py", "handle"), _item("right", "b.py", "handle"))

    plan = compute_concurrency_plan(
        graph,
        _policy(),
        semantic_graph=semantic,
        candidate_blocking_enabled=False,
    )

    assert len(plan.constraints) == 1
    report = ConflictPolicyRefinementReport.from_dict(
        plan.metadata["conflict_policy_refinement"]
    )
    pair = report.pairs[0]
    assert pair.classification is ConflictPolicyClass.CONSERVATIVE_UNKNOWN
    assert pair.effect is ConflictPolicyEffect.PRESERVE
    assert "narrowing_not_closed" in [reason.value for reason in pair.reasons]


def test_ambiguous_broad_carrier_is_not_treated_as_exact_closed_authority() -> None:
    semantic = build_python_dependency_graph(
        {
            "app.py": dedent(
                """
                def first():
                    return 1

                def second():
                    return 2
                """
            ).lstrip(),
            "other.py": "def first():\n    return 3\n",
        }
    )
    graph = WorkGraph.from_dict(
        {
            "protocol": "claim-plane.swarm-work-graph.v1",
            "work_items": [
                {
                    "work_id": "left",
                    "title": "left",
                    "goal": "left",
                    "operations": [
                        {
                            "access": "write",
                            "resource": {
                                "kind": "file",
                                "identifier": "app.py",
                                "region": "lines:1-6",
                            },
                        },
                        _symbol_op("app.py", "first"),
                    ],
                },
                _item("right", "other.py", "first"),
            ],
        }
    )

    plan = compute_concurrency_plan(
        graph,
        _policy(),
        semantic_graph=semantic,
        candidate_blocking_enabled=False,
    )

    report = ConflictPolicyRefinementReport.from_dict(
        plan.metadata["conflict_policy_refinement"]
    )
    pair = report.pairs[0]
    assert pair.effect is ConflictPolicyEffect.PRESERVE
    assert pair.classification is ConflictPolicyClass.CONSERVATIVE_UNKNOWN
    assert "non_exact_mutation_surface" in [reason.value for reason in pair.reasons]


def test_conflict_policy_refinement_round_trip_and_schema() -> None:
    semantic = build_python_dependency_graph(
        {
            "a.py": "def handle():\n    return 1\n",
            "b.py": "def handle():\n    return 2\n",
        }
    )
    graph = _graph(_item("left", "a.py", "handle"), _item("right", "b.py", "handle"))
    plan = compute_concurrency_plan(
        graph,
        _policy(),
        semantic_graph=semantic,
        candidate_blocking_enabled=False,
    )
    payload = plan.metadata["conflict_policy_refinement"]
    restored = ConflictPolicyRefinementReport.from_dict(payload)
    assert restored.fingerprint == payload["fingerprint"]

    root = Path("schemas/conflict-policy-refinement.schema.json")
    packaged = Path(
        "src/claim_plane/resources/schemas/conflict-policy-refinement.schema.json"
    )
    assert root.read_bytes() == packaged.read_bytes()
    schema = json.loads(root.read_text(encoding="utf-8"))
    assert (
        schema["properties"]["protocol"]["const"]
        == CONFLICT_POLICY_REFINEMENT_PROTOCOL
    )
