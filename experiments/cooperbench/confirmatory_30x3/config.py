"""Frozen protocol constants for the 30-pair, three-seed CooperBench study."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..common import Arm, PairRef, StudySpec
from ..planner_v1 import PLANNER_MODEL, PLANNER_POLICY_VERSION

STUDY_ID = "claim-plane-confirmatory-30x3"
PROTOCOL_CLAIM_PLANE_VERSION = "0.2.1"
CODER_MODEL = "deepseek/deepseek-v4-flash"
PAIR_SELECTION_SEED = 42
PLANNER_FREEZE_SEED = 1701
CODER_SEEDS = (101, 202, 303)
N_PAIRS = 30
CONFLICT_SHARE = 0.5
SHARD_SIZE = 10
SHARD_COUNT = 3
REPOSITORIES = (
    "samuelcolvin_dirty_equals_task",
    "pallets_click_task",
    "pallets_jinja_task",
)
ARMS = (
    Arm.PARALLEL,
    Arm.CLAIM_PLANE_STATIC,
    Arm.CLAIM_PLANE_DYNAMIC,
    Arm.ALWAYS_SERIAL,
)


def build_study(pairs: tuple[PairRef, ...]) -> StudySpec:
    """Build the immutable study declaration after pair selection is frozen."""
    if len(pairs) != N_PAIRS:
        raise ValueError(f"confirmatory study requires exactly {N_PAIRS} pairs")
    conflict_count = sum(pair.gold_conflict is True for pair in pairs)
    clean_count = sum(pair.gold_conflict is False for pair in pairs)
    if conflict_count != 15 or clean_count != 15:
        raise ValueError(
            "confirmatory study requires exactly 15 conflict and 15 clean pairs"
        )
    return StudySpec(
        study_id=STUDY_ID,
        description=(
            "Frozen-plan 30-pair CooperBench confirmatory study with three independent "
            "coder seeds and four coordination arms."
        ),
        claim_plane_version=PROTOCOL_CLAIM_PLANE_VERSION,
        planner_policy_version=PLANNER_POLICY_VERSION,
        planner_model=PLANNER_MODEL,
        coder_model=CODER_MODEL,
        pair_selection_seed=PAIR_SELECTION_SEED,
        coder_seeds=CODER_SEEDS,
        arms=ARMS,
        pairs=pairs,
        metadata={
            "benchmark_notebook_origin": "V9",
            "planner_freeze_seed": PLANNER_FREEZE_SEED,
            "planner_outputs_frozen_once": True,
            "planner_reused_across_coder_seeds": True,
            "planner_reused_across_static_dynamic": True,
            "oracle_localized_initial_context": True,
            "api_calls_physically_sequential": True,
            "logical_parallelism": True,
            "n_pairs": N_PAIRS,
            "conflict_share": CONFLICT_SHARE,
            "shard_size": SHARD_SIZE,
            "shard_count": SHARD_COUNT,
        },
    )


@dataclass(frozen=True, slots=True)
class ConfirmatoryPaths:
    """Filesystem roots shared by protocol freezing and execution."""

    cooperbench: Path
    artifact_root: Path
    repo_cache: Path
    workspace_root: Path

    @classmethod
    def from_values(
        cls,
        cooperbench: str | Path,
        artifact_root: str | Path = ".claim-plane/experiments",
        repo_cache: str | Path = ".claim-plane/cooperbench/repos",
        workspace_root: str | Path = ".claim-plane/cooperbench/worktrees",
    ) -> "ConfirmatoryPaths":
        return cls(
            cooperbench=Path(cooperbench).expanduser().resolve(),
            artifact_root=Path(artifact_root).expanduser().resolve(),
            repo_cache=Path(repo_cache).expanduser().resolve(),
            workspace_root=Path(workspace_root).expanduser().resolve(),
        )

    @property
    def dataset(self) -> Path:
        return self.cooperbench / "dataset"

    @property
    def protocol_dir(self) -> Path:
        return self.artifact_root / STUDY_ID / "protocol"

    @property
    def study_file(self) -> Path:
        return self.protocol_dir / "study.json"

    @property
    def selected_pairs_file(self) -> Path:
        return self.protocol_dir / "selected_pairs.json"

    @property
    def benchmark_pairs_file(self) -> Path:
        return self.protocol_dir / "benchmark_pairs.json"

    @property
    def gold_sanity_file(self) -> Path:
        return self.protocol_dir / "gold_sanity.json"

    @property
    def frozen_plans_file(self) -> Path:
        return self.protocol_dir / "frozen_plans.json"

    @property
    def frozen_plan_manifest_file(self) -> Path:
        return self.protocol_dir / "frozen_plan_manifest.json"
