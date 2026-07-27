from __future__ import annotations

import hashlib
import json
from pathlib import Path

from experiments.cooperbench.cli import main as experiment_main
from experiments.cooperbench.common import (
    ArtifactLayout,
    Checkpoint,
    CheckpointStore,
    PairRef,
    ShardSpec,
    build_run_identity,
)
from experiments.cooperbench.common.identity import study_fingerprint
from experiments.cooperbench.confirmatory_30x3.aggregation import (
    aggregate_study,
    verify_analysis,
)
from experiments.cooperbench.confirmatory_30x3.config import (
    ConfirmatoryPaths,
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


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _prepared_analysis_artifacts(tmp_path: Path) -> ConfirmatoryPaths:
    pairs = tuple(
        PairRef(
            "pallets_jinja_task",
            i // 3,
            (i % 3) + 1,
            (i % 3) + 10,
            True if i < 15 else False,
        )
        for i in range(30)
    )
    study = build_study(pairs)
    paths = ConfirmatoryPaths.from_values(
        tmp_path / "cooperbench",
        artifact_root=tmp_path / "artifacts",
        repo_cache=tmp_path / "repos",
        workspace_root=tmp_path / "worktrees",
    )
    _write_json(paths.study_file, study.to_dict())
    fingerprint = study_fingerprint(study)
    frozen_manifest = {
        "schema_version": 1,
        "study_fingerprint": fingerprint,
        "pair_count": 30,
        "total_planner_logical_cost": 3.0,
        "rows": [],
    }
    _write_json(paths.frozen_plan_manifest_file, frozen_manifest)
    frozen_manifest_sha256 = hashlib.sha256(
        json.dumps(
            frozen_manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()

    arm_values = [arm.value for arm in study.arms]
    for seed in study.coder_seeds:
        for shard_index in range(1, SHARD_COUNT + 1):
            run = build_run_identity(
                study,
                coder_seed=seed,
                shard=ShardSpec(shard_index, SHARD_COUNT),
            )
            layout = ArtifactLayout.for_run(paths.artifact_root, run)
            layout.create()
            shard_pairs = pairs[
                (shard_index - 1) * SHARD_SIZE : shard_index * SHARD_SIZE
            ]
            _write_json(
                layout.run_dir / "protocol.json",
                {
                    "study_fingerprint": fingerprint,
                    "coder_seed": seed,
                    "coder_seed_index": list(study.coder_seeds).index(seed),
                    "shard_index": shard_index,
                    "shard_count": SHARD_COUNT,
                    "pair_keys": [pair.key for pair in shard_pairs],
                    "frozen_plan_manifest_sha256": frozen_manifest_sha256,
                },
            )
            results = []
            completed = Checkpoint(run_id=run.run_id)
            for pair in shard_pairs:
                for arm in arm_values:
                    pair_pass = arm != "parallel" or pair.gold_conflict is False
                    serialized = arm == "always-serial" or (
                        arm == "claim-plane-dynamic" and pair.gold_conflict is True
                    )
                    results.append(
                        {
                            "pair": pair.key,
                            "arm": arm,
                            "gold_conflict": pair.gold_conflict,
                            "coder_seed": seed,
                            "shard_index": shard_index,
                            "pair_pass": pair_pass,
                            "integration_success": pair_pass,
                            "initial_serialized": arm == "always-serial",
                            "runtime_serialized": (
                                arm == "claim-plane-dynamic"
                                and pair.gold_conflict is True
                            ),
                            "serialized": serialized,
                            "scope_promotion_attempts": 1
                            if arm == "claim-plane-dynamic"
                            else 0,
                            "scope_promotions_succeeded": 1
                            if arm == "claim-plane-dynamic"
                            else 0,
                            "scope_promotions_rejected": 0,
                            "scope_undeclared_blocks": 0,
                            "dynamic_restart_count": 1
                            if arm == "claim-plane-dynamic"
                            and pair.gold_conflict is True
                            else 0,
                            "dynamic_wasted_steps": 2
                            if arm == "claim-plane-dynamic"
                            and pair.gold_conflict is True
                            else 0,
                            "dynamic_wasted_coder_cost": 0.01
                            if arm == "claim-plane-dynamic"
                            and pair.gold_conflict is True
                            else 0.0,
                            "coder_cost": 0.1,
                            "logical_system_cost_estimate": 0.12,
                            "logical_llm_critical_path": 1.0,
                            "planner_failure": False,
                            "scope_enforcement_failure": False,
                            "agent_execution_failure": False,
                            "harness_failure": False,
                        }
                    )
                    completed = completed.mark_completed(f"{pair.key}/{arm}")
            completed = completed.with_state("completed")
            CheckpointStore(layout.checkpoint_file).save(completed)
            _write_json(layout.run_dir / "results.json", results)
    return paths


def test_confirmatory_aggregation_writes_verifiable_publication_artifacts(
    tmp_path: Path,
) -> None:
    paths = _prepared_analysis_artifacts(tmp_path)

    result = aggregate_study(paths, bootstrap_samples=40, bootstrap_seed=17)

    assert result["complete"] is True
    assert result["arm_executions"] == 360
    analysis_dir = Path(result["analysis_dir"])
    assert (analysis_dir / "arm_results.json").exists()
    assert (analysis_dir / "feature_pair_summary.csv").exists()
    assert (analysis_dir / "task_cluster_summary.csv").exists()
    assert (analysis_dir / "bootstrap_ci.json").exists()
    assert (analysis_dir / "failure_taxonomy.json").exists()
    assert (analysis_dir / "mechanism_summary.json").exists()
    assert (analysis_dir / "cost_summary.json").exists()
    verified = verify_analysis(paths)
    assert verified["valid"] is True
    assert verified["files_verified"] == 15
    assert verified["inputs_verified"] == 20


def test_confirmatory_analysis_verification_detects_tampering(tmp_path: Path) -> None:
    paths = _prepared_analysis_artifacts(tmp_path)
    result = aggregate_study(paths, bootstrap_samples=10, bootstrap_seed=17)
    analysis_dir = Path(result["analysis_dir"])

    with (analysis_dir / "arm_summary.json").open("a", encoding="utf-8") as stream:
        stream.write("tampered\n")

    verified = verify_analysis(paths)
    assert verified["valid"] is False
    assert any(
        row["file"] == "arm_summary.json" and row["reason"] == "sha256_mismatch"
        for row in verified["mismatches"]
    )
