from __future__ import annotations

import json
import subprocess
from pathlib import Path
from textwrap import dedent

from claim_plane import (
    SAME_FILE_ADMISSION_PROTOCOL,
    SameFileAdmissionAction,
    SameFileAdmissionDecision,
    SameFileAdmissionReason,
    build_python_dependency_graph,
)
from claim_plane.connectors.codex import init_project
from claim_plane.swarm import (
    ConcurrencyConstraintReason,
    SwarmBudgetPolicy,
    WorkGraph,
    compute_concurrency_plan,
    create_swarm_session,
    plan_swarm_concurrency,
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
    work_id: str, path: str, qualified: str, *, change_kind: str = "implementation"
) -> dict[str, object]:
    return {
        "work_id": work_id,
        "title": work_id,
        "goal": f"Update {qualified}",
        "operations": [
            {"access": "write", "resource": {"kind": "file", "identifier": path}},
            _symbol_op(path, qualified, change_kind=change_kind),
        ],
    }


def _graph(*items: dict[str, object]) -> WorkGraph:
    return WorkGraph.from_dict(
        {"protocol": "claim-plane.swarm-work-graph.v1", "work_items": list(items)}
    )


def test_same_file_different_methods_are_admitted_in_parallel() -> None:
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
        _item("parse", "parser.py", "Parser.parse"),
        _item("validate", "parser.py", "Parser.validate"),
    )

    plan = compute_concurrency_plan(graph, _policy(), semantic_graph=semantic)

    assert [wave.work_ids for wave in plan.waves] == [("parse", "validate")]
    assert plan.constraints == ()
    evidence = plan.metadata["same_file_admissions"]
    assert len(evidence) == 1
    assert evidence[0]["protocol"] == SAME_FILE_ADMISSION_PROTOCOL
    assert evidence[0]["action"] == SameFileAdmissionAction.PARALLEL.value
    assert evidence[0]["reason"] == SameFileAdmissionReason.SEMANTIC_INDEPENDENT.value
    restored = SameFileAdmissionDecision.from_dict(evidence[0])
    assert restored.parallel_safe is True


def test_semantic_dependency_orders_same_file_work() -> None:
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
    constraint = plan.constraints[0]
    assert (constraint.before, constraint.after) == ("producer", "consumer")
    assert ConcurrencyConstraintReason.SEMANTIC_ORDER in constraint.reasons
    evidence = plan.metadata["same_file_admissions"][0]
    assert evidence["semantic_kind"] == "ordered"
    assert evidence["order"] == "right_before_left"


def test_explicit_same_file_serialize_policy_cannot_be_overridden() -> None:
    semantic = build_python_dependency_graph(
        {"app.py": "def first():\n    return 1\n\ndef second():\n    return 2\n"}
    )
    graph = _graph(
        _item("first", "app.py", "first"),
        _item("second", "app.py", "second"),
    )

    plan = compute_concurrency_plan(
        graph, _policy(same_file="serialize"), semantic_graph=semantic
    )

    assert [wave.work_ids for wave in plan.waves] == [("first",), ("second",)]
    assert plan.metadata["same_file_admissions"][0]["reason"] == "policy_serialize"


def test_missing_semantic_roots_preserve_conservative_fallback() -> None:
    semantic = build_python_dependency_graph(
        {"app.py": "def first():\n    return 1\n\ndef second():\n    return 2\n"}
    )
    graph = WorkGraph.from_dict(
        {
            "protocol": "claim-plane.swarm-work-graph.v1",
            "work_items": [
                {
                    "work_id": "first",
                    "title": "first",
                    "goal": "first",
                    "operations": [
                        {
                            "access": "write",
                            "resource": {"kind": "file", "identifier": "app.py"},
                        }
                    ],
                },
                {
                    "work_id": "second",
                    "title": "second",
                    "goal": "second",
                    "operations": [
                        {
                            "access": "write",
                            "resource": {"kind": "file", "identifier": "app.py"},
                        }
                    ],
                },
            ],
        }
    )

    plan = compute_concurrency_plan(graph, _policy(), semantic_graph=semantic)

    assert [wave.work_ids for wave in plan.waves] == [("first",), ("second",)]
    assert plan.constraints[0].reasons[0].value == "unknown_overlap"
    assert plan.metadata["same_file_admissions"][0]["action"] == "fallback"
    assert (
        plan.metadata["same_file_admissions"][0]["reason"] == "missing_semantic_roots"
    )


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def test_repository_planner_uses_pinned_python_revision(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Claim Plane Tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    (repo / "parser.py").write_text(
        dedent(
            """
            class Parser:
                def parse(self, value: str) -> str:
                    return value.strip()

                def validate(self, value: str) -> bool:
                    return bool(value)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    _git(repo, "add", "parser.py")
    _git(repo, "commit", "-qm", "base")
    init_project(repo)

    create_swarm_session(
        repo,
        session_id="semantic-same-file",
        spec={
            "protocol": "claim-plane.swarm-session-spec.v1",
            "root_task": {"title": "Parser", "goal": "Update parser methods."},
            "work_graph": {
                "protocol": "claim-plane.swarm-work-graph.v1",
                "work_items": [
                    _item("parse", "parser.py", "Parser.parse"),
                    _item("validate", "parser.py", "Parser.validate"),
                ],
            },
            "budget_policy": _policy().to_dict(),
        },
    )

    # Dirty working-tree content must not become the semantic planning source.
    (repo / "parser.py").write_text("this is not valid python !!!\n", encoding="utf-8")
    result = plan_swarm_concurrency(repo, "semantic-same-file")

    assert result["summary"]["peak_concurrency"] == 2
    admission = result["concurrency_plan"]["metadata"]["same_file_admissions"][0]
    assert admission["action"] == "parallel"
    assert admission["graph_fingerprint"]


def test_same_file_admission_schema_is_packaged() -> None:
    schema = Path("schemas/same-file-admission.schema.json")
    payload = json.loads(schema.read_text(encoding="utf-8"))
    assert payload["properties"]["protocol"]["const"] == SAME_FILE_ADMISSION_PROTOCOL


def test_overlapping_regions_can_be_refined_by_semantic_roots() -> None:
    semantic = build_python_dependency_graph(
        {"app.py": "def first():\n    return 1\n\ndef second():\n    return 2\n"}
    )
    graph = WorkGraph.from_dict(
        {
            "protocol": "claim-plane.swarm-work-graph.v1",
            "work_items": [
                {
                    "work_id": "first",
                    "title": "first",
                    "goal": "Update first",
                    "operations": [
                        {
                            "access": "write",
                            "resource": {
                                "kind": "file",
                                "identifier": "app.py",
                                "region": "lines:1-5",
                            },
                        },
                        _symbol_op("app.py", "first"),
                    ],
                },
                {
                    "work_id": "second",
                    "title": "second",
                    "goal": "Update second",
                    "operations": [
                        {
                            "access": "write",
                            "resource": {
                                "kind": "file",
                                "identifier": "app.py",
                                "region": "lines:1-5",
                            },
                        },
                        _symbol_op("app.py", "second"),
                    ],
                },
            ],
        }
    )

    plan = compute_concurrency_plan(graph, _policy(), semantic_graph=semantic)

    assert [wave.work_ids for wave in plan.waves] == [("first", "second")]
    assert plan.constraints == ()
    evidence = plan.metadata["same_file_admissions"]
    assert len(evidence) == 1
    assert evidence[0]["action"] == "parallel"
    assert evidence[0]["reason"] == "semantic_independent"
