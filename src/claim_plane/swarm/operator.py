"""Operator-facing orchestration, status, logs, and deterministic swarm demo."""

from __future__ import annotations

import json
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from claim_plane.connectors.codex import init_project
from claim_plane.swarm.codex_runner import run_codex_work_item
from claim_plane.swarm.merge_queue import MergeEntryState, MergeQueueStatus
from claim_plane.swarm.merge_service import (
    drain_swarm_merge_queue,
    get_swarm_merge_queue,
    plan_swarm_merge_queue,
)
from claim_plane.swarm.models import SwarmSessionState
from claim_plane.swarm.recovery import (
    inspect_swarm_recovery,
    replace_codex_worker,
)
from claim_plane.swarm.runs import CodexRunRecord, CodexRunState
from claim_plane.swarm.service import (
    admit_swarm_session,
    create_swarm_session,
    get_swarm_scheduler,
    get_swarm_session,
    inspect_swarm_worktrees,
    plan_swarm_concurrency,
    provision_swarm_worktrees,
    resolve_repository_root,
)
from claim_plane.swarm.verification import (
    SwarmVerificationStatus,
    get_swarm_verification,
    verify_swarm_session,
)

SWARM_OPERATOR_SNAPSHOT_PROTOCOL = "claim-plane.swarm-operator-snapshot.v1"
SWARM_OPERATOR_EVENT_PROTOCOL = "claim-plane.swarm-operator-event.v1"

_TERMINAL_STATES = {
    SwarmSessionState.COMPLETED,
    SwarmSessionState.FAILED,
    SwarmSessionState.CANCELLED,
}
_ACTIVE_RUN_STATES = {
    CodexRunState.RESERVED,
    CodexRunState.RUNNING,
    CodexRunState.CANCELLING,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_get(call: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
    try:
        return call()
    except KeyError:
        return None


def _latest_runs(records: list[CodexRunRecord]) -> dict[str, CodexRunRecord]:
    latest: dict[str, CodexRunRecord] = {}
    for record in sorted(records, key=lambda item: (item.attempt, item.created_at)):
        latest[record.work_id] = record
    return latest


def _operator_phase(
    state: SwarmSessionState,
    scheduler: Mapping[str, Any] | None,
    merge_queue: Mapping[str, Any] | None,
    verification: Mapping[str, Any] | None,
) -> str:
    if state is SwarmSessionState.COMPLETED:
        return "verified"
    if state is SwarmSessionState.FAILED:
        return "failed"
    if state is SwarmSessionState.CANCELLED:
        return "cancelled"
    if state is SwarmSessionState.PAUSED:
        return "paused"
    if state is SwarmSessionState.VERIFYING:
        return "verifying"
    if verification is not None:
        status = str(verification.get("summary", {}).get("status") or "")
        if status == SwarmVerificationStatus.VERIFIED.value:
            return "verified"
        if status == SwarmVerificationStatus.FAILED.value:
            return "failed"
    if merge_queue is not None:
        status = str(merge_queue.get("summary", {}).get("status") or "")
        if status == MergeQueueStatus.CONFLICT.value:
            return "integration_conflict"
        if status in {
            MergeQueueStatus.READY.value,
            MergeQueueStatus.INTEGRATING.value,
        }:
            return "integrating"
        if status == MergeQueueStatus.COMPLETED.value:
            return "ready_to_verify"
    if scheduler is not None:
        status = str(scheduler.get("summary", {}).get("status") or "")
        if status == "replan_required":
            return "replan_required"
        if status == "completed":
            return "ready_to_integrate"
        states = scheduler.get("summary", {}).get("states") or {}
        if states.get("active"):
            return "executing"
        if scheduler.get("summary", {}).get("dispatchable_work_ids"):
            return "ready"
        return "blocked"
    return "planning"


def _work_action(
    scheduler_state: str,
    merge_state: str | None,
    verified: bool,
    run: CodexRunRecord | None,
) -> str:
    if verified:
        return "done"
    if merge_state == MergeEntryState.CONFLICT.value:
        return "resolve integration conflict"
    if merge_state == MergeEntryState.READY.value:
        return "integrate"
    if scheduler_state == "runnable":
        return "dispatch"
    if scheduler_state == "retryable":
        return "replace worker"
    if scheduler_state == "active":
        return "wait"
    if scheduler_state in {"blocked", "pending", "queued_capacity"}:
        return "wait for dependency or capacity"
    if scheduler_state == "replan_required":
        return "replan"
    if (
        run is not None
        and run.state.terminal
        and run.state is not CodexRunState.SUCCEEDED
    ):
        return "inspect failed run"
    return "wait"


def get_swarm_operator_snapshot(repo: str | Path, session_id: str) -> dict[str, Any]:
    """Return one read-only operator view across all swarm protocols."""

    root = resolve_repository_root(repo)
    session = get_swarm_session(root, session_id)
    scheduler = _safe_get(lambda: get_swarm_scheduler(root, session_id))
    merge_queue = _safe_get(
        lambda: get_swarm_merge_queue(root, session_id, refresh=False)
    )
    verification = _safe_get(lambda: get_swarm_verification(root, session_id))
    recovery = inspect_swarm_recovery(root, session_id)
    worktrees = inspect_swarm_worktrees(root, session_id)

    from claim_plane.swarm.codex_runner import list_codex_runs

    records = list_codex_runs(root, session_id)
    latest = _latest_runs(records)
    scheduler_map: dict[str, Mapping[str, Any]] = {}
    if scheduler is not None:
        scheduler_map = {
            str(item["work_id"]): item
            for item in scheduler["scheduler"].get("work") or ()
        }
    merge_map: dict[str, Mapping[str, Any]] = {}
    if merge_queue is not None:
        merge_map = {
            str(item["work_id"]): item
            for item in merge_queue["merge_queue"].get("entries") or ()
        }
    verified_map: dict[str, bool] = {}
    if verification is not None:
        verified_map = {
            str(item["work_id"]): bool(item.get("verified"))
            for item in verification["verification"].get("work_evidence") or ()
        }
    worktree_map = {
        str(item["record"]["work_id"]): item
        for item in worktrees.get("worktrees") or ()
    }

    work: list[dict[str, Any]] = []
    for item in session.work_graph.work_items:
        scheduled = scheduler_map.get(item.work_id, {})
        run = latest.get(item.work_id)
        merge = merge_map.get(item.work_id)
        verified = verified_map.get(item.work_id, False)
        scheduler_state = str(scheduled.get("state") or "planned")
        merge_state = None if merge is None else str(merge.get("state"))
        work.append(
            {
                "work_id": item.work_id,
                "title": item.title,
                "scheduler_state": scheduler_state,
                "effective_dependencies": list(
                    scheduled.get("effective_dependencies") or item.depends_on
                ),
                "run_id": None if run is None else run.run_id,
                "run_state": None if run is None else run.state.value,
                "attempt": 0 if run is None else run.attempt,
                "tokens": 0 if run is None else run.usage.total_tokens,
                "merge_state": merge_state,
                "verified": verified,
                "worktree_health": (
                    None
                    if item.work_id not in worktree_map
                    else worktree_map[item.work_id]["health"]
                ),
                "detail": str(scheduled.get("detail") or ""),
                "next_action": _work_action(
                    scheduler_state, merge_state, verified, run
                ),
            }
        )

    tokens = sum(record.usage.total_tokens for record in records)
    duration = sum(record.duration_seconds or 0.0 for record in records)
    active = sum(1 for record in records if record.state in _ACTIVE_RUN_STATES)
    succeeded = sum(
        1 for record in latest.values() if record.state is CodexRunState.SUCCEEDED
    )
    failed = sum(
        1
        for record in latest.values()
        if record.state.terminal and record.state is not CodexRunState.SUCCEEDED
    )
    phase = _operator_phase(session.state, scheduler, merge_queue, verification)
    return {
        "protocol": SWARM_OPERATOR_SNAPSHOT_PROTOCOL,
        "generated_at": _utc_now(),
        "session_id": session.session_id,
        "session_state": session.state.value,
        "phase": phase,
        "root_task": session.root_task.to_dict(),
        "base_commit": session.base_commit,
        "integration_target": session.integration_target.to_dict(),
        "graph": {
            "version": session.graph_version,
            "fingerprint": session.graph_fingerprint,
            "work_items": len(session.work_graph.work_items),
        },
        "budget": {
            "version": session.budget_version,
            "fingerprint": session.budget_fingerprint,
            "max_active_workers": session.budget_policy.workers.max_active,
            "max_total_launches": session.budget_policy.workers.max_total_launches,
            "max_total_tokens": session.budget_policy.resources.max_total_tokens,
            "max_cost_usd": session.budget_policy.resources.max_cost_usd,
            "max_wall_time_seconds": (
                session.budget_policy.resources.max_wall_time_seconds
            ),
        },
        "usage": {
            "runs": len(records),
            "active_workers": active,
            "succeeded_work_items": succeeded,
            "failed_work_items": failed,
            "total_tokens": tokens,
            "summed_worker_duration_seconds": round(duration, 6),
        },
        "scheduler": None if scheduler is None else scheduler["summary"],
        "merge_queue": None if merge_queue is None else merge_queue["summary"],
        "verification": (None if verification is None else verification["summary"]),
        "recovery": recovery["summary"],
        "worktree_summary": worktrees["summary"],
        "work": work,
    }


@dataclass(frozen=True, slots=True)
class OperatorEvent:
    timestamp: str
    event: str
    session_id: str
    work_id: str | None = None
    run_id: str | None = None
    detail: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SWARM_OPERATOR_EVENT_PROTOCOL

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "timestamp": self.timestamp,
            "event": self.event,
            "session_id": self.session_id,
            "work_id": self.work_id,
            "run_id": self.run_id,
            "detail": self.detail,
            "metadata": dict(self.metadata),
        }


def list_swarm_operator_logs(
    repo: str | Path,
    session_id: str,
    *,
    work_id: str | None = None,
    limit: int = 200,
    include_codex_events: bool = True,
) -> list[dict[str, Any]]:
    """Aggregate durable runner, recovery, merge, and verification events."""

    if limit <= 0:
        raise ValueError("log limit must be positive")
    root = resolve_repository_root(repo)
    get_swarm_session(root, session_id)
    from claim_plane.swarm.codex_runner import list_codex_runs
    from claim_plane.swarm.recovery import list_swarm_recovery_events

    records = list_codex_runs(root, session_id, work_id=work_id)
    events: list[OperatorEvent] = []
    for record in records:
        events.append(
            OperatorEvent(
                record.created_at,
                "worker.reserved",
                session_id,
                work_id=record.work_id,
                run_id=record.run_id,
                detail=f"attempt {record.attempt}",
            )
        )
        if include_codex_events:
            path = Path(record.events_path)
            if path.is_file():
                lines = path.read_text(encoding="utf-8").splitlines()
                for index, line in enumerate(lines):
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        payload = {"type": "invalid_jsonl", "line": line[:500]}
                    event_type = str(payload.get("type") or "unknown")
                    events.append(
                        OperatorEvent(
                            str(
                                payload.get("timestamp")
                                or record.started_at
                                or record.created_at
                            ),
                            f"codex.{event_type}",
                            session_id,
                            work_id=record.work_id,
                            run_id=record.run_id,
                            metadata={"index": index, "payload": payload},
                        )
                    )
        events.append(
            OperatorEvent(
                record.finished_at or record.updated_at,
                f"worker.{record.state.value}",
                session_id,
                work_id=record.work_id,
                run_id=record.run_id,
                detail=record.termination_reason or "",
                metadata={
                    "attempt": record.attempt,
                    "tokens": record.usage.total_tokens,
                    "exit_code": record.exit_code,
                },
            )
        )
    for recovery in list_swarm_recovery_events(root, session_id):
        if work_id is not None and recovery.work_id not in {None, work_id}:
            continue
        events.append(
            OperatorEvent(
                recovery.created_at,
                f"recovery.{recovery.action}",
                session_id,
                work_id=recovery.work_id,
                run_id=recovery.run_id,
                detail=recovery.detail or "",
                metadata=recovery.metadata,
            )
        )
    merge = _safe_get(lambda: get_swarm_merge_queue(root, session_id, refresh=False))
    if merge is not None:
        timestamp = str(merge["merge_queue"].get("updated_at") or _utc_now())
        for entry in merge["merge_queue"].get("entries") or ():
            if work_id is not None and entry["work_id"] != work_id:
                continue
            events.append(
                OperatorEvent(
                    timestamp,
                    f"merge.{entry['state']}",
                    session_id,
                    work_id=str(entry["work_id"]),
                    run_id=entry.get("run_id"),
                    detail=str(entry.get("detail") or ""),
                    metadata={
                        "integration_commit": entry.get("integration_commit"),
                        "conflict_paths": entry.get("conflict_paths") or [],
                    },
                )
            )
    verification = _safe_get(lambda: get_swarm_verification(root, session_id))
    if verification is not None:
        report = verification["verification"]
        events.append(
            OperatorEvent(
                str(report.get("created_at") or _utc_now()),
                f"verification.{report['status']}",
                session_id,
                detail=(
                    "SWARM VERIFIED"
                    if report["status"] == SwarmVerificationStatus.VERIFIED.value
                    else "swarm verification failed"
                ),
                metadata={
                    "integration_head": report.get("integration_head"),
                    "findings": len(report.get("findings") or ()),
                },
            )
        )
    ordered = sorted(
        events,
        key=lambda event: (
            event.timestamp,
            event.run_id or "",
            event.event,
            event.work_id or "",
        ),
    )
    return [event.to_dict() for event in ordered[-limit:]]


def _prepare_swarm(root: Path, session_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    session = get_swarm_session(root, session_id)
    if session.state is SwarmSessionState.PAUSED:
        raise ValueError(
            "swarm session is paused; run 'claim-plane swarm resume' first"
        )
    if session.state in _TERMINAL_STATES:
        return events
    try:
        plan = plan_swarm_concurrency(root, session_id)
        events.append({"stage": "plan", "summary": plan["summary"]})
    except ValueError as exc:
        if "while session is running" not in str(exc):
            raise
    admission = admit_swarm_session(root, session_id)
    events.append({"stage": "admission", "summary": admission["summary"]})
    if admission["summary"]["status"] != "ready":
        raise ValueError("shared admission requires replanning")
    inspection = inspect_swarm_worktrees(root, session_id)
    if not inspection["worktrees"]:
        worktrees = provision_swarm_worktrees(root, session_id)
        events.append({"stage": "worktrees", "summary": worktrees["summary"]})
    else:
        unhealthy = [
            item
            for item in inspection["worktrees"]
            if item["health"] not in {"ready", "dirty"}
        ]
        if unhealthy or inspection["orphans"]:
            raise ValueError("managed worktrees require operator attention")
    merge = plan_swarm_merge_queue(root, session_id)
    events.append({"stage": "merge_queue", "summary": merge["summary"]})
    return events


def _dispatch_one(
    root: Path,
    session_id: str,
    work_id: str,
    *,
    codex_binary: str,
    model: str | None,
    reasoning_effort: str | None,
    timeout_seconds: int | None,
    token_limit: int | None,
    reset_failed_worktrees: bool,
) -> CodexRunRecord:
    from claim_plane.swarm.codex_runner import list_codex_runs

    records = list_codex_runs(root, session_id, work_id=work_id)
    latest = records[-1] if records else None
    if latest is None:
        inspection = inspect_swarm_worktrees(root, session_id)
        selected = next(
            item
            for item in inspection["worktrees"]
            if item["record"]["work_id"] == work_id
        )
        if selected["health"] == "dirty":
            raise ValueError("refusing first dispatch into a dirty managed worktree")
    if (
        latest is not None
        and latest.state.terminal
        and latest.state is not CodexRunState.SUCCEEDED
    ):
        return replace_codex_worker(
            root,
            session_id,
            work_id,
            replaced_run_id=latest.run_id,
            reset_worktree=reset_failed_worktrees,
            codex_binary=codex_binary,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,
            token_limit=token_limit,
        )
    return run_codex_work_item(
        root,
        session_id,
        work_id,
        codex_binary=codex_binary,
        model=model,
        reasoning_effort=reasoning_effort,
        timeout_seconds=timeout_seconds,
        token_limit=token_limit,
    )


def start_swarm_session(
    repo: str | Path,
    session_id: str,
    *,
    codex_binary: str = "codex",
    model: str | None = None,
    reasoning_effort: str | None = None,
    timeout_seconds: int | None = None,
    token_limit: int | None = None,
    acceptance_timeout: int = 300,
    run_acceptance: bool = True,
    prepare_only: bool = False,
    reset_failed_worktrees: bool = False,
    max_cycles: int = 100,
    on_event: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Drive a swarm to verification using only existing fail-closed protocols."""

    if max_cycles <= 0:
        raise ValueError("max_cycles must be positive")
    root = resolve_repository_root(repo)
    events = _prepare_swarm(root, session_id)
    for event in events:
        if on_event is not None:
            on_event(event)
    if prepare_only:
        return {
            "session_id": session_id,
            "status": "prepared",
            "events": events,
            "snapshot": get_swarm_operator_snapshot(root, session_id),
        }

    errors: list[dict[str, Any]] = []
    for cycle in range(1, max_cycles + 1):
        session = get_swarm_session(root, session_id)
        if session.state in _TERMINAL_STATES:
            break
        if session.state is SwarmSessionState.PAUSED:
            break
        scheduler = get_swarm_scheduler(root, session_id)
        dispatchable = tuple(scheduler["summary"]["dispatchable_work_ids"])
        if dispatchable:
            event = {
                "stage": "dispatch",
                "cycle": cycle,
                "work_ids": list(dispatchable),
            }
            events.append(event)
            if on_event is not None:
                on_event(event)
            max_workers = min(
                len(dispatchable),
                int(scheduler["summary"]["available_slots"] or 1),
            )
            batch_errors: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        _dispatch_one,
                        root,
                        session_id,
                        work_id,
                        codex_binary=codex_binary,
                        model=model,
                        reasoning_effort=reasoning_effort,
                        timeout_seconds=timeout_seconds,
                        token_limit=token_limit,
                        reset_failed_worktrees=reset_failed_worktrees,
                    ): work_id
                    for work_id in dispatchable
                }
                for future in as_completed(futures):
                    work_id = futures[future]
                    try:
                        record = future.result()
                    except Exception as exc:  # operator reports bounded worker failures
                        failure = {
                            "stage": "worker_error",
                            "work_id": work_id,
                            "error": str(exc),
                        }
                        errors.append(failure)
                        batch_errors.append(failure)
                        events.append(failure)
                        if on_event is not None:
                            on_event(failure)
                    else:
                        errors[:] = [
                            item for item in errors if item.get("work_id") != work_id
                        ]
                        completed = {
                            "stage": "worker",
                            "work_id": work_id,
                            "run_id": record.run_id,
                            "state": record.state.value,
                            "tokens": record.usage.total_tokens,
                        }
                        events.append(completed)
                        if on_event is not None:
                            on_event(completed)
            merged = drain_swarm_merge_queue(root, session_id)
            merge_event = {"stage": "merge", "summary": merged["summary"]}
            events.append(merge_event)
            if on_event is not None:
                on_event(merge_event)
            if merged["summary"]["status"] == MergeQueueStatus.CONFLICT.value:
                break
            if batch_errors:
                # Exceptions here are control-plane or worktree failures rather than
                # ordinary agent exits. Stop instead of spinning on the same runnable
                # scheduler item; the operator can inspect and explicitly recover it.
                break
            continue

        merged = drain_swarm_merge_queue(root, session_id)
        if merged["summary"]["status"] == MergeQueueStatus.CONFLICT.value:
            break
        if merged["summary"]["status"] == MergeQueueStatus.COMPLETED.value:
            verification = verify_swarm_session(
                root,
                session_id,
                run_acceptance=run_acceptance,
                acceptance_timeout=acceptance_timeout,
            )
            event = {"stage": "verification", "summary": verification["summary"]}
            events.append(event)
            if on_event is not None:
                on_event(event)
            break
        refreshed = get_swarm_scheduler(root, session_id)
        if not refreshed["summary"]["dispatchable_work_ids"]:
            break
    else:
        errors.append(
            {
                "stage": "operator",
                "error": f"max_cycles={max_cycles} exhausted",
            }
        )

    snapshot = get_swarm_operator_snapshot(root, session_id)
    verified = snapshot["phase"] == "verified"
    return {
        "session_id": session_id,
        "status": "verified" if verified else "attention_required",
        "verified": verified,
        "events": events,
        "errors": errors,
        "snapshot": snapshot,
    }


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or result.stdout.strip() or "git failed")
    return result.stdout.strip()


def _demo_codex_script(path: Path) -> None:
    path.write_text(
        r"""#!/usr/bin/env python3
import json
import pathlib
import re
import sys

if "--version" in sys.argv:
    print("claim-plane deterministic demo agent 1.0")
    raise SystemExit(0)
args = sys.argv[1:]
output = pathlib.Path(args[args.index("--output-last-message") + 1])
prompt = args[-1]
match = re.search(r"Work item: ([A-Za-z0-9._-]+)", prompt)
if match is None:
    raise SystemExit("missing work item")
work_id = match.group(1)
root = pathlib.Path.cwd()
if work_id == "greeting":
    target = root / "src" / "greeting.py"
    target.write_text('def greet(name: str) -> str:\n    return f"Hello, {name}!"\n', encoding="utf-8")
elif work_id == "arithmetic":
    target = root / "src" / "arithmetic.py"
    target.write_text('def add(left: int, right: int) -> int:\n    return left + right\n', encoding="utf-8")
elif work_id == "integration-summary":
    target = root / "SWARM_RESULT.md"
    target.write_text("# Verified swarm result\n\nGreeting and arithmetic modules are integrated.\n", encoding="utf-8")
else:
    raise SystemExit(f"unknown demo work item: {work_id}")
print(json.dumps({"type": "thread.started", "thread_id": f"demo-{work_id}"}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 10}}), flush=True)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(f"completed {work_id}\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def create_and_run_swarm_demo(
    directory: str | Path | None = None,
    *,
    keep: bool = True,
) -> dict[str, Any]:
    """Create and run a deterministic three-worker demo without network access."""

    if directory is None:
        root = Path(tempfile.mkdtemp(prefix="claim-plane-swarm-demo-"))
    else:
        root = Path(directory).expanduser().resolve()
        if root.exists() and any(root.iterdir()):
            raise ValueError(f"demo directory must be empty: {root}")
        root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Claim Plane Demo")
    _git(root, "config", "user.email", "demo@claim-plane.invalid")
    (root / "src").mkdir()
    (root / "src" / "greeting.py").write_text("# planned by swarm\n", encoding="utf-8")
    (root / "src" / "arithmetic.py").write_text(
        "# planned by swarm\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# Claim Plane swarm demo\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial demo fixture")
    init_project(root)
    codex = root / ".claim-plane" / "demo-codex"
    codex.parent.mkdir(parents=True, exist_ok=True)
    _demo_codex_script(codex)
    spec = {
        "protocol": "claim-plane.swarm-session-spec.v1",
        "root_task": {
            "title": "Deterministic swarm demo",
            "goal": "Implement two independent modules and integrate a summary.",
            "acceptance": [
                (
                    'python -c "from src.greeting import greet; '
                    "assert greet('Ada') == 'Hello, Ada!'\""
                ),
                'python -c "from src.arithmetic import add; assert add(2, 3) == 5"',
                (
                    'python -c "from pathlib import Path; '
                    "assert Path('SWARM_RESULT.md').is_file()\""
                ),
            ],
        },
        "work_graph": {
            "protocol": "claim-plane.swarm-work-graph.v1",
            "work_items": [
                {
                    "work_id": "greeting",
                    "title": "Greeting module",
                    "goal": "Implement src/greeting.py.",
                    "operations": [
                        {
                            "access": "write",
                            "resource": {
                                "kind": "file",
                                "identifier": "src/greeting.py",
                            },
                        }
                    ],
                    "acceptance": [
                        (
                            'python -c "from src.greeting import greet; '
                            "assert greet('Ada') == 'Hello, Ada!'\""
                        )
                    ],
                },
                {
                    "work_id": "arithmetic",
                    "title": "Arithmetic module",
                    "goal": "Implement src/arithmetic.py.",
                    "operations": [
                        {
                            "access": "write",
                            "resource": {
                                "kind": "file",
                                "identifier": "src/arithmetic.py",
                            },
                        }
                    ],
                    "acceptance": [
                        (
                            'python -c "from src.arithmetic import add; '
                            'assert add(2, 3) == 5"'
                        )
                    ],
                },
                {
                    "work_id": "integration-summary",
                    "title": "Integration summary",
                    "goal": "Document the integrated result.",
                    "depends_on": ["greeting", "arithmetic"],
                    "operations": [
                        {
                            "access": "write",
                            "resource": {
                                "kind": "file",
                                "identifier": "SWARM_RESULT.md",
                            },
                        }
                    ],
                    "acceptance": [
                        (
                            'python -c "from pathlib import Path; '
                            "assert Path('SWARM_RESULT.md').is_file()\""
                        )
                    ],
                },
            ],
        },
        "budget_policy": {
            "protocol": "claim-plane.swarm-budget-policy.v1",
            "workers": {
                "max_active": 2,
                "max_active_per_work_item": 1,
                "max_work_items": 8,
                "max_total_launches": 8,
            },
            "resources": {
                "max_total_tokens": 10000,
                "max_cost_usd": "0.01",
                "max_wall_time_seconds": 300,
            },
            "retries": {"max_agent_restarts": 1},
        },
    }
    created = create_swarm_session(root, spec=spec, session_id="swm-demo")
    result = start_swarm_session(root, "swm-demo", codex_binary=str(codex))
    payload = {
        "repository": str(root),
        "session": created["session"],
        "result": result,
        "kept": keep,
    }
    if not keep and directory is None:
        # Temporary demos are retained until the result is returned; callers opting
        # out can remove the path after inspecting the structured evidence.
        payload["cleanup_required"] = True
    return payload
