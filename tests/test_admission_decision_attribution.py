from __future__ import annotations

from textwrap import dedent

from claim_plane import (
    ADMISSION_DECISION_ATTRIBUTION_PROTOCOL,
    AdmissionAttributionReason,
    AdmissionDecisionAttributionReport,
    AdmissionPairDisposition,
    SwarmBudgetPolicy,
    WorkGraph,
    build_python_dependency_graph,
)
from claim_plane.swarm import compute_concurrency_plan



def _policy(
    *,
    same_file: str = "region_safe",
    unknown_overlap: str = "serialize",
) -> SwarmBudgetPolicy:
    return SwarmBudgetPolicy.from_dict(
        {
            "workers": {
                "max_active": 4,
                "max_active_per_work_item": 1,
                "max_work_items": 16,
                "max_total_launches": 32,
            },
            "concurrency": {
                "same_file": same_file,
                "unknown_overlap": unknown_overlap,
                "shared_contract": "serialize",
                "schema_change": "serialize",
            },
        }
    )


def _file_item(
    work_id: str,
    path: str,
    *,
    depends_on: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "work_id": work_id,
        "title": work_id,
        "goal": f"Update {path}",
        "depends_on": list(depends_on),
        "operations": [
            {
                "access": "write",
                "resource": {"kind": "file", "identifier": path},
            }
        ],
    }


def _symbol_item(work_id: str, path: str, qualified: str) -> dict[str, object]:
    return {
        "work_id": work_id,
        "title": work_id,
        "goal": f"Update {qualified}",
        "operations": [
            {
                "access": "write",
                "resource": {"kind": "file", "identifier": path},
            },
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


def _attribution(plan) -> AdmissionDecisionAttributionReport:
    return AdmissionDecisionAttributionReport.from_dict(
        plan.metadata["admission_attribution"]
    )


def test_every_pair_gets_deterministic_attribution_and_authority() -> None:
    graph = _graph(
        _file_item("a", "src/a.py"),
        _file_item("b", "src/b.py"),
        _file_item("c", "src/c.py"),
    )

    first = compute_concurrency_plan(graph, _policy())
    second = compute_concurrency_plan(graph, _policy())
    report = _attribution(first)

    assert report.protocol == ADMISSION_DECISION_ATTRIBUTION_PROTOCOL
    assert report.fingerprint == _attribution(second).fingerprint
    assert report.summary()["pair_count"] == 3
    assert report.summary()["parallel_eligible_pairs"] == 3
    assert all(
        pair.disposition is AdmissionPairDisposition.PARALLEL_ELIGIBLE
        for pair in report.pairs
    )
    assert all(
        pair.primary_reason is AdmissionAttributionReason.NO_BLOCKING_EVIDENCE
        for pair in report.pairs
    )
    pair = report.pairs[0]
    assert pair.left_authority[0].path == "src/a.py"
    assert pair.left_authority[0].mutating is True
    plan_summary = first.metadata["admission_attribution_summary"]
    assert plan_summary
    assert plan_summary == report.summary()


def test_unknown_same_file_serialization_is_attributed_to_policy_surface() -> None:
    graph = _graph(
        _file_item("a", "src/shared.py"),
        _file_item("b", "src/shared.py"),
    )

    plan = compute_concurrency_plan(graph, _policy())
    pair = _attribution(plan).pairs[0]

    assert pair.disposition is AdmissionPairDisposition.SERIALIZED
    assert pair.primary_reason is AdmissionAttributionReason.UNKNOWN_OVERLAP
    assert pair.before_id == "a"
    assert pair.after_id == "b"
    assert pair.resources == ("src/shared.py",)
    assert pair.evidence["concurrency_constraint"]["action"] == "serialize"
    assert [surface.path for surface in pair.left_authority] == ["src/shared.py"]
    assert [surface.path for surface in pair.right_authority] == ["src/shared.py"]


def test_declared_dependencies_are_separate_from_controller_serialization() -> None:
    graph = _graph(
        _file_item("a", "src/a.py"),
        _file_item("b", "src/b.py", depends_on=("a",)),
        _file_item("c", "src/c.py", depends_on=("b",)),
    )

    report = _attribution(compute_concurrency_plan(graph, _policy()))
    by_pair = {frozenset((item.left_id, item.right_id)): item for item in report.pairs}

    direct = by_pair[frozenset(("a", "b"))]
    transitive = by_pair[frozenset(("a", "c"))]
    assert direct.disposition is AdmissionPairDisposition.ORDERED_BY_DEPENDENCY
    assert direct.primary_reason is AdmissionAttributionReason.DECLARED_DEPENDENCY
    assert direct.evidence["dependency"]["kind"] == "direct"
    assert transitive.evidence["dependency"]["kind"] == "transitive"
    assert report.summary()["dependency_ordered_pairs"] == 3
    assert report.summary()["serialized_pairs"] == 0


def test_candidate_blocking_prune_is_visible_as_parallel_evidence() -> None:
    semantic = build_python_dependency_graph(
        {
            "a.py": "def left():\n    return 1\n",
            "b.py": "def right():\n    return 2\n",
        }
    )
    graph = _graph(
        _symbol_item("left", "a.py", "left"),
        _symbol_item("right", "b.py", "right"),
    )

    plan = compute_concurrency_plan(graph, _policy(), semantic_graph=semantic)
    pair = _attribution(plan).pairs[0]

    assert pair.disposition is AdmissionPairDisposition.PARALLEL_ELIGIBLE
    assert pair.primary_reason is AdmissionAttributionReason.AFFECTED_SUBGRAPH_DISJOINT
    assert pair.evidence["candidate_blocking"]["state"] == "pruned"
    assert pair.evidence["semantic_classifications"] == []


def test_same_file_semantic_independence_is_attributed_without_changing_decision() -> None:
    semantic = build_python_dependency_graph(
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
    graph = _graph(
        _symbol_item("parse", "parser.py", "Parser.parse"),
        _symbol_item("validate", "parser.py", "Parser.validate"),
    )

    plan = compute_concurrency_plan(graph, _policy(), semantic_graph=semantic)
    pair = _attribution(plan).pairs[0]

    assert [wave.work_ids for wave in plan.waves] == [("parse", "validate")]
    assert pair.disposition is AdmissionPairDisposition.PARALLEL_ELIGIBLE
    assert pair.primary_reason is AdmissionAttributionReason.SEMANTIC_INDEPENDENT
    assert pair.evidence["same_file_admissions"][0]["reason"] == "semantic_independent"
    assert pair.evidence["candidate_blocking"]["state"] == "selected"


def test_attribution_round_trip_detects_tampering() -> None:
    graph = _graph(_file_item("a", "a.py"), _file_item("b", "b.py"))
    report = _attribution(compute_concurrency_plan(graph, _policy()))
    payload = report.to_dict()

    restored = AdmissionDecisionAttributionReport.from_dict(payload)
    assert restored.fingerprint == report.fingerprint

    payload["pairs"][0]["detail"] = "tampered"
    try:
        AdmissionDecisionAttributionReport.from_dict(payload)
    except ValueError as exc:
        assert "fingerprint mismatch" in str(exc)
    else:
        raise AssertionError("tampered attribution must fail fingerprint validation")
