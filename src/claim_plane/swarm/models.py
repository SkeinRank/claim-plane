"""Durable swarm-session and work-graph protocol models.

The planner may propose decomposition, scope, and dependencies. Claim Plane owns
session identity, repository binding, the pinned Git base, graph versioning, and
all later execution authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping

from claim_plane.core import IntentOperation, ResourceKind
from claim_plane.swarm.budget import SwarmBudgetPolicy

SWARM_SESSION_PROTOCOL = "claim-plane.swarm-session.v1"
SWARM_SESSION_SPEC_PROTOCOL = "claim-plane.swarm-session-spec.v1"
SWARM_WORK_GRAPH_PROTOCOL = "claim-plane.swarm-work-graph.v1"

_WORK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_PROTECTED_PREFIXES = (".claim-plane", ".git", ".codex")


def _clean(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _unique_strings(values: Iterable[str], *, field_name: str) -> tuple[str, ...]:
    cleaned = tuple(_clean(value, field_name=field_name) for value in values)
    if len(set(cleaned)) != len(cleaned):
        raise ValueError(f"{field_name} entries must be unique")
    return cleaned


def _canonical_fingerprint(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_repository_path(value: str) -> None:
    path = value.replace("\\", "/").strip()
    while path.startswith("./"):
        path = path[2:]
    if not path or path.startswith("/"):
        raise ValueError("work-item repository paths must be relative")
    parts = tuple(part for part in path.split("/") if part not in {"", "."})
    if ".." in parts:
        raise ValueError(f"work-item path escapes the repository: {value}")
    lowered = "/".join(parts).casefold()
    if any(
        lowered == prefix or lowered.startswith(prefix + "/")
        for prefix in _PROTECTED_PREFIXES
    ):
        raise ValueError(
            "work-item scope cannot include Claim Plane or Git control state: "
            f"{value}"
        )


def _validate_repository_resource(operation: IntentOperation) -> None:
    resource = operation.resource
    if resource.kind in {
        ResourceKind.FILE,
        ResourceKind.DOCUMENT,
        ResourceKind.CONFIG,
    }:
        _validate_repository_path(resource.identifier)
    metadata_path = resource.metadata.get("path")
    if metadata_path is not None:
        _validate_repository_path(str(metadata_path))


class SwarmSessionState(str, Enum):
    """Lifecycle states reserved for the complete swarm runtime."""

    PLANNED = "planned"
    RUNNING = "running"
    PAUSED = "paused"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RootTask:
    title: str
    goal: str
    acceptance: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "title", _clean(self.title, field_name="root task title")
        )
        object.__setattr__(self, "goal", _clean(self.goal, field_name="root task goal"))
        object.__setattr__(
            self,
            "acceptance",
            _unique_strings(self.acceptance, field_name="root acceptance"),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "goal": self.goal,
            "acceptance": list(self.acceptance),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RootTask":
        return cls(
            title=str(data.get("title") or data.get("goal") or ""),
            goal=str(data.get("goal") or ""),
            acceptance=tuple(data.get("acceptance") or ()),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class IntegrationTarget:
    branch: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        branch = _clean(self.branch, field_name="integration target branch")
        if branch.startswith("-") or ".." in branch or branch.endswith("."):
            raise ValueError("integration target branch is not a safe Git ref name")
        object.__setattr__(self, "branch", branch)
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {"branch": self.branch, "metadata": dict(self.metadata)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "IntegrationTarget":
        return cls(
            branch=str(data.get("branch") or ""),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class WorkItem:
    work_id: str
    title: str
    goal: str
    operations: tuple[IntentOperation, ...]
    depends_on: tuple[str, ...] = ()
    preserves: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        work_id = _clean(self.work_id, field_name="work_id")
        if not _WORK_ID_RE.fullmatch(work_id):
            raise ValueError(
                "work_id must start with an alphanumeric character and contain only "
                "letters, digits, '.', '_', or '-' (maximum 64 characters)"
            )
        object.__setattr__(self, "work_id", work_id)
        object.__setattr__(
            self, "title", _clean(self.title, field_name="work item title")
        )
        object.__setattr__(self, "goal", _clean(self.goal, field_name="work item goal"))
        operations = tuple(
            (
                item
                if isinstance(item, IntentOperation)
                else IntentOperation.from_dict(item)
            )
            for item in self.operations
        )
        if not operations:
            raise ValueError(
                f"work item {work_id!r} must declare at least one operation"
            )
        for operation in operations:
            _validate_repository_resource(operation)
        object.__setattr__(self, "operations", operations)
        depends_on = _unique_strings(self.depends_on, field_name="work dependency")
        if work_id in depends_on:
            raise ValueError(f"work item {work_id!r} cannot depend on itself")
        object.__setattr__(self, "depends_on", depends_on)
        object.__setattr__(
            self, "preserves", _unique_strings(self.preserves, field_name="preserve")
        )
        object.__setattr__(
            self,
            "acceptance",
            _unique_strings(self.acceptance, field_name="work acceptance"),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "title": self.title,
            "goal": self.goal,
            "depends_on": list(self.depends_on),
            "operations": [operation.to_dict() for operation in self.operations],
            "preserves": list(self.preserves),
            "acceptance": list(self.acceptance),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkItem":
        operations = data.get("operations")
        if not isinstance(operations, list):
            raise ValueError("work item operations must be an array")
        return cls(
            work_id=str(data.get("work_id") or ""),
            title=str(data.get("title") or data.get("goal") or ""),
            goal=str(data.get("goal") or ""),
            depends_on=tuple(data.get("depends_on") or ()),
            operations=tuple(IntentOperation.from_dict(item) for item in operations),
            preserves=tuple(data.get("preserves") or ()),
            acceptance=tuple(data.get("acceptance") or ()),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class WorkGraph:
    work_items: tuple[WorkItem, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SWARM_WORK_GRAPH_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SWARM_WORK_GRAPH_PROTOCOL:
            raise ValueError(f"unsupported work-graph protocol {self.protocol!r}")
        items = tuple(
            item if isinstance(item, WorkItem) else WorkItem.from_dict(item)
            for item in self.work_items
        )
        if not items:
            raise ValueError("work graph must contain at least one work item")
        identifiers = [item.work_id for item in items]
        if len(set(identifiers)) != len(identifiers):
            duplicates = sorted(
                {item for item in identifiers if identifiers.count(item) > 1}
            )
            raise ValueError("duplicate work_id values: " + ", ".join(duplicates))
        known = set(identifiers)
        for item in items:
            missing = sorted(set(item.depends_on) - known)
            if missing:
                raise ValueError(
                    f"work item {item.work_id!r} references unknown dependencies: "
                    + ", ".join(missing)
                )
        object.__setattr__(
            self,
            "work_items",
            tuple(sorted(items, key=lambda item: item.work_id)),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))
        self.topological_order()

    @property
    def item_map(self) -> dict[str, WorkItem]:
        return {item.work_id: item for item in self.work_items}

    @property
    def roots(self) -> tuple[str, ...]:
        return tuple(item.work_id for item in self.work_items if not item.depends_on)

    @property
    def leaves(self) -> tuple[str, ...]:
        depended_on = {
            dependency for item in self.work_items for dependency in item.depends_on
        }
        return tuple(
            item.work_id
            for item in self.work_items
            if item.work_id not in depended_on
        )

    def topological_order(self) -> tuple[str, ...]:
        dependencies = {item.work_id: set(item.depends_on) for item in self.work_items}
        dependents: dict[str, set[str]] = {
            item.work_id: set() for item in self.work_items
        }
        for work_id, required in dependencies.items():
            for dependency in required:
                dependents[dependency].add(work_id)
        ready = sorted(
            work_id for work_id, required in dependencies.items() if not required
        )
        order: list[str] = []
        while ready:
            work_id = ready.pop(0)
            order.append(work_id)
            for dependent in sorted(dependents[work_id]):
                dependencies[dependent].discard(work_id)
                if (
                    not dependencies[dependent]
                    and dependent not in order
                    and dependent not in ready
                ):
                    ready.append(dependent)
                    ready.sort()
        if len(order) != len(self.work_items):
            remaining = sorted(
                work_id for work_id, required in dependencies.items() if required
            )
            raise ValueError(
                "work graph contains a dependency cycle involving: "
                + ", ".join(remaining)
            )
        return tuple(order)

    def dependency_layers(self) -> tuple[tuple[str, ...], ...]:
        layer_by_id: dict[str, int] = {}
        for work_id in self.topological_order():
            item = self.item_map[work_id]
            layer_by_id[work_id] = (
                0
                if not item.depends_on
                else max(layer_by_id[dependency] for dependency in item.depends_on) + 1
            )
        if not layer_by_id:
            return ()
        return tuple(
            tuple(
                sorted(
                    work_id
                    for work_id, layer in layer_by_id.items()
                    if layer == index
                )
            )
            for index in range(max(layer_by_id.values()) + 1)
        )

    def fingerprint(self) -> str:
        return _canonical_fingerprint(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "work_items": [item.to_dict() for item in self.work_items],
            "metadata": dict(self.metadata),
        }

    def summary(self) -> dict[str, Any]:
        edge_count = sum(len(item.depends_on) for item in self.work_items)
        return {
            "work_items": len(self.work_items),
            "dependency_edges": edge_count,
            "roots": list(self.roots),
            "leaves": list(self.leaves),
            "topological_order": list(self.topological_order()),
            "dependency_layers": [list(layer) for layer in self.dependency_layers()],
            "fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkGraph":
        protocol = str(data.get("protocol") or SWARM_WORK_GRAPH_PROTOCOL)
        work_items = data.get("work_items")
        if not isinstance(work_items, list):
            raise ValueError("work_graph.work_items must be an array")
        raw_ids = [
            str(item.get("work_id") or "")
            for item in work_items
            if isinstance(item, Mapping)
        ]
        duplicates = sorted(
            {work_id for work_id in raw_ids if raw_ids.count(work_id) > 1}
        )
        if duplicates:
            raise ValueError("duplicate work_id values: " + ", ".join(duplicates))
        return cls(
            protocol=protocol,
            work_items=tuple(WorkItem.from_dict(item) for item in work_items),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class SwarmSession:
    session_id: str
    repository_root: str
    repository_identity: str
    base_commit: str
    base_branch: str
    root_task: RootTask
    integration_target: IntegrationTarget
    work_graph: WorkGraph
    budget_policy: SwarmBudgetPolicy
    graph_version: int
    budget_version: int
    state: SwarmSessionState
    created_at: str
    updated_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SWARM_SESSION_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SWARM_SESSION_PROTOCOL:
            raise ValueError(f"unsupported swarm-session protocol {self.protocol!r}")
        object.__setattr__(
            self, "session_id", _clean(self.session_id, field_name="session_id")
        )
        object.__setattr__(
            self,
            "repository_root",
            _clean(self.repository_root, field_name="repository_root"),
        )
        object.__setattr__(
            self,
            "repository_identity",
            _clean(self.repository_identity, field_name="repository_identity"),
        )
        commit = _clean(self.base_commit, field_name="base_commit").lower()
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise ValueError("base_commit must be a full hexadecimal object id")
        object.__setattr__(self, "base_commit", commit)
        object.__setattr__(
            self,
            "base_branch",
            _clean(self.base_branch, field_name="base_branch"),
        )
        if not isinstance(self.root_task, RootTask):
            object.__setattr__(self, "root_task", RootTask.from_dict(self.root_task))
        if not isinstance(self.integration_target, IntegrationTarget):
            object.__setattr__(
                self,
                "integration_target",
                IntegrationTarget.from_dict(self.integration_target),
            )
        if not isinstance(self.work_graph, WorkGraph):
            object.__setattr__(self, "work_graph", WorkGraph.from_dict(self.work_graph))
        if not isinstance(self.budget_policy, SwarmBudgetPolicy):
            object.__setattr__(
                self,
                "budget_policy",
                SwarmBudgetPolicy.from_dict(self.budget_policy),
            )
        self.budget_policy.validate_work_item_count(len(self.work_graph.work_items))
        if self.graph_version <= 0:
            raise ValueError("graph_version must be positive")
        if self.budget_version <= 0:
            raise ValueError("budget_version must be positive")
        object.__setattr__(self, "state", SwarmSessionState(self.state))
        object.__setattr__(
            self, "created_at", _clean(self.created_at, field_name="created_at")
        )
        object.__setattr__(
            self, "updated_at", _clean(self.updated_at, field_name="updated_at")
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def graph_fingerprint(self) -> str:
        return self.work_graph.fingerprint()

    @property
    def budget_fingerprint(self) -> str:
        return self.budget_policy.fingerprint()

    def with_graph(self, graph: WorkGraph, *, updated_at: str) -> "SwarmSession":
        if self.state is not SwarmSessionState.PLANNED:
            raise ValueError(
                f"cannot replace work graph while session is {self.state.value}"
            )
        self.budget_policy.validate_work_item_count(len(graph.work_items))
        return replace(
            self,
            work_graph=graph,
            graph_version=self.graph_version + 1,
            updated_at=updated_at,
        )

    def with_budget_policy(
        self, policy: SwarmBudgetPolicy, *, updated_at: str
    ) -> "SwarmSession":
        if self.state is not SwarmSessionState.PLANNED:
            raise ValueError(
                f"cannot replace budget policy while session is {self.state.value}"
            )
        policy.validate_work_item_count(len(self.work_graph.work_items))
        return replace(
            self,
            budget_policy=policy,
            budget_version=self.budget_version + 1,
            updated_at=updated_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "session_id": self.session_id,
            "repository_root": self.repository_root,
            "repository_identity": self.repository_identity,
            "base_commit": self.base_commit,
            "base_branch": self.base_branch,
            "root_task": self.root_task.to_dict(),
            "integration_target": self.integration_target.to_dict(),
            "state": self.state.value,
            "graph_version": self.graph_version,
            "graph_fingerprint": self.graph_fingerprint,
            "work_graph": self.work_graph.to_dict(),
            "budget_version": self.budget_version,
            "budget_fingerprint": self.budget_fingerprint,
            "budget_policy": self.budget_policy.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SwarmSession":
        return cls(
            protocol=str(data.get("protocol") or SWARM_SESSION_PROTOCOL),
            session_id=str(data.get("session_id") or ""),
            repository_root=str(data.get("repository_root") or ""),
            repository_identity=str(data.get("repository_identity") or ""),
            base_commit=str(data.get("base_commit") or ""),
            base_branch=str(data.get("base_branch") or ""),
            root_task=RootTask.from_dict(data.get("root_task") or {}),
            integration_target=IntegrationTarget.from_dict(
                data.get("integration_target") or {}
            ),
            work_graph=WorkGraph.from_dict(data.get("work_graph") or {}),
            budget_policy=SwarmBudgetPolicy.from_dict(data.get("budget_policy")),
            graph_version=int(data.get("graph_version") or 0),
            budget_version=int(data.get("budget_version") or 1),
            state=SwarmSessionState(
                data.get("state") or SwarmSessionState.PLANNED.value
            ),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            metadata=dict(data.get("metadata") or {}),
        )
