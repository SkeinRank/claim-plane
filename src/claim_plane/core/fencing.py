"""Runtime premise fencing for stale autonomous writers.

A runtime fence is the durable execution consequence of premise invalidation.  The
intent graph already determines which dependents become stale; this module gives the
resulting revocation a versioned evidence shape so broker capabilities can be stopped
atomically with that state transition.

Fencing is intentionally narrower than recovery.  It revokes mutation authority and
records why execution became stale.  Refresh/rebase/resume is a separate lifecycle
step and must establish fresh authority before a worker continues.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

RUNTIME_FENCE_PROTOCOL = "claim-plane.runtime-fence.v1"


class RuntimeFenceReason(str, Enum):
    """Stable causes for revoking live mutation authority."""

    PREMISE_INVALIDATED = "premise_invalidated"
    PRODUCER_AMENDED = "producer_amended"
    VERIFICATION_CONTRACT_FAILURE = "verification_contract_failure"
    MANUAL = "manual"


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class RuntimeFence:
    """Immutable evidence that a stale intent lost live mutation authority."""

    fence_id: str
    intent_id: str
    producer_intent_id: str | None
    reason: str
    resource_keys: tuple[str, ...]
    dependency_chain: tuple[str, ...]
    broker_instance_id: str | None
    root_path: str | None
    fencing_token: int | None
    status: str
    created_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = RUNTIME_FENCE_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != RUNTIME_FENCE_PROTOCOL:
            raise ValueError(f"unsupported runtime-fence protocol {self.protocol!r}")
        for name in ("fence_id", "intent_id", "reason", "status", "created_at"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"{name} must not be empty")
            object.__setattr__(self, name, value)
        if self.producer_intent_id is not None:
            producer = str(self.producer_intent_id).strip()
            object.__setattr__(self, "producer_intent_id", producer or None)
        object.__setattr__(
            self,
            "resource_keys",
            tuple(
                sorted(
                    {
                        str(item).strip()
                        for item in self.resource_keys
                        if str(item).strip()
                    }
                )
            ),
        )
        object.__setattr__(
            self,
            "dependency_chain",
            tuple(
                str(item).strip() for item in self.dependency_chain if str(item).strip()
            ),
        )
        if self.fencing_token is not None and self.fencing_token <= 0:
            raise ValueError("fencing_token must be positive when present")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "fence_id": self.fence_id,
            "intent_id": self.intent_id,
            "producer_intent_id": self.producer_intent_id,
            "reason": self.reason,
            "resource_keys": list(self.resource_keys),
            "dependency_chain": list(self.dependency_chain),
            "broker_instance_id": self.broker_instance_id,
            "root_path": self.root_path,
            "fencing_token": self.fencing_token,
            "status": self.status,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RuntimeFence":
        fencing_token = data.get("fencing_token")
        return cls(
            protocol=str(data.get("protocol") or RUNTIME_FENCE_PROTOCOL),
            fence_id=str(data.get("fence_id") or ""),
            intent_id=str(data.get("intent_id") or ""),
            producer_intent_id=(
                None
                if data.get("producer_intent_id") is None
                else str(data.get("producer_intent_id"))
            ),
            reason=str(data.get("reason") or ""),
            resource_keys=tuple(str(item) for item in data.get("resource_keys") or ()),
            dependency_chain=tuple(
                str(item) for item in data.get("dependency_chain") or ()
            ),
            broker_instance_id=(
                None
                if data.get("broker_instance_id") is None
                else str(data.get("broker_instance_id"))
            ),
            root_path=(
                None if data.get("root_path") is None else str(data.get("root_path"))
            ),
            fencing_token=None if fencing_token is None else int(fencing_token),
            status=str(data.get("status") or ""),
            created_at=str(data.get("created_at") or ""),
            metadata=dict(data.get("metadata") or {}),
        )

    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict())).hexdigest()
