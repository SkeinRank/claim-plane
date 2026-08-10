from __future__ import annotations

import json
import subprocess
from pathlib import Path

from claim_plane.connectors.codex import init_project
from claim_plane.swarm import (
    MergeEntryState,
    MergeQueueEntry,
    create_swarm_session,
    get_swarm_scheduler,
    integrate_next_swarm_result,
    list_integration_rescues,
    plan_swarm_concurrency,
    plan_swarm_merge_queue,
    provision_swarm_worktrees,
    rescue_swarm_integration,
    run_codex_work_item,
)
from claim_plane.swarm.service import _store


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Claim Plane Tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    (repo / "app.py").write_text(
        "def greet(name: str) -> str:\n    return name\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    init_project(repo)
    return repo


def _session(repo: Path, session_id: str, *, semantic: bool = False) -> None:
    operations: list[dict[str, object]] = [
        {
            "access": "write",
            "resource": {"kind": "file", "identifier": "app.py"},
        }
    ]
    if semantic:
        operations.append(
            {
                "access": "write",
                "resource": {
                    "kind": "symbol",
                    "identifier": "greet",
                    "metadata": {
                        "path": "app.py",
                        "language": "python",
                        "qualified_identifier": "greet",
                    },
                },
            }
        )
    create_swarm_session(
        repo,
        session_id=session_id,
        spec={
            "protocol": "claim-plane.swarm-session-spec.v1",
            "root_task": {"title": "Rescue", "goal": "Exercise rescue."},
            "integration_target": {"branch": "main"},
            "work_graph": {
                "protocol": "claim-plane.swarm-work-graph.v1",
                "work_items": [
                    {
                        "work_id": "worker",
                        "title": "worker",
                        "goal": "Update the greeting.",
                        "operations": operations,
                    }
                ],
            },
            "budget_policy": {
                "protocol": "claim-plane.swarm-budget-policy.v1",
                "workers": {"max_active": 1, "max_work_items": 4, "max_total_launches": 4},
                "resources": {"max_wall_time_seconds": 60},
                "retries": {"max_repairs_per_work_item": 2},
                "concurrency": {
                    "same_file": "region_safe",
                    "unknown_overlap": "serialize",
                    "shared_contract": "serialize",
                    "schema_change": "serialize",
                },
            },
        },
    )
    plan_swarm_concurrency(repo, session_id)
    provision_swarm_worktrees(repo, session_id)
    plan_swarm_merge_queue(repo, session_id)


def _fake_codex(tmp_path: Path) -> Path:
    script = tmp_path / "fake-codex-rescue"
    script.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys
if "--version" in sys.argv:
    print("codex-cli test")
    raise SystemExit(0)
root = pathlib.Path.cwd()
path = root / "app.py"
text = path.read_text(encoding="utf-8")
text = text.replace("return name", "return name.strip()")
path.write_text(text, encoding="utf-8")
args = sys.argv[1:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
print(json.dumps({"type": "thread.started", "thread_id": "thread-rescue"}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 3}}), flush=True)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("done\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _force_textual_conflict(repo: Path, session_id: str) -> str:
    planned = plan_swarm_merge_queue(repo, session_id)
    queue = planned["merge_queue"]
    ready = next(item for item in queue["entries"] if item["state"] == "ready")
    now = "2026-08-10T00:00:00Z"
    with _store(repo) as store:
        claimed_queue, _, claimed = store.claim_merge_entry(
            session_id,
            ready["work_id"],
            expected_queue_fingerprint=planned["queue_fingerprint"],
            updated_at=now,
        )
        conflict = MergeQueueEntry(
            work_id=claimed.work_id,
            order=claimed.order,
            effective_dependencies=claimed.effective_dependencies,
            source_branch=claimed.source_branch,
            state=MergeEntryState.CONFLICT,
            run_id=claimed.run_id,
            conflict_paths=("app.py",),
            detail="synthetic textual conflict",
        )
        store.finish_merge_entry(
            session_id,
            conflict,
            integration_head=claimed_queue.integration_head,
            updated_at=now,
        )
    return str(claimed.run_id)


def test_textual_conflict_prepares_bounded_serial_rerun(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo, "swm-rescue-serial")
    codex = _fake_codex(tmp_path)
    first = run_codex_work_item(repo, "swm-rescue-serial", "worker", codex_binary=str(codex))
    assert first.state.value == "succeeded"
    old_run_id = _force_textual_conflict(repo, "swm-rescue-serial")

    rescue = rescue_swarm_integration(repo, "swm-rescue-serial")

    assert rescue["prepared"] is True
    assert rescue["decision"]["disposition"] == "serial_rerun"
    assert rescue["decision"]["reason"] == "textual_integration_conflict"
    assert rescue["decision"]["source_run_id"] == old_run_id
    scheduler = get_swarm_scheduler(repo, "swm-rescue-serial")
    assert scheduler["summary"]["dispatchable_work_ids"] == ["worker"]

    second = run_codex_work_item(repo, "swm-rescue-serial", "worker", codex_binary=str(codex))
    assert second.state.value == "succeeded"
    assert second.attempt == 2
    assert second.base_commit == rescue["decision"]["integration_head"]
    integrated = integrate_next_swarm_result(repo, "swm-rescue-serial")
    assert integrated["integrated"] is True
    rescues = list_integration_rescues(repo, "swm-rescue-serial")
    assert len(rescues) == 1
    assert rescues[0]["prepared"] is True


def test_authority_failure_never_auto_repairs(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo, "swm-rescue-authority", semantic=True)
    codex = tmp_path / "fake-codex-wrong-scope"
    codex.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys
if "--version" in sys.argv:
    print("codex-cli test")
    raise SystemExit(0)
root = pathlib.Path.cwd()
path = root / "other.py"
path.write_text("value = 1\\n", encoding="utf-8")
args = sys.argv[1:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
print(json.dumps({"type": "thread.started", "thread_id": "wrong"}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 2}}), flush=True)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text("done\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    assert run_codex_work_item(
        repo, "swm-rescue-authority", "worker", codex_binary=str(codex)
    ).state.value == "succeeded"
    merged = integrate_next_swarm_result(repo, "swm-rescue-authority")
    assert merged["integrated"] is False

    rescue = rescue_swarm_integration(repo, "swm-rescue-authority")

    assert rescue["prepared"] is False
    assert rescue["decision"]["disposition"] == "manual"
    assert rescue["decision"]["reason"] == "authority_violation"
    assert get_swarm_scheduler(repo, "swm-rescue-authority")["summary"]["dispatchable_work_ids"] == []
