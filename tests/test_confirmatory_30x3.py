from __future__ import annotations

import json
from pathlib import Path

from experiments.cooperbench.cli import main as experiment_main
from experiments.cooperbench.common import PairRef
from experiments.cooperbench.confirmatory_30x3.config import (
    CODER_SEEDS,
    N_PAIRS,
    SHARD_COUNT,
    SHARD_SIZE,
    build_study,
)
from experiments.cooperbench.confirmatory_30x3.plans import pair_plan_seed
from experiments.cooperbench.confirmatory_30x3.runner import contiguous_shard
from experiments.cooperbench.confirmatory_30x3.selection import (
    freeze_gold_valid_pairs,
    select_candidate_stream,
)


def _pairs(count: int = 40) -> tuple[PairRef, ...]:
    rows = []
    for index in range(count):
        rows.append(
            PairRef(
                repo="pallets_jinja_task",
                task_id=index // 4,
                feature_a=(index % 4) + 1,
                feature_b=(index % 4) + 10,
                gold_conflict=index % 2 == 0,
            )
        )
    return tuple(rows)


def test_confirmatory_dimensions_are_frozen() -> None:
    assert N_PAIRS == 30
    assert CODER_SEEDS == (101, 202, 303)
    assert SHARD_SIZE == 10
    assert SHARD_COUNT == 3
    assert N_PAIRS * len(CODER_SEEDS) * 4 == 360


def test_candidate_selection_and_gold_freeze_are_balanced() -> None:
    initial, reserve = select_candidate_stream(_pairs(60))
    validity = {pair.key: True for pair in (*initial, *reserve)}
    frozen = freeze_gold_valid_pairs(initial, reserve, validity)

    assert len(initial) == 30
    assert len(frozen) == 30
    assert sum(pair.gold_conflict is True for pair in frozen) == 15
    assert sum(pair.gold_conflict is False for pair in frozen) == 15


def test_planner_freeze_seed_is_stable_and_agent_specific() -> None:
    pair = _pairs(2)[0]
    assert pair_plan_seed(pair, "A") == pair_plan_seed(pair, "A")
    assert pair_plan_seed(pair, "A") != pair_plan_seed(pair, "B")


def test_contiguous_shards_match_v9_ten_pair_layout() -> None:
    pairs = tuple(PairRef("pallets_jinja_task", i, 1, 2, i % 2 == 0) for i in range(30))
    first = contiguous_shard(pairs, 1)
    second = contiguous_shard(pairs, 2)
    third = contiguous_shard(pairs, 3)

    assert [pair.task_id for pair in first] == list(range(10))
    assert [pair.task_id for pair in second] == list(range(10, 20))
    assert [pair.task_id for pair in third] == list(range(20, 30))


def test_build_study_requires_15_conflict_and_15_clean_pairs() -> None:
    pairs = tuple(
        PairRef(
            "pallets_jinja_task",
            i,
            1,
            2,
            True if i < 15 else False,
        )
        for i in range(30)
    )
    study = build_study(pairs)

    assert study.claim_plane_version == "0.2.1"
    assert study.coder_seeds == CODER_SEEDS
    assert len(study.pairs) == 30
    assert study.metadata["planner_outputs_frozen_once"] is True


def test_confirmatory_info_cli_is_offline(capsys) -> None:
    assert experiment_main(["confirmatory", "info"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["pairs"] == 30
    assert payload["coder_seeds"] == [101, 202, 303]
    assert payload["total_shards"] == 9
    assert payload["planned_arm_executions"] == 360


def test_confirmatory_status_before_prepare_is_offline(tmp_path: Path, capsys) -> None:
    assert (
        experiment_main(
            ["confirmatory", "status", "--artifacts", str(tmp_path / "artifacts")]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["prepared"] is False
