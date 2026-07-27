"""Execution topology for the published six-pair CooperBench mechanism check."""

from __future__ import annotations

import copy
import hashlib
import json
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
)
from ..environment import runtime_environment
from ..planner_v1 import (
    OpenRouterClient,
    PLANNER_MODEL,
    PlannerExecutionError,
    PlannerV1,
    plan_fingerprint,
)
from ..planner_v1.policy import RUN_PLANNER_UNCERTAINTY_CALIBRATION
from .coder import (
    AGENT_TRACE_LOGS,
    AGENT_WORKSPACE_ROOT,
    AgentExecutionError,
    DynamicScopeBlocked,
    DynamicScopeController,
    configure_workspace_root,
    create_worktree,
    remove_worktree,
    reset_agent_traces,
    run_live_agent,
    run_official_feature_test,
)
from .config import (
    ARMS,
    CLAIM_PLANE_ARMS,
    FROZEN_PAIRS,
    LLM_SEEDS,
    PAPER_STUDY,
    REFERENCE_SUMMARY,
    RUN_PAIR_TESTS,
    PaperPaths,
)
from .dataset import (
    TaskInfo,
    benchmark_provenance,
    get_repo as prepare_repo,
    q,
    sh,
    stable_seed as stable_seed_ref,
    validate_frozen_pairs,
    verify_pair_labels,
)
from .provider import STATS as CODER_PROVIDER_STATS, reset_provider_state
from .scope import (
    admission_verdict,
    build_scope_plane,
    build_single_scope_plane,
    declared_committed_files,
    declared_contingent_files,
    declared_files,
    declared_scope_records,
    gate_decision_fingerprint,
    jaccard,
    scope_precision_recall,
)

tasks: dict[tuple[str, int], TaskInfo] = {}
_REPO_CACHE = Path(".claim-plane/cooperbench/repos").resolve()
_PLANNER: PlannerV1 | None = None
_PLAN_DIR: Path | None = None


def _pair_ref(pair: dict[str, Any]) -> PairRef:
    return PairRef(
        repo=str(pair["repo"]),
        task_id=int(pair["tid"]),
        feature_a=int(pair["a"]),
        feature_b=int(pair["b"]),
        gold_conflict=(
            None if pair.get("gold_conflict") is None else bool(pair["gold_conflict"])
        ),
    )


def stable_seed(pair: dict[str, Any], repetition: int, role: str, phase: str) -> int:
    return stable_seed_ref(_pair_ref(pair), repetition, role, phase)


def get_repo(url: str, base: str) -> Path:
    return prepare_repo(url, base, _REPO_CACHE)


def get_shared_calibrated_plan(
    cache_key: object, tree: Path, feature_dir: Path, *, seed: int
) -> dict[str, Any]:
    if _PLANNER is None:
        raise RuntimeError("paper-study planner is not configured")
    key = json.dumps(cache_key, sort_keys=True, default=str, ensure_ascii=False)
    cache_file: Path | None = None
    if _PLAN_DIR is not None:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
        cache_file = _PLAN_DIR / f"shared-plan-{digest}.json"
        if cache_file.exists():
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("cache_key") != key:
                raise RuntimeError(f"invalid persisted shared plan: {cache_file}")
            result = dict(payload["result"])
            result["shared_plan_cache_hit"] = True
            return result

    result = _PLANNER.get_shared_calibrated_plan(key, tree, feature_dir, seed=seed)
    if cache_file is not None:
        _immutable_json(cache_file, {"cache_key": key, "result": result})
    return result


def planner_stability_probe(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    # The published V8.5 run disabled the diagnostic stability probe.
    return {"rows": [], "diagnostic_cost": 0.0, "diagnostic_latency": 0.0}


def _stable_agent_seed(
    pair,
    repetition,
    agent,
    *,
    coder_seed=None,
):
    selected_seed = LLM_SEEDS[repetition] if coder_seed is None else int(coder_seed)
    return (
        stable_seed(
            pair,
            repetition,
            agent,
            "implementation",
        )
        + selected_seed
    )


def _new_agent_path(
    safe_name,
    label,
):
    return AGENT_WORKSPACE_ROOT / f"{safe_name}-{label}"


def _run_agent(
    repo,
    worktrees,
    *,
    path,
    base_commit,
    task_dir,
    feature_dir,
    seed,
    message,
    trace_id,
    mutation_guard=None,
    scope_base_commit=None,
):
    tree = create_worktree(
        repo,
        path,
        base_commit,
    )

    worktrees.append(tree)

    return (
        tree,
        run_live_agent(
            tree,
            task_dir,
            feature_dir,
            seed=seed,
            message=message,
            trace_id=trace_id,
            mutation_guard=mutation_guard,
            scope_base_commit=(scope_base_commit or base_commit),
        ),
    )


def _single_scope_controller(
    plan,
    *,
    intent_id,
    owner,
    agent,
    force_all_committed,
    scope_events,
    base_commit,
):
    plane = build_single_scope_plane(
        plan,
        intent_id=intent_id,
        owner=owner,
        force_all_committed=force_all_committed,
        base_commit=base_commit,
    )

    return DynamicScopeController(
        plane,
        intent_id,
        agent=agent,
        event_sink=scope_events,
    )


def _merge_parallel_worktrees(
    worktree_a,
    worktree_b,
):
    rc, out, err = sh(
        f"git -C {q(worktree_a)} fetch -q {q(worktree_b)} HEAD",
        timeout=120,
    )

    if rc != 0:
        raise RuntimeError(f"fetch B failed: {(out + err)[-1000:]}")

    rc, out, err = sh(
        f"git -C {q(worktree_a)} merge -q --no-edit FETCH_HEAD",
        timeout=120,
    )

    if rc != 0:
        sh(f"git -C {q(worktree_a)} merge --abort")

        return {
            "integration_success": False,
            "clean_merge": False,
            "final_tree": None,
        }

    return {
        "integration_success": True,
        "clean_merge": True,
        "final_tree": worktree_a,
    }


def _partial_cost(
    exc,
):
    partial = exc.partial_result or {}

    return {
        "logical_cost": float(
            partial.get(
                "logical_cost",
                0.0,
            )
            or 0.0
        ),
        "logical_latency": float(
            partial.get(
                "logical_latency",
                0.0,
            )
            or 0.0
        ),
        "steps_used": int(
            partial.get(
                "steps_used",
                0,
            )
            or 0
        ),
        "accepted_llm_responses": int(
            partial.get(
                "accepted_llm_responses",
                0,
            )
            or 0
        ),
    }


def _task_inputs(pair: dict[str, Any]) -> tuple[TaskInfo, Path, Path, str]:
    task = tasks[(str(pair["repo"]), int(pair["tid"]))]
    feature_a = task.features[int(pair["a"])]
    feature_b = task.features[int(pair["b"])]
    return task, feature_a, feature_b, task.base_commit


def run_pair(
    pair,
    arm,
    repetition,
    *,
    coder_seed=None,
    frozen_plans=None,
):
    assert arm in ARMS

    task, feature_a, feature_b, base = _task_inputs(pair)
    task_dir = task.directory

    repo = get_repo(
        task.clone_url,
        base,
    )

    pair_id = f"{pair['repo']}/task{pair['tid']}/feature{pair['a']}+feature{pair['b']}"

    run_id = f"{pair_id}|rep={repetition}|arm={arm}"

    safe_name = hashlib.sha256(run_id.encode()).hexdigest()[:16]

    seed_a = _stable_agent_seed(
        pair,
        repetition,
        "A",
        coder_seed=coder_seed,
    )

    seed_b = _stable_agent_seed(
        pair,
        repetition,
        "B",
        coder_seed=coder_seed,
    )

    plan_seed_a = stable_seed(
        pair,
        repetition,
        "A",
        "plan",
    )

    plan_seed_b = stable_seed(
        pair,
        repetition,
        "B",
        "plan",
    )

    record = {
        "pair": pair_id,
        "arm": arm,
        "repetition": repetition,
        "gold_conflict": pair["gold_conflict"],
        "initial_serialized": (arm == "always-serial"),
        "serialized": (arm == "always-serial"),
        "runtime_serialized": False,
        "dynamic_serialization_order": None,
        "dynamic_restart_count": 0,
        "dynamic_wasted_coder_cost": 0.0,
        "dynamic_wasted_coder_latency": 0.0,
        "dynamic_wasted_steps": 0,
        "gate_kind": None,
        "gate_reason": None,
        "gate_valid_for_accuracy": None,
        "effective_gate_kind": None,
        "plan_a_valid": None,
        "plan_b_valid": None,
        "plan_a": None,
        "plan_b": None,
        "primary_plan_a": None,
        "primary_plan_b": None,
        "plan_attempts_a": None,
        "plan_attempts_b": None,
        "planner_shared_plan_cache_hit_a": None,
        "planner_shared_plan_cache_hit_b": None,
        "planner_calibration_valid_a": None,
        "planner_calibration_valid_b": None,
        "planner_calibration_applied_a": None,
        "planner_calibration_applied_b": None,
        "planner_calibration_attempts_a": None,
        "planner_calibration_attempts_b": None,
        "planner_calibration_cost_a": 0.0,
        "planner_calibration_cost_b": 0.0,
        "planner_calibration_downgraded_a": 0,
        "planner_calibration_downgraded_b": 0,
        "planner_calibration_added_contingent_a": 0,
        "planner_calibration_added_contingent_b": 0,
        "planner_calibration_auto_added_a": 0,
        "planner_calibration_auto_added_b": 0,
        "planner_calibration_model_selected_a": 0,
        "planner_calibration_model_selected_b": 0,
        "planner_calibration_candidate_count_a": 0,
        "planner_calibration_candidate_count_b": 0,
        "planner_calibration_selected_kinds_a": None,
        "planner_calibration_selected_kinds_b": None,
        "primary_declared_regions_a": None,
        "primary_declared_regions_b": None,
        "declared_a": None,
        "declared_b": None,
        "declared_committed_a": None,
        "declared_committed_b": None,
        "declared_contingent_a": None,
        "declared_contingent_b": None,
        "declared_regions_a": None,
        "declared_regions_b": None,
        "written_a": None,
        "written_b": None,
        "written_regions_a": None,
        "written_regions_b": None,
        "decl_jaccard_a": None,
        "decl_jaccard_b": None,
        "scope_file_precision_a": None,
        "scope_file_recall_a": None,
        "scope_region_precision_a": None,
        "scope_region_recall_a": None,
        "scope_region_evaluable_a": None,
        "scope_file_precision_b": None,
        "scope_file_recall_b": None,
        "scope_region_precision_b": None,
        "scope_region_recall_b": None,
        "scope_region_evaluable_b": None,
        "scope_events": [],
        "scope_promotion_attempts": 0,
        "scope_promotions_succeeded": 0,
        "scope_promotions_rejected": 0,
        "scope_undeclared_blocks": 0,
        "scope_enforcement_failure": False,
        "agent_a_pass": None,
        "agent_b_pass": None,
        "agent_a_steps": None,
        "agent_b_steps": None,
        "agent_a_tool_errors": None,
        "agent_b_tool_errors": None,
        "agent_a_protocol_errors": None,
        "agent_b_protocol_errors": None,
        "agent_a_native_tool_actions": None,
        "agent_b_native_tool_actions": None,
        "agent_a_native_tool_batches": None,
        "agent_b_native_tool_batches": None,
        "agent_a_json_fallback_actions": None,
        "agent_b_json_fallback_actions": None,
        "agent_a_accepted_llm_responses": None,
        "agent_b_accepted_llm_responses": None,
        "agent_a_llm_cache_hits": None,
        "agent_b_llm_cache_hits": None,
        "agent_a_exploration_nudges": None,
        "agent_b_exploration_nudges": None,
        "agent_a_test_runs": None,
        "agent_b_test_runs": None,
        "agent_a_auto_test_runs": None,
        "agent_b_auto_test_runs": None,
        "agent_a_manual_test_runs": None,
        "agent_b_manual_test_runs": None,
        "agent_a_finish_blocked_count": None,
        "agent_b_finish_blocked_count": None,
        "agent_a_finish_reason": None,
        "agent_b_finish_reason": None,
        "agent_a_final_test_log": None,
        "agent_b_final_test_log": None,
        "single_a_pass": None,
        "single_b_pass": None,
        "coordination_eligible": None,
        "integration_success": False,
        "clean_merge": None,
        "tests_a": None,
        "tests_b": None,
        "pair_pass": False,
        "planner_cost": 0.0,
        "frozen_planner_cost_pair": 0.0,
        "logical_system_cost_estimate": 0.0,
        "frozen_plan_reused": bool(
            frozen_plans is not None and arm in CLAIM_PLANE_ARMS
        ),
        "coder_pre_failure_cost": 0.0,
        "coder_post_failure_cost": 0.0,
        "coder_cost": 0.0,
        "logical_total_cost": 0.0,
        "planner_latency_critical": 0.0,
        "coder_latency_critical": 0.0,
        "logical_llm_critical_path": 0.0,
        "planner_failure": False,
        "planner_error": None,
        "planner_provider_failures": None,
        "plan_a_fingerprint": None,
        "plan_b_fingerprint": None,
        "gate_fingerprint": None,
        "planner_stability_runs": None,
        "planner_stability_cost": 0.0,
        "planner_stability_decision_agreement": None,
        "planner_stability_plan_a_exact_agreement": None,
        "planner_stability_plan_b_exact_agreement": None,
        "planner_stability_mean_file_jaccard": None,
        "planner_stability_unique_gate_fingerprints": None,
        "agent_execution_failure": False,
        "harness_failure": False,
        "error": None,
    }

    worktrees = []
    result_a = None
    result_b = None
    final_tree = None

    try:
        plan_a = None
        plan_b = None

        # -------------------------------------------------------------
        # Shared planner declarations for both Claim Plane ablations.
        # -------------------------------------------------------------
        if arm in CLAIM_PLANE_ARMS:
            if frozen_plans is None:
                plan_a_result = get_shared_calibrated_plan(
                    (
                        pair_id,
                        repetition,
                        "A",
                        plan_seed_a,
                        PLANNER_MODEL,
                        bool(RUN_PLANNER_UNCERTAINTY_CALIBRATION),
                    ),
                    repo,
                    feature_a,
                    seed=plan_seed_a,
                )
                plan_b_result = get_shared_calibrated_plan(
                    (
                        pair_id,
                        repetition,
                        "B",
                        plan_seed_b,
                        PLANNER_MODEL,
                        bool(RUN_PLANNER_UNCERTAINTY_CALIBRATION),
                    ),
                    repo,
                    feature_b,
                    seed=plan_seed_b,
                )
                record["planner_cost"] = float(
                    plan_a_result["logical_cost"] + plan_b_result["logical_cost"]
                )
                record["planner_latency_critical"] = max(
                    float(plan_a_result["logical_latency"]),
                    float(plan_b_result["logical_latency"]),
                )
            else:
                pair_payload = frozen_plans.get(pair_id)
                if not isinstance(pair_payload, dict):
                    raise RuntimeError(
                        f"Frozen Planner v1 output missing for {pair_id}."
                    )
                plan_a_result = copy.deepcopy(pair_payload["A"])
                plan_b_result = copy.deepcopy(pair_payload["B"])
                plan_a_result["shared_plan_cache_hit"] = True
                plan_b_result["shared_plan_cache_hit"] = True
                record["frozen_planner_cost_pair"] = sum(
                    float(pair_payload[agent].get("logical_cost", 0.0) or 0.0)
                    for agent in ("A", "B")
                )
                plan_seed_a = int(
                    plan_a_result.get("confirmatory_plan_seed", plan_seed_a)
                )
                plan_seed_b = int(
                    plan_b_result.get("confirmatory_plan_seed", plan_seed_b)
                )

            plan_a = plan_a_result["plan"]
            plan_b = plan_b_result["plan"]
            record["plan_a"] = plan_a
            record["plan_b"] = plan_b
            record["primary_plan_a"] = plan_a_result.get("primary_plan")
            record["primary_plan_b"] = plan_b_result.get("primary_plan")

            record["plan_a_valid"] = plan_a_result["valid"]
            record["plan_b_valid"] = plan_b_result["valid"]
            record["plan_attempts_a"] = plan_a_result["attempts"]
            record["plan_attempts_b"] = plan_b_result["attempts"]

            for suffix, planner_result in [
                (
                    "a",
                    plan_a_result,
                ),
                (
                    "b",
                    plan_b_result,
                ),
            ]:
                record[f"planner_shared_plan_cache_hit_{suffix}"] = bool(
                    planner_result.get(
                        "shared_plan_cache_hit",
                        False,
                    )
                )

                record[f"planner_calibration_valid_{suffix}"] = planner_result.get(
                    "calibration_valid"
                )

                record[f"planner_calibration_applied_{suffix}"] = bool(
                    planner_result.get(
                        "calibration_applied",
                        False,
                    )
                )

                record[f"planner_calibration_attempts_{suffix}"] = planner_result.get(
                    "calibration_attempts",
                    0,
                )

                record[f"planner_calibration_cost_{suffix}"] = float(
                    planner_result.get(
                        "calibration_logical_cost",
                        0.0,
                    )
                    or 0.0
                )

                record[f"planner_calibration_downgraded_{suffix}"] = int(
                    planner_result.get(
                        "calibration_downgraded_count",
                        0,
                    )
                    or 0
                )

                record[f"planner_calibration_added_contingent_{suffix}"] = int(
                    planner_result.get(
                        "calibration_added_contingent_count",
                        0,
                    )
                    or 0
                )

                record[f"planner_calibration_auto_added_{suffix}"] = int(
                    planner_result.get(
                        "calibration_auto_added_count",
                        0,
                    )
                    or 0
                )

                record[f"planner_calibration_model_selected_{suffix}"] = int(
                    planner_result.get(
                        "calibration_model_selected_count",
                        0,
                    )
                    or 0
                )

                record[f"planner_calibration_candidate_count_{suffix}"] = int(
                    planner_result.get(
                        "calibration_candidate_count",
                        0,
                    )
                    or 0
                )

                record[f"planner_calibration_selected_kinds_{suffix}"] = (
                    planner_result.get(
                        "calibration_selected_candidate_kinds",
                        {},
                    )
                )

                record[f"primary_declared_regions_{suffix}"] = declared_scope_records(
                    planner_result.get(
                        "primary_plan",
                        {"files": []},
                    )
                )

            record["declared_a"] = declared_files(plan_a)
            record["declared_b"] = declared_files(plan_b)

            record["declared_committed_a"] = declared_committed_files(plan_a)
            record["declared_committed_b"] = declared_committed_files(plan_b)
            record["declared_contingent_a"] = declared_contingent_files(plan_a)
            record["declared_contingent_b"] = declared_contingent_files(plan_b)

            record["declared_regions_a"] = declared_scope_records(plan_a)
            record["declared_regions_b"] = declared_scope_records(plan_b)

            record["planner_provider_failures"] = plan_a_result.get(
                "provider_failures",
                [],
            ) + plan_b_result.get(
                "provider_failures",
                [],
            )

            force_all_committed = arm == "claim-plane-static"

            if not (plan_a_result["valid"] and plan_b_result["valid"]):
                verdict = {
                    "serialized": True,
                    "kind": "declaration_invalid",
                    "allowed": False,
                    "reason": ("At least one planner declaration was invalid."),
                    "valid_for_accuracy": False,
                }

            else:
                verdict = admission_verdict(
                    plan_a,
                    plan_b,
                    force_all_committed=(force_all_committed),
                )

            record["initial_serialized"] = verdict["serialized"]

            record["serialized"] = verdict["serialized"]

            record["gate_kind"] = verdict["kind"]

            record["effective_gate_kind"] = verdict["kind"]

            record["gate_reason"] = verdict["reason"]

            record["gate_valid_for_accuracy"] = verdict["valid_for_accuracy"]

            record["plan_a_fingerprint"] = plan_fingerprint(plan_a)

            record["plan_b_fingerprint"] = plan_fingerprint(plan_b)

            record["gate_fingerprint"] = gate_decision_fingerprint(
                plan_a,
                plan_b,
                verdict,
            )

            if plan_a_result["valid"] and plan_b_result["valid"]:
                stability = planner_stability_probe(
                    repo,
                    feature_a,
                    feature_b,
                    primary_plan_a=plan_a,
                    primary_plan_b=plan_b,
                    primary_verdict=verdict,
                    seed_a=plan_seed_a,
                    seed_b=plan_seed_b,
                    force_all_committed=(force_all_committed),
                )

                record["planner_stability_runs"] = stability["rows"]

                record["planner_stability_cost"] = stability["diagnostic_cost"]

        # -------------------------------------------------------------
        # Execute topology.
        # -------------------------------------------------------------
        if arm == "always-serial":
            tree_a, result_a = _run_agent(
                repo,
                worktrees,
                path=_new_agent_path(
                    safe_name,
                    "A",
                ),
                base_commit=base,
                task_dir=task_dir,
                feature_dir=feature_a,
                seed=seed_a,
                message="feature A",
                trace_id=(f"{run_id}|agent=A"),
            )

            tree_b, result_b = _run_agent(
                repo,
                worktrees,
                path=_new_agent_path(
                    safe_name,
                    "B",
                ),
                base_commit=result_a["head"],
                task_dir=task_dir,
                feature_dir=feature_b,
                seed=seed_b,
                message="feature B",
                trace_id=(f"{run_id}|agent=B"),
            )

            final_tree = tree_b
            record["integration_success"] = True
            record["clean_merge"] = True
            record["coder_latency_critical"] = (
                result_a["logical_latency"] + result_b["logical_latency"]
            )

        elif arm == "parallel":
            tree_a, result_a = _run_agent(
                repo,
                worktrees,
                path=_new_agent_path(
                    safe_name,
                    "A",
                ),
                base_commit=base,
                task_dir=task_dir,
                feature_dir=feature_a,
                seed=seed_a,
                message="feature A",
                trace_id=(f"{run_id}|agent=A"),
            )

            tree_b, result_b = _run_agent(
                repo,
                worktrees,
                path=_new_agent_path(
                    safe_name,
                    "B",
                ),
                base_commit=base,
                task_dir=task_dir,
                feature_dir=feature_b,
                seed=seed_b,
                message="feature B",
                trace_id=(f"{run_id}|agent=B"),
            )

            record["single_a_pass"] = result_a["feature_pass"]
            record["single_b_pass"] = result_b["feature_pass"]
            record["coordination_eligible"] = bool(
                result_a["feature_pass"] and result_b["feature_pass"]
            )

            merged = _merge_parallel_worktrees(
                tree_a,
                tree_b,
            )

            record["integration_success"] = merged["integration_success"]
            record["clean_merge"] = merged["clean_merge"]
            final_tree = merged["final_tree"]

            record["coder_latency_critical"] = max(
                result_a["logical_latency"],
                result_b["logical_latency"],
            )

        elif arm == "claim-plane-static":
            force_all_committed = True

            if record["initial_serialized"]:
                controller_a = _single_scope_controller(
                    plan_a,
                    intent_id="A",
                    owner="agent-a",
                    agent="A",
                    force_all_committed=(force_all_committed),
                    scope_events=record["scope_events"],
                    base_commit=base,
                )

                tree_a, result_a = _run_agent(
                    repo,
                    worktrees,
                    path=_new_agent_path(
                        safe_name,
                        "A",
                    ),
                    base_commit=base,
                    task_dir=task_dir,
                    feature_dir=feature_a,
                    seed=seed_a,
                    message="feature A",
                    trace_id=(f"{run_id}|agent=A"),
                    mutation_guard=None,
                )

                controller_b = _single_scope_controller(
                    plan_b,
                    intent_id="B",
                    owner="agent-b",
                    agent="B",
                    force_all_committed=(force_all_committed),
                    scope_events=record["scope_events"],
                    base_commit=base,
                )

                tree_b, result_b = _run_agent(
                    repo,
                    worktrees,
                    path=_new_agent_path(
                        safe_name,
                        "B",
                    ),
                    base_commit=result_a["head"],
                    task_dir=task_dir,
                    feature_dir=feature_b,
                    seed=seed_b,
                    message="feature B",
                    trace_id=(f"{run_id}|agent=B"),
                    mutation_guard=None,
                )

                final_tree = tree_b
                record["integration_success"] = True
                record["clean_merge"] = True
                record["coder_latency_critical"] = (
                    result_a["logical_latency"] + result_b["logical_latency"]
                )

            else:
                controller_a = _single_scope_controller(
                    plan_a,
                    intent_id="A",
                    owner="agent-a",
                    agent="A",
                    force_all_committed=True,
                    scope_events=record["scope_events"],
                    base_commit=base,
                )

                controller_b = _single_scope_controller(
                    plan_b,
                    intent_id="B",
                    owner="agent-b",
                    agent="B",
                    force_all_committed=True,
                    scope_events=record["scope_events"],
                    base_commit=base,
                )

                tree_a, result_a = _run_agent(
                    repo,
                    worktrees,
                    path=_new_agent_path(
                        safe_name,
                        "A",
                    ),
                    base_commit=base,
                    task_dir=task_dir,
                    feature_dir=feature_a,
                    seed=seed_a,
                    message="feature A",
                    trace_id=(f"{run_id}|agent=A"),
                    mutation_guard=None,
                )

                tree_b, result_b = _run_agent(
                    repo,
                    worktrees,
                    path=_new_agent_path(
                        safe_name,
                        "B",
                    ),
                    base_commit=base,
                    task_dir=task_dir,
                    feature_dir=feature_b,
                    seed=seed_b,
                    message="feature B",
                    trace_id=(f"{run_id}|agent=B"),
                    mutation_guard=None,
                )

                merged = _merge_parallel_worktrees(
                    tree_a,
                    tree_b,
                )

                record["integration_success"] = merged["integration_success"]
                record["clean_merge"] = merged["clean_merge"]
                final_tree = merged["final_tree"]

                record["coder_latency_critical"] = max(
                    result_a["logical_latency"],
                    result_b["logical_latency"],
                )

        elif arm == "claim-plane-dynamic":
            if record["initial_serialized"]:
                controller_a = _single_scope_controller(
                    plan_a,
                    intent_id="A",
                    owner="agent-a",
                    agent="A",
                    force_all_committed=False,
                    scope_events=record["scope_events"],
                    base_commit=base,
                )

                tree_a, result_a = _run_agent(
                    repo,
                    worktrees,
                    path=_new_agent_path(
                        safe_name,
                        "A",
                    ),
                    base_commit=base,
                    task_dir=task_dir,
                    feature_dir=feature_a,
                    seed=seed_a,
                    message="feature A",
                    trace_id=(f"{run_id}|agent=A"),
                    mutation_guard=(controller_a.before_mutation),
                    scope_base_commit=base,
                )

                controller_b = _single_scope_controller(
                    plan_b,
                    intent_id="B",
                    owner="agent-b",
                    agent="B",
                    force_all_committed=False,
                    scope_events=record["scope_events"],
                    base_commit=base,
                )

                tree_b, result_b = _run_agent(
                    repo,
                    worktrees,
                    path=_new_agent_path(
                        safe_name,
                        "B",
                    ),
                    base_commit=result_a["head"],
                    task_dir=task_dir,
                    feature_dir=feature_b,
                    seed=seed_b,
                    message="feature B",
                    trace_id=(f"{run_id}|agent=B"),
                    mutation_guard=(controller_b.before_mutation),
                    scope_base_commit=base,
                )

                final_tree = tree_b
                record["integration_success"] = True
                record["clean_merge"] = True
                record["coder_latency_critical"] = (
                    result_a["logical_latency"] + result_b["logical_latency"]
                )

            else:
                session = build_scope_plane(
                    plan_a,
                    plan_b,
                    force_all_committed=False,
                    base_commit=base,
                )

                controller_a = DynamicScopeController(
                    session["plane"],
                    "A",
                    agent="A",
                    event_sink=record["scope_events"],
                )

                controller_b = DynamicScopeController(
                    session["plane"],
                    "B",
                    agent="B",
                    event_sink=record["scope_events"],
                )

                try:
                    tree_a, result_a = _run_agent(
                        repo,
                        worktrees,
                        path=_new_agent_path(
                            safe_name,
                            "A0",
                        ),
                        base_commit=base,
                        task_dir=task_dir,
                        feature_dir=feature_a,
                        seed=seed_a,
                        message="feature A",
                        trace_id=(f"{run_id}|agent=A|attempt=parallel"),
                        mutation_guard=(controller_a.before_mutation),
                        scope_base_commit=base,
                    )

                except DynamicScopeBlocked as exc:
                    if exc.block_type != "promotion_rejected":
                        raise

                    wasted = _partial_cost(exc)

                    record["dynamic_wasted_coder_cost"] += wasted["logical_cost"]
                    record["dynamic_wasted_coder_latency"] += wasted["logical_latency"]
                    record["dynamic_wasted_steps"] += wasted["steps_used"]

                    record["runtime_serialized"] = True
                    record["serialized"] = True
                    record["effective_gate_kind"] = "runtime_serialize"
                    record["dynamic_serialization_order"] = "B->A"
                    record["dynamic_restart_count"] += 1

                    controller_b_serial = _single_scope_controller(
                        plan_b,
                        intent_id="B",
                        owner="agent-b",
                        agent="B",
                        force_all_committed=False,
                        scope_events=record["scope_events"],
                        base_commit=base,
                    )

                    tree_b, result_b = _run_agent(
                        repo,
                        worktrees,
                        path=_new_agent_path(
                            safe_name,
                            "B1",
                        ),
                        base_commit=base,
                        task_dir=task_dir,
                        feature_dir=feature_b,
                        seed=seed_b,
                        message="feature B",
                        trace_id=(f"{run_id}|agent=B|attempt=serial"),
                        mutation_guard=(controller_b_serial.before_mutation),
                    )

                    controller_a_serial = _single_scope_controller(
                        plan_a,
                        intent_id="A",
                        owner="agent-a",
                        agent="A",
                        force_all_committed=False,
                        scope_events=record["scope_events"],
                        base_commit=base,
                    )

                    tree_a, result_a = _run_agent(
                        repo,
                        worktrees,
                        path=_new_agent_path(
                            safe_name,
                            "A1",
                        ),
                        base_commit=result_b["head"],
                        task_dir=task_dir,
                        feature_dir=feature_a,
                        seed=seed_a,
                        message="feature A",
                        trace_id=(f"{run_id}|agent=A|attempt=serial-restart"),
                        mutation_guard=(controller_a_serial.before_mutation),
                    )

                    final_tree = tree_a
                    record["integration_success"] = True
                    record["clean_merge"] = True
                    record["coder_latency_critical"] = (
                        max(
                            wasted["logical_latency"],
                            result_b["logical_latency"],
                        )
                        + result_a["logical_latency"]
                    )

                else:
                    try:
                        tree_b, result_b = _run_agent(
                            repo,
                            worktrees,
                            path=_new_agent_path(
                                safe_name,
                                "B0",
                            ),
                            base_commit=base,
                            task_dir=task_dir,
                            feature_dir=feature_b,
                            seed=seed_b,
                            message="feature B",
                            trace_id=(f"{run_id}|agent=B|attempt=parallel"),
                            mutation_guard=(controller_b.before_mutation),
                        )

                    except DynamicScopeBlocked as exc:
                        if exc.block_type != "promotion_rejected":
                            raise

                        wasted = _partial_cost(exc)

                        record["dynamic_wasted_coder_cost"] += wasted["logical_cost"]
                        record["dynamic_wasted_coder_latency"] += wasted[
                            "logical_latency"
                        ]
                        record["dynamic_wasted_steps"] += wasted["steps_used"]

                        record["runtime_serialized"] = True
                        record["serialized"] = True
                        record["effective_gate_kind"] = "runtime_serialize"
                        record["dynamic_serialization_order"] = "A->B"
                        record["dynamic_restart_count"] += 1

                        controller_b_serial = _single_scope_controller(
                            plan_b,
                            intent_id="B",
                            owner="agent-b",
                            agent="B",
                            force_all_committed=False,
                            scope_events=record["scope_events"],
                            base_commit=base,
                        )

                        tree_b, result_b = _run_agent(
                            repo,
                            worktrees,
                            path=_new_agent_path(
                                safe_name,
                                "B1",
                            ),
                            base_commit=result_a["head"],
                            task_dir=task_dir,
                            feature_dir=feature_b,
                            seed=seed_b,
                            message="feature B",
                            trace_id=(f"{run_id}|agent=B|attempt=serial-restart"),
                            mutation_guard=(controller_b_serial.before_mutation),
                            scope_base_commit=base,
                        )

                        final_tree = tree_b
                        record["integration_success"] = True
                        record["clean_merge"] = True
                        record["coder_latency_critical"] = (
                            max(
                                result_a["logical_latency"],
                                wasted["logical_latency"],
                            )
                            + result_b["logical_latency"]
                        )

                    else:
                        merged = _merge_parallel_worktrees(
                            tree_a,
                            tree_b,
                        )

                        record["integration_success"] = merged["integration_success"]
                        record["clean_merge"] = merged["clean_merge"]
                        final_tree = merged["final_tree"]

                        record["coder_latency_critical"] = max(
                            result_a["logical_latency"],
                            result_b["logical_latency"],
                        )

        # -------------------------------------------------------------
        # Persist scope-event metrics.
        # -------------------------------------------------------------
        record["scope_promotion_attempts"] = sum(
            event["event_type"] == "promotion_attempted"
            for event in record["scope_events"]
        )

        record["scope_promotions_succeeded"] = sum(
            event["event_type"] == "promotion_succeeded"
            for event in record["scope_events"]
        )

        record["scope_promotions_rejected"] = sum(
            event["event_type"] == "promotion_rejected"
            for event in record["scope_events"]
        )

        record["scope_undeclared_blocks"] = sum(
            event["event_type"] == "undeclared_scope_blocked"
            for event in record["scope_events"]
        )

        # -------------------------------------------------------------
        # Persist final per-agent metrics.
        # -------------------------------------------------------------
        record["written_a"] = result_a["written_files"]
        record["written_b"] = result_b["written_files"]

        record["written_regions_a"] = result_a["written_regions"]
        record["written_regions_b"] = result_b["written_regions"]

        for suffix, result in [
            (
                "a",
                result_a,
            ),
            (
                "b",
                result_b,
            ),
        ]:
            record[f"agent_{suffix}_pass"] = result["feature_pass"]
            record[f"agent_{suffix}_steps"] = result["steps_used"]
            record[f"agent_{suffix}_tool_errors"] = result["tool_errors"]
            record[f"agent_{suffix}_protocol_errors"] = result["protocol_errors"]
            record[f"agent_{suffix}_native_tool_actions"] = result[
                "native_tool_actions"
            ]
            record[f"agent_{suffix}_native_tool_batches"] = result[
                "native_tool_batches"
            ]
            record[f"agent_{suffix}_json_fallback_actions"] = result[
                "json_fallback_actions"
            ]
            record[f"agent_{suffix}_accepted_llm_responses"] = result[
                "accepted_llm_responses"
            ]
            record[f"agent_{suffix}_llm_cache_hits"] = result["llm_cache_hits"]
            record[f"agent_{suffix}_exploration_nudges"] = result["exploration_nudges"]
            record[f"agent_{suffix}_test_runs"] = result["test_runs"]
            record[f"agent_{suffix}_auto_test_runs"] = result["auto_test_runs"]
            record[f"agent_{suffix}_manual_test_runs"] = result["manual_test_runs"]
            record[f"agent_{suffix}_finish_blocked_count"] = result[
                "finish_blocked_count"
            ]
            record[f"agent_{suffix}_finish_reason"] = result["finish_reason"]
            record[f"agent_{suffix}_final_test_log"] = result["final_test_log"]

        record["coder_pre_failure_cost"] = (
            result_a["pre_failure_cost"] + result_b["pre_failure_cost"]
        )

        record["coder_post_failure_cost"] = (
            result_a["post_failure_cost"] + result_b["post_failure_cost"]
        )

        record["coder_cost"] = (
            result_a["logical_cost"]
            + result_b["logical_cost"]
            + record["dynamic_wasted_coder_cost"]
        )

        # -------------------------------------------------------------
        # Planner scope quality for both Claim Plane arms.
        # -------------------------------------------------------------
        if arm in CLAIM_PLANE_ARMS:
            for suffix in [
                "a",
                "b",
            ]:
                written_files = record[f"written_{suffix}"] or []

                if not written_files:
                    continue

                record[f"decl_jaccard_{suffix}"] = jaccard(
                    record[f"declared_{suffix}"],
                    written_files,
                )

            scope_a = None
            scope_b = None

            if record["written_a"]:
                scope_a = scope_precision_recall(
                    record["declared_regions_a"],
                    record["written_regions_a"],
                    region_evaluable=True,
                )

            same_written_file = bool(
                set(record["written_a"] or []) & set(record["written_b"] or [])
            )

            if record["written_b"]:
                scope_b = scope_precision_recall(
                    record["declared_regions_b"],
                    record["written_regions_b"],
                    region_evaluable=not (record["serialized"] and same_written_file),
                )

            for suffix, metrics in [
                (
                    "a",
                    scope_a,
                ),
                (
                    "b",
                    scope_b,
                ),
            ]:
                if metrics is None:
                    continue

                record[f"scope_file_precision_{suffix}"] = metrics["file_precision"]
                record[f"scope_file_recall_{suffix}"] = metrics["file_recall"]
                record[f"scope_region_precision_{suffix}"] = metrics["region_precision"]
                record[f"scope_region_recall_{suffix}"] = metrics["region_recall"]
                record[f"scope_region_evaluable_{suffix}"] = metrics["region_evaluable"]

        # -------------------------------------------------------------
        # Final integrated pair tests.
        # -------------------------------------------------------------
        if record["integration_success"] and RUN_PAIR_TESTS:
            (
                record["tests_a"],
                record["tests_a_log"],
            ) = run_official_feature_test(
                final_tree,
                task_dir,
                feature_a,
            )

            (
                record["tests_b"],
                record["tests_b_log"],
            ) = run_official_feature_test(
                final_tree,
                task_dir,
                feature_b,
            )

            record["pair_pass"] = bool(record["tests_a"] and record["tests_b"])

        elif not RUN_PAIR_TESTS:
            record["pair_pass"] = None

        record["logical_total_cost"] = record["planner_cost"] + record["coder_cost"]

        record["logical_llm_critical_path"] = (
            record["planner_latency_critical"] + record["coder_latency_critical"]
        )

    except PlannerExecutionError as exc:
        record["planner_failure"] = True
        record["planner_error"] = str(exc)[:3000]
        record["planner_provider_failures"] = exc.provider_failures
        record["pair_pass"] = False
        record["error"] = "PLANNER FAILURE: " + str(exc)[:2800]

    except DynamicScopeBlocked as exc:
        partial = _partial_cost(exc)

        completed_results = [
            result
            for result in [
                result_a,
                result_b,
            ]
            if result is not None
        ]

        completed_cost = sum(result["logical_cost"] for result in completed_results)

        completed_latency = sum(
            result["logical_latency"] for result in completed_results
        )

        record["dynamic_wasted_coder_cost"] += partial["logical_cost"]
        record["dynamic_wasted_coder_latency"] += partial["logical_latency"]
        record["dynamic_wasted_steps"] += partial["steps_used"]

        record["coder_cost"] = completed_cost + partial["logical_cost"]

        record["coder_latency_critical"] = (
            completed_latency + partial["logical_latency"]
        )

        record["scope_enforcement_failure"] = True
        record["pair_pass"] = False
        record["error"] = "SCOPE ENFORCEMENT BLOCK: " + str(exc)[:2800]

        record["scope_promotion_attempts"] = sum(
            event["event_type"] == "promotion_attempted"
            for event in record["scope_events"]
        )

        record["scope_promotions_succeeded"] = sum(
            event["event_type"] == "promotion_succeeded"
            for event in record["scope_events"]
        )

        record["scope_promotions_rejected"] = sum(
            event["event_type"] == "promotion_rejected"
            for event in record["scope_events"]
        )

        record["scope_undeclared_blocks"] = sum(
            event["event_type"] == "undeclared_scope_blocked"
            for event in record["scope_events"]
        )

    except AgentExecutionError as exc:
        completed_results = [
            result
            for result in [
                result_a,
                result_b,
            ]
            if result is not None
        ]

        record["coder_pre_failure_cost"] += (
            sum(result["pre_failure_cost"] for result in completed_results)
            + exc.pre_failure_cost
        )

        record["coder_post_failure_cost"] += (
            sum(result["post_failure_cost"] for result in completed_results)
            + exc.post_failure_cost
        )

        record["coder_cost"] += (
            sum(result["logical_cost"] for result in completed_results)
            + exc.logical_cost
        )

        record["coder_latency_critical"] = (
            sum(result["logical_latency"] for result in completed_results)
            + exc.logical_latency
        )

        record["agent_execution_failure"] = True
        record["pair_pass"] = False
        record["error"] = str(exc)[:3000]

    except Exception as exc:
        record["harness_failure"] = True
        record["pair_pass"] = False
        record["error"] = str(exc)[:3000]

    finally:
        record["logical_total_cost"] = record["planner_cost"] + record["coder_cost"]
        record["logical_system_cost_estimate"] = record["coder_cost"] + (
            record["frozen_planner_cost_pair"]
            if record["frozen_plan_reused"]
            else record["planner_cost"]
        )

        if record["logical_llm_critical_path"] == 0.0:
            record["logical_llm_critical_path"] = (
                record["planner_latency_critical"]
                + record["coder_latency_critical"]
                + record["dynamic_wasted_coder_latency"]
            )

        for worktree in reversed(worktrees):
            remove_worktree(
                repo,
                worktree,
            )

    return record


def _atomic_json(path: str | Path, payload: object) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def _immutable_json(path: str | Path, payload: object) -> None:
    target = Path(path)
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing != payload:
            raise RuntimeError(
                f"immutable paper-study artifact already exists with different content: {target}"
            )
        return
    _atomic_json(target, payload)


def _legacy_pair(pair: PairRef) -> dict[str, Any]:
    return {
        "repo": pair.repo,
        "tid": pair.task_id,
        "a": pair.feature_a,
        "b": pair.feature_b,
        "gold_conflict": pair.gold_conflict,
    }


def _unit_id(pair: PairRef, arm: str) -> str:
    return f"{pair.key}/{arm}"


def _unit_filename(pair: PairRef, arm: str) -> str:
    digest = hashlib.sha256(_unit_id(pair, arm).encode("utf-8")).hexdigest()[:16]
    return f"{digest}-{arm}.json"


def _gold_sanity_record(
    pair: PairRef,
    feature_id: int,
    *,
    paths: PaperPaths,
) -> dict[str, Any]:
    task = tasks[(pair.repo, pair.task_id)]
    feature_dir = task.features[feature_id]
    repo = prepare_repo(task.clone_url, task.base_commit, paths.repo_cache)
    safe_name = hashlib.sha256(
        f"gold|{pair.repo}|{pair.task_id}|{feature_id}".encode("utf-8")
    ).hexdigest()[:16]
    worktree = AGENT_WORKSPACE_ROOT / f"gold-{safe_name}"
    result = {
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
            feature_dir,
            feature_patch=feature_dir / "feature.patch",
        )
        result["gold_test_pass"] = passed is True
        result["test_log"] = log
    except Exception as exc:  # pragma: no cover - environment dependent
        result["error"] = str(exc)[:3000]
    finally:
        remove_worktree(repo, worktree)
    return result


def run_gold_sanity(
    paths: PaperPaths, *, progress: ResearchProgress | None = None
) -> list[dict[str, Any]]:
    """Validate all frozen features through CooperBench's own task runner."""
    configure_runtime(paths, planner=None)
    cache: dict[tuple[str, int, int], dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for pair_index, pair in enumerate(FROZEN_PAIRS, start=1):
        if progress is not None:
            progress.activity("gold", pair_index, len(FROZEN_PAIRS), pair.key)
        records = []
        for feature_id in (pair.feature_a, pair.feature_b):
            key = (pair.repo, pair.task_id, feature_id)
            if key not in cache:
                cache[key] = _gold_sanity_record(pair, feature_id, paths=paths)
            records.append(cache[key])
        row = {
            **_legacy_pair(pair),
            "gold_a_pass": records[0]["gold_test_pass"],
            "gold_b_pass": records[1]["gold_test_pass"],
            "benchmark_harness_valid": bool(
                records[0]["gold_test_pass"] and records[1]["gold_test_pass"]
            ),
            "gold_a_error": records[0]["error"],
            "gold_b_error": records[1]["error"],
            "gold_a_log": records[0]["test_log"],
            "gold_b_log": records[1]["test_log"],
        }
        rows.append(row)
    return rows


def configure_runtime(
    paths: Any,
    planner: PlannerV1 | None,
    *,
    pairs=FROZEN_PAIRS,
) -> None:
    global tasks, _REPO_CACHE, _PLANNER, _PLAN_DIR, AGENT_WORKSPACE_ROOT
    paths.repo_cache.mkdir(parents=True, exist_ok=True)
    paths.workspace_root.mkdir(parents=True, exist_ok=True)
    tasks = validate_frozen_pairs(paths.dataset, pairs)
    verify_pair_labels(paths.dataset, pairs)
    _REPO_CACHE = paths.repo_cache
    _PLANNER = planner
    AGENT_WORKSPACE_ROOT = configure_workspace_root(paths.workspace_root)


def aggregate_results(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate the paper's primary mechanism counts without third-party packages."""
    summary: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        rows = [row for row in results if row.get("arm") == arm]
        n = len(rows)
        logical_costs = [
            float(row.get("logical_total_cost", 0.0) or 0.0) for row in rows
        ]
        summary[arm] = {
            "n": n,
            "pair_pass": sum(bool(row.get("pair_pass")) for row in rows),
            "pair_pass_rate": (
                sum(bool(row.get("pair_pass")) for row in rows) / n if n else None
            ),
            "integration_success": sum(
                bool(row.get("integration_success")) for row in rows
            ),
            "integration_success_rate": (
                sum(bool(row.get("integration_success")) for row in rows) / n
                if n
                else None
            ),
            "initial_serialized": sum(
                bool(row.get("initial_serialized")) for row in rows
            ),
            "initial_serialization_rate": (
                sum(bool(row.get("initial_serialized")) for row in rows) / n
                if n
                else None
            ),
            "effective_serialized": sum(bool(row.get("serialized")) for row in rows),
            "effective_serialization_rate": (
                sum(bool(row.get("serialized")) for row in rows) / n if n else None
            ),
            "promotions": sum(
                int(row.get("scope_promotions_succeeded", 0) or 0) for row in rows
            ),
            "rejected_promotions": sum(
                int(row.get("scope_promotions_rejected", 0) or 0) for row in rows
            ),
            "undeclared_blocks": sum(
                int(row.get("scope_undeclared_blocks", 0) or 0) for row in rows
            ),
            "planner_failures": sum(bool(row.get("planner_failure")) for row in rows),
            "scope_enforcement_failures": sum(
                bool(row.get("scope_enforcement_failure")) for row in rows
            ),
            "agent_execution_failures": sum(
                bool(row.get("agent_execution_failure")) for row in rows
            ),
            "harness_failures": sum(bool(row.get("harness_failure")) for row in rows),
            "mean_logical_cost": sum(logical_costs) / n if n else None,
        }
    return summary


def compare_reference(summary: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compare deterministic mechanism counts with the published V8.5 run."""
    fields = (
        "n",
        "pair_pass",
        "integration_success",
        "initial_serialized",
        "promotions",
        "undeclared_blocks",
    )
    differences: list[dict[str, Any]] = []
    for arm, expected in REFERENCE_SUMMARY.items():
        observed = summary.get(arm, {})
        for field in fields:
            if observed.get(field) != expected.get(field):
                differences.append(
                    {
                        "arm": arm,
                        "field": field,
                        "expected": expected.get(field),
                        "observed": observed.get(field),
                    }
                )
    return {
        "matches_published_mechanism_counts": not differences,
        "differences": differences,
        "note": (
            "Model/provider nondeterminism can change live outcomes even with frozen seeds. "
            "The comparison is a regression aid, not an assertion that a future API run must "
            "reproduce every stochastic outcome byte-for-byte."
        ),
    }


def _write_summary_csv(path: Path, summary: dict[str, dict[str, Any]]) -> None:
    import csv

    fields = [
        "arm",
        "n",
        "pair_pass",
        "pair_pass_rate",
        "integration_success",
        "integration_success_rate",
        "initial_serialized",
        "initial_serialization_rate",
        "effective_serialized",
        "effective_serialization_rate",
        "promotions",
        "rejected_promotions",
        "undeclared_blocks",
        "planner_failures",
        "scope_enforcement_failures",
        "agent_execution_failures",
        "harness_failures",
        "mean_logical_cost",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for arm in ARMS:
            writer.writerow({"arm": arm, **summary.get(arm, {})})
    temporary.replace(path)


def _load_completed_results(
    layout: Any, completed_units: set[str]
) -> list[dict[str, Any]]:
    loaded: list[dict[str, Any]] = []
    for pair in FROZEN_PAIRS:
        for arm in ARMS:
            unit = _unit_id(pair, arm)
            if unit not in completed_units:
                continue
            path = layout.results_dir / _unit_filename(pair, arm)
            if not path.exists():
                raise RuntimeError(
                    f"checkpoint marks {unit} completed but result artifact is missing: {path}"
                )
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError(f"invalid result artifact: {path}")
            loaded.append(payload)
    return loaded


def run_paper_study(
    paths: PaperPaths,
    *,
    repo_root: str | Path = ".",
    resume: bool = True,
    skip_gold_sanity: bool = False,
) -> dict[str, Any]:
    """Run all 24 arm executions of the published six-pair study."""
    if LLM_SEEDS != (101,):
        raise RuntimeError("paper study seed declaration changed unexpectedly")

    run, layout = create_run(
        PAPER_STUDY,
        coder_seed=101,
        artifact_root=paths.artifact_root,
        shard=ShardSpec(1, 1),
        repo_root=repo_root,
    )
    global _PLAN_DIR
    _PLAN_DIR = layout.plans_dir
    _immutable_json(
        layout.run_dir / "benchmark.json",
        benchmark_provenance(paths.cooperbench),
    )
    _immutable_json(
        layout.run_dir / "environment.json",
        runtime_environment(),
    )

    reset_agent_traces()
    reset_provider_state()
    traces_file = layout.traces_dir / "agent_traces.json"
    if resume and traces_file.exists():
        persisted_traces = json.loads(traces_file.read_text(encoding="utf-8"))
        if not isinstance(persisted_traces, list):
            raise RuntimeError("invalid persisted agent trace artifact")
        AGENT_TRACE_LOGS.extend(persisted_traces)

    checkpoint_store = CheckpointStore(layout.checkpoint_file)
    checkpoint = checkpoint_store.load()
    if not resume and checkpoint.completed_units:
        raise RuntimeError(
            "run already contains completed units; use --resume or choose a different artifact root"
        )
    completed = set(checkpoint.completed_units) if resume else set()
    results = _load_completed_results(layout, completed) if resume else []

    units = [
        ProgressUnit(
            unit_id=_unit_id(pair, arm),
            label=f"{pair.key} · {arm}",
            arm=arm,
        )
        for pair in FROZEN_PAIRS
        for arm in ARMS
    ]
    historical_durations: dict[str, float] = {}
    for row in results:
        pair_name = str(row.get("pair", ""))
        arm = str(row.get("arm", ""))
        duration = float(row.get("wall_time_seconds", 0.0) or 0.0)
        if pair_name and arm and duration > 0:
            historical_durations[f"{pair_name}/{arm}"] = duration

    progress = ResearchProgress(
        "paper 6-pair reproduction · seed 101",
        units,
        completed_units=completed,
        historical_durations=historical_durations,
    )
    progress.start()

    try:
        progress.phase(1, 4, "validate frozen CooperBench inputs")
        configure_runtime(paths, planner=None)

        gold_file = layout.logs_dir / "gold_sanity.json"
        if skip_gold_sanity:
            progress.phase(2, 4, "benchmark gold sanity", detail="skipped by request")
            gold_rows: list[dict[str, Any]] = []
        elif resume and gold_file.exists():
            progress.phase(2, 4, "benchmark gold sanity", detail="cached")
            payload = json.loads(gold_file.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise RuntimeError("invalid persisted gold sanity artifact")
            gold_rows = payload
        else:
            progress.phase(2, 4, "benchmark gold sanity", detail="6 frozen pairs")
            gold_rows = run_gold_sanity(paths, progress=progress)
            _atomic_json(gold_file, gold_rows)

        if gold_rows and not all(
            bool(row.get("benchmark_harness_valid")) for row in gold_rows
        ):
            invalid = [
                f"{row['repo']}/task{row['tid']}/feature{row['a']}+feature{row['b']}"
                for row in gold_rows
                if not row.get("benchmark_harness_valid")
            ]
            raise RuntimeError(
                "Gold sanity failed; stopping before paid model calls: "
                + ", ".join(invalid)
            )

        planner_provider = OpenRouterClient()
        planner = PlannerV1(planner_provider)
        configure_runtime(paths, planner=planner)

        checkpoint = checkpoint.with_state("running")
        checkpoint_store.save(checkpoint)
        progress.phase(
            3,
            4,
            "execute frozen study matrix",
            detail=f"{len(completed)}/24 durable executions already complete",
        )

        for pair in FROZEN_PAIRS:
            legacy = _legacy_pair(pair)
            for arm in ARMS:
                unit = _unit_id(pair, arm)
                if unit in completed:
                    continue
                progress.start_unit(unit)
                started = time.monotonic()
                try:
                    row = run_pair(legacy, arm, 0)
                except Exception as exc:
                    progress.fail_unit(unit, exc)
                    raise
                wall_time = max(0.0, time.monotonic() - started)
                row["wall_time_seconds"] = wall_time
                result_file = layout.results_dir / _unit_filename(pair, arm)
                _atomic_json(result_file, row)
                if row.get("plan_a") is not None:
                    _immutable_json(
                        layout.declarations_dir
                        / f"{hashlib.sha256(pair.key.encode()).hexdigest()[:12]}-A.json",
                        row["plan_a"],
                    )
                if row.get("plan_b") is not None:
                    _immutable_json(
                        layout.declarations_dir
                        / f"{hashlib.sha256(pair.key.encode()).hexdigest()[:12]}-B.json",
                        row["plan_b"],
                    )
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

        progress.phase(4, 4, "aggregate and compare published results")

        # Stable order independent of resume boundaries.
        result_index = {(str(row["pair"]), str(row["arm"])): row for row in results}
        ordered_results = []
        for pair in FROZEN_PAIRS:
            pair_name = f"{pair.repo}/task{pair.task_id}/feature{pair.feature_a}+feature{pair.feature_b}"
            for arm in ARMS:
                row = result_index.get((pair_name, arm))
                if row is None:
                    raise RuntimeError(
                        f"missing completed result for {pair_name}/{arm}"
                    )
                ordered_results.append(row)

        summary = aggregate_results(ordered_results)
        comparison = compare_reference(summary)
        _atomic_json(layout.run_dir / "results.json", ordered_results)
        _atomic_json(layout.run_dir / "summary.json", summary)
        _write_summary_csv(layout.run_dir / "summary.csv", summary)
        _atomic_json(layout.run_dir / "reference_comparison.json", comparison)
        _atomic_json(
            layout.run_dir / "provider_stats.json",
            {
                "planner": {
                    "api_attempts": planner_provider.stats.api_attempts,
                    "http_200_responses": planner_provider.stats.http_200_responses,
                    "accepted_responses": planner_provider.stats.accepted_responses,
                    "actual_cost": planner_provider.stats.actual_cost,
                    "planner_cost": planner_provider.stats.planner_cost,
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
        progress.finish(
            detail=(
                "published mechanism counts matched"
                if comparison.get("matches_published_mechanism_counts")
                else "completed; see reference_comparison.json"
            )
        )
        return {
            "run_id": run.run_id,
            "run_dir": str(layout.run_dir),
            "summary": summary,
            "reference_comparison": comparison,
        }
    finally:
        progress.close()
