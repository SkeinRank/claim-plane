"""Atomic, resumable execution checkpoints."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_CHECKPOINT_SCHEMA_VERSION = 1
_VALID_STATES = {"initialized", "running", "completed", "failed"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class Checkpoint:
    run_id: str
    state: str = "initialized"
    completed_units: tuple[str, ...] = ()
    failed_units: Mapping[str, str] = field(default_factory=dict)
    updated_at_utc: str = field(default_factory=_utc_now)
    schema_version: int = _CHECKPOINT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported checkpoint schema version")
        if not self.run_id.strip():
            raise ValueError("checkpoint run_id must not be empty")
        if self.state not in _VALID_STATES:
            raise ValueError(f"invalid checkpoint state {self.state!r}")
        if len(set(self.completed_units)) != len(self.completed_units):
            raise ValueError("completed checkpoint units must be unique")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "state": self.state,
            "completed_units": list(self.completed_units),
            "failed_units": dict(self.failed_units),
            "updated_at_utc": self.updated_at_utc,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Checkpoint":
        completed = payload.get("completed_units", [])
        failed = payload.get("failed_units", {})
        if not isinstance(completed, list):
            raise ValueError("completed_units must be a JSON array")
        if not isinstance(failed, dict):
            raise ValueError("failed_units must be a JSON object")
        return cls(
            schema_version=int(payload.get("schema_version", 1)),
            run_id=str(payload["run_id"]),
            state=str(payload.get("state", "initialized")),
            completed_units=tuple(str(item) for item in completed),
            failed_units={str(key): str(value) for key, value in failed.items()},
            updated_at_utc=str(payload.get("updated_at_utc", _utc_now())),
        )

    def with_state(self, state: str) -> "Checkpoint":
        return Checkpoint(
            run_id=self.run_id,
            state=state,
            completed_units=self.completed_units,
            failed_units=self.failed_units,
        )

    def mark_completed(self, unit_id: str) -> "Checkpoint":
        unit = unit_id.strip()
        if not unit:
            raise ValueError("unit_id must not be empty")
        completed = tuple(dict.fromkeys((*self.completed_units, unit)))
        failed = dict(self.failed_units)
        failed.pop(unit, None)
        return Checkpoint(
            run_id=self.run_id,
            state="running",
            completed_units=completed,
            failed_units=failed,
        )

    def mark_failed(self, unit_id: str, error: str) -> "Checkpoint":
        unit = unit_id.strip()
        if not unit:
            raise ValueError("unit_id must not be empty")
        failed = dict(self.failed_units)
        failed[unit] = error[:4000]
        return Checkpoint(
            run_id=self.run_id,
            state="running",
            completed_units=self.completed_units,
            failed_units=failed,
        )


class CheckpointStore:
    """Persist one checkpoint using replace-on-commit writes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> Checkpoint:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("checkpoint file must contain one JSON object")
        return Checkpoint.from_dict(payload)

    def save(self, checkpoint: Checkpoint) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (
            json.dumps(
                checkpoint.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
            )
            + "\n"
        )
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, self.path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
