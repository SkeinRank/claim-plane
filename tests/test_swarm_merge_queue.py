"""Deterministic merge queue and dependency-baseline integration."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from claim_plane.connectors.codex import init_project
from claim_plane.swarm import (
    create_swarm_session,
    get_swarm_merge_queue,
    get_swarm_scheduler,
    integrate_next_swarm_result,
    plan_swarm_concurrency,
    plan_swarm_merge_queue,
    provision_swarm_worktrees,
    run_codex_work_item,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Claim Plane Tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "src" / "b.py").write_text("b = 1\n", encoding="utf-8")
    (repo / "src" / "shared.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    init_project(repo)
    return repo


def _item(
    work_id: str,
    path: str,
    *,
    depends_on: tuple[str, ...] = (),
    region: str | None = None,
) -> dict[str, object]:
    resource: dict[str, object] = {"kind": "file", "identifier": path}
    if region is not None:
        resource["region"] = region
    return {
        "work_id": work_id,
        "title": work_id,
        "goal": f"Update {path}.",
        "depends_on": list(depends_on),
        "operations": [
            {
                "access": "write",
                "resource": resource,
            }
        ],
    }


def _session(
    repo: Path,
    items: list[dict[str, object]],
    session_id: str = "swm-merge",
) -> None:
    create_swarm_session(
        repo,
        session_id=session_id,
        spec={
            "protocol": "claim-plane.swarm-session-spec.v1",
            "root_task": {"title": "Merge queue", "goal": "Integrate workers."},
            "integration_target": {"branch": "main"},
            "work_graph": {
                "protocol": "claim-plane.swarm-work-graph.v1",
                "work_items": items,
            },
            "budget_policy": {
                "protocol": "claim-plane.swarm-budget-policy.v1",
                "workers": {
                    "max_active": 2,
                    "max_work_items": 8,
                    "max_total_launches": 8,
                },
                "resources": {"max_wall_time_seconds": 30},
            },
        },
    )
    plan_swarm_concurrency(repo, session_id)
    provision_swarm_worktrees(repo, session_id)


def _fake_codex(tmp_path: Path) -> Path:
    script = tmp_path / "fake-codex-merge"
    script.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys
if "--version" in sys.argv:
    print("codex-cli test")
    raise SystemExit(0)
prompt = sys.argv[-1]
root = pathlib.Path.cwd()
if "Work item: a " in prompt:
    (root / "src" / "a.py").write_text("a = 2\\n", encoding="utf-8")
elif "Work item: b " in prompt:
    if (root / "src" / "a.py").read_text(encoding="utf-8") != "a = 2\\n":
        raise SystemExit(9)
    (root / "src" / "b.py").write_text("b = 2\\n", encoding="utf-8")
elif "Work item: left " in prompt:
    (root / "src" / "shared.py").write_text("value = 'left'\\n", encoding="utf-8")
elif "Work item: right " in prompt:
    (root / "src" / "shared.py").write_text("value = 'right'\\n", encoding="utf-8")
args = sys.argv[1:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
print(json.dumps({"type": "thread.started", "thread_id": "thread-test"}), flush=True)
print(
    json.dumps(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }
    ),
    flush=True,
)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("done\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_dependency_is_released_only_after_integration_and_worker_sees_baseline(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _session(repo, [_item("a", "src/a.py"), _item("b", "src/b.py", depends_on=("a",))])
    codex = _fake_codex(tmp_path)

    initial = plan_swarm_merge_queue(repo, "swm-merge")
    assert initial["summary"]["status"] == "waiting"
    assert get_swarm_scheduler(repo, "swm-merge")["summary"][
        "dispatchable_work_ids"
    ] == ["a"]

    assert (
        run_codex_work_item(
            repo, "swm-merge", "a", codex_binary=str(codex)
        ).state.value
        == "succeeded"
    )
    blocked = get_swarm_scheduler(repo, "swm-merge")
    assert blocked["summary"]["dispatchable_work_ids"] == []
    assert "b" in {
        item["work_id"]
        for item in blocked["scheduler"]["work"]
        if item["state"] == "blocked"
    }

    first = integrate_next_swarm_result(repo, "swm-merge")
    assert first["integrated"] is True
    assert first["entry"]["work_id"] == "a"
    assert get_swarm_scheduler(repo, "swm-merge")["summary"][
        "dispatchable_work_ids"
    ] == ["b"]

    run_b = run_codex_work_item(repo, "swm-merge", "b", codex_binary=str(codex))
    assert run_b.state.value == "succeeded"
    second = integrate_next_swarm_result(repo, "swm-merge")
    assert second["entry"]["work_id"] == "b"
    assert second["summary"]["status"] == "completed"

    integration = Path(second["merge_queue"]["integration_worktree_path"])
    assert (integration / "src" / "a.py").read_text(encoding="utf-8") == "a = 2\n"
    assert (integration / "src" / "b.py").read_text(encoding="utf-8") == "b = 2\n"
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "a = 1\n"
    assert _git(repo, "rev-parse", "HEAD") == second["merge_queue"]["base_commit"]


def test_queue_order_is_deterministic_and_persisted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo, [_item("z", "src/b.py"), _item("a", "src/a.py")])

    first = plan_swarm_merge_queue(repo, "swm-merge")
    second = get_swarm_merge_queue(repo, "swm-merge")

    assert [entry["work_id"] for entry in first["merge_queue"]["entries"]] == ["a", "z"]
    assert first["queue_fingerprint"] == second["queue_fingerprint"]


def test_conflict_aborts_and_restores_previous_integration_head(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(
        repo,
        [
            _item("left", "src/shared.py", region="lines 1-1"),
            _item("right", "src/shared.py", region="lines 20-20"),
        ],
    )
    codex = _fake_codex(tmp_path)
    plan_swarm_merge_queue(repo, "swm-merge")

    run_codex_work_item(repo, "swm-merge", "left", codex_binary=str(codex))
    run_codex_work_item(repo, "swm-merge", "right", codex_binary=str(codex))
    left = integrate_next_swarm_result(repo, "swm-merge")
    previous_head = left["summary"]["integration_head"]

    right = integrate_next_swarm_result(repo, "swm-merge")
    assert right["integrated"] is False
    assert right["entry"]["state"] == "conflict"
    assert right["summary"]["status"] == "conflict"
    assert right["summary"]["integration_head"] == previous_head
    integration = Path(right["merge_queue"]["integration_worktree_path"])
    assert _git(integration, "rev-parse", "HEAD") == previous_head


def test_database_migrates_to_merge_queue_schema(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo, [_item("a", "src/a.py")])
    database = repo / ".claim-plane" / "swarm.db"
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE swarm_merge_queues")
    connection.execute("PRAGMA user_version=6")
    connection.commit()
    connection.close()

    plan_swarm_merge_queue(repo, "swm-merge")

    connection = sqlite3.connect(database)
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    connection.close()
    assert version == 7
    assert "swarm_merge_queues" in tables


def test_merge_next_requires_ready_entry(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo, [_item("a", "src/a.py")])
    plan_swarm_merge_queue(repo, "swm-merge")

    with pytest.raises(ValueError, match="no ready work item"):
        integrate_next_swarm_result(repo, "swm-merge")
