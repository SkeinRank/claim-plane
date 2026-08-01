"""Dynamic dependency scheduler over a shared-admitted swarm graph."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from claim_plane.swarm.admission import SharedAdmissionPlan, SharedAdmissionStatus
from claim_plane.swarm.models import SwarmSession
from claim_plane.swarm.runs import CodexRunRecord, CodexRunState

SWARM_SCHEDULER_SNAPSHOT_PROTOCOL = "claim-plane.swarm-scheduler-snapshot.v1"

_ACTIVE = {CodexRunState.RESERVED, CodexRunState.RUNNING, CodexRunState.CANCELLING}


class SchedulerStatus(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    REPLAN_REQUIRED = "replan_required"


class ScheduledWorkState(str, Enum):
    PENDING = "pending"
    BLOCKED = "blocked"
    RUNNABLE = "runnable"
    QUEUED_CAPACITY = "queued_capacity"
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    RETRYABLE = "retryable"
    FAILED = "failed"
    REPLAN_REQUIRED = "replan_required"


def _fingerprint(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def latest_runs(records: list[CodexRunRecord]) -> dict[str, CodexRunRecord]:
    latest: dict[str, CodexRunRecord] = {}
    for record in sorted(records, key=lambda item: (item.attempt, item.created_at)):
        latest[record.work_id] = record
    return latest


@dataclass(frozen=True, slots=True)
class ScheduledWork:
    work_id: str
    state: ScheduledWorkState
    effective_dependencies: tuple[str, ...]
    attempt_count: int
    latest_run_id: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.work_id:
            raise ValueError("work_id must not be empty")
        object.__setattr__(self, "state", ScheduledWorkState(self.state))
        object.__setattr__(
            self,
            "effective_dependencies",
            tuple(sorted(set(self.effective_dependencies))),
        )
        if self.attempt_count < 0:
            raise ValueError("attempt_count must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "state": self.state.value,
            "effective_dependencies": list(self.effective_dependencies),
            "attempt_count": self.attempt_count,
            "latest_run_id": self.latest_run_id,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class SchedulerSnapshot:
    session_id: str
    admission_fingerprint: str
    status: SchedulerStatus
    active_workers: int
    max_active_workers: int
    dispatchable_work_ids: tuple[str, ...]
    work: tuple[ScheduledWork, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SWARM_SCHEDULER_SNAPSHOT_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SWARM_SCHEDULER_SNAPSHOT_PROTOCOL:
            raise ValueError(f"unsupported scheduler protocol {self.protocol!r}")
        if self.active_workers < 0 or self.max_active_workers <= 0:
            raise ValueError("invalid scheduler worker counts")
        object.__setattr__(self, "status", SchedulerStatus(self.status))
        work = tuple(self.work)
        if len({item.work_id for item in work}) != len(work):
            raise ValueError("scheduler work_ids must be unique")
        known = {item.work_id for item in work}
        if not set(self.dispatchable_work_ids).issubset(known):
            raise ValueError("dispatchable work must exist in scheduler snapshot")
        object.__setattr__(self, "work", work)
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "session_id": self.session_id,
            "admission_fingerprint": self.admission_fingerprint,
            "status": self.status.value,
            "active_workers": self.active_workers,
            "max_active_workers": self.max_active_workers,
            "available_slots": max(0, self.max_active_workers - self.active_workers),
            "dispatchable_work_ids": list(self.dispatchable_work_ids),
            "work": [item.to_dict() for item in self.work],
            "metadata": dict(self.metadata),
        }

    def fingerprint(self) -> str:
        return _fingerprint(self.to_dict())

    def summary(self) -> dict[str, Any]:
        counts = {
            state.value: sum(1 for item in self.work if item.state is state)
            for state in ScheduledWorkState
            if any(item.state is state for item in self.work)
        }
        return {
            "status": self.status.value,
            "active_workers": self.active_workers,
            "max_active_workers": self.max_active_workers,
            "available_slots": max(0, self.max_active_workers - self.active_workers),
            "dispatchable_work_ids": list(self.dispatchable_work_ids),
            "states": counts,
            "fingerprint": self.fingerprint(),
        }


def compute_scheduler_snapshot(
    session: SwarmSession,
    admission: SharedAdmissionPlan,
    records: list[CodexRunRecord],
    *,
    integrated_work_ids: set[str] | None = None,
) -> SchedulerSnapshot:
    if (
        admission.session_id != session.session_id
        or admission.repository_identity != session.repository_identity
        or admission.graph_version != session.graph_version
        or admission.graph_fingerprint != session.graph_fingerprint
        or admission.budget_version != session.budget_version
        or admission.budget_fingerprint != session.budget_fingerprint
    ):
        raise ValueError("shared admission is stale for the swarm session")

    max_attempts = 1 + session.budget_policy.retries.max_agent_restarts
    latest = latest_runs(records)
    attempts = {
        work_id: sum(1 for record in records if record.work_id == work_id)
        for work_id in session.work_graph.item_map
    }
    active_workers = sum(1 for record in records if record.state in _ACTIVE)
    order = session.work_graph.topological_order()
    rank = {work_id: index for index, work_id in enumerate(order)}

    preliminary: dict[str, ScheduledWork] = {}
    candidates: list[str] = []
    for work_id in order:
        item = admission.admission_map[work_id]
        record = latest.get(work_id)
        run_count = attempts[work_id]
        if (
            admission.status is SharedAdmissionStatus.REPLAN_REQUIRED
            or not item.allowed
        ):
            preliminary[work_id] = ScheduledWork(
                work_id,
                ScheduledWorkState.REPLAN_REQUIRED,
                item.effective_dependencies,
                run_count,
                record.run_id if record else None,
                "shared admission did not grant this work item",
            )
            continue
        if record is not None and record.state is CodexRunState.SUCCEEDED:
            preliminary[work_id] = ScheduledWork(
                work_id,
                ScheduledWorkState.SUCCEEDED,
                item.effective_dependencies,
                run_count,
                record.run_id,
                "latest execution succeeded; verification remains separate",
            )
            continue
        if record is not None and record.state in _ACTIVE:
            preliminary[work_id] = ScheduledWork(
                work_id,
                ScheduledWorkState.ACTIVE,
                item.effective_dependencies,
                run_count,
                record.run_id,
                f"worker run is {record.state.value}",
            )
            continue
        missing = [
            dependency
            for dependency in item.effective_dependencies
            if latest.get(dependency) is None
            or latest[dependency].state is not CodexRunState.SUCCEEDED
            or (
                integrated_work_ids is not None
                and dependency not in integrated_work_ids
            )
        ]
        exhausted_dependency = [
            dependency
            for dependency in missing
            if latest.get(dependency) is not None
            and latest[dependency].state.terminal
            and latest[dependency].state is not CodexRunState.SUCCEEDED
            and attempts[dependency] >= max_attempts
        ]
        if exhausted_dependency:
            preliminary[work_id] = ScheduledWork(
                work_id,
                ScheduledWorkState.BLOCKED,
                item.effective_dependencies,
                run_count,
                record.run_id if record else None,
                "dependency retry budget exhausted: " + ", ".join(exhausted_dependency),
            )
            continue
        if missing:
            preliminary[work_id] = ScheduledWork(
                work_id,
                ScheduledWorkState.BLOCKED,
                item.effective_dependencies,
                run_count,
                record.run_id if record else None,
                "waiting for dependencies: " + ", ".join(missing),
            )
            continue
        if record is not None and record.state.terminal and run_count >= max_attempts:
            preliminary[work_id] = ScheduledWork(
                work_id,
                ScheduledWorkState.FAILED,
                item.effective_dependencies,
                run_count,
                record.run_id,
                "worker retry budget exhausted",
            )
            continue
        candidates.append(work_id)
        preliminary[work_id] = ScheduledWork(
            work_id,
            (
                ScheduledWorkState.RETRYABLE
                if record is not None
                else ScheduledWorkState.PENDING
            ),
            item.effective_dependencies,
            run_count,
            record.run_id if record else None,
            "dependencies satisfied",
        )

    available = max(0, session.budget_policy.workers.max_active - active_workers)
    dispatchable = tuple(sorted(candidates, key=rank.__getitem__)[:available])
    dispatchable_set = set(dispatchable)
    final: list[ScheduledWork] = []
    for work_id in order:
        scheduled_item = preliminary[work_id]
        if work_id in candidates:
            state = (
                ScheduledWorkState.RUNNABLE
                if work_id in dispatchable_set
                else ScheduledWorkState.QUEUED_CAPACITY
            )
            detail = (
                "admitted, dependencies satisfied, worker slot available"
                if state is ScheduledWorkState.RUNNABLE
                else "admitted and dependency-ready; waiting for worker capacity"
            )
            scheduled_item = ScheduledWork(
                work_id,
                state,
                scheduled_item.effective_dependencies,
                scheduled_item.attempt_count,
                scheduled_item.latest_run_id,
                detail,
            )
        final.append(scheduled_item)

    if admission.status is SharedAdmissionStatus.REPLAN_REQUIRED:
        status = SchedulerStatus.REPLAN_REQUIRED
    elif all(item.state is ScheduledWorkState.SUCCEEDED for item in final):
        status = SchedulerStatus.COMPLETED
    elif dispatchable or active_workers:
        status = SchedulerStatus.READY
    else:
        status = SchedulerStatus.BLOCKED
    return SchedulerSnapshot(
        session_id=session.session_id,
        admission_fingerprint=admission.fingerprint(),
        status=status,
        active_workers=active_workers,
        max_active_workers=session.budget_policy.workers.max_active,
        dispatchable_work_ids=dispatchable,
        work=tuple(final),
        metadata={
            "success_semantics": "codex_execution_succeeded_not_verified",
            "ordering": "effective-dependencies-then-topological-fairness",
            "dependency_release": (
                "integrated"
                if integrated_work_ids is not None
                else "execution_succeeded"
            ),
        },
    )
