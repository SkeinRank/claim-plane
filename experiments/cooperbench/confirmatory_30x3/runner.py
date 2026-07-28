"""Protocol freezing and resumable execution for the confirmatory study."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from ..common import (
    CheckpointStore,
    PairRef,
    ProgressUnit,
    ResearchProgress,
    ShardSpec,
    create_run,
    load_study,
)
from ..common.identity import study_fingerprint
from ..environment import runtime_environment
from ..paper_6pair import runner as harness
from ..paper_6pair.coder import (
    AGENT_TRACE_LOGS,
    configure_workspace_root,
    create_worktree,
    remove_worktree,
    reset_agent_traces,
    run_official_feature_test,
)
from ..paper_6pair.dataset import (
    TaskInfo,
    benchmark_provenance,
    get_repo,
    load_tasks,
    read_gold_conflicts,
    validate_frozen_pairs,
    verify_pair_labels,
)
from ..paper_6pair.provider import STATS as CODER_PROVIDER_STATS, reset_provider_state
from .config import (
    CODER_SEEDS,
    N_PAIRS,
    REPOSITORIES,
    SHARD_COUNT,
    SHARD_SIZE,
    ConfirmatoryPaths,
    build_study,
)
from .plans import (
    freeze_plans,
    load_plan_bundle,
    planner_checkpoint_status,
    validate_plan_bundle,
)
from .selection import enumerate_pairs, freeze_gold_valid_pairs, select_candidate_stream


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _immutable_json(path: Path, payload: object) -> None:
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != payload:
            raise RuntimeError(
                f"immutable confirmatory artifact already exists with different content: {path}"
            )
        return
    _atomic_json(path, payload)


def _gold_feature(
    pair: PairRef,
    feature_id: int,
    *,
    task: TaskInfo,
    paths: ConfirmatoryPaths,
) -> dict[str, Any]:
    repo = get_repo(task.clone_url, task.base_commit, paths.repo_cache)
    safe_name = hashlib.sha256(
        f"confirmatory-gold|{pair.repo}|{pair.task_id}|{feature_id}".encode("utf-8")
    ).hexdigest()[:16]
    worktree = configure_workspace_root(paths.workspace_root) / f"gold-{safe_name}"
    result: dict[str, Any] = {
        "repo": pair.repo,
        "task": pair.task_id,
        "feature": feature_id,
        "gold_test_pass": False,
        "error": None,
        "test_log": None,
    }
    try:
        create_worktree(repo, worktree, task.base_commit)
        passed, log = run_official_feature_test(
            worktree,
            task.directory,
            task.features[feature_id],
            feature_patch=task.features[feature_id] / "feature.patch",
        )
        result["gold_test_pass"] = passed is True
        result["test_log"] = log
    except Exception as exc:  # pragma: no cover - environment dependent
        result["error"] = str(exc)[:3000]
    finally:
        remove_worktree(repo, worktree)
    return result


def load_confirmatory_study(paths: ConfirmatoryPaths):
    if not paths.study_file.exists():
        raise RuntimeError(
            "confirmatory protocol is not frozen; run `confirmatory prepare` first"
        )
    study = load_study(paths.study_file)
    if len(study.pairs) != N_PAIRS:
        raise RuntimeError("frozen confirmatory study no longer contains 30 pairs")
    if tuple(study.coder_seeds) != CODER_SEEDS:
        raise RuntimeError(
            "frozen confirmatory study coder seeds do not match protocol"
        )
    return study


def prepare_protocol(paths: ConfirmatoryPaths) -> dict[str, Any]:
    """Freeze the exact 30-pair set after CooperBench-native gold sanity."""
    paths.protocol_dir.mkdir(parents=True, exist_ok=True)
    paths.repo_cache.mkdir(parents=True, exist_ok=True)
    paths.workspace_root.mkdir(parents=True, exist_ok=True)
    configure_workspace_root(paths.workspace_root)

    if paths.study_file.exists():
        study = load_confirmatory_study(paths)
        tasks = validate_frozen_pairs(paths.dataset, study.pairs)
        verify_pair_labels(paths.dataset, study.pairs)
        return {
            "ready": True,
            "reused_frozen_protocol": True,
            "study_fingerprint": study_fingerprint(study),
            "pairs": len(study.pairs),
            "gold_conflicts": sum(pair.gold_conflict is True for pair in study.pairs),
            "gold_clean": sum(pair.gold_conflict is False for pair in study.pairs),
            "compatible_tasks": len(tasks),
            "protocol_dir": str(paths.protocol_dir),
            "benchmark": benchmark_provenance(paths.cooperbench, study.pairs),
        }

    tasks = load_tasks(paths.dataset, REPOSITORIES)
    conflicts = read_gold_conflicts(paths.dataset)
    all_pairs = enumerate_pairs(tasks, conflicts)
    initial, reserve = select_candidate_stream(all_pairs)
    if len(initial) != N_PAIRS:
        raise RuntimeError(
            f"dataset provides only {len(initial)} initial confirmatory candidates"
        )

    cache: dict[tuple[str, int, int], dict[str, Any]] = {}
    validity: dict[str, bool] = {}
    rows: list[dict[str, Any]] = []
    valid_conflict = 0
    valid_clean = 0

    for pair in (*initial, *reserve):
        if pair.key in validity:
            continue
        task = tasks[(pair.repo, pair.task_id)]
        records = []
        for feature_id in (pair.feature_a, pair.feature_b):
            key = (pair.repo, pair.task_id, feature_id)
            if key not in cache:
                cache[key] = _gold_feature(
                    pair,
                    feature_id,
                    task=task,
                    paths=paths,
                )
            records.append(cache[key])
        valid = bool(records[0]["gold_test_pass"] and records[1]["gold_test_pass"])
        validity[pair.key] = valid
        rows.append(
            {
                **pair.to_dict(),
                "pair": pair.key,
                "gold_a_pass": records[0]["gold_test_pass"],
                "gold_b_pass": records[1]["gold_test_pass"],
                "benchmark_harness_valid": valid,
                "gold_a_error": records[0]["error"],
                "gold_b_error": records[1]["error"],
                "gold_a_log": records[0]["test_log"],
                "gold_b_log": records[1]["test_log"],
            }
        )
        if valid and pair.gold_conflict is True and valid_conflict < 15:
            valid_conflict += 1
        elif valid and pair.gold_conflict is False and valid_clean < 15:
            valid_clean += 1
        if valid_conflict == 15 and valid_clean == 15:
            break

    frozen_pairs = freeze_gold_valid_pairs(initial, reserve, validity)
    study = build_study(frozen_pairs)

    _immutable_json(paths.selected_pairs_file, [pair.to_dict() for pair in initial])
    _immutable_json(paths.gold_sanity_file, rows)
    _immutable_json(
        paths.benchmark_pairs_file, [pair.to_dict() for pair in frozen_pairs]
    )
    _immutable_json(paths.study_file, study.to_dict())
    _immutable_json(
        paths.protocol_dir / "benchmark.json",
        benchmark_provenance(paths.cooperbench, frozen_pairs),
    )

    return {
        "ready": True,
        "reused_frozen_protocol": False,
        "study_fingerprint": study_fingerprint(study),
        "pairs": len(frozen_pairs),
        "gold_conflicts": sum(pair.gold_conflict is True for pair in frozen_pairs),
        "gold_clean": sum(pair.gold_conflict is False for pair in frozen_pairs),
        "compatible_tasks": len(tasks),
        "candidate_pairs": len(all_pairs),
        "gold_sanity_candidates_tested": len(rows),
        "protocol_dir": str(paths.protocol_dir),
    }


def freeze_protocol_plans(paths: ConfirmatoryPaths) -> dict[str, Any]:
    study = load_confirmatory_study(paths)
    tasks = validate_frozen_pairs(paths.dataset, study.pairs)
    verify_pair_labels(paths.dataset, study.pairs)
    manifest = freeze_plans(paths, study, tasks)
    return {
        "frozen": True,
        "study_fingerprint": study_fingerprint(study),
        "pair_count": manifest["pair_count"],
        "planner_freeze_seed": manifest["planner_freeze_seed"],
        "total_planner_logical_cost": manifest["total_planner_logical_cost"],
        "frozen_plans": str(paths.frozen_plans_file),
        "manifest": str(paths.frozen_plan_manifest_file),
    }


def contiguous_shard(
    pairs: tuple[PairRef, ...], shard_index: int
) -> tuple[PairRef, ...]:
    if not 1 <= shard_index <= SHARD_COUNT:
        raise ValueError(f"shard index must be within 1..{SHARD_COUNT}")
    start = (shard_index - 1) * SHARD_SIZE
    return tuple(pairs[start : start + SHARD_SIZE])


def _unit_id(pair: PairRef, arm: str) -> str:
    return f"{pair.key}/{arm}"


def _unit_filename(pair: PairRef, arm: str) -> str:
    digest = hashlib.sha256(_unit_id(pair, arm).encode("utf-8")).hexdigest()[:16]
    return f"{digest}-{arm}.json"


def _legacy_pair(pair: PairRef) -> dict[str, Any]:
    return {
        "repo": pair.repo,
        "tid": pair.task_id,
        "a": pair.feature_a,
        "b": pair.feature_b,
        "gold_conflict": pair.gold_conflict,
    }


def run_shard(
    paths: ConfirmatoryPaths,
    *,
    coder_seed: int,
    shard_index: int,
    repo_root: str | Path = ".",
    resume: bool = True,
) -> dict[str, Any]:
    """Run one of the nine frozen-plan coder-seed shards."""
    study = load_confirmatory_study(paths)
    if coder_seed not in study.coder_seeds:
        raise ValueError(f"coder seed must be one of {list(study.coder_seeds)}")
    shard_pairs = contiguous_shard(study.pairs, shard_index)
    if len(shard_pairs) != SHARD_SIZE:
        raise RuntimeError("confirmatory protocol no longer yields 10 pairs per shard")

    bundle = load_plan_bundle(paths.frozen_plans_file)
    validate_plan_bundle(bundle, study)
    frozen_pair_plans = bundle["pairs"]

    run, layout = create_run(
        study,
        coder_seed=coder_seed,
        artifact_root=paths.artifact_root,
        shard=ShardSpec(shard_index, SHARD_COUNT),
        repo_root=repo_root,
    )
    _immutable_json(
        layout.run_dir / "benchmark.json",
        benchmark_provenance(paths.cooperbench, study.pairs),
    )
    _immutable_json(layout.run_dir / "environment.json", runtime_environment())
    manifest_payload = json.loads(
        paths.frozen_plan_manifest_file.read_text(encoding="utf-8")
    )
    _immutable_json(
        layout.run_dir / "protocol.json",
        {
            "study_fingerprint": study_fingerprint(study),
            "coder_seed": coder_seed,
            "coder_seed_index": list(study.coder_seeds).index(coder_seed),
            "shard_index": shard_index,
            "shard_count": SHARD_COUNT,
            "pair_keys": [pair.key for pair in shard_pairs],
            "frozen_plan_manifest_sha256": hashlib.sha256(
                json.dumps(
                    manifest_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
        },
    )

    harness.configure_runtime(paths, planner=None, pairs=study.pairs)
    reset_provider_state()
    reset_agent_traces()
    traces_file = layout.traces_dir / "agent_traces.json"
    if resume and traces_file.exists():
        payload = json.loads(traces_file.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise RuntimeError("invalid persisted agent trace artifact")
        AGENT_TRACE_LOGS.extend(payload)

    checkpoint_store = CheckpointStore(layout.checkpoint_file)
    checkpoint = checkpoint_store.load()
    if not resume and checkpoint.completed_units:
        raise RuntimeError(
            "run already contains completed units; use resume or a different artifact root"
        )
    checkpoint = checkpoint.with_state("running")
    checkpoint_store.save(checkpoint)
    completed = set(checkpoint.completed_units) if resume else set()

    results: list[dict[str, Any]] = []
    if resume:
        for pair in shard_pairs:
            for arm in (item.value for item in study.arms):
                unit = _unit_id(pair, arm)
                if unit not in completed:
                    continue
                path = layout.results_dir / _unit_filename(pair, arm)
                if not path.exists():
                    raise RuntimeError(
                        f"checkpoint marks {unit} complete but result artifact is missing"
                    )
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise RuntimeError(f"invalid result artifact: {path}")
                results.append(payload)

    units = [
        ProgressUnit(
            unit_id=_unit_id(pair, arm_item.value),
            label=f"{pair.key} · {arm_item.value}",
            arm=arm_item.value,
        )
        for pair in shard_pairs
        for arm_item in study.arms
    ]
    historical_durations: dict[str, float] = {}
    for row in results:
        pair_name = str(row.get("pair", ""))
        arm = str(row.get("arm", ""))
        duration = float(row.get("wall_time_seconds", 0.0) or 0.0)
        if pair_name and arm and duration > 0:
            historical_durations[f"{pair_name}/{arm}"] = duration
    progress = ResearchProgress(
        f"confirmatory 30x3 · seed {coder_seed} · shard {shard_index}/{SHARD_COUNT}",
        units,
        completed_units=completed,
        historical_durations=historical_durations,
    )
    progress.start()
    progress.phase(1, 2, "execute frozen coder shard")

    repetition = list(study.coder_seeds).index(coder_seed)
    for pair in shard_pairs:
        for arm_item in study.arms:
            arm = arm_item.value
            unit = _unit_id(pair, arm)
            if unit in completed:
                continue
            progress.start_unit(unit)
            started = time.monotonic()
            try:
                row = harness.run_pair(
                    _legacy_pair(pair),
                    arm,
                    repetition,
                    coder_seed=coder_seed,
                    frozen_plans=frozen_pair_plans,
                )
            except Exception as exc:
                progress.fail_unit(unit, exc)
                raise
            wall_time = max(0.0, time.monotonic() - started)
            row["wall_time_seconds"] = wall_time
            row["coder_seed"] = coder_seed
            row["coder_seed_index"] = repetition
            row["shard_index"] = shard_index
            result_file = layout.results_dir / _unit_filename(pair, arm)
            _atomic_json(result_file, row)
            results.append(row)
            checkpoint = checkpoint.mark_completed(unit)
            checkpoint_store.save(checkpoint)
            completed.add(unit)
            _atomic_json(layout.run_dir / "results.json", results)
            _atomic_json(traces_file, AGENT_TRACE_LOGS)
            progress.complete_unit(
                unit,
                duration_seconds=wall_time,
                result="PASS" if bool(row.get("pair_pass")) else "FAIL",
                cost=float(row.get("logical_total_cost", 0.0) or 0.0),
            )

    progress.phase(2, 2, "finalize shard artifacts")
    result_index = {(str(row["pair"]), str(row["arm"])): row for row in results}
    ordered: list[dict[str, Any]] = []
    for pair in shard_pairs:
        for arm_item in study.arms:
            key = (pair.key, arm_item.value)
            completed_row = result_index.get(key)
            if completed_row is None:
                raise RuntimeError(
                    f"missing completed result for {pair.key}/{arm_item.value}"
                )
            ordered.append(completed_row)

    _atomic_json(layout.run_dir / "results.json", ordered)
    _atomic_json(traces_file, AGENT_TRACE_LOGS)
    _atomic_json(
        layout.run_dir / "provider_stats.json",
        {
            "planner": {
                "network_calls_during_coder_shard": 0,
                "note": "Planner v1 outputs were frozen once before coder-seed execution.",
            },
            "coder": {
                "api_attempts": CODER_PROVIDER_STATS.api_attempts,
                "http_200_responses": CODER_PROVIDER_STATS.http_200_responses,
                "accepted_responses": CODER_PROVIDER_STATS.accepted_responses,
                "actual_cost": CODER_PROVIDER_STATS.actual_cost,
                "cost_by_role": dict(CODER_PROVIDER_STATS.cost_by_role),
            },
        },
    )
    checkpoint_store.save(checkpoint.with_state("completed"))
    progress.finish(detail="shard complete")
    return {
        "run_id": run.run_id,
        "run_dir": str(layout.run_dir),
        "coder_seed": coder_seed,
        "coder_seed_index": repetition,
        "shard_index": shard_index,
        "shard_count": SHARD_COUNT,
        "pair_count": len(shard_pairs),
        "arm_executions": len(ordered),
        "complete": True,
    }


def study_status(paths: ConfirmatoryPaths) -> dict[str, Any]:
    """Report protocol, planner-freeze, and nine-shard completion state."""
    if not paths.study_file.exists():
        return {"prepared": False, "protocol_dir": str(paths.protocol_dir)}
    study = load_confirmatory_study(paths)
    planner_freeze = planner_checkpoint_status(
        paths.frozen_plans_file,
        study,
        manifest_exists=paths.frozen_plan_manifest_file.exists(),
    )
    frozen = planner_freeze["state"] == "complete"

    fingerprint = study_fingerprint(study)
    study_dir = paths.artifact_root / study.study_id / fingerprint[:12]
    shards: list[dict[str, Any]] = []
    completed_count = 0
    for seed in study.coder_seeds:
        for shard_index in range(1, SHARD_COUNT + 1):
            run_id = (
                f"{study.study_id}--seed-{seed}--shard-{shard_index:02d}-of-"
                f"{SHARD_COUNT:02d}--{fingerprint[:12]}"
            )
            checkpoint_file = study_dir / "runs" / run_id / "checkpoint.json"
            state = "not-started"
            completed_units = 0
            if checkpoint_file.exists():
                checkpoint = CheckpointStore(checkpoint_file).load()
                state = checkpoint.state
                completed_units = len(checkpoint.completed_units)
                if state == "completed" and completed_units == SHARD_SIZE * len(
                    study.arms
                ):
                    completed_count += 1
            shards.append(
                {
                    "coder_seed": seed,
                    "shard_index": shard_index,
                    "state": state,
                    "completed_units": completed_units,
                    "expected_units": SHARD_SIZE * len(study.arms),
                }
            )
    return {
        "prepared": True,
        "plans_frozen": frozen,
        "planner_freeze": planner_freeze,
        "study_fingerprint": fingerprint,
        "pairs": len(study.pairs),
        "coder_seeds": list(study.coder_seeds),
        "arms": [arm.value for arm in study.arms],
        "expected_arm_executions": len(study.pairs)
        * len(study.coder_seeds)
        * len(study.arms),
        "completed_shards": completed_count,
        "total_shards": len(study.coder_seeds) * SHARD_COUNT,
        "shards": shards,
    }
