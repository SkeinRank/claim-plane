"""Operator UX, orchestration, logs, and offline demo regression coverage."""

from __future__ import annotations

import subprocess
from pathlib import Path

from claim_plane.connectors.codex import init_project
from claim_plane.swarm import (
    SWARM_OPERATOR_EVENT_PROTOCOL,
    SWARM_OPERATOR_SNAPSHOT_PROTOCOL,
    create_and_run_swarm_demo,
    create_swarm_session,
    get_swarm_operator_snapshot,
    list_swarm_operator_logs,
    start_swarm_session,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def _planned_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Claim Plane Tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    init_project(repo)
    create_swarm_session(
        repo,
        session_id="swm-operator",
        spec={
            "protocol": "claim-plane.swarm-session-spec.v1",
            "root_task": {"title": "Operator", "goal": "Update a.py."},
            "work_graph": {
                "protocol": "claim-plane.swarm-work-graph.v1",
                "work_items": [
                    {
                        "work_id": "a",
                        "title": "Update a",
                        "goal": "Update src/a.py.",
                        "operations": [
                            {
                                "access": "write",
                                "resource": {
                                    "kind": "file",
                                    "identifier": "src/a.py",
                                },
                            }
                        ],
                    }
                ],
            },
            "budget_policy": {
                "protocol": "claim-plane.swarm-budget-policy.v1",
                "workers": {
                    "max_active": 1,
                    "max_work_items": 4,
                    "max_total_launches": 4,
                },
                "resources": {
                    "max_total_tokens": 1000,
                    "max_wall_time_seconds": 120,
                },
            },
        },
    )
    return repo


def test_prepare_only_materializes_operator_prerequisites(tmp_path: Path) -> None:
    repo = _planned_repo(tmp_path)

    result = start_swarm_session(
        repo,
        "swm-operator",
        prepare_only=True,
        codex_binary="not-needed-for-prepare-only",
    )

    assert result["status"] == "prepared"
    assert [event["stage"] for event in result["events"]] == [
        "plan",
        "admission",
        "worktrees",
        "merge_queue",
    ]
    snapshot = result["snapshot"]
    assert snapshot["protocol"] == SWARM_OPERATOR_SNAPSHOT_PROTOCOL
    assert snapshot["phase"] == "ready"
    assert snapshot["usage"]["runs"] == 0
    assert snapshot["work"][0]["next_action"] == "dispatch"


def test_offline_demo_reaches_swarm_verified_with_parallel_first_wave(
    tmp_path: Path,
) -> None:
    result = create_and_run_swarm_demo(tmp_path / "demo")
    run = result["result"]

    assert run["verified"] is True
    assert run["status"] == "verified"
    assert run["errors"] == []
    snapshot = run["snapshot"]
    assert snapshot["phase"] == "verified"
    assert snapshot["usage"]["runs"] == 3
    assert snapshot["usage"]["total_tokens"] == 90
    assert snapshot["verification"]["verified"] is True
    assert {item["work_id"] for item in snapshot["work"]} == {
        "greeting",
        "arithmetic",
        "integration-summary",
    }
    assert all(item["verified"] for item in snapshot["work"])
    dispatches = [
        event["work_ids"] for event in run["events"] if event["stage"] == "dispatch"
    ]
    assert set(dispatches[0]) == {"greeting", "arithmetic"}
    assert dispatches[1] == ["integration-summary"]


def test_operator_status_and_logs_are_read_only_and_idempotent(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    first = create_and_run_swarm_demo(repo)["result"]

    snapshot = get_swarm_operator_snapshot(repo, "swm-demo")
    repeated = start_swarm_session(
        repo,
        "swm-demo",
        codex_binary=str(repo / ".claim-plane" / "demo-codex"),
    )
    events = list_swarm_operator_logs(
        repo,
        "swm-demo",
        include_codex_events=False,
        limit=20,
    )

    assert first["verified"] is True
    assert repeated["verified"] is True
    assert repeated["events"] == []
    assert snapshot["protocol"] == SWARM_OPERATOR_SNAPSHOT_PROTOCOL
    assert snapshot["usage"]["runs"] == 3
    assert events
    assert all(event["protocol"] == SWARM_OPERATOR_EVENT_PROTOCOL for event in events)
    assert events[-1]["event"] == "verification.verified"
    assert any(event["event"] == "merge.integrated" for event in events)
    assert any(event["event"] == "worker.succeeded" for event in events)


def test_operator_log_filter_and_limit(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    create_and_run_swarm_demo(repo)

    events = list_swarm_operator_logs(
        repo,
        "swm-demo",
        work_id="greeting",
        include_codex_events=False,
        limit=2,
    )

    assert len(events) == 2
    assert all(event["work_id"] in {None, "greeting"} for event in events)
