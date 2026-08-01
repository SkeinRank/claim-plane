"""Adaptive concurrency controller and durable execution-wave planning."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claim_plane.connectors.codex import init_project
from claim_plane.swarm import (
    ConcurrencyPlanStatus,
    SwarmBudgetPolicy,
    WorkGraph,
    compute_concurrency_plan,
    create_swarm_session,
    get_swarm_concurrency_plan,
    plan_swarm_concurrency,
    replace_swarm_budget_policy,
    replace_swarm_work_graph,
)


def _op(
    path: str,
    *,
    region: str | None = None,
    kind: str = "file",
    commitment: str = "committed",
    subject: str | None = None,
) -> dict[str, object]:
    resource: dict[str, object] = {"kind": kind, "identifier": path}
    if region is not None:
        resource["region"] = region
    if subject is not None:
        resource["subject_concept_id"] = subject
    payload: dict[str, object] = {"access": "write", "resource": resource}
    if commitment != "committed":
        payload["commitment"] = commitment
    return payload


def _item(
    work_id: str,
    path: str,
    *,
    depends_on: tuple[str, ...] = (),
    region: str | None = None,
    kind: str = "file",
    commitment: str = "committed",
    subject: str | None = None,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "work_id": work_id,
        "title": work_id,
        "goal": f"Complete {work_id}.",
        "depends_on": list(depends_on),
        "operations": [
            _op(
                path,
                region=region,
                kind=kind,
                commitment=commitment,
                subject=subject,
            )
        ],
        "metadata": metadata or {},
    }


def _graph(items: list[dict[str, object]]) -> WorkGraph:
    return WorkGraph.from_dict(
        {
            "protocol": "claim-plane.swarm-work-graph.v1",
            "work_items": items,
        }
    )


def _policy(
    *,
    max_active: int = 4,
    same_file: str = "region_safe",
    unknown_overlap: str = "serialize",
    shared_contract: str = "serialize",
    schema_change: str = "serialize",
) -> SwarmBudgetPolicy:
    return SwarmBudgetPolicy.from_dict(
        {
            "workers": {
                "max_active": max_active,
                "max_active_per_work_item": 1,
                "max_work_items": 32,
                "max_total_launches": 64,
            },
            "concurrency": {
                "same_file": same_file,
                "unknown_overlap": unknown_overlap,
                "shared_contract": shared_contract,
                "schema_change": schema_change,
            },
        }
    )


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Claim Plane Tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    (repo / "src").mkdir()
    for name in ("a.py", "b.py", "c.py", "shared.py"):
        (repo / "src" / name).write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    init_project(repo)
    return repo


def _spec() -> dict[str, object]:
    return {
        "protocol": "claim-plane.swarm-session-spec.v1",
        "root_task": {"title": "Parallel change", "goal": "Update modules."},
        "work_graph": {
            "protocol": "claim-plane.swarm-work-graph.v1",
            "work_items": [
                _item("a", "src/a.py"),
                _item("b", "src/b.py"),
                _item("c", "src/c.py"),
            ],
        },
        "budget_policy": _policy(max_active=2).to_dict(),
    }


def test_independent_work_is_packed_by_worker_budget() -> None:
    graph = _graph([_item("a", "a.py"), _item("b", "b.py"), _item("c", "c.py")])
    plan = compute_concurrency_plan(graph, _policy(max_active=2))

    assert plan.status is ConcurrencyPlanStatus.READY
    assert [wave.work_ids for wave in plan.waves] == [("a", "b"), ("c",)]
    assert plan.peak_concurrency == 2


def test_dependencies_are_respected_before_budget_packing() -> None:
    graph = _graph(
        [
            _item("a", "a.py"),
            _item("b", "b.py", depends_on=("a",)),
            _item("c", "c.py"),
        ]
    )
    plan = compute_concurrency_plan(graph, _policy(max_active=3))

    assert [wave.work_ids for wave in plan.waves] == [("a", "c"), ("b",)]


def test_disjoint_regions_of_same_file_can_run_together() -> None:
    graph = _graph(
        [
            _item("a", "shared.py", region="lines 1-20"),
            _item("b", "shared.py", region="lines 40-60"),
        ]
    )
    plan = compute_concurrency_plan(graph, _policy(max_active=2))

    assert [wave.work_ids for wave in plan.waves] == [("a", "b")]
    assert plan.constraints == ()


def test_unknown_same_file_overlap_is_serialized() -> None:
    graph = _graph([_item("a", "shared.py"), _item("b", "shared.py")])
    plan = compute_concurrency_plan(graph, _policy(max_active=2))

    assert [wave.work_ids for wave in plan.waves] == [("a",), ("b",)]
    assert plan.constraints[0].reasons[0].value == "unknown_overlap"


def test_deny_policy_requires_replan_instead_of_emitting_waves() -> None:
    graph = _graph([_item("a", "shared.py"), _item("b", "shared.py")])
    plan = compute_concurrency_plan(
        graph, _policy(max_active=2, unknown_overlap="deny")
    )

    assert plan.status is ConcurrencyPlanStatus.REPLAN_REQUIRED
    assert plan.waves == ()
    assert len(plan.denied_constraints) == 1


def test_schema_change_is_isolated_from_other_mutations() -> None:
    graph = _graph(
        [
            _item("a", "UserSchema", kind="schema"),
            _item("b", "b.py"),
            _item("c", "c.py"),
        ]
    )
    plan = compute_concurrency_plan(graph, _policy(max_active=3))

    assert [wave.work_ids for wave in plan.waves] == [("a",), ("b", "c")]
    assert any(
        "schema_change" in [reason.value for reason in item.reasons]
        for item in plan.constraints
    )


def test_shared_contract_is_serialized() -> None:
    graph = _graph(
        [
            _item("contract", "AuthContract", kind="contract", subject="Auth"),
            _item("implementation", "Auth", kind="concept"),
        ]
    )
    plan = compute_concurrency_plan(graph, _policy(max_active=2))

    assert [wave.work_ids for wave in plan.waves] == [
        ("contract",),
        ("implementation",),
    ]
    assert plan.constraints[0].reasons[0].value == "shared_contract"


def test_contingent_scope_does_not_reduce_initial_parallelism() -> None:
    graph = _graph(
        [
            _item("a", "shared.py", commitment="contingent"),
            _item("b", "shared.py"),
        ]
    )
    plan = compute_concurrency_plan(graph, _policy(max_active=2))

    assert [wave.work_ids for wave in plan.waves] == [("a", "b")]


def test_plan_fingerprint_is_independent_of_input_order() -> None:
    items = [_item("b", "b.py"), _item("a", "a.py"), _item("c", "c.py")]
    left = compute_concurrency_plan(_graph(items), _policy(max_active=2))
    right = compute_concurrency_plan(
        _graph(list(reversed(items))), _policy(max_active=2)
    )

    assert left.to_dict() == right.to_dict()
    assert left.fingerprint() == right.fingerprint()


def test_session_plan_is_durable_idempotent_and_invalidated(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    create_swarm_session(repo, spec=_spec(), session_id="swm-plan")

    first = plan_swarm_concurrency(repo, "swm-plan")
    second = plan_swarm_concurrency(repo, "swm-plan")
    loaded = get_swarm_concurrency_plan(repo, "swm-plan")

    assert first["created"] is True
    assert second["created"] is False
    assert first["plan_version"] == second["plan_version"] == 1
    assert loaded["plan_fingerprint"] == first["plan_fingerprint"]

    replacement = _spec()["work_graph"]
    assert isinstance(replacement, dict)
    replacement["work_items"].append(  # type: ignore[union-attr]
        _item("d", "src/shared.py")
    )
    replace_swarm_work_graph(
        repo,
        "swm-plan",
        graph_data=replacement,
        expected_version=1,
    )
    with pytest.raises(KeyError, match="has no concurrency plan"):
        get_swarm_concurrency_plan(repo, "swm-plan")

    plan_swarm_concurrency(repo, "swm-plan")
    replace_swarm_budget_policy(
        repo,
        "swm-plan",
        policy_data=_policy(max_active=1).to_dict(),
        expected_version=1,
    )
    with pytest.raises(KeyError, match="has no concurrency plan"):
        get_swarm_concurrency_plan(repo, "swm-plan")
