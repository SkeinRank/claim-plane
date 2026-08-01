"""Crash recovery, session control, and bounded worker replacement for swarms."""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from claim_plane.swarm.codex_runner import run_codex_work_item
from claim_plane.swarm.models import SwarmSessionState
from claim_plane.swarm.runs import CodexRunRecord, CodexRunState
from claim_plane.swarm.service import (
    _repository_identity,
    _require_initialized,
    _store,
    _validate_session_id,
    admit_swarm_session,
    inspect_swarm_worktrees,
    resolve_repository_root,
)
from claim_plane.swarm.worktrees import WorktreeHealth

SWARM_RECOVERY_PROTOCOL = "claim-plane.swarm-recovery.v1"
_DEFAULT_STALE_AFTER_SECONDS = 30


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp has no timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _process_alive(pid: int | None) -> bool:
    if pid is None:
        return False

    if os.name == "posix":
        try:
            waited_pid, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        except ProcessLookupError:
            return False
        else:
            if waited_pid == pid:
                return False

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    return True


def _terminate_pid(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        if os.name == "posix":
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    return True


def _terminate_and_confirm(
    pid: int | None,
    *,
    timeout_seconds: float = 5.0,
) -> bool:
    if not _terminate_pid(pid):
        return not _process_alive(pid)

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.05)

    if pid is not None and os.name == "posix":
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except PermissionError:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if not _process_alive(pid):
                return True
            time.sleep(0.05)

    return not _process_alive(pid)


class RecoveryHealth(str, Enum):
    HEALTHY = "healthy"
    RESERVED = "reserved"
    STALE = "stale"
    LOST = "lost"


@dataclass(frozen=True, slots=True)
class RunRecoveryInspection:
    run_id: str
    work_id: str
    state: CodexRunState
    health: RecoveryHealth
    runner_alive: bool
    agent_alive: bool
    heartbeat_at: str
    lease_expires_at: str | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "work_id": self.work_id,
            "state": self.state.value,
            "health": self.health.value,
            "runner_alive": self.runner_alive,
            "agent_alive": self.agent_alive,
            "heartbeat_at": self.heartbeat_at,
            "lease_expires_at": self.lease_expires_at,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class RecoveryEvent:
    event_id: str
    session_id: str
    action: str
    created_at: str
    run_id: str | None = None
    work_id: str | None = None
    detail: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SWARM_RECOVERY_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SWARM_RECOVERY_PROTOCOL:
            raise ValueError(f"unsupported recovery protocol {self.protocol!r}")
        if not self.event_id or not self.session_id or not self.action:
            raise ValueError("recovery event identity fields must not be empty")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "action": self.action,
            "run_id": self.run_id,
            "work_id": self.work_id,
            "detail": self.detail,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RecoveryEvent":
        return cls(
            protocol=str(data.get("protocol") or SWARM_RECOVERY_PROTOCOL),
            event_id=str(data.get("event_id") or ""),
            session_id=str(data.get("session_id") or ""),
            action=str(data.get("action") or ""),
            run_id=None if data.get("run_id") is None else str(data["run_id"]),
            work_id=None if data.get("work_id") is None else str(data["work_id"]),
            detail=None if data.get("detail") is None else str(data["detail"]),
            created_at=str(data.get("created_at") or ""),
            metadata=dict(data.get("metadata") or {}),
        )


def _event(
    session_id: str,
    action: str,
    *,
    run_id: str | None = None,
    work_id: str | None = None,
    detail: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> RecoveryEvent:
    return RecoveryEvent(
        event_id=f"recovery-{secrets.token_hex(12)}",
        session_id=session_id,
        action=action,
        run_id=run_id,
        work_id=work_id,
        detail=detail,
        created_at=_utc_now(),
        metadata=dict(metadata or {}),
    )


def _inspect_run(
    record: CodexRunRecord, *, stale_after_seconds: int, now: datetime
) -> RunRecoveryInspection:
    heartbeat = record.heartbeat_at or record.updated_at
    heartbeat_time = _parse_timestamp(heartbeat)
    lease_expired = False
    if record.lease_expires_at is not None:
        lease_expired = _parse_timestamp(record.lease_expires_at) <= now
    else:
        lease_expired = (now - heartbeat_time).total_seconds() > stale_after_seconds
    runner_alive = _process_alive(record.runner_pid)
    agent_alive = _process_alive(record.agent_pid)
    if record.state is CodexRunState.RESERVED:
        if lease_expired:
            health = RecoveryHealth.LOST
            detail = "reservation lease expired before an agent process was bound"
        else:
            health = RecoveryHealth.RESERVED
            detail = "worker reservation is inside its lease window"
    elif not agent_alive:
        health = RecoveryHealth.LOST
        detail = "active run has no live agent process"
    elif lease_expired:
        health = RecoveryHealth.STALE
        detail = "agent process exists but its runner heartbeat lease expired"
    else:
        health = RecoveryHealth.HEALTHY
        detail = "agent process and runner lease are healthy"
    return RunRecoveryInspection(
        run_id=record.run_id,
        work_id=record.work_id,
        state=record.state,
        health=health,
        runner_alive=runner_alive,
        agent_alive=agent_alive,
        heartbeat_at=heartbeat,
        lease_expires_at=record.lease_expires_at,
        detail=detail,
    )


def inspect_swarm_recovery(
    repo: str | Path,
    session_id: str,
    *,
    stale_after_seconds: int = _DEFAULT_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    if stale_after_seconds <= 0:
        raise ValueError("stale_after_seconds must be positive")
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    with _store(root) as store:
        session = store.require(session_id)
        active = [
            record
            for record in store.list_codex_runs(session_id)
            if record.state.active
        ]
        events = store.list_recovery_events(session_id)
    if session.repository_identity != _repository_identity(root):
        raise ValueError("swarm session is bound to a different repository identity")
    now = datetime.now(timezone.utc)
    inspections = tuple(
        _inspect_run(record, stale_after_seconds=stale_after_seconds, now=now)
        for record in active
    )
    counts = {
        health.value: sum(1 for item in inspections if item.health is health)
        for health in RecoveryHealth
    }
    return {
        "protocol": SWARM_RECOVERY_PROTOCOL,
        "session_id": session_id,
        "session_state": session.state.value,
        "stale_after_seconds": stale_after_seconds,
        "active_runs": [item.to_dict() for item in inspections],
        "summary": counts,
        "recovery_events": len(events),
    }


def recover_swarm_session(
    repo: str | Path,
    session_id: str,
    *,
    stale_after_seconds: int = _DEFAULT_STALE_AFTER_SECONDS,
    terminate_stale: bool = False,
) -> dict[str, Any]:
    root = resolve_repository_root(repo)
    inspection = inspect_swarm_recovery(
        root, session_id, stale_after_seconds=stale_after_seconds
    )
    session_id = _validate_session_id(session_id)
    now = _utc_now()
    recovered: list[CodexRunRecord] = []
    skipped_stale: list[str] = []
    with _store(root) as store:
        session = store.require(session_id)
        by_id = {record.run_id: record for record in store.list_codex_runs(session_id)}
        for item in inspection["active_runs"]:
            health = RecoveryHealth(item["health"])
            current = by_id[item["run_id"]]
            if health is RecoveryHealth.HEALTHY or health is RecoveryHealth.RESERVED:
                continue
            if health is RecoveryHealth.STALE and not terminate_stale:
                skipped_stale.append(current.run_id)
                continue
            terminated = False
            if health is RecoveryHealth.STALE:
                terminated = _terminate_and_confirm(current.agent_pid)
                if not terminated:
                    skipped_stale.append(current.run_id)
                    continue
            target = (
                CodexRunState.CANCELLED
                if (
                    session.state is SwarmSessionState.CANCELLED
                    or current.state is CodexRunState.CANCELLING
                )
                else CodexRunState.LOST
            )
            reason = (
                "recovered_cancellation_completed"
                if current.state is CodexRunState.CANCELLING
                else (
                    "recovered_cancelled_session"
                    if target is CodexRunState.CANCELLED
                    else (
                        "recovered_stale_lease"
                        if health is RecoveryHealth.STALE
                        else "recovered_missing_process"
                    )
                )
            )
            terminal = replace(
                current,
                state=target,
                updated_at=now,
                finished_at=now,
                heartbeat_at=now,
                lease_expires_at=None,
                termination_reason=reason,
                error=item["detail"],
                recovery_generation=current.recovery_generation + 1,
                metadata={
                    **current.metadata,
                    "recovered": True,
                    "recovery_health": health.value,
                    "stale_process_terminated": terminated,
                },
            )
            try:
                stored = store.recover_codex_run(
                    terminal, expected_updated_at=current.updated_at
                )
            except ValueError as exc:
                if "heartbeat changed" in str(exc):
                    continue
                raise
            recovered.append(stored)
            event = _event(
                session_id,
                "run_recovered",
                run_id=stored.run_id,
                work_id=stored.work_id,
                detail=reason,
                metadata={
                    "previous_state": current.state.value,
                    "health": health.value,
                },
            )
            store.save_recovery_event(event.to_dict())
        current_session = store.require(session_id)
        if current_session.state is SwarmSessionState.VERIFYING:
            verification = store.get_verification(session_id)
            if verification is None:
                current_session = store.set_session_state(
                    session_id,
                    target=SwarmSessionState.RUNNING,
                    allowed_from={SwarmSessionState.VERIFYING},
                    updated_at=now,
                )
                store.save_recovery_event(
                    _event(
                        session_id,
                        "verification_reopened",
                        detail="interrupted verification returned to running state",
                    ).to_dict()
                )
    return {
        "protocol": SWARM_RECOVERY_PROTOCOL,
        "session_id": session_id,
        "session_state": current_session.state.value,
        "recovered_runs": [record.to_dict() for record in recovered],
        "recovered_count": len(recovered),
        "stale_requires_termination": skipped_stale,
        "idempotent": len(recovered) == 0,
    }


def pause_swarm_session(repo: str | Path, session_id: str) -> dict[str, Any]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    now = _utc_now()
    with _store(root) as store:
        active = [
            record
            for record in store.list_codex_runs(session_id)
            if record.state.active
        ]
        if active:
            raise ValueError("cannot pause while swarm workers are active")
        previous = store.require(session_id)
        if previous.state is SwarmSessionState.PAUSED:
            session = previous
            event = _event(
                session_id,
                "session_pause_unchanged",
                detail="swarm session was already paused",
            )
        else:
            session = store.set_session_state(
                session_id,
                target=SwarmSessionState.PAUSED,
                allowed_from={SwarmSessionState.PLANNED, SwarmSessionState.RUNNING},
                updated_at=now,
            )
            event = _event(
                session_id,
                "session_paused",
                metadata={"previous_state": previous.state.value},
            )
        store.save_recovery_event(event.to_dict())
    return {"session": session.to_dict(), "event": event.to_dict()}


def resume_swarm_session(repo: str | Path, session_id: str) -> dict[str, Any]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    now = _utc_now()
    with _store(root) as store:
        session = store.require(session_id)
        if session.state in {SwarmSessionState.RUNNING, SwarmSessionState.PLANNED}:
            resumed = session
        elif session.state is SwarmSessionState.PAUSED:
            events = store.list_recovery_events(session_id)
            previous_state = next(
                (
                    str(recovery_event.get("metadata", {}).get("previous_state"))
                    for recovery_event in reversed(events)
                    if recovery_event.get("action") == "session_paused"
                ),
                SwarmSessionState.RUNNING.value,
            )
            target = (
                SwarmSessionState.PLANNED
                if previous_state == SwarmSessionState.PLANNED.value
                else SwarmSessionState.RUNNING
            )
            resumed = store.set_session_state(
                session_id,
                target=target,
                allowed_from={SwarmSessionState.PAUSED},
                updated_at=now,
            )
        else:
            raise ValueError(f"cannot resume swarm session from {session.state.value}")
        event = _event(
            session_id,
            "session_resumed",
            metadata={"restored_state": resumed.state.value},
        )
        store.save_recovery_event(event.to_dict())
    return {"session": resumed.to_dict(), "event": event.to_dict()}


def cancel_swarm_session(repo: str | Path, session_id: str) -> dict[str, Any]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    now = _utc_now()
    with _store(root) as store:
        session, records = store.request_session_cancellation(
            session_id, updated_at=now
        )
        event = _event(
            session_id,
            "session_cancelled",
            detail=f"requested cancellation for {len(records)} active worker(s)",
        )
        store.save_recovery_event(event.to_dict())
    signalled = [
        record.run_id for record in records if _terminate_pid(record.agent_pid)
    ]
    return {
        "session": session.to_dict(),
        "active_runs": [record.to_dict() for record in records],
        "signalled_run_ids": signalled,
        "event": event.to_dict(),
    }


def list_swarm_recovery_events(
    repo: str | Path, session_id: str
) -> list[RecoveryEvent]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    with _store(root) as store:
        session = store.require(session_id)
        payloads = store.list_recovery_events(session_id)
    if session.repository_identity != _repository_identity(root):
        raise ValueError("swarm session is bound to a different repository identity")
    return [RecoveryEvent.from_dict(payload) for payload in payloads]


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=path, text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise ValueError(
            completed.stderr.strip() or completed.stdout.strip() or "git failed"
        )
    return completed.stdout.strip()


def _reset_replacement_worktree(path: Path, base_commit: str) -> None:
    _git(path, "reset", "--hard", base_commit)
    _git(path, "clean", "-fd", "-e", ".codex/hooks.json", "-e", ".claim-plane/")


def replace_codex_worker(
    repo: str | Path,
    session_id: str,
    work_id: str,
    *,
    replaced_run_id: str,
    reset_worktree: bool = False,
    codex_binary: str = "codex",
    model: str | None = None,
    reasoning_effort: str | None = None,
    timeout_seconds: int | None = None,
    token_limit: int | None = None,
) -> CodexRunRecord:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    with _store(root) as store:
        session = store.require(session_id)
        source = store.require_codex_run(replaced_run_id)
        records = store.list_codex_runs(session_id, work_id=work_id)
    if source.session_id != session_id or source.work_id != work_id:
        raise ValueError("replacement source is bound to a different work item")
    if not source.state.terminal or source.state is CodexRunState.SUCCEEDED:
        raise ValueError("replacement source must be a terminal unsuccessful run")
    if not records or records[-1].run_id != source.run_id:
        raise ValueError("only the latest run for a work item may be replaced")
    if session.state not in {SwarmSessionState.PLANNED, SwarmSessionState.RUNNING}:
        raise ValueError(
            f"cannot replace a worker while session is {session.state.value}"
        )
    worktrees = inspect_swarm_worktrees(root, session_id)
    selected = next(
        item for item in worktrees["worktrees"] if item["record"]["work_id"] == work_id
    )
    health = WorktreeHealth(selected["health"])
    path = Path(selected["record"]["worktree_path"])
    head = _git(path, "rev-parse", "HEAD").lower()
    contaminated = health is WorktreeHealth.DIRTY or head != source.base_commit
    if contaminated and not reset_worktree:
        raise ValueError(
            "replacement worktree contains predecessor changes; use --reset-worktree "
            "to start from the controlled execution base"
        )
    if contaminated:
        _reset_replacement_worktree(path, source.base_commit)
    admission = admit_swarm_session(root, session_id)
    if admission["summary"]["status"] != "ready":
        raise ValueError("replacement authority could not be re-admitted")
    result = run_codex_work_item(
        root,
        session_id,
        work_id,
        codex_binary=codex_binary,
        model=model,
        reasoning_effort=reasoning_effort,
        timeout_seconds=timeout_seconds,
        token_limit=token_limit,
        replacement_of_run_id=source.run_id,
    )
    with _store(root) as store:
        store.save_recovery_event(
            _event(
                session_id,
                "worker_replaced",
                run_id=result.run_id,
                work_id=work_id,
                detail=f"replacement for {source.run_id}",
                metadata={
                    "replaced_run_id": source.run_id,
                    "fresh_run_identity": True,
                    "inherited_codex_thread": False,
                    "worktree_reset": contaminated,
                },
            ).to_dict()
        )
    return result
