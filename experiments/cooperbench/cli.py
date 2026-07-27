"""Model-free CLI for validating studies and creating resumable run directories."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .common import CheckpointStore, ShardSpec, create_run, load_study
from .common.identity import study_fingerprint


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m experiments.cooperbench",
        description="Reproducible CooperBench study infrastructure for Claim Plane.",
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
