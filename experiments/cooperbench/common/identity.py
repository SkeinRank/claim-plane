"""Deterministic study and run identities."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .config import canonical_json
from .models import ShardSpec, StudySpec

_SAFE_ID = re.compile(r"[^a-zA-Z0-9._-]+")


def _safe_component(value: str) -> str:
    normalized = _SAFE_ID.sub("-", value.strip()).strip("-.")
    if not normalized:
        raise ValueError("identifier component becomes empty after normalization")
    return normalized


def study_fingerprint(study: StudySpec) -> str:
    return hashlib.sha256(canonical_json(study.to_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RunIdentity:
    study_id: str
    study_fingerprint: str
    coder_seed: int
    shard: ShardSpec
    run_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "study_id": self.study_id,
            "study_fingerprint": self.study_fingerprint,
            "coder_seed": self.coder_seed,
            "shard": self.shard.to_dict(),
            "run_id": self.run_id,
        }


def build_run_identity(
    study: StudySpec,
    *,
    coder_seed: int,
    shard: ShardSpec | None = None,
) -> RunIdentity:
    if coder_seed not in study.coder_seeds:
        raise ValueError(
            f"coder seed {coder_seed} is not declared by study {study.study_id}"
        )
    resolved_shard = shard or ShardSpec()
    fingerprint = study_fingerprint(study)
    study_id = _safe_component(study.study_id)
    run_id = (
        f"{study_id}--seed-{coder_seed}--"
        f"shard-{resolved_shard.index:02d}-of-{resolved_shard.count:02d}--"
        f"{fingerprint[:12]}"
    )
    return RunIdentity(
        study_id=study.study_id,
        study_fingerprint=fingerprint,
        coder_seed=coder_seed,
        shard=resolved_shard,
        run_id=run_id,
    )
