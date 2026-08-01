"""Swarm budget policy and durable session binding."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from claim_plane.connectors.codex import init_project
from claim_plane.core import AccessMode, IntentOperation, ResourceKind, ResourceRef
from claim_plane.swarm import (
    IntegrationTarget,
    RootTask,
    SwarmBudgetPolicy,
    SwarmSession,
    SwarmSessionState,
    WorkGraph,
    WorkItem,
    create_swarm_session,
    get_swarm_session,
    replace_swarm_budget_policy,
    replace_swarm_work_graph,
    validate_budget_policy,
)
from claim_plane.swarm.store import SwarmSessionStore


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Claim Plane Tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src/app.py").write_text("def run():\n    return True\n")
    (repo / "tests/test_app.py").write_text("def test_app():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    init_project(repo)
    return repo


def _operation(path: str) -> dict[str, object]:
    return {
        "access": "write",
        "resource": {"kind": "file", "identifier": path},
    }


def _graph(count: int = 2) -> dict[str, object]:
    items: list[dict[str, object]] = []
    for index in range(count):
        work_id = f"work-{index + 1}"
        items.append(
            {
                "work_id": work_id,
                "title": f"Work item {index + 1}",
                "goal": f"Complete work item {index + 1}.",
                "depends_on": [] if index == 0 else [f"work-{index}"],
                "operations": [_operation(f"src/work_{index + 1}.py")],
            }
        )
    return {
        "protocol": "claim-plane.swarm-work-graph.v1",
        "work_items": items,
    }


def _policy(**worker_overrides: int) -> dict[str, object]:
    workers: dict[str, int] = {
        "max_active": 4,
        "max_active_per_work_item": 1,
        "max_work_items": 8,
        "max_total_launches": 16,
    }
    workers.update(worker_overrides)
    return {
        "protocol": "claim-plane.swarm-budget-policy.v1",
        "workers": workers,
        "resources": {
            "max_total_tokens": 250000,
            "max_cost_usd": "12.500000",
            "max_wall_time_seconds": 3600,
        },
        "retries": {
            "max_replans": 1,
            "max_repairs_per_work_item": 2,
            "max_agent_restarts": 1,
        },
        "concurrency": {
            "same_file": "region_safe",
            "unknown_overlap": "serialize",
            "shared_contract": "serialize",
            "schema_change": "deny",
        },
    }


def _spec(*, policy: dict[str, object] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "protocol": "claim-plane.swarm-session-spec.v1",
        "root_task": {
            "title": "Budgeted task",
            "goal": "Exercise bounded swarm planning.",
        },
        "work_graph": _graph(),
    }
    if policy is not None:
        payload["budget_policy"] = policy
    return payload


def test_budget_policy_normalizes_defaults_and_fingerprint() -> None:
    default = SwarmBudgetPolicy.from_dict({})
    reordered = SwarmBudgetPolicy.from_dict(
        {
            "concurrency": {},
            "retries": {},
            "resources": {},
            "workers": {},
            "protocol": "claim-plane.swarm-budget-policy.v1",
        }
    )

    assert default.to_dict() == reordered.to_dict()
    assert default.fingerprint() == reordered.fingerprint()
    assert default.workers.max_active == 4
    assert default.resources.max_cost_usd == "25"
    assert default.concurrency.same_file.value == "region_safe"


def test_budget_policy_is_strict_and_cross_field_validated() -> None:
    with pytest.raises(ValueError, match="unknown fields: max_agents"):
        SwarmBudgetPolicy.from_dict({"workers": {"max_agents": 10}})

    with pytest.raises(ValueError, match="cannot exceed workers.max_active"):
        SwarmBudgetPolicy.from_dict(
            {"workers": {"max_active": 2, "max_active_per_work_item": 3}}
        )

    with pytest.raises(ValueError, match="cannot be smaller"):
        SwarmBudgetPolicy.from_dict(
            {"workers": {"max_work_items": 10, "max_total_launches": 9}}
        )

    with pytest.raises(ValueError, match="at most 6 decimal places"):
        SwarmBudgetPolicy.from_dict(
            {"resources": {"max_cost_usd": "1.0000001"}}
        )

    with pytest.raises(ValueError, match="invalid concurrency policy"):
        SwarmBudgetPolicy.from_dict(
            {"concurrency": {"unknown_overlap": "allow"}}
        )


def test_session_binds_explicit_policy_and_reports_capacity(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = create_swarm_session(
        repo,
        spec=_spec(policy=_policy()),
        session_id="swm-budget",
    )

    session = get_swarm_session(repo, "swm-budget")
    assert result["budget"]["max_active_workers"] == 4
    assert result["budget"]["minimum_required_launches"] == 2
    assert result["budget"]["remaining_launch_capacity_after_first_attempt"] == 14
    assert session.budget_version == 1
    assert session.budget_policy.resources.max_cost_usd == "12.5"
    assert len(session.budget_fingerprint) == 64


def test_default_policy_is_bound_when_spec_omits_budget(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    create_swarm_session(repo, spec=_spec(), session_id="swm-default-budget")

    session = get_swarm_session(repo, "swm-default-budget")
    assert session.budget_policy == SwarmBudgetPolicy()
    assert session.to_dict()["budget_version"] == 1
    assert session.to_dict()["budget_policy"]["workers"]["max_active"] == 4


def test_budget_replacement_is_optimistic_and_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    create_swarm_session(repo, spec=_spec(), session_id="swm-budget-version")

    updated = replace_swarm_budget_policy(
        repo,
        "swm-budget-version",
        policy_data=_policy(max_active=3),
        expected_version=1,
    )
    assert updated.budget_version == 2
    assert updated.budget_policy.workers.max_active == 3

    unchanged = replace_swarm_budget_policy(
        repo,
        "swm-budget-version",
        policy_data=_policy(max_active=3),
        expected_version=2,
    )
    assert unchanged.budget_version == 2

    with pytest.raises(ValueError, match="stale budget policy"):
        replace_swarm_budget_policy(
            repo,
            "swm-budget-version",
            policy_data=_policy(max_active=2),
            expected_version=1,
        )


def test_policy_rejects_existing_or_replacement_graph_above_limit(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    too_small = _policy(max_work_items=1, max_total_launches=2)
    with pytest.raises(ValueError, match="graph has 2 items"):
        create_swarm_session(
            repo,
            spec=_spec(policy=too_small),
            session_id="swm-too-small",
        )

    create_swarm_session(
        repo,
        spec=_spec(policy=_policy(max_work_items=2, max_total_launches=4)),
        session_id="swm-graph-cap",
    )
    with pytest.raises(ValueError, match="graph has 3 items"):
        replace_swarm_work_graph(
            repo,
            "swm-graph-cap",
            graph_data=_graph(3),
            expected_version=1,
        )

    with pytest.raises(ValueError, match="graph has 2 items"):
        replace_swarm_budget_policy(
            repo,
            "swm-graph-cap",
            policy_data=too_small,
            expected_version=1,
        )


def test_validate_budget_returns_canonical_policy_and_graph_fit() -> None:
    result = validate_budget_policy(_policy(), work_items=2)

    assert result["policy"]["resources"]["max_cost_usd"] == "12.5"
    assert result["summary"]["minimum_required_launches"] == 2
    assert result["summary"]["remaining_launch_capacity_after_first_attempt"] == 14
    assert len(result["summary"]["fingerprint"]) == 64


def test_v1_database_migrates_to_default_budget_policy(tmp_path: Path) -> None:
    database = tmp_path / "swarm.db"
    graph = WorkGraph(
        work_items=(
            WorkItem(
                work_id="work",
                title="Work",
                goal="Do work.",
                operations=(
                    IntentOperation(
                        access=AccessMode.WRITE,
                        resource=ResourceRef(ResourceKind.FILE, "src/app.py"),
                    ),
                ),
            ),
        )
    )
    session = SwarmSession(
        session_id="swm-migrate",
        repository_root=str(tmp_path),
        repository_identity="a" * 64,
        base_commit="b" * 40,
        base_branch="main",
        root_task=RootTask(title="Migrate", goal="Migrate state."),
        integration_target=IntegrationTarget(branch="main"),
        work_graph=graph,
        budget_policy=SwarmBudgetPolicy(),
        graph_version=1,
        budget_version=1,
        state=SwarmSessionState.PLANNED,
        created_at="2026-08-01T00:00:00Z",
        updated_at="2026-08-01T00:00:00Z",
    )
    legacy = session.to_dict()
    legacy.pop("budget_policy")
    legacy.pop("budget_version")
    legacy.pop("budget_fingerprint")

    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE swarm_sessions (
            session_id TEXT PRIMARY KEY,
            repository_identity TEXT NOT NULL,
            base_commit TEXT NOT NULL,
            state TEXT NOT NULL,
            graph_version INTEGER NOT NULL,
            graph_fingerprint TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO swarm_sessions (
            session_id, repository_identity, base_commit, state,
            graph_version, graph_fingerprint, payload_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "swm-migrate",
            "a" * 64,
            "b" * 40,
            "planned",
            1,
            graph.fingerprint(),
            json.dumps(legacy),
            "2026-08-01T00:00:00Z",
            "2026-08-01T00:00:00Z",
        ),
    )
    connection.execute("PRAGMA user_version=1")
    connection.commit()
    connection.close()

    with SwarmSessionStore(database) as store:
        migrated = store.require("swm-migrate")
        version = int(store._connection.execute("PRAGMA user_version").fetchone()[0])

    assert version == 2
    assert migrated.budget_policy == SwarmBudgetPolicy()
    assert migrated.budget_version == 1
