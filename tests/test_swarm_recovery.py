"""Swarm crash recovery, pause/resume, cancellation, and worker replacement."""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claim_plane.connectors.codex import init_project
from claim_plane.swarm import (
    CodexRunState,
    SwarmSessionState,
    cancel_swarm_session,
    create_swarm_session,
    get_swarm_scheduler,
    get_swarm_session,
    inspect_swarm_recovery,
    inspect_swarm_worktrees,
    list_swarm_recovery_events,
    pause_swarm_session,
    plan_swarm_concurrency,
    provision_swarm_worktrees,
    recover_swarm_session,
    replace_codex_worker,
    resume_swarm_session,
)
from claim_plane.swarm.codex_runner import _reserve_run
from claim_plane.swarm.service import _store


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
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    init_project(repo)
    return repo


def _fake_codex(tmp_path: Path) -> Path:
    script = tmp_path / "fake-codex"
    script.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys
if "--version" in sys.argv:
    print("codex-cli test")
    raise SystemExit(0)
args = sys.argv[1:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
print(json.dumps({"type": "thread.started", "thread_id": "replacement-thread"}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}), flush=True)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("replacement complete\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _session(repo: Path, *, session_id: str = "swm-recovery") -> None:
    create_swarm_session(
        repo,
        session_id=session_id,
        spec={
            "protocol": "claim-plane.swarm-session-spec.v1",
            "root_task": {"title": "Recovery", "goal": "Update a.py."},
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
                "retries": {"max_agent_restarts": 1},
            },
        },
    )
    plan_swarm_concurrency(repo, session_id)
    provision_swarm_worktrees(repo, session_id)


def _expired_reservation(repo: Path, executable: Path):
    record, _ = _reserve_run(
        repo,
        "swm-recovery",
        "a",
        executable=str(executable),
        model=None,
        reasoning_effort=None,
        timeout_seconds=30,
        token_limit=100,
    )
    old = (
        (datetime.now(timezone.utc) - timedelta(minutes=5))
        .isoformat()
        .replace("+00:00", "Z")
    )
    expired = replace(
        record,
        updated_at=old,
        heartbeat_at=old,
        lease_expires_at=old,
    )
    with _store(repo) as store:
        store.update_codex_run(expired)
    return expired


def test_recovery_marks_expired_reservation_lost_and_is_idempotent(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _session(repo)
    source = _expired_reservation(repo, _fake_codex(tmp_path))

    inspection = inspect_swarm_recovery(repo, "swm-recovery", stale_after_seconds=1)
    assert inspection["summary"]["lost"] == 1

    result = recover_swarm_session(repo, "swm-recovery", stale_after_seconds=1)
    assert result["recovered_count"] == 1
    assert result["recovered_runs"][0]["state"] == CodexRunState.LOST.value
    assert result["recovered_runs"][0]["run_id"] == source.run_id
    assert get_swarm_scheduler(repo, "swm-recovery")["summary"]["states"] == {
        "runnable": 1
    }

    repeated = recover_swarm_session(repo, "swm-recovery", stale_after_seconds=1)
    assert repeated["recovered_count"] == 0
    assert repeated["idempotent"] is True
    events = list_swarm_recovery_events(repo, "swm-recovery")
    assert [event.action for event in events] == ["run_recovered"]


def test_replacement_rechecks_authority_and_uses_fresh_run_identity(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _session(repo)
    codex = _fake_codex(tmp_path)
    source = _expired_reservation(repo, codex)
    recover_swarm_session(repo, "swm-recovery", stale_after_seconds=1)

    replacement = replace_codex_worker(
        repo,
        "swm-recovery",
        "a",
        replaced_run_id=source.run_id,
        codex_binary=str(codex),
    )

    assert replacement.state is CodexRunState.SUCCEEDED
    assert replacement.run_id != source.run_id
    assert replacement.replacement_of_run_id == source.run_id
    assert replacement.attempt == 2
    assert replacement.codex_thread_id == "replacement-thread"
    assert replacement.intent_id is None
    assert replacement.recovery_generation > source.recovery_generation
    events = list_swarm_recovery_events(repo, "swm-recovery")
    assert [event.action for event in events] == [
        "run_recovered",
        "worker_replaced",
    ]


def test_replacement_refuses_predecessor_changes_without_explicit_reset(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _session(repo)
    codex = _fake_codex(tmp_path)
    source = _expired_reservation(repo, codex)
    recover_swarm_session(repo, "swm-recovery", stale_after_seconds=1)
    worktrees = inspect_swarm_worktrees(repo, "swm-recovery")
    worktree = Path(worktrees["worktrees"][0]["record"]["worktree_path"])
    (worktree / "src" / "a.py").write_text("partial = True\n", encoding="utf-8")

    with pytest.raises(ValueError, match="predecessor changes"):
        replace_codex_worker(
            repo,
            "swm-recovery",
            "a",
            replaced_run_id=source.run_id,
            codex_binary=str(codex),
        )

    replacement = replace_codex_worker(
        repo,
        "swm-recovery",
        "a",
        replaced_run_id=source.run_id,
        reset_worktree=True,
        codex_binary=str(codex),
    )
    assert replacement.state is CodexRunState.SUCCEEDED
    assert (worktree / "src" / "a.py").read_text(encoding="utf-8") == "value = 1\n"


def test_pause_resume_and_cancel_are_durable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo)

    paused = pause_swarm_session(repo, "swm-recovery")
    assert paused["session"]["state"] == SwarmSessionState.PAUSED.value
    resumed = resume_swarm_session(repo, "swm-recovery")
    assert resumed["session"]["state"] == SwarmSessionState.PLANNED.value
    with _store(repo) as store:
        store.set_session_state(
            "swm-recovery",
            target=SwarmSessionState.RUNNING,
            allowed_from={SwarmSessionState.PLANNED},
            updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
    pause_swarm_session(repo, "swm-recovery")
    resumed_running = resume_swarm_session(repo, "swm-recovery")
    assert resumed_running["session"]["state"] == SwarmSessionState.RUNNING.value
    cancelled = cancel_swarm_session(repo, "swm-recovery")
    assert cancelled["session"]["state"] == SwarmSessionState.CANCELLED.value
    assert get_swarm_session(repo, "swm-recovery").state is SwarmSessionState.CANCELLED
    with pytest.raises(ValueError, match="cannot resume"):
        resume_swarm_session(repo, "swm-recovery")


def test_recovery_reopens_interrupted_verification(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo)
    with _store(repo) as store:
        store.set_session_state(
            "swm-recovery",
            target=SwarmSessionState.VERIFYING,
            allowed_from={SwarmSessionState.PLANNED},
            updated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )

    result = recover_swarm_session(repo, "swm-recovery")
    assert result["session_state"] == SwarmSessionState.RUNNING.value
    assert list_swarm_recovery_events(repo, "swm-recovery")[-1].action == (
        "verification_reopened"
    )


def test_cancelled_active_reservation_recovers_as_cancelled(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo)
    source = _expired_reservation(repo, _fake_codex(tmp_path))

    cancelled = cancel_swarm_session(repo, "swm-recovery")
    assert cancelled["active_runs"][0]["state"] == CodexRunState.CANCELLING.value

    recovered = recover_swarm_session(repo, "swm-recovery", stale_after_seconds=1)
    assert recovered["recovered_count"] == 1
    assert recovered["recovered_runs"][0]["run_id"] == source.run_id
    assert recovered["recovered_runs"][0]["state"] == CodexRunState.CANCELLED.value
    assert (
        recovered["recovered_runs"][0]["termination_reason"]
        == "recovered_cancellation_completed"
    )


def test_live_process_with_expired_lease_requires_explicit_reclaim(
    tmp_path: Path,
) -> None:
    import os

    repo = _repo(tmp_path)
    _session(repo)
    source = _expired_reservation(repo, _fake_codex(tmp_path))
    running = replace(
        source,
        state=CodexRunState.RUNNING,
        runner_pid=os.getpid(),
        agent_pid=os.getpid(),
    )
    with _store(repo) as store:
        store.update_codex_run(running)

    status = inspect_swarm_recovery(repo, "swm-recovery", stale_after_seconds=1)
    assert status["summary"]["stale"] == 1
    recovered = recover_swarm_session(repo, "swm-recovery", stale_after_seconds=1)
    assert recovered["recovered_count"] == 0
    assert recovered["stale_requires_termination"] == [source.run_id]


def test_explicit_reclaim_terminates_stale_process_before_releasing_slot(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _session(repo)
    source = _expired_reservation(repo, _fake_codex(tmp_path))
    process = subprocess.Popen(
        ["python", "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    running = replace(
        source,
        state=CodexRunState.RUNNING,
        runner_pid=process.pid,
        agent_pid=process.pid,
    )
    with _store(repo) as store:
        store.update_codex_run(running)

    recovered = recover_swarm_session(
        repo,
        "swm-recovery",
        stale_after_seconds=1,
        terminate_stale=True,
    )
    process.wait(timeout=5)
    assert recovered["stale_requires_termination"] == []
    assert recovered["recovered_count"] == 1
    assert recovered["recovered_runs"][0]["state"] == CodexRunState.LOST.value
    assert (
        recovered["recovered_runs"][0]["metadata"]["stale_process_terminated"] is True
    )


def test_database_migrates_to_recovery_schema(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo)
    database = repo / ".claim-plane" / "swarm.db"
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE swarm_recovery_events")
    connection.execute("PRAGMA user_version=8")
    connection.commit()
    connection.close()

    inspect_swarm_recovery(repo, "swm-recovery")

    connection = sqlite3.connect(database)
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    connection.close()
    assert version == 9
    assert "swarm_recovery_events" in tables
