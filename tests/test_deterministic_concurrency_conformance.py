from __future__ import annotations

import json
from pathlib import Path

from claim_plane import (
    CANONICAL_CONCURRENCY_SCENARIOS,
    DETERMINISTIC_CONCURRENCY_CONFORMANCE_PROTOCOL,
    DETERMINISTIC_CONCURRENCY_CONFORMANCE_VERSION,
    CommutativityProof,
    build_python_dependency_graph,
    run_deterministic_concurrency_conformance,
)
from claim_plane.cli import main
from claim_plane.swarm import SwarmBudgetPolicy, WorkGraph, compute_concurrency_plan


def _policy() -> SwarmBudgetPolicy:
    return SwarmBudgetPolicy.from_dict(
        {
            "workers": {
                "max_active": 2,
                "max_active_per_work_item": 1,
                "max_work_items": 8,
                "max_total_launches": 16,
            },
            "concurrency": {
                "same_file": "region_safe",
                "unknown_overlap": "serialize",
                "shared_contract": "serialize",
                "schema_change": "serialize",
            },
        }
    )


def _item(
    work_id: str, path: str, symbol: str, *, change_kind: str = "implementation"
) -> dict:
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
                    "identifier": symbol,
                    "metadata": {
                        "path": path,
                        "language": "python",
                        "qualified_identifier": symbol,
                    },
                },
                "metadata": {"semantic_change_kind": change_kind},
            },
        ],
    }


def test_canonical_concurrency_conformance_is_green() -> None:
    report = run_deterministic_concurrency_conformance()

    assert report.protocol == DETERMINISTIC_CONCURRENCY_CONFORMANCE_PROTOCOL
    assert report.conformance_version == DETERMINISTIC_CONCURRENCY_CONFORMANCE_VERSION
    assert report.passed is True
    assert len(report.results) == len(CANONICAL_CONCURRENCY_SCENARIOS) == 14
    assert report.metrics.safe_parallel_recall == 1.0
    assert report.metrics.false_parallel_rate == 0.0
    assert report.metrics.unnecessary_serialization_rate == 0.0
    assert report.metrics.ordered_dependency_accuracy == 1.0
    assert report.metrics.amendment_recovery_rate == 1.0
    assert len(report.fingerprint) == 64


def test_cross_file_contract_dependency_is_ordered() -> None:
    semantic = build_python_dependency_graph(
        {
            "producer.py": "def parse(value: str) -> str:\n    return value\n",
            "consumer.py": (
                "from producer import parse\n\n"
                "def consume(value: str) -> str:\n"
                "    return parse(value)\n"
            ),
        }
    )
    graph = WorkGraph.from_dict(
        {
            "protocol": "claim-plane.swarm-work-graph.v1",
            "work_items": [
                _item("producer", "producer.py", "parse", change_kind="contract"),
                _item("consumer", "consumer.py", "consume"),
            ],
        }
    )

    plan = compute_concurrency_plan(graph, _policy(), semantic_graph=semantic)

    assert [wave.work_ids for wave in plan.waves] == [("producer",), ("consumer",)]
    constraint = plan.constraints[0]
    assert (constraint.before, constraint.after) == ("producer", "consumer")
    assert "semantic_order" in {reason.value for reason in constraint.reasons}


def test_commutative_same_symbol_proof_is_not_reblocked_by_coarse_overlap() -> None:
    semantic = build_python_dependency_graph({"state.py": "STATE = set()\n"})
    left = _item("left", "state.py", "STATE", change_kind="state")
    right = _item("right", "state.py", "STATE", change_kind="state")
    for item in (left, right):
        item["operations"][1]["resource"]["metadata"]["state"] = True
    graph = WorkGraph.from_dict(
        {
            "protocol": "claim-plane.swarm-work-graph.v1",
            "work_items": [left, right],
        }
    )
    proof = CommutativityProof(
        "symbol:state.py#STATE",
        "symbol:state.py#STATE",
        "test-distinct-set-additions",
    )

    plan = compute_concurrency_plan(
        graph,
        _policy(),
        semantic_graph=semantic,
        commutativity_proofs=(proof,),
    )

    assert [wave.work_ids for wave in plan.waves] == [("left", "right")]
    assert plan.constraints == ()
    assert plan.metadata["same_file_admissions"][0]["reason"] == "semantic_commutative"


def test_conformance_cli_writes_machine_readable_report(tmp_path: Path) -> None:
    output = tmp_path / "conformance.json"

    assert main(["swarm", "conformance", "--json", "--out", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["protocol"] == DETERMINISTIC_CONCURRENCY_CONFORMANCE_PROTOCOL
    assert payload["passed"] is True
    assert payload["summary"] == {"passed": 14, "failed": 0, "total": 14}


def test_concurrency_conformance_schema_is_packaged() -> None:
    root_schema = Path("schemas/deterministic-concurrency-conformance.schema.json")
    package_schema = Path(
        "src/claim_plane/resources/schemas/deterministic-concurrency-conformance.schema.json"
    )
    payload = json.loads(root_schema.read_text(encoding="utf-8"))

    assert payload["properties"]["protocol"]["const"] == (
        DETERMINISTIC_CONCURRENCY_CONFORMANCE_PROTOCOL
    )
    assert root_schema.read_bytes() == package_schema.read_bytes()
