"""Machine-checkable swarm budget and concurrency policy.

The planner may propose decomposition, but it cannot grant itself unbounded worker,
retry, token, cost, or wall-time capacity.  This module normalizes a conservative
policy into a deterministic protocol object that later schedulers and runners can
enforce without consulting the model.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping

SWARM_BUDGET_POLICY_PROTOCOL = "claim-plane.swarm-budget-policy.v1"


def _canonical_fingerprint(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return value


def _reject_unknown(
    data: Mapping[str, Any], *, allowed: set[str], field_name: str
) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(
            f"{field_name} contains unknown fields: " + ", ".join(unknown)
        )


def _bounded_int(
    value: Any,
    *,
    field_name: str,
    default: int,
    minimum: int = 0,
    maximum: int = 1_000_000_000,
) -> int:
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(
            f"{field_name} must be between {minimum} and {maximum}"
        )
    return value


def _optional_bounded_int(
    value: Any,
    *,
    field_name: str,
    default: int | None,
    minimum: int = 1,
    maximum: int = 10_000_000_000,
) -> int | None:
    if value is None:
        return default
    return _bounded_int(
        value,
        field_name=field_name,
        default=minimum,
        minimum=minimum,
        maximum=maximum,
    )


def _cost_usd(value: Any, *, default: str | None) -> str | None:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("resources.max_cost_usd must be a number or decimal string")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(
            "resources.max_cost_usd must be a number or decimal string"
        ) from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("resources.max_cost_usd must be finite and greater than zero")
    if amount > Decimal("1000000"):
        raise ValueError("resources.max_cost_usd must not exceed 1000000")
    quantum = Decimal("0.000001")
    rounded = amount.quantize(quantum)
    if rounded != amount:
        raise ValueError("resources.max_cost_usd supports at most 6 decimal places")
    text = format(rounded, "f").rstrip("0").rstrip(".")
    return text or "0"


class SameFilePolicy(str, Enum):
    REGION_SAFE = "region_safe"
    SERIALIZE = "serialize"
    DENY = "deny"


class ConflictPolicy(str, Enum):
    SERIALIZE = "serialize"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class WorkerBudget:
    """Hard worker and graph-size ceilings for one swarm session."""

    max_active: int = 4
    max_active_per_work_item: int = 1
    max_work_items: int = 32
    max_total_launches: int = 64

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("workers.max_active", self.max_active, 256),
            ("workers.max_active_per_work_item", self.max_active_per_work_item, 64),
            ("workers.max_work_items", self.max_work_items, 4096),
            ("workers.max_total_launches", self.max_total_launches, 65536),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            if value < 1 or value > maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
        if self.max_active_per_work_item > self.max_active:
            raise ValueError(
                "workers.max_active_per_work_item cannot exceed workers.max_active"
            )
        if self.max_total_launches < self.max_work_items:
            raise ValueError(
                "workers.max_total_launches cannot be smaller than "
                "workers.max_work_items"
            )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_active": self.max_active,
            "max_active_per_work_item": self.max_active_per_work_item,
            "max_work_items": self.max_work_items,
            "max_total_launches": self.max_total_launches,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "WorkerBudget":
        raw = _mapping(data, field_name="workers")
        _reject_unknown(
            raw,
            allowed={
                "max_active",
                "max_active_per_work_item",
                "max_work_items",
                "max_total_launches",
            },
            field_name="workers",
        )
        return cls(
            max_active=_bounded_int(
                raw.get("max_active"),
                field_name="workers.max_active",
                default=4,
                minimum=1,
                maximum=256,
            ),
            max_active_per_work_item=_bounded_int(
                raw.get("max_active_per_work_item"),
                field_name="workers.max_active_per_work_item",
                default=1,
                minimum=1,
                maximum=64,
            ),
            max_work_items=_bounded_int(
                raw.get("max_work_items"),
                field_name="workers.max_work_items",
                default=32,
                minimum=1,
                maximum=4096,
            ),
            max_total_launches=_bounded_int(
                raw.get("max_total_launches"),
                field_name="workers.max_total_launches",
                default=64,
                minimum=1,
                maximum=65536,
            ),
        )


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    """Session-wide metered resource ceilings."""

    max_total_tokens: int | None = 500_000
    max_cost_usd: str | None = "25"
    max_wall_time_seconds: int = 7_200

    def __post_init__(self) -> None:
        tokens = _optional_bounded_int(
            self.max_total_tokens,
            field_name="resources.max_total_tokens",
            default=None,
            minimum=1,
            maximum=10_000_000_000,
        )
        wall_time = _bounded_int(
            self.max_wall_time_seconds,
            field_name="resources.max_wall_time_seconds",
            default=7_200,
            minimum=1,
            maximum=31_536_000,
        )
        cost = _cost_usd(self.max_cost_usd, default=None)
        object.__setattr__(self, "max_total_tokens", tokens)
        object.__setattr__(self, "max_cost_usd", cost)
        object.__setattr__(self, "max_wall_time_seconds", wall_time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_total_tokens": self.max_total_tokens,
            "max_cost_usd": self.max_cost_usd,
            "max_wall_time_seconds": self.max_wall_time_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ResourceBudget":
        raw = _mapping(data, field_name="resources")
        _reject_unknown(
            raw,
            allowed={"max_total_tokens", "max_cost_usd", "max_wall_time_seconds"},
            field_name="resources",
        )
        return cls(
            max_total_tokens=_optional_bounded_int(
                raw.get("max_total_tokens"),
                field_name="resources.max_total_tokens",
                default=500_000,
                minimum=1,
                maximum=10_000_000_000,
            ),
            max_cost_usd=_cost_usd(raw.get("max_cost_usd"), default="25"),
            max_wall_time_seconds=_bounded_int(
                raw.get("max_wall_time_seconds"),
                field_name="resources.max_wall_time_seconds",
                default=7_200,
                minimum=1,
                maximum=31_536_000,
            ),
        )


@dataclass(frozen=True, slots=True)
class RetryBudget:
    max_replans: int = 2
    max_repairs_per_work_item: int = 2
    max_agent_restarts: int = 1

    def __post_init__(self) -> None:
        for name, value in (
            ("retries.max_replans", self.max_replans),
            ("retries.max_repairs_per_work_item", self.max_repairs_per_work_item),
            ("retries.max_agent_restarts", self.max_agent_restarts),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            if value < 0 or value > 100:
                raise ValueError(f"{name} must be between 0 and 100")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_replans": self.max_replans,
            "max_repairs_per_work_item": self.max_repairs_per_work_item,
            "max_agent_restarts": self.max_agent_restarts,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "RetryBudget":
        raw = _mapping(data, field_name="retries")
        _reject_unknown(
            raw,
            allowed={
                "max_replans",
                "max_repairs_per_work_item",
                "max_agent_restarts",
            },
            field_name="retries",
        )
        return cls(
            max_replans=_bounded_int(
                raw.get("max_replans"),
                field_name="retries.max_replans",
                default=2,
                minimum=0,
                maximum=100,
            ),
            max_repairs_per_work_item=_bounded_int(
                raw.get("max_repairs_per_work_item"),
                field_name="retries.max_repairs_per_work_item",
                default=2,
                minimum=0,
                maximum=100,
            ),
            max_agent_restarts=_bounded_int(
                raw.get("max_agent_restarts"),
                field_name="retries.max_agent_restarts",
                default=1,
                minimum=0,
                maximum=100,
            ),
        )


@dataclass(frozen=True, slots=True)
class ConcurrencyBudget:
    same_file: SameFilePolicy = SameFilePolicy.REGION_SAFE
    unknown_overlap: ConflictPolicy = ConflictPolicy.SERIALIZE
    shared_contract: ConflictPolicy = ConflictPolicy.SERIALIZE
    schema_change: ConflictPolicy = ConflictPolicy.SERIALIZE

    def __post_init__(self) -> None:
        object.__setattr__(self, "same_file", SameFilePolicy(self.same_file))
        object.__setattr__(
            self, "unknown_overlap", ConflictPolicy(self.unknown_overlap)
        )
        object.__setattr__(
            self, "shared_contract", ConflictPolicy(self.shared_contract)
        )
        object.__setattr__(self, "schema_change", ConflictPolicy(self.schema_change))

    def to_dict(self) -> dict[str, str]:
        return {
            "same_file": self.same_file.value,
            "unknown_overlap": self.unknown_overlap.value,
            "shared_contract": self.shared_contract.value,
            "schema_change": self.schema_change.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "ConcurrencyBudget":
        raw = _mapping(data, field_name="concurrency")
        _reject_unknown(
            raw,
            allowed={
                "same_file",
                "unknown_overlap",
                "shared_contract",
                "schema_change",
            },
            field_name="concurrency",
        )
        try:
            return cls(
                same_file=SameFilePolicy(
                    raw.get("same_file", SameFilePolicy.REGION_SAFE.value)
                ),
                unknown_overlap=ConflictPolicy(
                    raw.get("unknown_overlap", ConflictPolicy.SERIALIZE.value)
                ),
                shared_contract=ConflictPolicy(
                    raw.get("shared_contract", ConflictPolicy.SERIALIZE.value)
                ),
                schema_change=ConflictPolicy(
                    raw.get("schema_change", ConflictPolicy.SERIALIZE.value)
                ),
            )
        except ValueError as exc:
            raise ValueError(f"invalid concurrency policy: {exc}") from exc


@dataclass(frozen=True, slots=True)
class SwarmBudgetPolicy:
    workers: WorkerBudget = field(default_factory=WorkerBudget)
    resources: ResourceBudget = field(default_factory=ResourceBudget)
    retries: RetryBudget = field(default_factory=RetryBudget)
    concurrency: ConcurrencyBudget = field(default_factory=ConcurrencyBudget)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SWARM_BUDGET_POLICY_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SWARM_BUDGET_POLICY_PROTOCOL:
            raise ValueError(f"unsupported swarm-budget protocol {self.protocol!r}")
        if not isinstance(self.workers, WorkerBudget):
            object.__setattr__(
                self, "workers", WorkerBudget.from_dict(self.workers)
            )
        if not isinstance(self.resources, ResourceBudget):
            object.__setattr__(
                self, "resources", ResourceBudget.from_dict(self.resources)
            )
        if not isinstance(self.retries, RetryBudget):
            object.__setattr__(self, "retries", RetryBudget.from_dict(self.retries))
        if not isinstance(self.concurrency, ConcurrencyBudget):
            object.__setattr__(
                self,
                "concurrency",
                ConcurrencyBudget.from_dict(self.concurrency),
            )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "workers": self.workers.to_dict(),
            "resources": self.resources.to_dict(),
            "retries": self.retries.to_dict(),
            "concurrency": self.concurrency.to_dict(),
            "metadata": dict(self.metadata),
        }

    def fingerprint(self) -> str:
        return _canonical_fingerprint(self.to_dict())

    def validate_work_item_count(self, count: int) -> None:
        if count < 1:
            raise ValueError("work graph must contain at least one work item")
        if count > self.workers.max_work_items:
            raise ValueError(
                f"work graph has {count} items but budget allows at most "
                f"{self.workers.max_work_items}"
            )
        if count > self.workers.max_total_launches:
            raise ValueError(
                f"work graph requires at least {count} launches but budget allows "
                f"at most {self.workers.max_total_launches}"
            )

    def summary(self, *, work_items: int | None = None) -> dict[str, Any]:
        if work_items is not None:
            self.validate_work_item_count(work_items)
        remaining_launches = (
            None
            if work_items is None
            else self.workers.max_total_launches - work_items
        )
        return {
            "fingerprint": self.fingerprint(),
            "max_active_workers": self.workers.max_active,
            "max_active_per_work_item": self.workers.max_active_per_work_item,
            "max_work_items": self.workers.max_work_items,
            "max_total_launches": self.workers.max_total_launches,
            "minimum_required_launches": work_items,
            "remaining_launch_capacity_after_first_attempt": remaining_launches,
            "max_total_tokens": self.resources.max_total_tokens,
            "max_cost_usd": self.resources.max_cost_usd,
            "max_wall_time_seconds": self.resources.max_wall_time_seconds,
            "retries": self.retries.to_dict(),
            "concurrency": self.concurrency.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "SwarmBudgetPolicy":
        raw = _mapping(data, field_name="budget_policy")
        _reject_unknown(
            raw,
            allowed={
                "protocol",
                "workers",
                "resources",
                "retries",
                "concurrency",
                "metadata",
            },
            field_name="budget_policy",
        )
        protocol = str(raw.get("protocol") or SWARM_BUDGET_POLICY_PROTOCOL)
        metadata = raw.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise ValueError("budget_policy.metadata must be an object")
        return cls(
            protocol=protocol,
            workers=WorkerBudget.from_dict(raw.get("workers")),
            resources=ResourceBudget.from_dict(raw.get("resources")),
            retries=RetryBudget.from_dict(raw.get("retries")),
            concurrency=ConcurrencyBudget.from_dict(raw.get("concurrency")),
            metadata=dict(metadata),
        )
