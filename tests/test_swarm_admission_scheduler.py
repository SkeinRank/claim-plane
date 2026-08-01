"""Shared swarm admission and dynamic dependency scheduling."""

from __future__ import annotations

import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from claim_plane.connectors.codex import init_project
from claim_plane.swarm import (
    SharedAdmissionStatus,
    admit_swarm_session,
    create_swarm_session,
    get_swarm_admission,
    get_swarm_scheduler,
    plan_swarm_concurrency,
    provision_swarm_worktrees,
    replace_swarm_work_graph,
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
    for name in ("a.py", "b.py", "shared.py"):
        (repo / "src" / name).write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    init_project(repo)
    return repo


def _item(
    work_id: str,
    path: str,
    *,
    depends_on: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "work_id": work_id,
        "title": work_id,
        "goal": f"Update {path}.",
        "depends_on": list(depends_on),
        "operations": [
            {
                "access": "write",
                "resource": {"kind": "file", "identifier": path},
            }
        ],
        "acceptance": ["python -m compileall src"],
    }


def _create(
    repo: Path,
    items: list[dict[str, object]],
    *,
    session_id: str = "swm-admission",
    max_active: int = 2,
    max_restarts: int = 1,
) -> None:
    create_swarm_session(
        repo,
        session_id=session_id,
        spec={
            "protocol": "claim-plane.swarm-session-spec.v1",
            "root_task": {"title": "Shared admission", "goal": "Update code."},
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
                "resources": {"max_wall_time_seconds": 30},
                "retries": {"max_agent_restarts": max_restarts},
            },
        },
    )
    plan_swarm_concurrency(repo, session_id)


def _fake_codex(tmp_path: Path, *, exit_code: int = 0) -> Path:
    script = tmp_path / f"fake-codex-{exit_code}"
    script.write_text(
        f"""#!/usr/bin/env python3
import json
import pathlib
import sys
if "--version" in sys.argv:
    print("codex-cli test")
    raise SystemExit(0)
args = sys.argv[1:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
print(json.dumps({{"type": "thread.started", "thread_id": "thread-test"}}), flush=True)
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 10, "output_tokens": 5}}}}), flush=True)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("done\\n", encoding="utf-8")
raise SystemExit({exit_code})
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script




def _sleeping_fake_codex(tmp_path: Path) -> Path:
    script = tmp_path / "fake-codex-sleeping"
    script.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys
import time
if "--version" in sys.argv:
    print("codex-cli test")
    raise SystemExit(0)
args = sys.argv[1:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
print(json.dumps({"type": "thread.started", "thread_id": "thread-test"}), flush=True)
time.sleep(0.5)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}), flush=True)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("done\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script

def test_shared_admission_turns_serialization_into_effective_dependency(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _create(repo, [_item("a", "src/shared.py"), _item("b", "src/shared.py")])

    result = admit_swarm_session(repo, "swm-admission")

    assert result["summary"]["status"] == SharedAdmissionStatus.READY.value
    records = {
        item["work_id"]: item for item in result["shared_admission"]["admissions"]
    }
    assert records["a"]["effective_dependencies"] == []
    assert records["b"]["effective_dependencies"] == ["a"]
    scheduler = get_swarm_scheduler(repo, "swm-admission")
    assert scheduler["summary"]["dispatchable_work_ids"] == ["a"]


def test_scheduler_releases_dependency_after_successful_execution(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _create(
        repo,
        [_item("a", "src/a.py"), _item("b", "src/b.py", depends_on=("a",))],
    )
    admit_swarm_session(repo, "swm-admission")
    provision_swarm_worktrees(repo, "swm-admission")
    codex = _fake_codex(tmp_path)

    before = get_swarm_scheduler(repo, "swm-admission")
    assert before["summary"]["dispatchable_work_ids"] == ["a"]
    run_codex_work_item(repo, "swm-admission", "a", codex_binary=str(codex))
    after = get_swarm_scheduler(repo, "swm-admission")
    assert after["summary"]["dispatchable_work_ids"] == ["b"]
    states = {item["work_id"]: item["state"] for item in after["scheduler"]["work"]}
    assert states == {"a": "succeeded", "b": "runnable"}


def test_failed_dependency_with_exhausted_retry_budget_blocks_dependents(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _create(
        repo,
        [_item("a", "src/a.py"), _item("b", "src/b.py", depends_on=("a",))],
        max_restarts=0,
    )
    admit_swarm_session(repo, "swm-admission")
    provision_swarm_worktrees(repo, "swm-admission")
    codex = _fake_codex(tmp_path, exit_code=7)

    run_codex_work_item(repo, "swm-admission", "a", codex_binary=str(codex))
    result = get_swarm_scheduler(repo, "swm-admission")
    states = {item["work_id"]: item for item in result["scheduler"]["work"]}
    assert states["a"]["state"] == "failed"
    assert states["b"]["state"] == "blocked"
    assert "dependency retry budget exhausted" in states["b"]["detail"]
    assert result["summary"]["dispatchable_work_ids"] == []


def test_graph_replacement_invalidates_shared_admission(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _create(repo, [_item("a", "src/a.py")])
    admit_swarm_session(repo, "swm-admission")

    replace_swarm_work_graph(
        repo,
        "swm-admission",
        expected_version=1,
        graph_data={
            "protocol": "claim-plane.swarm-work-graph.v1",
            "work_items": [_item("b", "src/b.py")],
        },
    )

    with pytest.raises(KeyError, match="no shared admission"):
        get_swarm_admission(repo, "swm-admission")


def test_runner_auto_creates_shared_admission_for_020_workflow(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _create(repo, [_item("a", "src/a.py")])
    provision_swarm_worktrees(repo, "swm-admission")
    codex = _fake_codex(tmp_path)

    run_codex_work_item(repo, "swm-admission", "a", codex_binary=str(codex))

    result = get_swarm_admission(repo, "swm-admission")
    assert result["summary"]["status"] == "ready"


def test_database_migrates_to_shared_admission_schema(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _create(repo, [_item("a", "src/a.py")])
    database = repo / ".claim-plane" / "swarm.db"
    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE swarm_shared_admissions")
    connection.execute("PRAGMA user_version=5")
    connection.commit()
    connection.close()

    admit_swarm_session(repo, "swm-admission")

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
    assert "swarm_shared_admissions" in tables

def test_atomic_scheduler_reservation_prevents_stale_double_dispatch(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _create(
        repo,
        [_item("a", "src/a.py"), _item("b", "src/b.py")],
        max_active=1,
    )
    admit_swarm_session(repo, "swm-admission")
    provision_swarm_worktrees(repo, "swm-admission")
    codex = _sleeping_fake_codex(tmp_path)
    barrier = threading.Barrier(2)

    def launch(work_id: str) -> object:
        barrier.wait(timeout=5)
        return run_codex_work_item(
            repo,
            "swm-admission",
            work_id,
            codex_binary=str(codex),
        )

    outcomes: list[object] = []
    errors: list[Exception] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(launch, work_id) for work_id in ("a", "b")]
        for future in futures:
            try:
                outcomes.append(future.result(timeout=10))
            except Exception as exc:  # noqa: BLE001
                # The competing launch must fail closed after atomic reservation.
                errors.append(exc)

    assert len(outcomes) == 1
    assert len(errors) == 1
    assert "dispatchable" in str(errors[0]) or "max_active" in str(errors[0])

