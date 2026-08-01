"""Headless Codex execution bound to managed swarm worktrees and budgets."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import secrets
import shutil
import signal
import stat
import subprocess
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, TextIO

from claim_plane.connectors.codex import (
    codex_intent_status,
    connect_codex,
    init_project,
)
from claim_plane.core import IntentOperation
from claim_plane.swarm.admission import SharedAdmissionStatus
from claim_plane.swarm.merge_queue import MergeEntryState
from claim_plane.swarm.models import SwarmSessionState, WorkItem
from claim_plane.swarm.runs import (
    CodexRunBudget,
    CodexRunRecord,
    CodexRunState,
    CodexUsage,
)
from claim_plane.swarm.service import (
    _git_result,
    _repository_identity,
    _require_initialized,
    _resolve_commit,
    _worktree_dirty,
    _store,
    _validate_session_id,
    ensure_swarm_admission,
    inspect_swarm_worktrees,
    resolve_repository_root,
)
from claim_plane.swarm.scheduler import compute_scheduler_snapshot
from claim_plane.swarm.worktrees import WorktreeHealth

_ACTIVE_STATES = {
    CodexRunState.RESERVED,
    CodexRunState.RUNNING,
    CodexRunState.CANCELLING,
}
_TERMINATE_GRACE_SECONDS = 5.0
_HEARTBEAT_INTERVAL_SECONDS = 1.0
_RUN_LEASE_SECONDS = 15


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _lease_expires_at(*, seconds: int = _RUN_LEASE_SECONDS) -> str:
    expires = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return expires.isoformat().replace("+00:00", "Z")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _resolve_executable(value: str) -> str:
    candidate = Path(value).expanduser()
    if candidate.parent != Path(".") or os.sep in value:
        path = candidate.resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f"Codex executable is not executable: {path}")
        return str(path)
    resolved = shutil.which(value)
    if resolved is None:
        raise ValueError(
            f"Codex executable {value!r} was not found in PATH; "
            "install Codex or use --codex-bin"
        )
    return str(Path(resolved).resolve())


def _codex_version(executable: str) -> str | None:
    try:
        result = subprocess.run(
            [executable, "--version"],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (result.stdout.strip() or result.stderr.strip()).splitlines()
    return lines[0] if lines else None


def _operation_line(operation: IntentOperation) -> str:
    resource = operation.resource
    commitment = operation.commitment.value
    region = f" region={resource.region}" if resource.region else ""
    return (
        f"- {commitment} {operation.access.value} "
        f"{resource.kind.value}:{resource.identifier}{region}"
    )


def build_codex_worker_prompt(session_id: str, item: WorkItem) -> str:
    operations = "\n".join(_operation_line(operation) for operation in item.operations)
    dependencies = "\n".join(f"- {value}" for value in item.depends_on) or "- none"
    preserves = "\n".join(f"- {value}" for value in item.preserves) or "- none"
    acceptance = "\n".join(f"- {value}" for value in item.acceptance) or "- none"
    return "\n".join(
        [
            f"You are the bounded worker for Claim Plane swarm session {session_id}.",
            f"Work item: {item.work_id} — {item.title}",
            f"Goal: {item.goal}",
            "",
            "Declared scope proposal:",
            operations,
            "",
            "Dependencies already required by the work graph:",
            dependencies,
            "",
            "Preserve requirements:",
            preserves,
            "",
            "Acceptance criteria:",
            acceptance,
            "",
            "Work only in this managed Git worktree. Do not commit, merge, rebase, "
            "switch branches, or modify .git, .codex, or .claim-plane control state.",
            "Before the first repository mutation, follow the Claim Plane hook context "
            "and admit one ChangeIntent. Its committed and contingent operations must "
            "remain within the declared work-item scope above.",
            "A denied mutation is not permission to broaden the task. Use only an "
            "exact Claim Plane amendment ticket and provide the concrete dependency "
            "reason.",
            "Run the relevant acceptance checks. Finish with a concise summary of "
            "changed files and tests. Process success is not verification; Claim Plane "
            "verifies the result separately.",
        ]
    )


def _run_directory(root: Path, session_id: str, run_id: str) -> Path:
    safe_session = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return root / ".claim-plane" / "swarm" / "runs" / safe_session / run_id


def _create_private_run_directory(root: Path, run_dir: Path) -> None:
    trusted_root = root.resolve()
    try:
        relative = run_dir.relative_to(trusted_root)
    except ValueError as exc:
        raise ValueError(f"Codex run directory escapes repository root: {run_dir}") from exc
    current = trusted_root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                # Parallel workers may create the same trusted parent between
                # lstat() and mkdir(). Re-read it and continue only after the
                # normal symlink/directory checks below.
                pass
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(
                f"Codex run namespace must not contain a symlink: {current}"
            )
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(
                f"Codex run namespace component is not a directory: {current}"
            )
    try:
        run_dir.chmod(0o700)
    except PermissionError as exc:
        raise ValueError(f"cannot secure Codex run directory: {run_dir}") from exc


def _usage_from_event(event: Mapping[str, Any]) -> CodexUsage:
    if event.get("type") != "turn.completed":
        return CodexUsage()
    usage = event.get("usage")
    if not isinstance(usage, Mapping):
        return CodexUsage()
    return CodexUsage(
        input_tokens=int(usage.get("input_tokens") or 0),
        cached_input_tokens=int(usage.get("cached_input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        reasoning_output_tokens=int(usage.get("reasoning_output_tokens") or 0),
    )


def _thread_reader(stream: TextIO, output: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            output.put(line)
    finally:
        output.put(None)


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass


def _latest_states(records: list[CodexRunRecord]) -> dict[str, CodexRunRecord]:
    latest: dict[str, CodexRunRecord] = {}
    for record in sorted(records, key=lambda item: (item.attempt, item.created_at)):
        latest[record.work_id] = record
    return latest



def _fair_share(
    remaining: int | None, unfinished: int, requested: int | None
) -> int | None:
    if remaining is None:
        return requested
    if remaining <= 0:
        raise ValueError("swarm token budget is exhausted")
    share = max(1, remaining // max(1, unfinished))
    if requested is None:
        return share
    if requested <= 0:
        raise ValueError("requested token limit must be positive")
    if requested > remaining:
        raise ValueError("requested token limit exceeds remaining swarm budget")
    return min(requested, share)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid stored Codex-run timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"stored Codex-run timestamp has no timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _remaining_wall_time(
    maximum_seconds: int, existing_runs: list[CodexRunRecord]
) -> int:
    if not existing_runs:
        return maximum_seconds
    started = min(
        _parse_timestamp(record.started_at or record.created_at)
        for record in existing_runs
    )
    elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    return int(maximum_seconds - elapsed)


def _reserve_run(
    root: Path,
    session_id: str,
    work_id: str,
    *,
    executable: str,
    model: str | None,
    reasoning_effort: str | None,
    timeout_seconds: int | None,
    token_limit: int | None,
    replacement_of_run_id: str | None = None,
) -> tuple[CodexRunRecord, WorkItem]:
    identity = _repository_identity(root)
    now = _utc_now()
    ensure_swarm_admission(root, session_id)
    with _store(root) as store:
        session = store.require(session_id)
        plan_data = store.get_concurrency_plan(session_id)
        shared_data = store.get_shared_admission(session_id)
        worktrees = store.list_worktrees(session_id)
        existing_runs = store.list_codex_runs(session_id)
        merge_queue_data = store.get_merge_queue(session_id)
        replacement_source = (
            None
            if replacement_of_run_id is None
            else store.require_codex_run(replacement_of_run_id)
        )
    if session.repository_identity != identity:
        raise ValueError("swarm session is bound to a different repository identity")
    if replacement_source is not None:
        if (
            replacement_source.session_id != session_id
            or replacement_source.work_id != work_id
        ):
            raise ValueError(
                "replacement source is bound to a different session or work item"
            )
        if not replacement_source.state.terminal:
            raise ValueError("replacement source must be terminal")
        if replacement_source.state is CodexRunState.SUCCEEDED:
            raise ValueError("a succeeded worker cannot be replaced")
    if session.state not in {SwarmSessionState.PLANNED, SwarmSessionState.RUNNING}:
        raise ValueError(f"cannot start Codex while session is {session.state.value}")
    if _resolve_commit(root, session.base_commit) != session.base_commit:
        raise ValueError("swarm session base commit is no longer resolvable")
    if plan_data is None:
        raise ValueError("swarm session has no concurrency plan")
    plan, _ = plan_data
    if (
        plan.graph_version != session.graph_version
        or plan.graph_fingerprint != session.graph_fingerprint
        or plan.budget_version != session.budget_version
        or plan.budget_fingerprint != session.budget_fingerprint
    ):
        raise ValueError("stored concurrency plan is stale")
    if shared_data is None:
        raise ValueError("swarm session has no shared admission")
    shared_admission, _ = shared_data
    if shared_admission.status is not SharedAdmissionStatus.READY:
        raise ValueError("shared admission requires replanning")
    if (
        shared_admission.graph_version != session.graph_version
        or shared_admission.graph_fingerprint != session.graph_fingerprint
        or shared_admission.budget_version != session.budget_version
        or shared_admission.budget_fingerprint != session.budget_fingerprint
        or shared_admission.concurrency_plan_fingerprint != plan.fingerprint()
    ):
        raise ValueError("stored shared admission is stale")
    item = session.work_graph.item_map.get(work_id)
    if item is None:
        raise KeyError(f"unknown work item {work_id!r}")
    by_work = {record.work_id: record for record in worktrees}
    worktree = by_work.get(work_id)
    if worktree is None:
        raise ValueError(
            f"work item {work_id!r} has no managed worktree; "
            "run provision-worktrees first"
        )
    integrated_work_ids = (
        None
        if merge_queue_data is None
        else {
            entry.work_id
            for entry in merge_queue_data[0].entries
            if entry.state is MergeEntryState.INTEGRATED
        }
    )
    execution_base = _resolve_commit(Path(worktree.worktree_path), "HEAD")
    admission_record = shared_admission.admission_map[work_id]
    if merge_queue_data is not None and admission_record.effective_dependencies:
        missing_integrated = [
            dependency
            for dependency in admission_record.effective_dependencies
            if dependency not in (integrated_work_ids or set())
        ]
        if missing_integrated:
            raise ValueError(
                "work item dependencies have not been integrated: "
                + ", ".join(missing_integrated)
            )
        target_head = merge_queue_data[0].integration_head
        worktree_path = Path(worktree.worktree_path)
        if _worktree_dirty(worktree_path):
            raise ValueError(
                "cannot advance a dirty dependent worktree to the integration head"
            )
        if execution_base != target_head:
            ancestor = _git_result(
                worktree_path,
                "merge-base",
                "--is-ancestor",
                execution_base,
                target_head,
            )
            if ancestor.returncode != 0:
                raise ValueError(
                    "dependent worktree cannot be advanced to the integration head"
                )
            reset = _git_result(worktree_path, "reset", "--hard", target_head)
            if reset.returncode != 0:
                raise ValueError(
                    reset.stderr.strip()
                    or reset.stdout.strip()
                    or "failed to advance dependent worktree"
                )
            execution_base = target_head
    inspection_payload = inspect_swarm_worktrees(root, session_id)
    inspection = next(
        value
        for value in inspection_payload["worktrees"]
        if value["record"]["work_id"] == work_id
    )
    if inspection["health"] not in {
        WorktreeHealth.READY.value,
        WorktreeHealth.DIRTY.value,
    }:
        raise ValueError(
            f"managed worktree {work_id!r} is not runnable: {inspection['health']}"
        )
    latest = _latest_states(existing_runs)
    current = latest.get(work_id)
    if current is not None and current.state in _ACTIVE_STATES:
        raise ValueError(
            "workers.max_active_per_work_item is exhausted for "
            f"{work_id!r}"
        )
    attempts_for_work = sum(1 for record in existing_runs if record.work_id == work_id)
    if (
        current is not None
        and current.state.terminal
        and current.state is not CodexRunState.SUCCEEDED
        and attempts_for_work >= 1 + session.budget_policy.retries.max_agent_restarts
    ):
        raise ValueError(f"restart budget is exhausted for work item {work_id!r}")
    scheduler = compute_scheduler_snapshot(
        session,
        shared_admission,
        existing_runs,
        integrated_work_ids=integrated_work_ids,
    )
    if work_id not in scheduler.dispatchable_work_ids:
        work_state = next(
            value.state.value for value in scheduler.work if value.work_id == work_id
        )
        runnable = ", ".join(scheduler.dispatchable_work_ids) or "none"
        raise ValueError(
            f"work item {work_id!r} is not runnable in current wave; "
            f"scheduler state={work_state}; dispatchable: {runnable}"
        )
    policy = session.budget_policy
    completed_tokens = sum(
        record.usage.total_tokens for record in existing_runs if record.state.terminal
    )
    active_reserved = sum(
        record.budget.token_limit or 0
        for record in existing_runs
        if record.state in _ACTIVE_STATES
    )
    max_tokens = policy.resources.max_total_tokens
    remaining_tokens = (
        None if max_tokens is None else max_tokens - completed_tokens - active_reserved
    )
    unfinished = sum(
        1
        for candidate in session.work_graph.work_items
        if latest.get(candidate.work_id) is None
        or latest[candidate.work_id].state is not CodexRunState.SUCCEEDED
    )
    run_token_limit = _fair_share(remaining_tokens, unfinished, token_limit)

    remaining_seconds = _remaining_wall_time(
        policy.resources.max_wall_time_seconds, existing_runs
    )
    if remaining_seconds <= 0:
        raise ValueError("swarm wall-time budget is exhausted")
    run_timeout = remaining_seconds if timeout_seconds is None else timeout_seconds
    if run_timeout <= 0:
        raise ValueError("timeout must be positive")
    run_timeout = min(run_timeout, remaining_seconds)

    run_id = f"run-{secrets.token_hex(12)}"
    run_dir = _run_directory(root, session_id, run_id)
    prompt = build_codex_worker_prompt(session_id, item)
    final_path = run_dir / "final-message.txt"
    command = [
        executable,
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "--output-last-message",
        str(final_path),
    ]
    if model:
        command.extend(["--model", model])
    if reasoning_effort:
        command.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
    command.append(prompt)
    attempt = 1 + sum(1 for record in existing_runs if record.work_id == work_id)
    budget = CodexRunBudget(
        token_limit=run_token_limit,
        wall_time_limit_seconds=run_timeout,
        cost_limit_usd=policy.resources.max_cost_usd,
    )
    record = CodexRunRecord(
        run_id=run_id,
        session_id=session_id,
        work_id=work_id,
        attempt=attempt,
        state=CodexRunState.RESERVED,
        repository_identity=identity,
        graph_version=session.graph_version,
        graph_fingerprint=session.graph_fingerprint,
        budget_version=session.budget_version,
        budget_fingerprint=session.budget_fingerprint,
        worktree_path=worktree.worktree_path,
        branch=worktree.branch,
        base_commit=execution_base,
        command=tuple(command),
        prompt_sha256=_sha256_text(prompt),
        budget=budget,
        run_directory=str(run_dir),
        events_path=str(run_dir / "events.jsonl"),
        stderr_path=str(run_dir / "stderr.log"),
        final_message_path=str(final_path),
        created_at=now,
        updated_at=now,
        heartbeat_at=now,
        lease_expires_at=_lease_expires_at(),
        replacement_of_run_id=replacement_of_run_id,
        recovery_generation=(
            0
            if replacement_source is None
            else replacement_source.recovery_generation + 1
        ),
        metadata={
            "shared_admission_fingerprint": shared_admission.fingerprint(),
            "effective_dependencies": list(
                admission_record.effective_dependencies
            ),
            "scheduler_snapshot_fingerprint": scheduler.fingerprint(),
            "codex_version": _codex_version(executable),
            "cost_metering": "unavailable_from_codex_jsonl",
            "replacement_requires_fresh_admission": replacement_source is not None,
        },
    )
    _create_private_run_directory(root, run_dir)
    try:
        (run_dir / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
        (run_dir / "record.reserved.json").write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with _store(root) as store:
            record = store.reserve_codex_run(
                record,
                max_active=policy.workers.max_active,
                max_active_per_work_item=policy.workers.max_active_per_work_item,
                max_total_launches=policy.workers.max_total_launches,
                max_attempts_per_work_item=1 + policy.retries.max_agent_restarts,
                max_total_tokens=policy.resources.max_total_tokens,
                expected_admission_fingerprint=shared_admission.fingerprint(),
            )
    except Exception:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    return record, item


def run_codex_work_item(
    repo: str | Path,
    session_id: str,
    work_id: str,
    *,
    codex_binary: str = "codex",
    model: str | None = None,
    reasoning_effort: str | None = None,
    timeout_seconds: int | None = None,
    token_limit: int | None = None,
    replacement_of_run_id: str | None = None,
) -> CodexRunRecord:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    executable = _resolve_executable(codex_binary)
    record, _ = _reserve_run(
        root,
        session_id,
        work_id,
        executable=executable,
        model=model,
        reasoning_effort=reasoning_effort,
        timeout_seconds=timeout_seconds,
        token_limit=token_limit,
        replacement_of_run_id=replacement_of_run_id,
    )
    worktree = Path(record.worktree_path)
    try:
        init_project(worktree)
        connect_codex(worktree)
    except Exception as exc:
        failed_at = _utc_now()
        terminal = record.with_updates(
            state=CodexRunState.SPAWN_FAILED,
            updated_at=failed_at,
            finished_at=failed_at,
            termination_reason="connector_setup_failed",
            error=str(exc),
            heartbeat_at=failed_at,
            lease_expires_at=None,
        )
        with _store(root) as store:
            store.update_codex_run(terminal)
        Path(terminal.run_directory, "record.final.json").write_text(
            json.dumps(terminal.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return terminal

    started_at = _utc_now()
    started_monotonic = time.monotonic()
    stderr_path = Path(record.stderr_path)
    events_path = Path(record.events_path)
    usage = CodexUsage()
    event_count = 0
    last_event_type: str | None = None
    thread_id: str | None = None
    process: subprocess.Popen[str] | None = None
    termination_reason: str | None = None
    final_state = CodexRunState.FAILED
    error: str | None = None
    exit_code: int | None = None
    try:
        with stderr_path.open("w", encoding="utf-8") as stderr_handle, events_path.open(
            "w", encoding="utf-8"
        ) as events_handle:
            process = subprocess.Popen(
                list(record.command),
                cwd=worktree,
                text=True,
                stdout=subprocess.PIPE,
                stderr=stderr_handle,
                start_new_session=True,
                env=os.environ.copy(),
                bufsize=1,
            )
            running = record.with_updates(
                state=CodexRunState.RUNNING,
                started_at=started_at,
                updated_at=started_at,
                runner_pid=os.getpid(),
                agent_pid=process.pid,
                heartbeat_at=started_at,
                lease_expires_at=_lease_expires_at(),
            )
            with _store(root) as store:
                store.update_codex_run(running)
            record = running
            assert process.stdout is not None
            output_queue: queue.Queue[str | None] = queue.Queue()
            reader = threading.Thread(
                target=_thread_reader,
                args=(process.stdout, output_queue),
                daemon=True,
            )
            reader.start()
            stream_closed = False
            deadline = started_monotonic + record.budget.wall_time_limit_seconds
            last_heartbeat = started_monotonic
            while not stream_closed or process.poll() is None:
                if time.monotonic() - last_heartbeat >= _HEARTBEAT_INTERVAL_SECONDS:
                    heartbeat_at = _utc_now()
                    heartbeat = record.with_updates(
                        state=CodexRunState.RUNNING,
                        updated_at=heartbeat_at,
                        heartbeat_at=heartbeat_at,
                        lease_expires_at=_lease_expires_at(),
                        usage=usage,
                        event_count=event_count,
                        last_event_type=last_event_type,
                        codex_thread_id=thread_id,
                    )
                    with _store(root) as store:
                        current = store.require_codex_run(record.run_id)
                        if current.state is CodexRunState.CANCELLING:
                            heartbeat = heartbeat.with_updates(
                                state=CodexRunState.CANCELLING,
                                termination_reason=current.termination_reason,
                            )
                        store.update_codex_run(heartbeat)
                    record = heartbeat
                    last_heartbeat = time.monotonic()
                if time.monotonic() >= deadline and process.poll() is None:
                    termination_reason = "wall_time_budget_exceeded"
                    final_state = CodexRunState.TIMED_OUT
                    _terminate_process(process)
                with _store(root) as store:
                    current = store.require_codex_run(record.run_id)
                if current.state is CodexRunState.CANCELLING and process.poll() is None:
                    termination_reason = "cancellation_requested"
                    final_state = CodexRunState.CANCELLED
                    _terminate_process(process)
                try:
                    line = output_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    stream_closed = True
                    continue
                events_handle.write(line)
                events_handle.flush()
                event_count += 1
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    last_event_type = "invalid_jsonl"
                    continue
                if not isinstance(event, Mapping):
                    last_event_type = "invalid_event"
                    continue
                last_event_type = str(event.get("type") or "unknown")
                if event.get("type") == "thread.started" and event.get("thread_id"):
                    thread_id = str(event["thread_id"])
                usage = usage.plus(_usage_from_event(event))
                if (
                    record.budget.token_limit is not None
                    and usage.total_tokens > record.budget.token_limit
                ):
                    termination_reason = "token_budget_exceeded"
                    final_state = CodexRunState.TOKEN_BUDGET_EXCEEDED
                    if process.poll() is None:
                        _terminate_process(process)
            exit_code = process.wait()
            if termination_reason is None:
                with _store(root) as store:
                    current = store.require_codex_run(record.run_id)
                if current.state is CodexRunState.CANCELLING:
                    final_state = CodexRunState.CANCELLED
                    termination_reason = "cancellation_requested"
                else:
                    final_state = (
                        CodexRunState.SUCCEEDED
                        if exit_code == 0
                        else CodexRunState.FAILED
                    )
                    if exit_code != 0:
                        termination_reason = "codex_exit_nonzero"
    except OSError as exc:
        error = str(exc)
        final_state = CodexRunState.SPAWN_FAILED
        termination_reason = "spawn_failed"
        if process is not None:
            _terminate_process(process)
            exit_code = process.poll()
    except KeyboardInterrupt:
        final_state = CodexRunState.CANCELLED
        termination_reason = "runner_interrupted"
        if process is not None:
            _terminate_process(process)
            exit_code = process.poll()
    finished_at = _utc_now()
    duration = max(0.0, time.monotonic() - started_monotonic)
    intent_id: str | None = None
    if thread_id:
        try:
            status = codex_intent_status(worktree, session_id=thread_id)
            value = status.get("intent_id")
            intent_id = str(value) if value else None
        except (KeyError, ValueError, json.JSONDecodeError):
            intent_id = None
    terminal = record.with_updates(
        state=final_state,
        updated_at=finished_at,
        finished_at=finished_at,
        exit_code=exit_code,
        duration_seconds=round(duration, 6),
        usage=usage,
        event_count=event_count,
        last_event_type=last_event_type,
        codex_thread_id=thread_id,
        intent_id=intent_id,
        termination_reason=termination_reason,
        error=error,
        heartbeat_at=finished_at,
        lease_expires_at=None,
    )
    with _store(root) as store:
        store.update_codex_run(terminal)
        store.bind_worktree_runner(
            session_id,
            work_id,
            worker_id=terminal.run_id,
            intent_id=intent_id,
            updated_at=finished_at,
        )
    Path(terminal.run_directory, "record.final.json").write_text(
        json.dumps(terminal.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return terminal


def list_codex_runs(
    repo: str | Path, session_id: str, *, work_id: str | None = None
) -> list[CodexRunRecord]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    with _store(root) as store:
        session = store.require(session_id)
        records = store.list_codex_runs(session_id, work_id=work_id)
    if session.repository_identity != _repository_identity(root):
        raise ValueError("swarm session is bound to a different repository identity")
    return records


def get_codex_run(repo: str | Path, run_id: str) -> CodexRunRecord:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    with _store(root) as store:
        return store.require_codex_run(run_id)


def cancel_codex_run(repo: str | Path, run_id: str) -> CodexRunRecord:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    now = _utc_now()
    with _store(root) as store:
        record = store.require_codex_run(run_id)
        if record.state.terminal:
            return record
        cancelling = replace(
            record,
            state=CodexRunState.CANCELLING,
            updated_at=now,
            termination_reason="cancellation_requested",
        )
        store.update_codex_run(cancelling)
    pid = cancelling.agent_pid
    if pid is not None:
        try:
            if os.name == "posix":
                os.killpg(pid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return cancelling
