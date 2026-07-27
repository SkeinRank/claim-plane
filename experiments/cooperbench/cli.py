"""CLI for reproducible Claim Plane CooperBench research studies."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .common import CheckpointStore, ShardSpec, create_run, load_study
from .common.identity import study_fingerprint
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
    from .paper_6pair.dataset import validate_frozen_pairs, verify_pair_labels

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

    _add_planner_commands(sub)
    _add_paper_commands(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
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
