"""Managed Git worktree records and health inspection for swarm execution.

Claim Plane provisions one isolated linked worktree per work item. Records are
bound to the repository identity, pinned session base, and exact work-graph
version that authorized their creation. Git remains the source of truth for
physical worktrees; the durable record is the ownership and cleanup boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

SWARM_MANAGED_WORKTREE_PROTOCOL = "claim-plane.swarm-managed-worktree.v1"

_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")


def _clean(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _digest(value: str, length: int = 10) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _slug(value: str, *, limit: int = 32) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-_").lower()
    cleaned = cleaned or "item"
    return cleaned[:limit].rstrip(".-_") or "item"


def managed_session_component(session_id: str) -> str:
    """Return a stable, collision-resistant directory component."""

    return f"{_slug(session_id)}-{_digest(session_id)}"


def managed_work_component(work_id: str) -> str:
    """Return a stable, collision-resistant work-item directory component."""

    return f"{_slug(work_id)}-{_digest(work_id)}"


def managed_branch_name(session_id: str, work_id: str) -> str:
    """Return the Claim Plane-owned branch name for one work item."""

    return (
        "claim-plane/swarm/"
        f"{managed_session_component(session_id)}/"
        f"{managed_work_component(work_id)}"
    )


def managed_worktree_path(root: Path, session_id: str, work_id: str) -> Path:
    """Return the repository-local ignored path for one linked worktree."""

    return (
        root
        / ".claim-plane"
        / "worktrees"
        / managed_session_component(session_id)
        / managed_work_component(work_id)
    ).resolve()


class ManagedWorktreeState(str, Enum):
    PROVISIONED = "provisioned"
    RELEASED = "released"


class WorktreeHealth(str, Enum):
    READY = "ready"
    DIRTY = "dirty"
    STALE_GRAPH = "stale_graph"
    MISSING = "missing"
    UNREGISTERED = "unregistered"
    BRANCH_MISMATCH = "branch_mismatch"
    BASE_MISMATCH = "base_mismatch"


@dataclass(frozen=True, slots=True)
class ManagedWorktree:
    session_id: str
    work_id: str
    repository_identity: str
    graph_version: int
    graph_fingerprint: str
    base_commit: str
    branch: str
    worktree_path: str
    created_at: str
    updated_at: str
    state: ManagedWorktreeState = ManagedWorktreeState.PROVISIONED
    worker_id: str | None = None
    intent_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SWARM_MANAGED_WORKTREE_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SWARM_MANAGED_WORKTREE_PROTOCOL:
            raise ValueError(f"unsupported managed-worktree protocol {self.protocol!r}")
        session_id = _clean(self.session_id, field_name="session_id")
        work_id = _clean(self.work_id, field_name="work_id")
        if not _SAFE_ID_RE.fullmatch(session_id):
            raise ValueError("managed worktree session_id is not safe")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", work_id):
            raise ValueError("managed worktree work_id is not safe")
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "work_id", work_id)
        identity = _clean(
            self.repository_identity, field_name="repository_identity"
        ).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ValueError("repository_identity must be a SHA-256 digest")
        object.__setattr__(self, "repository_identity", identity)
        if self.graph_version <= 0:
            raise ValueError("graph_version must be positive")
        fingerprint = _clean(
            self.graph_fingerprint, field_name="graph_fingerprint"
        ).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("graph_fingerprint must be a SHA-256 digest")
        object.__setattr__(self, "graph_fingerprint", fingerprint)
        base_commit = _clean(self.base_commit, field_name="base_commit").lower()
        if not re.fullmatch(r"[0-9a-f]{40,64}", base_commit):
            raise ValueError("base_commit must be a full Git object id")
        object.__setattr__(self, "base_commit", base_commit)
        branch = _clean(self.branch, field_name="branch")
        if not branch.startswith("claim-plane/swarm/"):
            raise ValueError("managed worktree branch is outside Claim Plane namespace")
        object.__setattr__(self, "branch", branch)
        path = Path(_clean(self.worktree_path, field_name="worktree_path"))
        if not path.is_absolute():
            raise ValueError("managed worktree path must be absolute")
        object.__setattr__(self, "worktree_path", str(path.resolve()))
        object.__setattr__(
            self, "created_at", _clean(self.created_at, field_name="created_at")
        )
        object.__setattr__(
            self, "updated_at", _clean(self.updated_at, field_name="updated_at")
        )
        object.__setattr__(self, "state", ManagedWorktreeState(self.state))
        if self.worker_id is not None:
            object.__setattr__(
                self, "worker_id", _clean(self.worker_id, field_name="worker_id")
            )
        if self.intent_id is not None:
            object.__setattr__(
                self, "intent_id", _clean(self.intent_id, field_name="intent_id")
            )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "session_id": self.session_id,
            "work_id": self.work_id,
            "repository_identity": self.repository_identity,
            "graph_version": self.graph_version,
            "graph_fingerprint": self.graph_fingerprint,
            "base_commit": self.base_commit,
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "state": self.state.value,
            "worker_id": self.worker_id,
            "intent_id": self.intent_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    def fingerprint(self) -> str:
        raw = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ManagedWorktree":
        return cls(
            protocol=str(data.get("protocol") or SWARM_MANAGED_WORKTREE_PROTOCOL),
            session_id=str(data.get("session_id") or ""),
            work_id=str(data.get("work_id") or ""),
            repository_identity=str(data.get("repository_identity") or ""),
            graph_version=int(data.get("graph_version") or 0),
            graph_fingerprint=str(data.get("graph_fingerprint") or ""),
            base_commit=str(data.get("base_commit") or ""),
            branch=str(data.get("branch") or ""),
            worktree_path=str(data.get("worktree_path") or ""),
            state=ManagedWorktreeState(data.get("state") or "provisioned"),
            worker_id=(
                None if data.get("worker_id") is None else str(data.get("worker_id"))
            ),
            intent_id=(
                None if data.get("intent_id") is None else str(data.get("intent_id"))
            ),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class WorktreeInspection:
    record: ManagedWorktree
    health: WorktreeHealth
    exists: bool
    registered: bool
    dirty: bool
    head_commit: str | None
    actual_branch: str | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "health": self.health.value,
            "exists": self.exists,
            "registered": self.registered,
            "dirty": self.dirty,
            "head_commit": self.head_commit,
            "actual_branch": self.actual_branch,
            "detail": self.detail,
        }
