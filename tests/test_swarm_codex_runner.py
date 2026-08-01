"""Headless Codex runner lifecycle, budgets, waves, and cancellation."""

from __future__ import annotations

import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

from claim_plane.connectors.codex import init_project
from claim_plane.swarm import (
    CodexRunState,
    cancel_codex_run,
    create_swarm_session,
    inspect_swarm_worktrees,
    list_codex_runs,
    plan_swarm_concurrency,
    provision_swarm_worktrees,
    run_codex_work_item,
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
    (repo / "src" / "a.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "src" / "b.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    init_project(repo)
    return repo


def _item(
    work_id: str, path: str, *, depends_on: tuple[str, ...] = ()
) -> dict[str, object]:
    return {
        "work_id": work_id,
        "title": f"Work {work_id}",
        "goal": f"Update {path}.",
        "depends_on": list(depends_on),
        "operations": [
            {
                "access": "write",
                "resource": {"kind": "file", "identifier": path},
            }
        ],
        "preserves": ["Do not change public APIs."],
        "acceptance": ["python -m compileall src"],
    }


def _session(
    repo: Path,
    *,
    session_id: str = "swm-runner",
    dependent: bool = False,
    max_active: int = 2,
    max_tokens: int = 1000,
    max_restarts: int = 1,
) -> None:
    items = [
        _item("a", "src/a.py"),
        _item("b", "src/b.py", depends_on=("a",) if dependent else ()),
    ]
    spec = {
        "protocol": "claim-plane.swarm-session-spec.v1",
        "root_task": {"title": "Runner test", "goal": "Update files."},
        "work_graph": {
            "protocol": "claim-plane.swarm-work-graph.v1",
            "work_items": items,
        },
        "budget_policy": {
            "protocol": "claim-plane.swarm-budget-policy.v1",
            "workers": {
                "max_active": max_active,
                "max_work_items": 16,
                "max_total_launches": 16,
            },
            "resources": {
                "max_total_tokens": max_tokens,
                "max_cost_usd": "10",
                "max_wall_time_seconds": 30,
            },
            "retries": {"max_agent_restarts": max_restarts},
        },
    }
    create_swarm_session(repo, spec=spec, session_id=session_id)
    plan_swarm_concurrency(repo, session_id)
    provision_swarm_worktrees(repo, session_id)


def _fake_codex(tmp_path: Path) -> Path:
    script = tmp_path / "fake-codex"
    script.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import time

if "--version" in sys.argv:
    print("codex-cli 0.143.0")
    raise SystemExit(0)

mode = os.environ.get("FAKE_CODEX_MODE", "success")
args = sys.argv[1:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
print(json.dumps({"type": "thread.started", "thread_id": "thread-test-001"}), flush=True)
if mode == "sleep":
    time.sleep(10)
usage = 900 if mode == "token" else 40
print(json.dumps({"type": "item.completed", "item": {"id": "m1", "type": "agent_message", "text": "done"}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": usage, "cached_input_tokens": 10, "output_tokens": 10, "reasoning_output_tokens": 2}}), flush=True)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("fake final message\\n", encoding="utf-8")
raise SystemExit(7 if mode == "fail" else 0)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_runner_executes_codex_in_owned_worktree_and_persists_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _session(repo)
    codex = _fake_codex(tmp_path)
    monkeypatch.setenv("FAKE_CODEX_MODE", "success")

    result = run_codex_work_item(
        repo,
        "swm-runner",
        "a",
        codex_binary=str(codex),
        timeout_seconds=10,
        token_limit=300,
    )

    assert result.state is CodexRunState.SUCCEEDED
    assert result.exit_code == 0
    assert result.codex_thread_id == "thread-test-001"
    assert result.usage.total_tokens == 50
    assert Path(result.events_path).read_text(encoding="utf-8").count("\n") == 3
    assert (
        Path(result.final_message_path).read_text(encoding="utf-8")
        == "fake final message\n"
    )
    assert result.command[0] == str(codex.resolve())
    assert "Work item: a" in Path(result.run_directory, "prompt.txt").read_text(
        encoding="utf-8"
    )
    records = list_codex_runs(repo, "swm-runner")
    assert [record.run_id for record in records] == [result.run_id]
    health = inspect_swarm_worktrees(repo, "swm-runner")
    assert {item["health"] for item in health["worktrees"]} == {"ready"}
    bound = next(
        item for item in health["worktrees"] if item["record"]["work_id"] == "a"
    )
    assert bound["record"]["worker_id"] == result.run_id
    assert bound["record"]["intent_id"] is None


def test_runner_enforces_dependency_waves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _session(repo, dependent=True)
    codex = _fake_codex(tmp_path)
    monkeypatch.setenv("FAKE_CODEX_MODE", "success")

    with pytest.raises(ValueError, match="not runnable in current wave"):
        run_codex_work_item(repo, "swm-runner", "b", codex_binary=str(codex))

    first = run_codex_work_item(repo, "swm-runner", "a", codex_binary=str(codex))
    second = run_codex_work_item(repo, "swm-runner", "b", codex_binary=str(codex))
    assert first.state is CodexRunState.SUCCEEDED
    assert second.state is CodexRunState.SUCCEEDED


def test_runner_classifies_nonzero_exit_and_restart_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _session(repo, max_restarts=0)
    codex = _fake_codex(tmp_path)
    monkeypatch.setenv("FAKE_CODEX_MODE", "fail")

    failed = run_codex_work_item(repo, "swm-runner", "a", codex_binary=str(codex))
    assert failed.state is CodexRunState.FAILED
    assert failed.exit_code == 7
    with pytest.raises(ValueError, match="restart budget is exhausted"):
        run_codex_work_item(repo, "swm-runner", "a", codex_binary=str(codex))


def test_runner_stops_on_token_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _session(repo, max_tokens=200)
    codex = _fake_codex(tmp_path)
    monkeypatch.setenv("FAKE_CODEX_MODE", "token")

    result = run_codex_work_item(
        repo,
        "swm-runner",
        "a",
        codex_binary=str(codex),
        token_limit=100,
    )
    assert result.state is CodexRunState.TOKEN_BUDGET_EXCEEDED
    assert result.usage.total_tokens == 910
    assert result.termination_reason == "token_budget_exceeded"


def test_runner_enforces_active_worker_per_item_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _session(repo, max_active=2)
    codex = _fake_codex(tmp_path)
    monkeypatch.setenv("FAKE_CODEX_MODE", "sleep")
    holder: dict[str, object] = {}

    def execute() -> None:
        holder["record"] = run_codex_work_item(
            repo,
            "swm-runner",
            "a",
            codex_binary=str(codex),
            timeout_seconds=8,
        )

    thread = threading.Thread(target=execute)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        records = list_codex_runs(repo, "swm-runner")
        if records and records[0].state is CodexRunState.RUNNING:
            break
        time.sleep(0.05)
    else:
        pytest.fail("first Codex run did not enter running state")

    with pytest.raises(
        ValueError, match="workers.max_active_per_work_item is exhausted"
    ):
        run_codex_work_item(repo, "swm-runner", "a", codex_binary=str(codex))
    cancel_codex_run(repo, records[0].run_id)
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert holder["record"].state is CodexRunState.CANCELLED  # type: ignore[union-attr]


def test_runner_timeout_is_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _session(repo)
    codex = _fake_codex(tmp_path)
    monkeypatch.setenv("FAKE_CODEX_MODE", "sleep")

    result = run_codex_work_item(
        repo,
        "swm-runner",
        "a",
        codex_binary=str(codex),
        timeout_seconds=1,
    )
    assert result.state is CodexRunState.TIMED_OUT
    assert result.termination_reason == "wall_time_budget_exceeded"
    assert list_codex_runs(repo, "swm-runner")[0].state is CodexRunState.TIMED_OUT



def test_runner_rejects_symlinked_evidence_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _session(repo)
    codex = _fake_codex(tmp_path)
    monkeypatch.setenv("FAKE_CODEX_MODE", "success")
    outside = tmp_path / "outside"
    outside.mkdir()
    runs = repo / ".claim-plane" / "swarm" / "runs"
    runs.parent.mkdir(parents=True, exist_ok=True)
    runs.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="must not contain a symlink"):
        run_codex_work_item(repo, "swm-runner", "a", codex_binary=str(codex))

    assert list_codex_runs(repo, "swm-runner") == []
    assert list(outside.iterdir()) == []

def test_database_migrates_to_codex_run_schema(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo)
    database = repo / ".claim-plane" / "swarm.db"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version=4")
    connection.commit()
    connection.close()

    # Opening through the public service performs the migration.
    assert list_codex_runs(repo, "swm-runner") == []
    connection = sqlite3.connect(database)
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    connection.close()
    assert version == 9
    assert "swarm_codex_runs" in tables
