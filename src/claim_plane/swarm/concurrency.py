"""Deterministic adaptive concurrency planning for swarm work graphs.

The planner proposes work items and dependencies.  This module computes the
maximum safe execution waves allowed by the pinned work graph and budget policy.
It never launches workers and never grants mutation authority.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import posixpath
from dataclasses import dataclass, field
from enum import Enum
from itertools import combinations
from typing import Any, Iterable, Mapping

from claim_plane.coordination.admission import parse_line_region
from claim_plane.core import IntentOperation, ResourceKind
from claim_plane.swarm.budget import (
    ConflictPolicy,
    SameFilePolicy,
    SwarmBudgetPolicy,
)
from claim_plane.swarm.models import WorkGraph, WorkItem

SWARM_CONCURRENCY_PLAN_PROTOCOL = "claim-plane.swarm-concurrency-plan.v1"


class ConcurrencyPlanStatus(str, Enum):
    READY = "ready"
    REPLAN_REQUIRED = "replan_required"


class ConcurrencyConstraintAction(str, Enum):
    SERIALIZE = "serialize"
    DENY = "deny"


class ConcurrencyConstraintReason(str, Enum):
    SAME_FILE = "same_file"
    UNKNOWN_OVERLAP = "unknown_overlap"
    SHARED_CONTRACT = "shared_contract"
    SCHEMA_CHANGE = "schema_change"


@dataclass(frozen=True, slots=True)
class ConcurrencyConstraint:
    """A deterministic pairwise restriction added by the controller."""

    before: str
    after: str
    action: ConcurrencyConstraintAction
    reasons: tuple[ConcurrencyConstraintReason, ...]
    resources: tuple[str, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.before or not self.after or self.before == self.after:
            raise ValueError("concurrency constraint requires two distinct work items")
        object.__setattr__(self, "action", ConcurrencyConstraintAction(self.action))
        reasons = tuple(
            sorted(
                {ConcurrencyConstraintReason(item) for item in self.reasons},
                key=lambda item: item.value,
            )
        )
        if not reasons:
            raise ValueError("concurrency constraint requires at least one reason")
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(self, "resources", tuple(sorted(set(self.resources))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "before": self.before,
            "after": self.after,
            "action": self.action.value,
            "reasons": [reason.value for reason in self.reasons],
            "resources": list(self.resources),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ConcurrencyConstraint":
        return cls(
            before=str(data.get("before") or ""),
            after=str(data.get("after") or ""),
            action=ConcurrencyConstraintAction(data.get("action") or "serialize"),
            reasons=tuple(
                ConcurrencyConstraintReason(item) for item in data.get("reasons") or ()
            ),
            resources=tuple(str(item) for item in data.get("resources") or ()),
            detail=str(data.get("detail") or ""),
        )


@dataclass(frozen=True, slots=True)
class ExecutionWave:
    index: int
    work_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.index <= 0:
            raise ValueError("execution wave index must be positive")
        if not self.work_ids:
            raise ValueError("execution wave must contain at least one work item")
        if len(set(self.work_ids)) != len(self.work_ids):
            raise ValueError("execution wave work_ids must be unique")

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "work_ids": list(self.work_ids)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutionWave":
        return cls(
            index=int(data.get("index") or 0),
            work_ids=tuple(str(item) for item in data.get("work_ids") or ()),
        )


@dataclass(frozen=True, slots=True)
class ConcurrencyPlan:
    """A source-bound, deterministic execution-wave proposal."""

    graph_version: int
    graph_fingerprint: str
    budget_version: int
    budget_fingerprint: str
    max_active_workers: int
    work_item_count: int
    status: ConcurrencyPlanStatus
    waves: tuple[ExecutionWave, ...]
    constraints: tuple[ConcurrencyConstraint, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SWARM_CONCURRENCY_PLAN_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SWARM_CONCURRENCY_PLAN_PROTOCOL:
            raise ValueError(f"unsupported concurrency-plan protocol {self.protocol!r}")
        if self.graph_version <= 0 or self.budget_version <= 0:
            raise ValueError("concurrency plan source versions must be positive")
        for name, value in (
            ("graph_fingerprint", self.graph_fingerprint),
            ("budget_fingerprint", self.budget_fingerprint),
        ):
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if self.max_active_workers <= 0:
            raise ValueError("max_active_workers must be positive")
        if self.work_item_count <= 0:
            raise ValueError("work_item_count must be positive")
        object.__setattr__(self, "status", ConcurrencyPlanStatus(self.status))
        waves = tuple(sorted(self.waves, key=lambda item: item.index))
        if waves and tuple(wave.index for wave in waves) != tuple(
            range(1, len(waves) + 1)
        ):
            raise ValueError("execution wave indexes must be contiguous from 1")
        scheduled = [work_id for wave in waves for work_id in wave.work_ids]
        if len(set(scheduled)) != len(scheduled):
            raise ValueError("a work item cannot appear in more than one wave")
        if any(len(wave.work_ids) > self.max_active_workers for wave in waves):
            raise ValueError("execution wave exceeds max_active_workers")
        constraints = tuple(
            sorted(
                self.constraints,
                key=lambda item: (
                    item.before,
                    item.after,
                    item.action.value,
                    tuple(reason.value for reason in item.reasons),
                ),
            )
        )
        denied = any(
            item.action is ConcurrencyConstraintAction.DENY for item in constraints
        )
        if denied and self.status is not ConcurrencyPlanStatus.REPLAN_REQUIRED:
            raise ValueError("denied constraints require replan_required status")
        if self.status is ConcurrencyPlanStatus.REPLAN_REQUIRED and waves:
            raise ValueError("replan-required plans cannot contain execution waves")
        if (
            self.status is ConcurrencyPlanStatus.READY
            and len(scheduled) != self.work_item_count
        ):
            raise ValueError("ready plan must schedule every work item exactly once")
        object.__setattr__(self, "waves", waves)
        object.__setattr__(self, "constraints", constraints)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def serialized_constraints(self) -> tuple[ConcurrencyConstraint, ...]:
        return tuple(
            item
            for item in self.constraints
            if item.action is ConcurrencyConstraintAction.SERIALIZE
        )

    @property
    def denied_constraints(self) -> tuple[ConcurrencyConstraint, ...]:
        return tuple(
            item
            for item in self.constraints
            if item.action is ConcurrencyConstraintAction.DENY
        )

    @property
    def peak_concurrency(self) -> int:
        return max((len(wave.work_ids) for wave in self.waves), default=0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "graph_version": self.graph_version,
            "graph_fingerprint": self.graph_fingerprint,
            "budget_version": self.budget_version,
            "budget_fingerprint": self.budget_fingerprint,
            "max_active_workers": self.max_active_workers,
            "work_item_count": self.work_item_count,
            "status": self.status.value,
            "waves": [wave.to_dict() for wave in self.waves],
            "constraints": [item.to_dict() for item in self.constraints],
            "metadata": dict(self.metadata),
        }

    def fingerprint(self) -> str:
        raw = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "work_items": self.work_item_count,
            "wave_count": len(self.waves),
            "peak_concurrency": self.peak_concurrency,
            "max_active_workers": self.max_active_workers,
            "serialized_pairs": len(self.serialized_constraints),
            "denied_pairs": len(self.denied_constraints),
            "waves": [list(wave.work_ids) for wave in self.waves],
            "fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ConcurrencyPlan":
        return cls(
            protocol=str(data.get("protocol") or SWARM_CONCURRENCY_PLAN_PROTOCOL),
            graph_version=int(data.get("graph_version") or 0),
            graph_fingerprint=str(data.get("graph_fingerprint") or ""),
            budget_version=int(data.get("budget_version") or 0),
            budget_fingerprint=str(data.get("budget_fingerprint") or ""),
            max_active_workers=int(data.get("max_active_workers") or 0),
            work_item_count=int(data.get("work_item_count") or 0),
            status=ConcurrencyPlanStatus(data.get("status") or "ready"),
            waves=tuple(
                ExecutionWave.from_dict(item) for item in data.get("waves") or ()
            ),
            constraints=tuple(
                ConcurrencyConstraint.from_dict(item)
                for item in data.get("constraints") or ()
            ),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class _Finding:
    reason: ConcurrencyConstraintReason
    action: ConcurrencyConstraintAction
    resources: tuple[str, ...]
    detail: str


def _committed_operations(item: WorkItem) -> tuple[IntentOperation, ...]:
    return tuple(operation for operation in item.operations if operation.committed)


def _mutating_operations(item: WorkItem) -> tuple[IntentOperation, ...]:
    return tuple(
        operation for operation in _committed_operations(item) if operation.mutating
    )


def _normal_path(value: str) -> str:
    value = value.replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    if any(ch in value for ch in "*?["):
        return value.rstrip("/")
    normalized = posixpath.normpath(value)
    return "" if normalized == "." else normalized.rstrip("/")


def _glob_prefix(pattern: str) -> str:
    indexes = [pattern.find(ch) for ch in "*?[" if pattern.find(ch) >= 0]
    return pattern[: min(indexes)] if indexes else pattern


def _path_relation(left: IntentOperation, right: IntentOperation) -> str:
    left_path = _normal_path(left.resource.identifier)
    right_path = _normal_path(right.resource.identifier)
    left_pattern = any(ch in left_path for ch in "*?[")
    right_pattern = any(ch in right_path for ch in "*?[")
    if not left_pattern and not right_pattern:
        if left_path != right_path:
            return "none"
        left_region = parse_line_region(left.resource.region or "")
        right_region = parse_line_region(right.resource.region or "")
        if left_region is None or right_region is None:
            return "same_file_unknown_region"
        if left_region[1] < right_region[0] or right_region[1] < left_region[0]:
            return "same_file_disjoint_region"
        return "same_file_overlapping_region"
    if left_pattern and not right_pattern:
        return (
            "unknown_overlap" if fnmatch.fnmatchcase(right_path, left_path) else "none"
        )
    if right_pattern and not left_pattern:
        return (
            "unknown_overlap" if fnmatch.fnmatchcase(left_path, right_path) else "none"
        )
    left_prefix = _glob_prefix(left_path)
    right_prefix = _glob_prefix(right_path)
    if (
        left_prefix
        and right_prefix
        and not (
            left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix)
        )
    ):
        return "none"
    return "unknown_overlap"


def _conflict_action(policy: ConflictPolicy) -> ConcurrencyConstraintAction:
    if policy is ConflictPolicy.DENY:
        return ConcurrencyConstraintAction.DENY
    return ConcurrencyConstraintAction.SERIALIZE


def _same_file_finding(
    relation: str,
    policy: SwarmBudgetPolicy,
    resource: str,
) -> _Finding | None:
    same_file = policy.concurrency.same_file
    if relation == "same_file_disjoint_region":
        if same_file is SameFilePolicy.REGION_SAFE:
            return None
        action = (
            ConcurrencyConstraintAction.DENY
            if same_file is SameFilePolicy.DENY
            else ConcurrencyConstraintAction.SERIALIZE
        )
        return _Finding(
            ConcurrencyConstraintReason.SAME_FILE,
            action,
            (resource,),
            "same-file concurrency is disabled by policy",
        )
    if relation == "same_file_overlapping_region":
        action = (
            ConcurrencyConstraintAction.DENY
            if same_file is SameFilePolicy.DENY
            else ConcurrencyConstraintAction.SERIALIZE
        )
        return _Finding(
            ConcurrencyConstraintReason.SAME_FILE,
            action,
            (resource,),
            "declared line regions overlap",
        )
    if relation == "same_file_unknown_region":
        if same_file is SameFilePolicy.DENY:
            action = ConcurrencyConstraintAction.DENY
            reason = ConcurrencyConstraintReason.SAME_FILE
            detail = "same-file concurrency is denied by policy"
        elif same_file is SameFilePolicy.SERIALIZE:
            action = ConcurrencyConstraintAction.SERIALIZE
            reason = ConcurrencyConstraintReason.SAME_FILE
            detail = "same-file work is serialized by policy"
        else:
            action = _conflict_action(policy.concurrency.unknown_overlap)
            reason = ConcurrencyConstraintReason.UNKNOWN_OVERLAP
            detail = "same-file regions are missing or cannot be proven disjoint"
        return _Finding(reason, action, (resource,), detail)
    if relation == "unknown_overlap":
        return _Finding(
            ConcurrencyConstraintReason.UNKNOWN_OVERLAP,
            _conflict_action(policy.concurrency.unknown_overlap),
            (resource,),
            "path patterns overlap or cannot be proven disjoint",
        )
    return None


def _schema_change(item: WorkItem) -> bool:
    if item.metadata.get("schema_change") is True:
        return True
    return any(
        operation.mutating
        and (
            operation.resource.kind is ResourceKind.SCHEMA
            or operation.metadata.get("schema_change") is True
            or operation.resource.metadata.get("schema_change") is True
        )
        for operation in _committed_operations(item)
    )


def _contract_keys(item: WorkItem) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for operation in _committed_operations(item):
        resource = operation.resource
        if resource.kind is not ResourceKind.CONTRACT:
            continue
        keys.add((resource.subject_key or "", resource.semantic_key))
    return keys


def _semantic_keys(item: WorkItem) -> set[str]:
    return {
        operation.resource.semantic_key
        for operation in _committed_operations(item)
        if operation.resource.kind
        in {
            ResourceKind.SYMBOL,
            ResourceKind.CONCEPT,
            ResourceKind.CONFIG,
            ResourceKind.ROUTE,
            ResourceKind.SCHEMA,
        }
    }


def _shared_contract_resources(left: WorkItem, right: WorkItem) -> tuple[str, ...]:
    left_contracts = _contract_keys(left)
    right_contracts = _contract_keys(right)
    shared = left_contracts & right_contracts
    resources = {f"contract:{subject}:{contract}" for subject, contract in shared}
    left_semantic = _semantic_keys(left)
    right_semantic = _semantic_keys(right)
    for subject, contract in left_contracts:
        if subject and subject in right_semantic:
            resources.add(f"contract:{subject}:{contract}")
    for subject, contract in right_contracts:
        if subject and subject in left_semantic:
            resources.add(f"contract:{subject}:{contract}")
    return tuple(sorted(resources))


def _operation_findings(
    left: WorkItem,
    right: WorkItem,
    policy: SwarmBudgetPolicy,
) -> list[_Finding]:
    findings: list[_Finding] = []
    path_kinds = {ResourceKind.FILE, ResourceKind.DOCUMENT}
    semantic_kinds = {
        ResourceKind.SYMBOL,
        ResourceKind.CONCEPT,
        ResourceKind.CONFIG,
        ResourceKind.ROUTE,
        ResourceKind.SCHEMA,
    }
    for left_op in _committed_operations(left):
        for right_op in _committed_operations(right):
            if not (left_op.mutating or right_op.mutating):
                continue
            left_resource = left_op.resource
            right_resource = right_op.resource
            if left_resource.kind in path_kinds and right_resource.kind in path_kinds:
                relation = _path_relation(left_op, right_op)
                resource = _normal_path(left_resource.identifier)
                finding = _same_file_finding(relation, policy, resource)
                if finding is not None:
                    findings.append(finding)
                continue
            if (
                left_resource.kind in semantic_kinds
                and right_resource.kind in semantic_kinds
                and left_resource.semantic_key == right_resource.semantic_key
            ):
                findings.append(
                    _Finding(
                        ConcurrencyConstraintReason.UNKNOWN_OVERLAP,
                        _conflict_action(policy.concurrency.unknown_overlap),
                        (f"{left_resource.kind.value}:{left_resource.semantic_key}",),
                        "semantic resources overlap",
                    )
                )
    return findings


def _ancestor_map(graph: WorkGraph) -> dict[str, set[str]]:
    ancestors: dict[str, set[str]] = {}
    for work_id in graph.topological_order():
        item = graph.item_map[work_id]
        current = set(item.depends_on)
        for dependency in item.depends_on:
            current.update(ancestors[dependency])
        ancestors[work_id] = current
    return ancestors


def _pair_constraint(
    left: WorkItem,
    right: WorkItem,
    policy: SwarmBudgetPolicy,
) -> ConcurrencyConstraint | None:
    findings: list[_Finding] = []
    if _schema_change(left) or _schema_change(right):
        if _mutating_operations(left) and _mutating_operations(right):
            findings.append(
                _Finding(
                    ConcurrencyConstraintReason.SCHEMA_CHANGE,
                    _conflict_action(policy.concurrency.schema_change),
                    ("schema-change",),
                    "schema-changing work is isolated from concurrent mutations",
                )
            )
    contracts = _shared_contract_resources(left, right)
    if contracts:
        findings.append(
            _Finding(
                ConcurrencyConstraintReason.SHARED_CONTRACT,
                _conflict_action(policy.concurrency.shared_contract),
                contracts,
                "work items share a contract or its bound subject",
            )
        )
    findings.extend(_operation_findings(left, right, policy))
    if not findings:
        return None
    action = (
        ConcurrencyConstraintAction.DENY
        if any(item.action is ConcurrencyConstraintAction.DENY for item in findings)
        else ConcurrencyConstraintAction.SERIALIZE
    )
    reasons = tuple(item.reason for item in findings)
    resources = tuple(resource for item in findings for resource in item.resources)
    details = "; ".join(sorted({item.detail for item in findings}))
    return ConcurrencyConstraint(
        before=left.work_id,
        after=right.work_id,
        action=action,
        reasons=reasons,
        resources=resources,
        detail=details,
    )


def _execution_waves(
    graph: WorkGraph,
    constraints: Iterable[ConcurrencyConstraint],
    *,
    max_active: int,
) -> tuple[ExecutionWave, ...]:
    order = graph.topological_order()
    rank = {work_id: index for index, work_id in enumerate(order)}
    dependencies = {item.work_id: set(item.depends_on) for item in graph.work_items}
    for constraint in constraints:
        if constraint.action is ConcurrencyConstraintAction.SERIALIZE:
            dependencies[constraint.after].add(constraint.before)
    remaining = set(order)
    completed: set[str] = set()
    waves: list[ExecutionWave] = []
    while remaining:
        ready = sorted(
            (
                work_id
                for work_id in remaining
                if dependencies[work_id].issubset(completed)
            ),
            key=rank.__getitem__,
        )
        if not ready:
            raise ValueError("adaptive concurrency constraints created a cycle")
        selected = tuple(ready[:max_active])
        waves.append(ExecutionWave(index=len(waves) + 1, work_ids=selected))
        completed.update(selected)
        remaining.difference_update(selected)
    return tuple(waves)


def compute_concurrency_plan(
    graph: WorkGraph,
    policy: SwarmBudgetPolicy,
    *,
    graph_version: int = 1,
    budget_version: int = 1,
) -> ConcurrencyPlan:
    """Compute deterministic safe waves without launching or admitting workers."""

    policy.validate_work_item_count(len(graph.work_items))
    order = graph.topological_order()
    rank = {work_id: index for index, work_id in enumerate(order)}
    ancestors = _ancestor_map(graph)
    constraints: list[ConcurrencyConstraint] = []
    for left_id, right_id in combinations(order, 2):
        if left_id in ancestors[right_id] or right_id in ancestors[left_id]:
            continue
        left_id, right_id = sorted((left_id, right_id), key=rank.__getitem__)
        constraint = _pair_constraint(
            graph.item_map[left_id], graph.item_map[right_id], policy
        )
        if constraint is not None:
            constraints.append(constraint)
    denied = any(
        item.action is ConcurrencyConstraintAction.DENY for item in constraints
    )
    status = (
        ConcurrencyPlanStatus.REPLAN_REQUIRED if denied else ConcurrencyPlanStatus.READY
    )
    waves = (
        ()
        if denied
        else _execution_waves(
            graph,
            constraints,
            max_active=policy.workers.max_active,
        )
    )
    return ConcurrencyPlan(
        graph_version=graph_version,
        graph_fingerprint=graph.fingerprint(),
        budget_version=budget_version,
        budget_fingerprint=policy.fingerprint(),
        max_active_workers=policy.workers.max_active,
        work_item_count=len(graph.work_items),
        status=status,
        waves=waves,
        constraints=tuple(constraints),
        metadata={
            "controller": "deterministic-pairwise-v1",
            "contingent_scope": "excluded_until_amendment",
        },
    )
