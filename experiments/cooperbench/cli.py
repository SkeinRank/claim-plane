"""CLI for reproducible Claim Plane CooperBench research studies."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .common import CheckpointStore, ShardSpec, create_run, load_study
from .common.identity import study_fingerprint
from .environment import runtime_environment
from .planner_v1 import (
    OpenRouterClient,
    PLANNER_MODEL,
    PLANNER_POLICY_FINGERPRINT,
    PLANNER_POLICY_VERSION,
    PlannerV1,
    plan_fingerprint,
)
from .planner_v1.tools import build_uncertainty_candidates_v2, read_context


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _write_json(path: str | Path, payload: object) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def _load_json_object(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return payload


def cmd_validate(args: argparse.Namespace) -> int:
    study = load_study(args.study)
    _print_json(
        {
            "valid": True,
            "study_id": study.study_id,
            "study_fingerprint": study_fingerprint(study),
            "pairs": len(study.pairs),
            "coder_seeds": list(study.coder_seeds),
            "arms": [arm.value for arm in study.arms],
        }
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    study = load_study(args.study)
    shard = ShardSpec(index=args.shard_index, count=args.shard_count)
    run, layout = create_run(
        study,
        coder_seed=args.seed,
        artifact_root=args.artifacts,
        shard=shard,
        repo_root=args.repo,
    )
    _print_json(
        {
            "run": run.to_dict(),
            "run_dir": str(layout.run_dir),
            "pairs_in_shard": len(shard.select(study.pairs)),
            "checkpoint": str(layout.checkpoint_file),
        }
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    checkpoint_path = Path(args.run_dir) / "checkpoint.json"
    checkpoint = CheckpointStore(checkpoint_path).load()
    _print_json(checkpoint.to_dict())
    return 0


def cmd_environment(_args: argparse.Namespace) -> int:
    _print_json(runtime_environment())
    return 0


def cmd_planner_policy(_args: argparse.Namespace) -> int:
    _print_json(
        {
            "planner_policy_version": PLANNER_POLICY_VERSION,
            "planner_policy_fingerprint": PLANNER_POLICY_FINGERPRINT,
            "planner_model": PLANNER_MODEL,
        }
    )
    return 0


def cmd_planner_context(args: argparse.Namespace) -> int:
    rendered = read_context(Path(args.tree), Path(args.feature_dir))
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


def cmd_planner_candidates(args: argparse.Namespace) -> int:
    primary = _load_json_object(args.plan)
    if "plan" in primary and isinstance(primary["plan"], dict):
        primary = primary["plan"]
    feature_text = (Path(args.feature_dir) / "feature.md").read_text(
        encoding="utf-8", errors="replace"
    )[:14_000]
    candidates = build_uncertainty_candidates_v2(Path(args.tree), feature_text, primary)
    payload = {
        "planner_policy_version": PLANNER_POLICY_VERSION,
        "planner_policy_fingerprint": PLANNER_POLICY_FINGERPRINT,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    if args.output:
        _write_json(args.output, payload)
    else:
        _print_json(payload)
    return 0


def cmd_planner_run(args: argparse.Namespace) -> int:
    provider = OpenRouterClient()
    planner = PlannerV1(provider)
    result = planner.get_calibrated_plan(
        Path(args.tree), Path(args.feature_dir), seed=args.seed
    )
    payload = {
        "schema_version": 1,
        "planner_policy_version": PLANNER_POLICY_VERSION,
        "planner_policy_fingerprint": PLANNER_POLICY_FINGERPRINT,
        "planner_model": PLANNER_MODEL,
        "planner_seed": args.seed,
        "plan_fingerprint": plan_fingerprint(result["plan"]),
        "result": result,
        "provider_stats": {
            "api_attempts": provider.stats.api_attempts,
            "http_200_responses": provider.stats.http_200_responses,
            "accepted_responses": provider.stats.accepted_responses,
            "actual_cost": provider.stats.actual_cost,
            "planner_cost": provider.stats.planner_cost,
        },
    }
    if args.output:
        _write_json(args.output, payload)
    else:
        _print_json(payload)
    return 0


def _add_planner_commands(sub: Any) -> None:
    planner = sub.add_parser(
        "planner", help="Inspect or execute the frozen Planner v1 research policy."
    )
    planner_sub = planner.add_subparsers(dest="planner_command", required=True)

    policy = planner_sub.add_parser(
        "policy", help="Print the Planner v1 model and policy fingerprint."
    )
    policy.set_defaults(func=cmd_planner_policy)

    context = planner_sub.add_parser(
        "context", help="Render the oracle-localized current-source planner context."
    )
    context.add_argument("--tree", required=True)
    context.add_argument("--feature-dir", required=True)
    context.add_argument("--output")
    context.set_defaults(func=cmd_planner_context)

    candidates = planner_sub.add_parser(
        "candidates",
        help="Build deterministic uncertainty candidates for a primary plan.",
    )
    candidates.add_argument("--tree", required=True)
    candidates.add_argument("--feature-dir", required=True)
    candidates.add_argument("--plan", required=True)
    candidates.add_argument("--output")
    candidates.set_defaults(func=cmd_planner_candidates)

    run = planner_sub.add_parser(
        "run", help="Run Planner v1 and its final uncertainty calibration."
    )
    run.add_argument("--tree", required=True)
    run.add_argument("--feature-dir", required=True)
    run.add_argument("--seed", required=True, type=int)
    run.add_argument("--output")
    run.set_defaults(func=cmd_planner_run)


def _paper_paths(args: argparse.Namespace):
    from .paper_6pair.config import PaperPaths

    return PaperPaths.from_values(
        args.cooperbench,
        artifact_root=args.artifacts,
        repo_cache=args.repo_cache,
        workspace_root=args.workspace,
    )


def cmd_paper_info(_args: argparse.Namespace) -> int:
    from .paper_6pair.config import PAPER_STUDY, REFERENCE_SUMMARY

    _print_json(
        {
            "study": PAPER_STUDY.to_dict(),
            "published_mechanism_counts": REFERENCE_SUMMARY,
        }
    )
    return 0


def cmd_paper_prepare(args: argparse.Namespace) -> int:
    from .paper_6pair.dataset import (
        benchmark_provenance,
        validate_frozen_pairs,
        verify_pair_labels,
    )

    paths = _paper_paths(args)
    tasks = validate_frozen_pairs(paths.dataset)
    verify_pair_labels(paths.dataset)
    _print_json(
        {
            "ready": True,
            "cooperbench": str(paths.cooperbench),
            "dataset": str(paths.dataset),
            "compatible_tasks": len(tasks),
            "artifact_root": str(paths.artifact_root),
            "repo_cache": str(paths.repo_cache),
            "workspace": str(paths.workspace_root),
            "benchmark": benchmark_provenance(paths.cooperbench),
        }
    )
    return 0


def cmd_reproduce_paper(args: argparse.Namespace) -> int:
    from .paper_6pair.runner import run_paper_study

    result = run_paper_study(
        _paper_paths(args),
        repo_root=args.repo,
        resume=not args.no_resume,
        skip_gold_sanity=args.skip_gold_sanity,
    )
    _print_json(result)
    return 0


def _add_paper_commands(sub: Any) -> None:
    paper = sub.add_parser(
        "paper6",
        help="Inspect or prepare the published six-pair CooperBench study.",
    )
    paper_sub = paper.add_subparsers(dest="paper_command", required=True)

    info = paper_sub.add_parser(
        "info",
        help="Print the frozen study declaration and published mechanism counts.",
    )
    info.set_defaults(func=cmd_paper_info)

    prepare = paper_sub.add_parser(
        "prepare",
        help="Validate a local CooperBench checkout against the six frozen pairs.",
    )
    _add_paper_paths(prepare)
    prepare.set_defaults(func=cmd_paper_prepare)

    reproduce = sub.add_parser(
        "reproduce-paper",
        help="Run the 24 arm executions reported in the published six-pair study.",
    )
    _add_paper_paths(reproduce)
    reproduce.add_argument("--repo", default=".")
    reproduce.add_argument(
        "--skip-gold-sanity",
        action="store_true",
        help="Skip CooperBench gold-feature sanity checks before model calls.",
    )
    reproduce.add_argument(
        "--no-resume",
        action="store_true",
        help="Refuse to reuse a run that already contains completed units.",
    )
    reproduce.set_defaults(func=cmd_reproduce_paper)


def _add_paper_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cooperbench",
        required=True,
        help="Path to a CooperBench checkout containing dataset/.",
    )
    parser.add_argument("--artifacts", default=".claim-plane/experiments")
    parser.add_argument("--repo-cache", default=".claim-plane/cooperbench/repos")
    parser.add_argument("--workspace", default=".claim-plane/cooperbench/worktrees")


def _confirmatory_paths(args: argparse.Namespace):
    from .confirmatory_30x3.config import ConfirmatoryPaths

    return ConfirmatoryPaths.from_values(
        getattr(args, "cooperbench", "."),
        artifact_root=args.artifacts,
        repo_cache=getattr(args, "repo_cache", ".claim-plane/cooperbench/repos"),
        workspace_root=getattr(args, "workspace", ".claim-plane/cooperbench/worktrees"),
    )


def cmd_confirmatory_info(_args: argparse.Namespace) -> int:
    from .confirmatory_30x3.config import (
        CODER_SEEDS,
        N_PAIRS,
        PLANNER_FREEZE_SEED,
        SHARD_COUNT,
        SHARD_SIZE,
        STUDY_ID,
    )

    _print_json(
        {
            "study_id": STUDY_ID,
            "pairs": N_PAIRS,
            "coder_seeds": list(CODER_SEEDS),
            "arms": [
                "parallel",
                "claim-plane-static",
                "claim-plane-dynamic",
                "always-serial",
            ],
            "planner_freeze_seed": PLANNER_FREEZE_SEED,
            "shard_size": SHARD_SIZE,
            "shard_count_per_seed": SHARD_COUNT,
            "total_shards": len(CODER_SEEDS) * SHARD_COUNT,
            "planned_arm_executions": N_PAIRS * len(CODER_SEEDS) * 4,
        }
    )
    return 0


def cmd_confirmatory_prepare(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.runner import prepare_protocol

    _print_json(prepare_protocol(_confirmatory_paths(args)))
    return 0


def cmd_confirmatory_freeze(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.runner import freeze_protocol_plans

    _print_json(freeze_protocol_plans(_confirmatory_paths(args)))
    return 0


def cmd_confirmatory_run(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.runner import run_shard

    result = run_shard(
        _confirmatory_paths(args),
        coder_seed=args.seed,
        shard_index=args.shard,
        repo_root=args.repo,
        resume=not args.no_resume,
    )
    _print_json(result)
    return 0


def cmd_confirmatory_physical_pair(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.physical import run_physical_pair

    result = run_physical_pair(
        _confirmatory_paths(args),
        coder_seed=args.seed,
        pair_index=args.pair,
        repo_root=args.repo,
        resume=not args.no_resume,
    )
    _print_json(result)
    return 0


def cmd_confirmatory_physical_run(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.physical import run_physical_batch
    from .physical_parallel import parse_pair_indexes

    pair_indexes = parse_pair_indexes(args.pairs, pair_count=30)
    result = run_physical_batch(
        _confirmatory_paths(args),
        coder_seed=args.seed,
        pair_indexes=pair_indexes,
        max_parallel_pairs=args.max_parallel_pairs,
        repo_root=args.repo,
        resume=not args.no_resume,
    )
    _print_json(result)
    return 0 if result.get("complete") else 1


def cmd_confirmatory_ablation_pair(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.ablation import parse_ablation_profiles, run_ablation_pair

    result = run_ablation_pair(
        _confirmatory_paths(args),
        coder_seed=args.seed,
        pair_index=args.pair,
        profiles=parse_ablation_profiles(args.profiles),
        repo_root=args.repo,
        resume=not args.no_resume,
    )
    _print_json(result)
    return 0


def cmd_confirmatory_ablation_run(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.ablation import parse_ablation_profiles, run_ablation_batch
    from .physical_parallel import parse_pair_indexes

    result = run_ablation_batch(
        _confirmatory_paths(args),
        coder_seed=args.seed,
        pair_indexes=parse_pair_indexes(args.pairs, pair_count=30),
        profiles=parse_ablation_profiles(args.profiles),
        max_parallel_pairs=args.max_parallel_pairs,
        repo_root=args.repo,
        resume=not args.no_resume,
    )
    _print_json(result)
    return 0 if result.get("complete") else 1


def cmd_confirmatory_scip_v3_pair(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.scip_v3 import parse_scip_v3_profiles, run_scip_v3_pair

    result = run_scip_v3_pair(
        _confirmatory_paths(args),
        coder_seed=args.seed,
        pair_index=args.pair,
        profiles=parse_scip_v3_profiles(args.profiles),
        repo_root=args.repo,
        resume=not args.no_resume,
    )
    _print_json(
        {
            "protocol": result.get("protocol"),
            "complete": result.get("complete"),
            "coder_seed": result.get("coder_seed"),
            "pair_index": result.get("pair_index"),
            "pair_key": result.get("pair_key"),
            "artifact": result.get("artifact"),
            "pair_wall_time_seconds": result.get("pair_wall_time_seconds"),
        }
    )
    return 0


def cmd_confirmatory_scip_v3_run(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.scip_v3 import (
        parse_scip_v3_profiles,
        run_scip_v3_batch,
    )
    from .confirmatory_30x3.final import parse_coder_seeds
    from .physical_parallel import parse_pair_indexes

    result = run_scip_v3_batch(
        _confirmatory_paths(args),
        seeds=parse_coder_seeds(args.seeds),
        pair_indexes=parse_pair_indexes(args.pairs, pair_count=30),
        profiles=parse_scip_v3_profiles(args.profiles),
        max_parallel_pairs=args.max_parallel_pairs,
        repo_root=args.repo,
        resume=not args.no_resume,
    )
    _print_json(result)
    return 0 if result.get("complete") else 1


def cmd_confirmatory_scip_v3_status(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.scip_v3 import scip_v3_status

    _print_json(scip_v3_status(_confirmatory_paths(args)))
    return 0


def cmd_confirmatory_scip_v3_aggregate(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.scip_v3 import aggregate_scip_v3

    _print_json(aggregate_scip_v3(_confirmatory_paths(args)))
    return 0


def cmd_confirmatory_final_pair(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.final import parse_confirmatory_modes, run_confirmatory_pair

    result = run_confirmatory_pair(
        _confirmatory_paths(args),
        coder_seed=args.seed,
        pair_index=args.pair,
        modes=parse_confirmatory_modes(args.modes),
        repo_root=args.repo,
        resume=not args.no_resume,
    )
    _print_json(
        {
            "protocol": result.get("protocol"),
            "complete": result.get("complete"),
            "coder_seed": result.get("coder_seed"),
            "pair_index": result.get("pair_index"),
            "pair_key": result.get("pair_key"),
            "artifact": result.get("artifact"),
            "pair_wall_time_seconds": result.get("pair_wall_time_seconds"),
        }
    )
    return 0


def cmd_confirmatory_final_run(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.final import (
        parse_coder_seeds,
        parse_confirmatory_modes,
        run_confirmatory_batch,
    )
    from .physical_parallel import parse_pair_indexes

    result = run_confirmatory_batch(
        _confirmatory_paths(args),
        seeds=parse_coder_seeds(args.seeds),
        pair_indexes=parse_pair_indexes(args.pairs, pair_count=30),
        modes=parse_confirmatory_modes(args.modes),
        max_parallel_pairs=args.max_parallel_pairs,
        repo_root=args.repo,
        resume=not args.no_resume,
    )
    _print_json(result)
    return 0 if result.get("complete") else 1


def cmd_confirmatory_final_status(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.final import confirmatory_status

    _print_json(confirmatory_status(_confirmatory_paths(args)))
    return 0


def cmd_confirmatory_final_aggregate(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.final import aggregate_confirmatory_experiment

    _print_json(aggregate_confirmatory_experiment(_confirmatory_paths(args)))
    return 0


def cmd_confirmatory_status(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.runner import study_status

    _print_json(study_status(_confirmatory_paths(args)))
    return 0


def cmd_confirmatory_aggregate(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.aggregation import aggregate_study

    _print_json(
        aggregate_study(
            _confirmatory_paths(args),
            bootstrap_samples=args.bootstrap_samples,
            bootstrap_seed=args.bootstrap_seed,
        )
    )
    return 0


def cmd_confirmatory_verify_analysis(args: argparse.Namespace) -> int:
    from .confirmatory_30x3.aggregation import verify_analysis

    result = verify_analysis(_confirmatory_paths(args))
    _print_json(result)
    return 0 if result.get("valid") else 1


def _add_confirmatory_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cooperbench",
        required=True,
        help="Path to a CooperBench checkout containing dataset/.",
    )
    parser.add_argument("--artifacts", default=".claim-plane/experiments")
    parser.add_argument("--repo-cache", default=".claim-plane/cooperbench/repos")
    parser.add_argument("--workspace", default=".claim-plane/cooperbench/worktrees")


def _add_confirmatory_commands(sub: Any) -> None:
    confirmatory = sub.add_parser(
        "confirmatory",
        help="Freeze and execute the 30-pair, three-seed CooperBench study.",
    )
    confirmatory_sub = confirmatory.add_subparsers(
        dest="confirmatory_command", required=True
    )

    info = confirmatory_sub.add_parser(
        "info", help="Print the frozen protocol dimensions and seed schedule."
    )
    info.set_defaults(func=cmd_confirmatory_info)

    prepare = confirmatory_sub.add_parser(
        "prepare",
        help="Select and gold-validate the exact 30-pair confirmatory set.",
    )
    _add_confirmatory_paths(prepare)
    prepare.set_defaults(func=cmd_confirmatory_prepare)

    freeze = confirmatory_sub.add_parser(
        "freeze-plans",
        help="Run Planner v1 once per feature and freeze all declarations.",
    )
    _add_confirmatory_paths(freeze)
    freeze.set_defaults(func=cmd_confirmatory_freeze)

    run = confirmatory_sub.add_parser(
        "run",
        help="Run or resume one 10-pair coder-seed shard using frozen plans.",
    )
    _add_confirmatory_paths(run)
    run.add_argument("--seed", required=True, type=int, choices=(101, 202, 303))
    run.add_argument("--shard", required=True, type=int, choices=(1, 2, 3))
    run.add_argument("--repo", default=".")
    run.add_argument(
        "--no-resume",
        action="store_true",
        help="Refuse to reuse a shard that already contains completed units.",
    )
    run.set_defaults(func=cmd_confirmatory_run)

    physical = confirmatory_sub.add_parser(
        "physical-run",
        help=(
            "Run frozen pairs through a bounded subprocess pool and measure actual "
            "worker overlap."
        ),
    )
    _add_confirmatory_paths(physical)
    physical.add_argument("--seed", required=True, type=int, choices=(101, 202, 303))
    physical.add_argument(
        "--pairs",
        default="1-6",
        help="One-based pair indexes or ranges, for example 1-6,9,12.",
    )
    physical.add_argument(
        "--max-parallel-pairs",
        type=int,
        default=6,
        help="Maximum number of independent pair processes active at once.",
    )
    physical.add_argument("--repo", default=".")
    physical.add_argument("--no-resume", action="store_true")
    physical.set_defaults(func=cmd_confirmatory_physical_run)

    physical_pair = confirmatory_sub.add_parser(
        "physical-pair",
        help="Execute one frozen pair with inner physical-concurrency instrumentation.",
    )
    _add_confirmatory_paths(physical_pair)
    physical_pair.add_argument(
        "--seed", required=True, type=int, choices=(101, 202, 303)
    )
    physical_pair.add_argument("--pair", required=True, type=int, choices=range(1, 31))
    physical_pair.add_argument("--repo", default=".")
    physical_pair.add_argument("--no-resume", action="store_true")
    physical_pair.set_defaults(func=cmd_confirmatory_physical_pair)

    ablation = confirmatory_sub.add_parser(
        "ablation-run",
        help=(
            "Run deterministic admission ablations over frozen pairs through the "
            "bounded physical worker pool."
        ),
    )
    _add_confirmatory_paths(ablation)
    ablation.add_argument("--seed", required=True, type=int, choices=(101, 202, 303))
    ablation.add_argument(
        "--pairs",
        default="1-6",
        help="One-based pair indexes or ranges, for example 1-6,9,12.",
    )
    ablation.add_argument(
        "--profiles",
        default=(
            "full_v2,file_region_baseline,symbols_without_dependencies,"
            "no_contract_propagation"
        ),
        help="Comma-separated deterministic ablation profiles.",
    )
    ablation.add_argument(
        "--max-parallel-pairs",
        type=int,
        default=6,
        help="Maximum number of independent pair processes active at once.",
    )
    ablation.add_argument("--repo", default=".")
    ablation.add_argument("--no-resume", action="store_true")
    ablation.set_defaults(func=cmd_confirmatory_ablation_run)

    ablation_pair = confirmatory_sub.add_parser(
        "ablation-pair",
        help="Execute one frozen pair once per deterministic admission profile.",
    )
    _add_confirmatory_paths(ablation_pair)
    ablation_pair.add_argument(
        "--seed", required=True, type=int, choices=(101, 202, 303)
    )
    ablation_pair.add_argument("--pair", required=True, type=int, choices=range(1, 31))
    ablation_pair.add_argument(
        "--profiles",
        default=(
            "full_v2,file_region_baseline,symbols_without_dependencies,"
            "no_contract_propagation"
        ),
    )
    ablation_pair.add_argument("--repo", default=".")
    ablation_pair.add_argument("--no-resume", action="store_true")
    ablation_pair.set_defaults(func=cmd_confirmatory_ablation_pair)

    scip_v3 = confirmatory_sub.add_parser(
        "scip-v3-run",
        help=(
            "Run the SCIP ablation and Physical Parallel Benchmark v3 over selected "
            "frozen pair/seed units."
        ),
    )
    _add_confirmatory_paths(scip_v3)
    scip_v3.add_argument(
        "--seeds",
        default="101",
        help="Comma-separated frozen coder seeds; use 101,202,303 for the full matrix.",
    )
    scip_v3.add_argument(
        "--pairs",
        default="1-6",
        help="One-based pair indexes or ranges, for example 1-6 or 1-30.",
    )
    scip_v3.add_argument(
        "--profiles",
        default=(
            "serial,naive_parallel,builtin_graph,scip_graph_cold,"
            "scip_cache_blocking"
        ),
        help="Comma-separated SCIP v3 execution profiles.",
    )
    scip_v3.add_argument(
        "--max-parallel-pairs",
        type=int,
        default=6,
        help="Maximum independent pair/seed subprocesses active at once.",
    )
    scip_v3.add_argument("--repo", default=".")
    scip_v3.add_argument("--no-resume", action="store_true")
    scip_v3.set_defaults(func=cmd_confirmatory_scip_v3_run)

    scip_v3_pair = confirmatory_sub.add_parser(
        "scip-v3-pair",
        help="Execute one frozen pair/seed unit under all selected SCIP v3 profiles.",
    )
    _add_confirmatory_paths(scip_v3_pair)
    scip_v3_pair.add_argument(
        "--seed", required=True, type=int, choices=(101, 202, 303)
    )
    scip_v3_pair.add_argument(
        "--pair", required=True, type=int, choices=range(1, 31)
    )
    scip_v3_pair.add_argument(
        "--profiles",
        default=(
            "serial,naive_parallel,builtin_graph,scip_graph_cold,"
            "scip_cache_blocking"
        ),
    )
    scip_v3_pair.add_argument("--repo", default=".")
    scip_v3_pair.add_argument("--no-resume", action="store_true")
    scip_v3_pair.set_defaults(func=cmd_confirmatory_scip_v3_pair)

    scip_v3_status = confirmatory_sub.add_parser(
        "scip-v3-status",
        help="Report SCIP v3 benchmark completion without model calls.",
    )
    scip_v3_status.add_argument("--artifacts", default=".claim-plane/experiments")
    scip_v3_status.set_defaults(func=cmd_confirmatory_scip_v3_status)

    scip_v3_aggregate = confirmatory_sub.add_parser(
        "scip-v3-aggregate",
        help="Require and seal the complete 30x3 SCIP v3 result matrix.",
    )
    scip_v3_aggregate.add_argument("--artifacts", default=".claim-plane/experiments")
    scip_v3_aggregate.set_defaults(func=cmd_confirmatory_scip_v3_aggregate)

    final_run = confirmatory_sub.add_parser(
        "final-run",
        help=(
            "Run the deterministic v2 confirmatory matrix with bounded outer pair "
            "concurrency and measured inner overlap."
        ),
    )
    _add_confirmatory_paths(final_run)
    final_run.add_argument(
        "--seeds",
        default="101,202,303",
        help="Comma-separated frozen coder seeds.",
    )
    final_run.add_argument(
        "--pairs",
        default="1-30",
        help="One-based pair indexes or ranges, for example 1-6,9,12.",
    )
    final_run.add_argument(
        "--modes",
        default="naive_parallel,legacy_static,deterministic_v2,always_serial",
        help="Comma-separated confirmatory execution modes.",
    )
    final_run.add_argument(
        "--max-parallel-pairs",
        type=int,
        default=6,
        help="Maximum number of independent pair/seed processes active at once.",
    )
    final_run.add_argument("--repo", default=".")
    final_run.add_argument("--no-resume", action="store_true")
    final_run.set_defaults(func=cmd_confirmatory_final_run)

    final_pair = confirmatory_sub.add_parser(
        "final-pair",
        help="Execute one pair/seed unit under the deterministic v2 confirmatory modes.",
    )
    _add_confirmatory_paths(final_pair)
    final_pair.add_argument("--seed", required=True, type=int, choices=(101, 202, 303))
    final_pair.add_argument("--pair", required=True, type=int, choices=range(1, 31))
    final_pair.add_argument(
        "--modes",
        default="naive_parallel,legacy_static,deterministic_v2,always_serial",
    )
    final_pair.add_argument("--repo", default=".")
    final_pair.add_argument("--no-resume", action="store_true")
    final_pair.set_defaults(func=cmd_confirmatory_final_pair)

    final_status = confirmatory_sub.add_parser(
        "final-status",
        help="Report deterministic v2 confirmatory completion without model calls.",
    )
    final_status.add_argument("--artifacts", default=".claim-plane/experiments")
    final_status.set_defaults(func=cmd_confirmatory_final_status)

    final_aggregate = confirmatory_sub.add_parser(
        "final-aggregate",
        help="Require and seal the complete deterministic v2 30x3 result matrix.",
    )
    final_aggregate.add_argument("--artifacts", default=".claim-plane/experiments")
    final_aggregate.set_defaults(func=cmd_confirmatory_final_aggregate)

    status = confirmatory_sub.add_parser(
        "status", help="Report planner-freeze and nine-shard completion state."
    )
    status.add_argument("--artifacts", default=".claim-plane/experiments")
    status.set_defaults(func=cmd_confirmatory_status)

    aggregate = confirmatory_sub.add_parser(
        "aggregate",
        help="Validate all nine shards and write publication-grade final results.",
    )
    aggregate.add_argument("--artifacts", default=".claim-plane/experiments")
    aggregate.add_argument("--bootstrap-samples", type=int, default=5000)
    aggregate.add_argument("--bootstrap-seed", type=int, default=20260727)
    aggregate.set_defaults(func=cmd_confirmatory_aggregate)

    verify = confirmatory_sub.add_parser(
        "verify-analysis",
        help="Verify final analysis artifacts against the publication manifest.",
    )
    verify.add_argument("--artifacts", default=".claim-plane/experiments")
    verify.set_defaults(func=cmd_confirmatory_verify_analysis)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m experiments.cooperbench",
        description="Reproducible CooperBench research infrastructure for Claim Plane.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser(
        "validate", help="Validate and fingerprint a study JSON file."
    )
    validate.add_argument("study")
    validate.set_defaults(func=cmd_validate)

    init = sub.add_parser(
        "init",
        help="Create the canonical artifact tree and initial checkpoint for one run.",
    )
    init.add_argument("study")
    init.add_argument("--seed", required=True, type=int)
    init.add_argument("--shard-index", type=int, default=1)
    init.add_argument("--shard-count", type=int, default=1)
    init.add_argument("--artifacts", default=".claim-plane/experiments")
    init.add_argument("--repo", default=".")
    init.set_defaults(func=cmd_init)

    status = sub.add_parser("status", help="Read a run checkpoint.")
    status.add_argument("run_dir")
    status.set_defaults(func=cmd_status)

    environment = sub.add_parser(
        "environment",
        help="Print the pinned research toolchain and current runtime diagnostics.",
    )
    environment.set_defaults(func=cmd_environment)

    _add_planner_commands(sub)
    _add_paper_commands(sub)
    _add_confirmatory_commands(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print(
            "interrupted: durable research progress was saved; rerun the same command to resume",
            file=sys.stderr,
        )
        return 130
    except (
        FileNotFoundError,
        KeyError,
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
