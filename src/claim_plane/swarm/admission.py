"""Shared admission for all work items in one pinned swarm session.

The planner proposes a work graph.  The adaptive concurrency controller adds
serialization edges.  Shared admission then derives one immutable ChangeIntent
per work item and evaluates every pair that may be active concurrently.  It
never launches workers and never treats a planner proposal as mutation authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping

from claim_plane.coordination.admission import AdmissionEngine
from claim_plane.core import AdmissionKind, ChangeIntent, ResourceKind
from claim_plane.swarm.concurrency import (
    ConcurrencyConstraintAction,
    ConcurrencyPlan,
    ConcurrencyPlanStatus,
)
from claim_plane.swarm.models import SwarmSession, WorkGraph, WorkItem

SWARM_SHARED_ADMISSION_PROTOCOL = "claim-plane.swarm-shared-admission.v1"


class SharedAdmissionStatus(str, Enum):
    READY = "ready"
    REPLAN_REQUIRED = "replan_required"


def _fingerprint(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _intent_id(session_id: str, work_id: str, graph_fingerprint: str) -> str:
    digest = hashlib.sha256(
        f"{session_id}\0{work_id}\0{graph_fingerprint}".encode("utf-8")
    ).hexdigest()[:20]
    return f"swarm-{digest}-{work_id}"


def effective_dependencies(
    graph: WorkGraph, plan: ConcurrencyPlan
) -> dict[str, tuple[str, ...]]:
    """Return explicit DAG edges plus deterministic serialization edges."""

    dependencies = {item.work_id: set(item.depends_on) for item in graph.work_items}
    for constraint in plan.constraints:
        if constraint.action is ConcurrencyConstraintAction.SERIALIZE:
            dependencies[constraint.after].add(constraint.before)
    return {
        work_id: tuple(sorted(values))
        for work_id, values in sorted(dependencies.items())
    }


def _topological_order(dependencies: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    pending = {work_id: set(values) for work_id, values in dependencies.items()}
    order: list[str] = []
    while pending:
        ready = sorted(work_id for work_id, values in pending.items() if not values)
        if not ready:
            raise ValueError("shared admission dependencies contain a cycle")
        for work_id in ready:
            order.append(work_id)
            pending.pop(work_id)
        for values in pending.values():
            values.difference_update(ready)
    return tuple(order)


def _ancestor_map(
    dependencies: Mapping[str, tuple[str, ...]], order: tuple[str, ...]
) -> dict[str, set[str]]:
    ancestors: dict[str, set[str]] = {}
    for work_id in order:
        current = set(dependencies[work_id])
        for dependency in dependencies[work_id]:
            current.update(ancestors[dependency])
        ancestors[work_id] = current
    return ancestors


def _intent_for(
    session: SwarmSession,
    item: WorkItem,
    dependencies: Mapping[str, tuple[str, ...]],
    intent_ids: Mapping[str, str],
) -> ChangeIntent:
    return ChangeIntent(
        intent_id=intent_ids[item.work_id],
        task_id=f"{session.session_id}:{item.work_id}",
        owner=f"swarm/{session.session_id}/{item.work_id}",
        base_revision=session.base_commit,
        base_commit=session.base_commit,
        operations=item.operations,
        preserves=item.preserves,
        acceptance=item.acceptance,
        dependencies=tuple(intent_ids[value] for value in dependencies[item.work_id]),
        metadata={
            "swarm_session_id": session.session_id,
            "work_id": item.work_id,
            "graph_version": session.graph_version,
            "graph_fingerprint": session.graph_fingerprint,
            "source": "swarm_work_graph",
        },
    )


@dataclass(frozen=True, slots=True)
class WorkAdmission:
    work_id: str
    intent: ChangeIntent
    allowed: bool
    kind: AdmissionKind
    effective_dependencies: tuple[str, ...]
    conflicts: tuple[Mapping[str, Any], ...] = ()
    constraints: tuple[str, ...] = ()
    notifications: tuple[str, ...] = ()
    guidance: str = ""

    def __post_init__(self) -> None:
        if not self.work_id:
            raise ValueError("work_id must not be empty")
        if not isinstance(self.intent, ChangeIntent):
            object.__setattr__(self, "intent", ChangeIntent.from_dict(self.intent))
        object.__setattr__(self, "kind", AdmissionKind(self.kind))
        object.__setattr__(
            self,
            "effective_dependencies",
            tuple(sorted(set(self.effective_dependencies))),
        )
        object.__setattr__(
            self,
            "conflicts",
            tuple(dict(item) for item in self.conflicts),
        )
        object.__setattr__(self, "constraints", tuple(dict.fromkeys(self.constraints)))
        object.__setattr__(
            self,
            "notifications",
            tuple(dict.fromkeys(self.notifications)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "intent": self.intent.to_dict(),
            "allowed": self.allowed,
            "kind": self.kind.value,
            "effective_dependencies": list(self.effective_dependencies),
            "conflicts": [dict(item) for item in self.conflicts],
            "constraints": list(self.constraints),
            "notifications": list(self.notifications),
            "guidance": self.guidance,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkAdmission":
        return cls(
            work_id=str(data.get("work_id") or ""),
            intent=ChangeIntent.from_dict(data.get("intent") or {}),
            allowed=bool(data.get("allowed")),
            kind=AdmissionKind(data.get("kind") or AdmissionKind.REJECT.value),
            effective_dependencies=tuple(
                str(item) for item in data.get("effective_dependencies") or ()
            ),
            conflicts=tuple(
                dict(item)
                for item in data.get("conflicts") or ()
                if isinstance(item, Mapping)
            ),
            constraints=tuple(str(item) for item in data.get("constraints") or ()),
            notifications=tuple(str(item) for item in data.get("notifications") or ()),
            guidance=str(data.get("guidance") or ""),
        )


@dataclass(frozen=True, slots=True)
class SharedAdmissionPlan:
    session_id: str
    repository_identity: str
    graph_version: int
    graph_fingerprint: str
    budget_version: int
    budget_fingerprint: str
    concurrency_plan_fingerprint: str
    status: SharedAdmissionStatus
    admissions: tuple[WorkAdmission, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SWARM_SHARED_ADMISSION_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SWARM_SHARED_ADMISSION_PROTOCOL:
            raise ValueError(f"unsupported shared-admission protocol {self.protocol!r}")
        if not self.session_id:
            raise ValueError("session_id must not be empty")
        if self.graph_version <= 0 or self.budget_version <= 0:
            raise ValueError("source versions must be positive")
        for name in (
            "repository_identity",
            "graph_fingerprint",
            "budget_fingerprint",
            "concurrency_plan_fingerprint",
        ):
            value = str(getattr(self, name)).lower()
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise ValueError(f"{name} must be a SHA-256 digest")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "status", SharedAdmissionStatus(self.status))
        admissions = tuple(sorted(self.admissions, key=lambda item: item.work_id))
        if not admissions:
            raise ValueError("shared admission must contain at least one work item")
        if len({item.work_id for item in admissions}) != len(admissions):
            raise ValueError("shared admission work_ids must be unique")
        if self.status is SharedAdmissionStatus.READY and not all(
            item.allowed for item in admissions
        ):
            raise ValueError("ready shared admission cannot contain blocked work")
        if self.status is SharedAdmissionStatus.REPLAN_REQUIRED and all(
            item.allowed for item in admissions
        ):
            raise ValueError("replan_required requires at least one blocked admission")
        object.__setattr__(self, "admissions", admissions)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def admission_map(self) -> dict[str, WorkAdmission]:
        return {item.work_id: item for item in self.admissions}

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "session_id": self.session_id,
            "repository_identity": self.repository_identity,
            "graph_version": self.graph_version,
            "graph_fingerprint": self.graph_fingerprint,
            "budget_version": self.budget_version,
            "budget_fingerprint": self.budget_fingerprint,
            "concurrency_plan_fingerprint": self.concurrency_plan_fingerprint,
            "status": self.status.value,
            "admissions": [item.to_dict() for item in self.admissions],
            "metadata": dict(self.metadata),
        }

    def fingerprint(self) -> str:
        return _fingerprint(self.to_dict())

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "work_items": len(self.admissions),
            "admitted": sum(1 for item in self.admissions if item.allowed),
            "blocked": sum(1 for item in self.admissions if not item.allowed),
            "kinds": {
                kind.value: sum(1 for item in self.admissions if item.kind is kind)
                for kind in AdmissionKind
                if any(item.kind is kind for item in self.admissions)
            },
            "fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SharedAdmissionPlan":
        return cls(
            protocol=str(data.get("protocol") or SWARM_SHARED_ADMISSION_PROTOCOL),
            session_id=str(data.get("session_id") or ""),
            repository_identity=str(data.get("repository_identity") or ""),
            graph_version=int(data.get("graph_version") or 0),
            graph_fingerprint=str(data.get("graph_fingerprint") or ""),
            budget_version=int(data.get("budget_version") or 0),
            budget_fingerprint=str(data.get("budget_fingerprint") or ""),
            concurrency_plan_fingerprint=str(
                data.get("concurrency_plan_fingerprint") or ""
            ),
            status=SharedAdmissionStatus(data.get("status") or "replan_required"),
            admissions=tuple(
                WorkAdmission.from_dict(item) for item in data.get("admissions") or ()
            ),
            metadata=dict(data.get("metadata") or {}),
        )


def _operation_path(operation: Any) -> str | None:
    resource = operation.resource
    if resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}:
        return resource.identifier.replace("\\", "/").removeprefix("./")
    value = (
        resource.metadata.get("path")
        or resource.metadata.get("file")
        or resource.metadata.get("source_path")
        or resource.metadata.get("repository_path")
    )
    if value is None:
        return None
    return str(value).replace("\\", "/").removeprefix("./")


def _semantic_conflict_projection(intent: ChangeIntent) -> ChangeIntent:
    """Drop redundant broad path writes from pairwise conflict analysis.

    Swarm workers still retain the original file mutation authority in their admitted
    intent.  The projection is used only by shared conflict analysis when the same
    path also has committed symbol/contract/schema authority.  Isolated worktrees
    prevent cross-worker filesystem races, while Deterministic Integration v2 checks
    the actual diff against the semantic authority before composition.
    """

    semantic_kinds = {ResourceKind.SYMBOL, ResourceKind.CONTRACT, ResourceKind.SCHEMA}
    semantic_paths = {
        path
        for operation in intent.operations
        if operation.committed
        and operation.mutating
        and operation.resource.kind in semantic_kinds
        and (path := _operation_path(operation)) is not None
    }
    if not semantic_paths:
        return intent
    projected = tuple(
        operation
        for operation in intent.operations
        if not (
            operation.committed
            and operation.mutating
            and operation.resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}
            and _operation_path(operation) in semantic_paths
        )
    )
    if projected == intent.operations:
        return intent
    return replace(
        intent,
        operations=projected,
        metadata={
            **dict(intent.metadata),
            "shared_conflict_projection": "semantic-authority-v2",
            "projected_file_paths": sorted(semantic_paths),
        },
    )


def compute_shared_admission(
    session: SwarmSession, plan: ConcurrencyPlan
) -> SharedAdmissionPlan:
    """Admit all potentially concurrent work against one shared authority graph."""

    if plan.status is not ConcurrencyPlanStatus.READY:
        raise ValueError("shared admission requires a ready concurrency plan")
    if (
        plan.graph_version != session.graph_version
        or plan.graph_fingerprint != session.graph_fingerprint
        or plan.budget_version != session.budget_version
        or plan.budget_fingerprint != session.budget_fingerprint
    ):
        raise ValueError("concurrency plan is stale for the swarm session")

    dependencies = effective_dependencies(session.work_graph, plan)
    order = _topological_order(dependencies)
    ancestors = _ancestor_map(dependencies, order)
    intent_ids = {
        work_id: _intent_id(session.session_id, work_id, session.graph_fingerprint)
        for work_id in order
    }
    intents = {
        work_id: _intent_for(
            session,
            session.work_graph.item_map[work_id],
            dependencies,
            intent_ids,
        )
        for work_id in order
    }
    engine = AdmissionEngine()
    known_intent_ids = tuple(intent_ids.values())
    admitted_by_work: dict[str, ChangeIntent] = {}
    records: list[WorkAdmission] = []
    for work_id in order:
        potentially_concurrent = [
            _semantic_conflict_projection(admitted_by_work[other])
            for other in order
            if other in admitted_by_work and other not in ancestors[work_id]
        ]
        decision = engine.evaluate(
            _semantic_conflict_projection(intents[work_id]),
            potentially_concurrent,
            known_intent_ids=known_intent_ids,
        )
        record = WorkAdmission(
            work_id=work_id,
            intent=intents[work_id],
            allowed=decision.allowed,
            kind=decision.kind,
            effective_dependencies=dependencies[work_id],
            conflicts=tuple(item.to_dict() for item in decision.conflicts),
            constraints=decision.constraints,
            notifications=decision.notifications,
            guidance=decision.guidance,
        )
        records.append(record)
        if decision.allowed:
            admitted_by_work[work_id] = decision.intent
    status = (
        SharedAdmissionStatus.READY
        if all(item.allowed for item in records)
        else SharedAdmissionStatus.REPLAN_REQUIRED
    )
    return SharedAdmissionPlan(
        session_id=session.session_id,
        repository_identity=session.repository_identity,
        graph_version=session.graph_version,
        graph_fingerprint=session.graph_fingerprint,
        budget_version=session.budget_version,
        budget_fingerprint=session.budget_fingerprint,
        concurrency_plan_fingerprint=plan.fingerprint(),
        status=status,
        admissions=tuple(records),
        metadata={
            "engine": (
                "claim-plane-graph-aware-admission-v1"
                if plan.metadata.get("semantic_graph_fingerprint") is not None
                else "claim-plane-admission-v1"
            ),
            "dependency_model": "work-graph-plus-graph-derived-serialization-edges",
            "candidate_blocking": plan.metadata.get("candidate_blocking"),
            "semantic_graph_fingerprint": plan.metadata.get("semantic_graph_fingerprint"),
            "semantic_graph_revision": plan.metadata.get("semantic_graph_revision"),
            "semantic_graph_workspace_fingerprint": plan.metadata.get(
                "semantic_graph_workspace_fingerprint"
            ),
            "semantic_graph_refresh_mode": plan.metadata.get(
                "semantic_graph_refresh_mode"
            ),
            "semantic_graph_invalidation_fingerprint": plan.metadata.get(
                "semantic_graph_invalidation_fingerprint"
            ),
            "semantic_pairs_pruned_before_classifier": plan.metadata.get(
                "semantic_pairs_pruned_before_classifier", 0
            ),
            "contingent_scope": "projected-as-read-until-amendment",
        },
    )
