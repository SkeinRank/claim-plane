"""Observed tool-access traces for dynamic dependency verification."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from claim_plane.core.models import (
    AccessMode,
    ObservedAccess,
    ResourceKind,
    ResourceRef,
)


@dataclass(frozen=True, slots=True)
class ObservationPolicy:
    """How runtime access evidence is accepted during integration.

    ``optional`` accepts legacy file traces or no trace. ``required`` requires
    some trace. ``trusted`` requires a sealed, HMAC-verified control-plane
    session with complete proxy/OS-monitor coverage. ``brokered`` additionally
    requires Claim Plane broker events produced by an intent-enforcing proxy.
    """

    mode: str = "optional"
    require_complete: bool = True
    allowed_coverages: tuple[str, ...] = ("brokered_proxy", "tool_proxy", "os_monitor")

    def __post_init__(self) -> None:
        if self.mode not in {"optional", "required", "trusted", "brokered"}:
            raise ValueError(
                "observation mode must be optional, required, trusted, or brokered"
            )
        object.__setattr__(
            self,
            "allowed_coverages",
            tuple(dict.fromkeys(str(item) for item in self.allowed_coverages)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "require_complete": self.require_complete,
            "allowed_coverages": list(self.allowed_coverages),
        }

    @classmethod
    def from_dict(cls, data: object | None) -> "ObservationPolicy":
        if data is None:
            return cls()
        if isinstance(data, str):
            return cls(mode=data)
        if not isinstance(data, Mapping):
            raise TypeError("observation policy must be an object or mode")
        return cls(
            mode=str(data.get("mode") or "optional"),
            require_complete=bool(data.get("require_complete", True)),
            allowed_coverages=tuple(
                str(item)
                for item in data.get("allowed_coverages")
                or ("brokered_proxy", "tool_proxy", "os_monitor")
            ),
        )


def load_observation_trace(path: str | Path) -> tuple[ObservedAccess, ...]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"observation trace does not exist: {source}")
    text = source.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if not stripped:
        return ()

    # Prefer a complete JSON document, but fall back to JSONL when multiple
    # top-level objects are present.
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if payload is not None:
            try:
                if isinstance(payload, Mapping):
                    if "mode" in payload and "resource" in payload:
                        return (ObservedAccess.from_dict(payload),)
                    raw = payload.get("accesses") or payload.get("events") or ()
                else:
                    raw = payload
                if not isinstance(raw, list):
                    raise ValueError("observation trace JSON must contain a list")
                return tuple(ObservedAccess.from_dict(item) for item in raw)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid observation trace {source}: {exc}") from exc

    items: list[ObservedAccess] = []
    try:
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, Mapping):
                raise ValueError(f"trace line {number} is not an object")
            items.append(ObservedAccess.from_dict(item))
        return tuple(items)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid observation trace {source}: {exc}") from exc


def append_observation(
    path: str | Path,
    *,
    mode: AccessMode | str,
    kind: ResourceKind | str,
    identifier: str,
    tool: str | None = None,
    timestamp: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ObservedAccess:
    item = ObservedAccess(
        mode=AccessMode(mode),
        resource=ResourceRef(ResourceKind(kind), identifier),
        tool=tool,
        timestamp=timestamp,
        metadata=dict(metadata or {}),
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True))
        handle.write("\n")
    return item


def observation_digest(items: Iterable[ObservedAccess]) -> str:
    import hashlib

    payload = json.dumps(
        [item.to_dict() for item in items],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
