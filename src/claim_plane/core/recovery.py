"""Deterministic pause, refresh, and resume evidence for stale intents.

Premise invalidation revokes live mutation authority through runtime fences.  Recovery
is a separate lifecycle: a stale intent is refreshed against a new pinned base,
re-admitted without expanding its declared authority, and only then may it resume.
The records in this module make that transition durable and inspectable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

RUNTIME_RECOVERY_PROTOCOL = "claim-plane.runtime-recovery.v1"


class RuntimeRecoveryState(str, Enum):
    """Stable states for one stale-intent recovery attempt."""

    REFRESHED = "refreshed"
    RESUMED = "resumed"


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class RuntimeRecovery:
    """Immutable evidence for one refresh/resume lifecycle."""

    recovery_id: str
    intent_id: str
    state: RuntimeRecoveryState
    from_base_commit: str | None
    to_base_commit: str
    old_content_version: int
    new_content_version: int
    old_fingerprint: str
    new_fingerprint: str
    fence_ids: tuple[str, ...]
    producer_versions: Mapping[str, int]
    created_at: str
    resumed_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = RUNTIME_RECOVERY_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != RUNTIME_RECOVERY_PROTOCOL:
            raise ValueError(f"unsupported runtime-recovery protocol {self.protocol!r}")
        object.__setattr__(self, "state", RuntimeRecoveryState(self.state))
        for name in (
            "recovery_id",
            "intent_id",
            "to_base_commit",
            "old_fingerprint",
            "new_fingerprint",
            "created_at",
        ):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"{name} must not be empty")
            object.__setattr__(self, name, value)
        if self.from_base_commit is not None:
            value = str(self.from_base_commit).strip()
            object.__setattr__(self, "from_base_commit", value or None)
        if self.old_content_version <= 0 or self.new_content_version <= 0:
            raise ValueError("content versions must be positive")
        if self.new_content_version <= self.old_content_version:
            raise ValueError("new_content_version must advance old_content_version")
        object.__setattr__(
            self,
            "fence_ids",
            tuple(sorted({str(item).strip() for item in self.fence_ids if str(item).strip()})),
        )
        object.__setattr__(
            self,
            "producer_versions",
            {
                str(key).strip(): int(value)
                for key, value in sorted(dict(self.producer_versions).items())
                if str(key).strip()
            },
        )
        if any(value <= 0 for value in self.producer_versions.values()):
            raise ValueError("producer versions must be positive")
        if self.resumed_at is not None:
            value = str(self.resumed_at).strip()
            object.__setattr__(self, "resumed_at", value or None)
        if self.state is RuntimeRecoveryState.RESUMED and not self.resumed_at:
            raise ValueError("resumed recovery requires resumed_at")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "recovery_id": self.recovery_id,
            "intent_id": self.intent_id,
            "state": self.state.value,
            "from_base_commit": self.from_base_commit,
            "to_base_commit": self.to_base_commit,
            "old_content_version": self.old_content_version,
            "new_content_version": self.new_content_version,
            "old_fingerprint": self.old_fingerprint,
            "new_fingerprint": self.new_fingerprint,
            "fence_ids": list(self.fence_ids),
            "producer_versions": dict(self.producer_versions),
            "created_at": self.created_at,
            "resumed_at": self.resumed_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RuntimeRecovery":
        return cls(
            protocol=str(data.get("protocol") or RUNTIME_RECOVERY_PROTOCOL),
            recovery_id=str(data.get("recovery_id") or ""),
            intent_id=str(data.get("intent_id") or ""),
            state=RuntimeRecoveryState(data.get("state") or RuntimeRecoveryState.REFRESHED.value),
            from_base_commit=(
                None if data.get("from_base_commit") is None else str(data.get("from_base_commit"))
            ),
            to_base_commit=str(data.get("to_base_commit") or ""),
            old_content_version=int(data.get("old_content_version") or 0),
            new_content_version=int(data.get("new_content_version") or 0),
            old_fingerprint=str(data.get("old_fingerprint") or ""),
            new_fingerprint=str(data.get("new_fingerprint") or ""),
            fence_ids=tuple(str(item) for item in data.get("fence_ids") or ()),
            producer_versions={
                str(key): int(value)
                for key, value in dict(data.get("producer_versions") or {}).items()
            },
            created_at=str(data.get("created_at") or ""),
            resumed_at=(None if data.get("resumed_at") is None else str(data.get("resumed_at"))),
            metadata=dict(data.get("metadata") or {}),
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict())).hexdigest()
