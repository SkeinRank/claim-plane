"""Typed experiment declarations with no model-provider dependency."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = 1
_STUDY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class Arm(str, Enum):
    """Execution policies used by the Claim Plane CooperBench studies."""

    PARALLEL = "parallel"
    CLAIM_PLANE_STATIC = "claim-plane-static"
    CLAIM_PLANE_DYNAMIC = "claim-plane-dynamic"
    ALWAYS_SERIAL = "always-serial"


@dataclass(frozen=True, slots=True)
class PairRef:
    """Immutable reference to one CooperBench feature pair."""

    repo: str
    task_id: int
    feature_a: int
    feature_b: int
    gold_conflict: bool | None = None

    def __post_init__(self) -> None:
        if not self.repo.strip():
            raise ValueError("pair repo must not be empty")
        if self.task_id < 0:
            raise ValueError("pair task_id must be non-negative")
        if self.feature_a < 0 or self.feature_b < 0:
            raise ValueError("pair feature ids must be non-negative")
        if self.feature_a == self.feature_b:
            raise ValueError("pair features must be distinct")

    @property
    def key(self) -> str:
        first, second = sorted((self.feature_a, self.feature_b))
        return f"{self.repo}/task{self.task_id}/feature{first}+feature{second}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "repo": self.repo,
            "task_id": self.task_id,
            "feature_a": self.feature_a,
            "feature_b": self.feature_b,
        }
        if self.gold_conflict is not None:
            payload["gold_conflict"] = self.gold_conflict
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PairRef":
        return cls(
            repo=str(payload["repo"]),
            task_id=int(payload["task_id"]),
            feature_a=int(payload["feature_a"]),
            feature_b=int(payload["feature_b"]),
            gold_conflict=(
                None
                if payload.get("gold_conflict") is None
                else bool(payload["gold_conflict"])
            ),
        )


@dataclass(frozen=True, slots=True)
class ShardSpec:
    """Deterministic partition of the frozen pair order."""

    index: int = 1
    count: int = 1

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError("shard count must be at least 1")
        if not 1 <= self.index <= self.count:
            raise ValueError("shard index must be within 1..count")

    def includes_position(self, zero_based_position: int) -> bool:
        if zero_based_position < 0:
            raise ValueError("pair position must be non-negative")
        return zero_based_position % self.count == self.index - 1

    def select(self, pairs: Sequence[PairRef]) -> tuple[PairRef, ...]:
        return tuple(
            pair
            for position, pair in enumerate(pairs)
            if self.includes_position(position)
        )

    def to_dict(self) -> dict[str, int]:
        return {"index": self.index, "count": self.count}


@dataclass(frozen=True, slots=True)
class StudySpec:
    """Frozen study inputs that affect experimental execution."""

    study_id: str
    description: str
    claim_plane_version: str
    planner_policy_version: str
    planner_model: str
    coder_model: str
    pairs: tuple[PairRef, ...]
    coder_seeds: tuple[int, ...]
    arms: tuple[Arm, ...] = (
        Arm.PARALLEL,
        Arm.CLAIM_PLANE_STATIC,
        Arm.CLAIM_PLANE_DYNAMIC,
        Arm.ALWAYS_SERIAL,
    )
    pair_selection_seed: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported study schema_version {self.schema_version}; "
                f"expected {SCHEMA_VERSION}"
            )
        if not self.study_id.strip():
            raise ValueError("study_id must not be empty")
        if _STUDY_ID.fullmatch(self.study_id) is None:
            raise ValueError(
                "study_id may contain only letters, digits, dot, underscore, and hyphen"
            )
        if not self.description.strip():
            raise ValueError("study description must not be empty")
        if not self.claim_plane_version.strip():
            raise ValueError("claim_plane_version must not be empty")
        if not self.planner_policy_version.strip():
            raise ValueError("planner_policy_version must not be empty")
        if not self.planner_model.strip() or not self.coder_model.strip():
            raise ValueError("planner_model and coder_model must not be empty")
        if not self.pairs:
            raise ValueError("study must contain at least one frozen pair")
        pair_keys = [pair.key for pair in self.pairs]
        if len(set(pair_keys)) != len(pair_keys):
            raise ValueError("study pairs must be unique")
        if not self.coder_seeds:
            raise ValueError("study must contain at least one coder seed")
        if len(set(self.coder_seeds)) != len(self.coder_seeds):
            raise ValueError("coder seeds must be unique")
        if not self.arms:
            raise ValueError("study must contain at least one arm")
        if len(set(self.arms)) != len(self.arms):
            raise ValueError("study arms must be unique")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "study_id": self.study_id,
            "description": self.description,
            "claim_plane_version": self.claim_plane_version,
            "planner_policy_version": self.planner_policy_version,
            "planner_model": self.planner_model,
            "coder_model": self.coder_model,
            "pair_selection_seed": self.pair_selection_seed,
            "coder_seeds": list(self.coder_seeds),
            "arms": [arm.value for arm in self.arms],
            "pairs": [pair.to_dict() for pair in self.pairs],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StudySpec":
        schema_version = int(payload.get("schema_version", SCHEMA_VERSION))
        pair_payloads = payload.get("pairs")
        if not isinstance(pair_payloads, list):
            raise ValueError("study pairs must be a JSON array")
        seed_payloads = payload.get("coder_seeds")
        if not isinstance(seed_payloads, list):
            raise ValueError("coder_seeds must be a JSON array")
        arm_payloads = payload.get("arms")
        if not isinstance(arm_payloads, list):
            raise ValueError("arms must be a JSON array")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be a JSON object")
        return cls(
            schema_version=schema_version,
            study_id=str(payload["study_id"]),
            description=str(payload["description"]),
            claim_plane_version=str(payload["claim_plane_version"]),
            planner_policy_version=str(payload["planner_policy_version"]),
            planner_model=str(payload["planner_model"]),
            coder_model=str(payload["coder_model"]),
            pair_selection_seed=(
                None
                if payload.get("pair_selection_seed") is None
                else int(payload["pair_selection_seed"])
            ),
            coder_seeds=tuple(int(seed) for seed in seed_payloads),
            arms=tuple(Arm(str(arm)) for arm in arm_payloads),
            pairs=tuple(PairRef.from_dict(item) for item in pair_payloads),
            metadata=metadata,
        )
