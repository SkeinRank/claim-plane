"""Study configuration loading and canonical JSON serialization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import StudySpec


def canonical_json(payload: Any) -> str:
    """Serialize JSON deterministically for fingerprints and persisted declarations."""

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def load_study(path: str | Path) -> StudySpec:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("study file must contain one JSON object")
    return StudySpec.from_dict(payload)


def write_pretty_json(path: str | Path, payload: Any) -> None:
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
