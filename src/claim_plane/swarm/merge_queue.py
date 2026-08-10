"""Deterministic merge-queue protocol for swarm integration.

The queue never mutates the user's target branch. It stages worker results on a
Claim Plane-owned integration branch in a deterministic effective-dependency and
work-graph order. Successful execution is only eligibility for integration; the
integrated result remains unverified until the later swarm-verification stage.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from claim_plane.swarm.admission import SharedAdmissionPlan
from claim_plane.swarm.models import SwarmSession
from claim_plane.swarm.runs import CodexRunRecord, CodexRunState
from claim_plane.swarm.worktrees import ManagedWorktree, managed_session_component

SWARM_MERGE_QUEUE_PROTOCOL = "claim-plane.swarm-merge-queue.v1"

_DIGEST_RE = re.compile(r"[0-9a-f]{40,64}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _clean(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _fingerprint(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def managed_integration_branch(session_id: str) -> str:
    return f"claim-plane/swarm/{managed_session_component(session_id)}/integration"


def managed_integration_worktree_path(root: Path, session_id: str) -> Path:
    return (
        root
        / ".claim-plane"
        / "worktrees"
        / managed_session_component(session_id)
        / "__integration__"
    ).resolve()


class MergeEntryState(str, Enum):
    PENDING = "pending"
    BLOCKED = "blocked"
    READY = "ready"
    INTEGRATING = "integrating"
    INTEGRATED = "integrated"
    CONFLICT = "conflict"


class MergeQueueStatus(str, Enum):
    WAITING = "waiting"
    READY = "ready"
    INTEGRATING = "integrating"
    CONFLICT = "conflict"
    COMPLETED = "completed"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class MergeQueueEntry:
    work_id: str
    order: int
    effective_dependencies: tuple[str, ...]
    source_branch: str
    state: MergeEntryState
    run_id: str | None = None
    source_commit: str | None = None
    integration_commit: str | None = None
    conflict_paths: tuple[str, ...] = ()
    integration_evidence: Mapping[str, Any] | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "work_id", _clean(self.work_id, field_name="work_id"))
        if self.order < 0:
            raise ValueError("merge queue order must be non-negative")
        object.__setattr__(
            self,
            "effective_dependencies",
            tuple(sorted(set(self.effective_dependencies))),
        )
        object.__setattr__(
            self,
            "source_branch",
            _clean(self.source_branch, field_name="source_branch"),
        )
        object.__setattr__(self, "state", MergeEntryState(self.state))
        for name in ("source_commit", "integration_commit"):
            value = getattr(self, name)
            if value is not None:
                cleaned = _clean(value, field_name=name).lower()
                if not _DIGEST_RE.fullmatch(cleaned):
                    raise ValueError(f"{name} must be a full Git object id")
                object.__setattr__(self, name, cleaned)
        object.__setattr__(
            self,
            "conflict_paths",
            tuple(sorted(set(self.conflict_paths))),
        )
        if self.integration_evidence is not None:
            object.__setattr__(
                self, "integration_evidence", dict(self.integration_evidence)
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "order": self.order,
            "effective_dependencies": list(self.effective_dependencies),
            "source_branch": self.source_branch,
            "state": self.state.value,
            "run_id": self.run_id,
            "source_commit": self.source_commit,
            "integration_commit": self.integration_commit,
            "conflict_paths": list(self.conflict_paths),
            "integration_evidence": (
                None
                if self.integration_evidence is None
                else dict(self.integration_evidence)
            ),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MergeQueueEntry":
        return cls(
            work_id=str(data.get("work_id") or ""),
            order=int(data.get("order") or 0),
            effective_dependencies=tuple(data.get("effective_dependencies") or ()),
            source_branch=str(data.get("source_branch") or ""),
            state=MergeEntryState(data.get("state") or "pending"),
            run_id=None if data.get("run_id") is None else str(data.get("run_id")),
            source_commit=(
                None
                if data.get("source_commit") is None
                else str(data.get("source_commit"))
            ),
            integration_commit=(
                None
                if data.get("integration_commit") is None
                else str(data.get("integration_commit"))
            ),
            conflict_paths=tuple(data.get("conflict_paths") or ()),
            integration_evidence=(
                None
                if data.get("integration_evidence") is None
                else dict(data.get("integration_evidence") or {})
            ),
            detail=str(data.get("detail") or ""),
        )


@dataclass(frozen=True, slots=True)
class DeterministicMergeQueue:
    session_id: str
    repository_identity: str
    base_commit: str
    graph_version: int
    graph_fingerprint: str
    budget_version: int
    budget_fingerprint: str
    admission_fingerprint: str
    integration_target_branch: str
    integration_branch: str
    integration_worktree_path: str
    integration_head: str
    status: MergeQueueStatus
    entries: tuple[MergeQueueEntry, ...]
    created_at: str
    updated_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SWARM_MERGE_QUEUE_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SWARM_MERGE_QUEUE_PROTOCOL:
            raise ValueError(f"unsupported merge-queue protocol {self.protocol!r}")
        object.__setattr__(
            self, "session_id", _clean(self.session_id, field_name="session_id")
        )
        identity = _clean(
            self.repository_identity, field_name="repository_identity"
        ).lower()
        if not _SHA256_RE.fullmatch(identity):
            raise ValueError("repository_identity must be a SHA-256 digest")
        object.__setattr__(self, "repository_identity", identity)
        for name in ("base_commit", "integration_head"):
            value = _clean(str(getattr(self, name)), field_name=name).lower()
            if not _DIGEST_RE.fullmatch(value):
                raise ValueError(f"{name} must be a full Git object id")
            object.__setattr__(self, name, value)
        if self.graph_version <= 0 or self.budget_version <= 0:
            raise ValueError("graph and budget versions must be positive")
        for name in (
            "graph_fingerprint",
            "budget_fingerprint",
            "admission_fingerprint",
        ):
            value = _clean(str(getattr(self, name)), field_name=name).lower()
            if not _SHA256_RE.fullmatch(value):
                raise ValueError(f"{name} must be a SHA-256 digest")
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "integration_target_branch",
            _clean(
                self.integration_target_branch,
                field_name="integration_target_branch",
            ),
        )
        branch = _clean(self.integration_branch, field_name="integration_branch")
        if not branch.startswith("claim-plane/swarm/"):
            raise ValueError("integration branch is outside Claim Plane namespace")
        object.__setattr__(self, "integration_branch", branch)
        path = Path(
            _clean(
                self.integration_worktree_path,
                field_name="integration_worktree_path",
            )
        )
        if not path.is_absolute():
            raise ValueError("integration worktree path must be absolute")
        object.__setattr__(self, "integration_worktree_path", str(path.resolve()))
        object.__setattr__(self, "status", MergeQueueStatus(self.status))
        entries = tuple(sorted(self.entries, key=lambda item: item.order))
        if len({entry.work_id for entry in entries}) != len(entries):
            raise ValueError("merge queue work_ids must be unique")
        if tuple(entry.order for entry in entries) != tuple(range(len(entries))):
            raise ValueError("merge queue order must be contiguous from zero")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(
            self, "created_at", _clean(self.created_at, field_name="created_at")
        )
        object.__setattr__(
            self, "updated_at", _clean(self.updated_at, field_name="updated_at")
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def entry_map(self) -> dict[str, MergeQueueEntry]:
        return {entry.work_id: entry for entry in self.entries}

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "session_id": self.session_id,
            "repository_identity": self.repository_identity,
            "base_commit": self.base_commit,
            "graph_version": self.graph_version,
            "graph_fingerprint": self.graph_fingerprint,
            "budget_version": self.budget_version,
            "budget_fingerprint": self.budget_fingerprint,
            "admission_fingerprint": self.admission_fingerprint,
            "integration_target_branch": self.integration_target_branch,
            "integration_branch": self.integration_branch,
            "integration_worktree_path": self.integration_worktree_path,
            "integration_head": self.integration_head,
            "status": self.status.value,
            "entries": [entry.to_dict() for entry in self.entries],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    def fingerprint(self) -> str:
        payload = self.to_dict()
        payload.pop("created_at", None)
        payload.pop("updated_at", None)
        return _fingerprint(payload)

    def summary(self) -> dict[str, Any]:
        counts = {
            state.value: sum(1 for entry in self.entries if entry.state is state)
            for state in MergeEntryState
            if any(entry.state is state for entry in self.entries)
        }
        ready = [
            entry.work_id
            for entry in self.entries
            if entry.state is MergeEntryState.READY
        ]
        return {
            "status": self.status.value,
            "entries": len(self.entries),
            "states": counts,
            "ready_work_ids": ready,
            "integration_head": self.integration_head,
            "fingerprint": self.fingerprint(),
        }

    def with_entry(
        self,
        updated: MergeQueueEntry,
        *,
        integration_head: str,
        updated_at: str,
    ) -> "DeterministicMergeQueue":
        entries = tuple(
            updated if entry.work_id == updated.work_id else entry
            for entry in self.entries
        )
        return replace(
            self,
            entries=entries,
            integration_head=integration_head,
            status=_queue_status(entries),
            updated_at=updated_at,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DeterministicMergeQueue":
        return cls(
            protocol=str(data.get("protocol") or SWARM_MERGE_QUEUE_PROTOCOL),
            session_id=str(data.get("session_id") or ""),
            repository_identity=str(data.get("repository_identity") or ""),
            base_commit=str(data.get("base_commit") or ""),
            graph_version=int(data.get("graph_version") or 0),
            graph_fingerprint=str(data.get("graph_fingerprint") or ""),
            budget_version=int(data.get("budget_version") or 0),
            budget_fingerprint=str(data.get("budget_fingerprint") or ""),
            admission_fingerprint=str(data.get("admission_fingerprint") or ""),
            integration_target_branch=str(data.get("integration_target_branch") or ""),
            integration_branch=str(data.get("integration_branch") or ""),
            integration_worktree_path=str(data.get("integration_worktree_path") or ""),
            integration_head=str(
                data.get("integration_head") or data.get("base_commit") or ""
            ),
            status=MergeQueueStatus(data.get("status") or "waiting"),
            entries=tuple(
                MergeQueueEntry.from_dict(item) for item in data.get("entries") or ()
            ),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            metadata=dict(data.get("metadata") or {}),
        )


def _queue_status(entries: tuple[MergeQueueEntry, ...]) -> MergeQueueStatus:
    if any(entry.state is MergeEntryState.CONFLICT for entry in entries):
        return MergeQueueStatus.CONFLICT
    if any(entry.state is MergeEntryState.INTEGRATING for entry in entries):
        return MergeQueueStatus.INTEGRATING
    if entries and all(entry.state is MergeEntryState.INTEGRATED for entry in entries):
        return MergeQueueStatus.COMPLETED
    if any(entry.state is MergeEntryState.READY for entry in entries):
        return MergeQueueStatus.READY
    return MergeQueueStatus.WAITING


def latest_successful_runs(records: list[CodexRunRecord]) -> dict[str, CodexRunRecord]:
    latest: dict[str, CodexRunRecord] = {}
    for record in sorted(records, key=lambda item: (item.attempt, item.created_at)):
        latest[record.work_id] = record
    return {
        work_id: record
        for work_id, record in latest.items()
        if record.state is CodexRunState.SUCCEEDED
    }


def compute_merge_queue(
    session: SwarmSession,
    admission: SharedAdmissionPlan,
    records: list[CodexRunRecord],
    worktrees: list[ManagedWorktree],
    *,
    root: Path,
    integration_head: str,
    now: str,
    previous: DeterministicMergeQueue | None = None,
) -> DeterministicMergeQueue:
    if admission.session_id != session.session_id:
        raise ValueError("shared admission belongs to another swarm session")
    if admission.fingerprint() == "":  # pragma: no cover - defensive
        raise ValueError("shared admission fingerprint is empty")
    by_worktree = {record.work_id: record for record in worktrees}
    successful = latest_successful_runs(records)
    previous_map = previous.entry_map if previous is not None else {}
    integrated = {
        entry.work_id
        for entry in previous_map.values()
        if entry.state is MergeEntryState.INTEGRATED
    }
    entries: list[MergeQueueEntry] = []
    admission_map = admission.admission_map
    for order, work_id in enumerate(session.work_graph.topological_order()):
        admitted = admission_map[work_id]
        record = successful.get(work_id)
        worktree = by_worktree.get(work_id)
        old = previous_map.get(work_id)
        if old is not None and old.state in {
            MergeEntryState.INTEGRATED,
            MergeEntryState.CONFLICT,
            MergeEntryState.INTEGRATING,
        }:
            entries.append(old)
            continue
        dependencies = admitted.effective_dependencies
        missing_integrations = [
            dependency for dependency in dependencies if dependency not in integrated
        ]
        if not admitted.allowed:
            state = MergeEntryState.BLOCKED
            detail = "shared admission did not grant this work item"
        elif worktree is None:
            state = MergeEntryState.BLOCKED
            detail = "managed worktree is not provisioned"
        elif record is None:
            state = MergeEntryState.PENDING
            detail = "waiting for a successful Codex execution"
        elif missing_integrations:
            state = MergeEntryState.BLOCKED
            detail = "waiting for integrated dependencies: " + ", ".join(
                missing_integrations
            )
        else:
            state = MergeEntryState.READY
            detail = "successful execution and integrated dependencies"
        preserved_source_commit = (
            old.source_commit
            if old is not None
            and record is not None
            and old.run_id == record.run_id
            and state is MergeEntryState.READY
            else None
        )
        entry = MergeQueueEntry(
            work_id=work_id,
            order=order,
            effective_dependencies=dependencies,
            source_branch=(
                worktree.branch
                if worktree is not None
                else "claim-plane/swarm/unprovisioned"
            ),
            state=state,
            run_id=None if record is None else record.run_id,
            source_commit=preserved_source_commit,
            detail=detail,
        )
        entries.append(entry)
        if state is MergeEntryState.INTEGRATED:
            integrated.add(work_id)
    entries_tuple = tuple(entries)
    created_at = now if previous is None else previous.created_at
    return DeterministicMergeQueue(
        session_id=session.session_id,
        repository_identity=session.repository_identity,
        base_commit=session.base_commit,
        graph_version=session.graph_version,
        graph_fingerprint=session.graph_fingerprint,
        budget_version=session.budget_version,
        budget_fingerprint=session.budget_fingerprint,
        admission_fingerprint=admission.fingerprint(),
        integration_target_branch=session.integration_target.branch,
        integration_branch=managed_integration_branch(session.session_id),
        integration_worktree_path=str(
            managed_integration_worktree_path(root, session.session_id)
        ),
        integration_head=integration_head,
        status=_queue_status(entries_tuple),
        entries=entries_tuple,
        created_at=created_at,
        updated_at=now,
        metadata={
            "ordering": "effective-dependencies-then-work-graph-topological-order",
            "target_branch_mutated": False,
            "verification": "required_after_integration",
        },
    )
