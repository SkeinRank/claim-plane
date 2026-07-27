"""Frozen configuration for the six-pair mechanism check reported in the paper."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..common import Arm, PairRef, StudySpec
from ..planner_v1 import PLANNER_MODEL, PLANNER_POLICY_VERSION

PAPER_STUDY_ID = "claim-plane-paper-6pair"
PAPER_CLAIM_PLANE_VERSION = "0.2.1"
CODER_MODEL = "deepseek/deepseek-v4-flash"
PAIR_SELECTION_SEED = 42
LLM_SEEDS = (101,)
REPETITIONS = 1

FROZEN_PAIRS = (
    PairRef("pallets_jinja_task", 1465, 1, 8, True),
    PairRef("pallets_jinja_task", 1559, 1, 4, False),
    PairRef("pallets_jinja_task", 1621, 1, 7, False),
    PairRef("pallets_click_task", 2800, 1, 3, True),
    PairRef("pallets_jinja_task", 1559, 6, 8, False),
    PairRef("samuelcolvin_dirty_equals_task", 43, 5, 7, True),
)

PAPER_STUDY = StudySpec(
    study_id=PAPER_STUDY_ID,
    description=(
        "Six-pair CooperBench mechanism check reported in Section 8 of the "
        "Claim Plane preprint."
    ),
    claim_plane_version=PAPER_CLAIM_PLANE_VERSION,
    planner_policy_version=PLANNER_POLICY_VERSION,
    planner_model=PLANNER_MODEL,
    coder_model=CODER_MODEL,
    pair_selection_seed=PAIR_SELECTION_SEED,
    coder_seeds=LLM_SEEDS,
    arms=(
        Arm.PARALLEL,
        Arm.CLAIM_PLANE_STATIC,
        Arm.CLAIM_PLANE_DYNAMIC,
        Arm.ALWAYS_SERIAL,
    ),
    pairs=FROZEN_PAIRS,
    metadata={
        "benchmark_notebook": "V8.5",
        "paper_section": "8",
        "oracle_localized_initial_context": True,
        "api_calls_physically_sequential": True,
        "logical_parallelism": True,
        "conflict_share": 0.5,
    },
)

# Coding-agent executor frozen in the V8.5 study.
MAX_AGENT_STEPS = 15
ACTION_RETRIES_PER_STEP = 4
USE_NATIVE_TOOL_CALLS = False
NATIVE_TOOL_ATTEMPTS_PER_STEP = 0
USE_JSON_MODE_FALLBACK = True
MAX_EXPLORATION_STEPS_BEFORE_EDIT = 8
EXPLORATION_NUDGE_INTERVAL = 3
MAX_AGENT_TEST_RUNS = 6
MAX_TOOL_ERRORS = 5
AUTO_TEST_AFTER_MUTATION = True
CODER_MAX_TOKENS = 5000

OFFICIAL_TEST_TIMEOUT_SECONDS = 900
MAX_TEST_LOG_CHARS = 8000
MAX_TOOL_RESULT_CHARS = 14000
MAX_READ_LINES = 350
MAX_SEARCH_RESULTS = 40
MAX_DIFF_CHARS = 14000
MAX_EXISTING_WRITE_FILE_CHARS = 12000

RUN_GOLD_SANITY = True
DROP_GOLD_INVALID_PAIRS = True
RUN_PAIR_TESTS = True
RUN_PLANNER_STABILITY_PROBE = False

REPOSITORIES = (
    "samuelcolvin_dirty_equals_task",
    "pallets_click_task",
    "pallets_jinja_task",
)

CLAIM_PLANE_ARMS = {
    Arm.CLAIM_PLANE_STATIC.value,
    Arm.CLAIM_PLANE_DYNAMIC.value,
}
ARMS = tuple(arm.value for arm in PAPER_STUDY.arms)

REFERENCE_SUMMARY = {
    "parallel": {
        "n": 6,
        "pair_pass": 2,
        "integration_success": 3,
        "initial_serialized": 0,
        "promotions": 0,
        "undeclared_blocks": 0,
    },
    "claim-plane-static": {
        "n": 6,
        "pair_pass": 6,
        "integration_success": 6,
        "initial_serialized": 6,
        "promotions": 0,
        "undeclared_blocks": 0,
    },
    "claim-plane-dynamic": {
        "n": 6,
        "pair_pass": 3,
        "integration_success": 4,
        "initial_serialized": 3,
        "promotions": 7,
        "undeclared_blocks": 2,
    },
    "always-serial": {
        "n": 6,
        "pair_pass": 4,
        "integration_success": 6,
        "initial_serialized": 6,
        "promotions": 0,
        "undeclared_blocks": 0,
    },
}


@dataclass(frozen=True, slots=True)
class PaperPaths:
    """Filesystem roots used by one local reproduction run."""

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
    ) -> "PaperPaths":
        return cls(
            cooperbench=Path(cooperbench).expanduser().resolve(),
            artifact_root=Path(artifact_root).expanduser().resolve(),
            repo_cache=Path(repo_cache).expanduser().resolve(),
            workspace_root=Path(workspace_root).expanduser().resolve(),
        )

    @property
    def dataset(self) -> Path:
        return self.cooperbench / "dataset"
