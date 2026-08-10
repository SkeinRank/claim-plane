"""Physical-parallel execution layer for the frozen confirmatory workload.

The original confirmatory protocol remains immutable.  This module reuses the
same frozen pairs, coder seeds, plans, and arms while changing only execution
instrumentation: admitted pair workers can overlap physically, and independent
pairs can run in isolated subprocesses through a bounded pool.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from ..common.identity import study_fingerprint
from ..environment import runtime_environment
from ..paper_6pair import runner as harness
from ..paper_6pair.coder import AGENT_TRACE_LOGS, reset_agent_traces
from ..paper_6pair.provider import STATS as CODER_PROVIDER_STATS, reset_provider_state
from ..physical_parallel import (
    PHYSICAL_PARALLEL_PROTOCOL,
    parse_pair_indexes,
    python_module_command,
    run_bounded_pair_processes,
)
from .config import CODER_SEEDS, N_PAIRS, ConfirmatoryPaths
from .plans import load_plan_bundle, validate_plan_bundle
from .runner import _legacy_pair, load_confirmatory_study


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


def _root(paths: ConfirmatoryPaths, fingerprint: str) -> Path:
    return (
        paths.artifact_root
        / "physical-parallel-v2"
        / "claim-plane-confirmatory-30x3"
        / fingerprint[:12]
    )


def _pair_dir(
    paths: ConfirmatoryPaths,
    *,
    fingerprint: str,
    coder_seed: int,
    pair_index: int,
) -> Path:
    return _root(paths, fingerprint) / f"seed-{coder_seed}" / f"pair-{pair_index:02d}"


def _provider_stats() -> dict[str, Any]:
    return {
        "api_attempts": CODER_PROVIDER_STATS.api_attempts,
        "http_200_responses": CODER_PROVIDER_STATS.http_200_responses,
        "accepted_responses": CODER_PROVIDER_STATS.accepted_responses,
        "actual_cost": CODER_PROVIDER_STATS.actual_cost,
        "cost_by_role": dict(CODER_PROVIDER_STATS.cost_by_role),
    }


def run_physical_pair(
    paths: ConfirmatoryPaths,
    *,
    coder_seed: int,
    pair_index: int,
    repo_root: str | Path = ".",
    resume: bool = True,
) -> dict[str, Any]:
    """Execute all four arms for one frozen pair with physical timing enabled."""

    study = load_confirmatory_study(paths)
    if coder_seed not in CODER_SEEDS:
        raise ValueError(f"coder seed must be one of {list(CODER_SEEDS)}")
    if not 1 <= pair_index <= len(study.pairs):
        raise ValueError(f"pair index must be within 1..{len(study.pairs)}")

    bundle = load_plan_bundle(paths.frozen_plans_file)
    validate_plan_bundle(bundle, study)
    frozen_pair_plans = bundle["pairs"]
    pair = study.pairs[pair_index - 1]
    fingerprint = study_fingerprint(study)
    output_dir = _pair_dir(
        paths,
        fingerprint=fingerprint,
        coder_seed=coder_seed,
        pair_index=pair_index,
    )
    output_file = output_dir / "result.json"
    if resume and output_file.exists():
        existing = json.loads(output_file.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            raise RuntimeError(f"invalid physical pair artifact: {output_file}")
        return existing
    if output_file.exists() and not resume:
        raise RuntimeError(
            f"physical pair artifact already exists; enable resume or remove {output_dir}"
        )

    # Each outer pair is a separate process.  Give it a unique worktree root as an
    # additional filesystem boundary even when multiple pair processes share one cache.
    isolated_workspace = (
        paths.workspace_root
        / "physical-parallel-v2"
        / f"seed-{coder_seed}"
        / f"pair-{pair_index:02d}"
    )
    isolated_repo_cache = (
        paths.repo_cache
        / "physical-parallel-v2"
        / f"seed-{coder_seed}"
        / f"pair-{pair_index:02d}"
    )
    isolated_paths = ConfirmatoryPaths(
        cooperbench=paths.cooperbench,
        artifact_root=paths.artifact_root,
        repo_cache=isolated_repo_cache,
        workspace_root=isolated_workspace,
    )
    harness.configure_runtime(isolated_paths, planner=None, pairs=study.pairs)
    reset_provider_state()
    reset_agent_traces()

    repetition = list(study.coder_seeds).index(coder_seed)
    rows: list[dict[str, Any]] = []
    pair_started_ns = time.time_ns()
    for arm_item in study.arms:
        arm = arm_item.value
        arm_started_ns = time.time_ns()
        row = harness.run_pair(
            _legacy_pair(pair),
            arm,
            repetition,
            coder_seed=coder_seed,
            frozen_plans=frozen_pair_plans,
            physical_parallel=True,
        )
        arm_finished_ns = time.time_ns()
        row["wall_time_seconds"] = (arm_finished_ns - arm_started_ns) / 1_000_000_000
        row["physical_pair_index"] = pair_index
        row["coder_seed"] = coder_seed
        row["coder_seed_index"] = repetition
        row["physical_parallel_protocol"] = PHYSICAL_PARALLEL_PROTOCOL
        rows.append(row)
        _atomic_json(output_dir / f"{arm}.json", row)
    pair_finished_ns = time.time_ns()

    result = {
        "protocol": PHYSICAL_PARALLEL_PROTOCOL,
        "study_id": study.study_id,
        "study_fingerprint": fingerprint,
        "claim_plane_runtime_version": _claim_plane_version(),
        "pair_index": pair_index,
        "pair_key": pair.key,
        "coder_seed": coder_seed,
        "coder_seed_index": repetition,
        "repo_root": str(Path(repo_root).resolve()),
        "pair_started_ns": pair_started_ns,
        "pair_finished_ns": pair_finished_ns,
        "pair_wall_time_seconds": (
            pair_finished_ns - pair_started_ns
        ) / 1_000_000_000,
        "arms": rows,
        "inner_physical_concurrency": {
            row["arm"]: {
                "enabled": bool(row.get("physical_parallel_enabled")),
                "observed": bool(row.get("physical_concurrency_observed")),
                "overlap_seconds": float(row.get("physical_overlap_seconds", 0.0) or 0.0),
                "overlap_fraction_of_shorter": float(
                    row.get("physical_overlap_fraction_of_shorter", 0.0) or 0.0
                ),
                "reason": row.get("physical_parallel_reason"),
            }
            for row in rows
        },
        "provider_stats": _provider_stats(),
        "agent_traces": list(AGENT_TRACE_LOGS),
        "environment": runtime_environment(),
        "complete": True,
    }
    _atomic_json(output_file, result)
    return result


def _claim_plane_version() -> str:
    try:
        from claim_plane import __version__

        return __version__
    except Exception:  # pragma: no cover - diagnostic only
        return "unknown"


def run_physical_batch(
    paths: ConfirmatoryPaths,
    *,
    coder_seed: int,
    pair_indexes: tuple[int, ...],
    max_parallel_pairs: int = 6,
    repo_root: str | Path = ".",
    resume: bool = True,
) -> dict[str, Any]:
    """Run independent frozen pairs through isolated bounded subprocesses."""

    study = load_confirmatory_study(paths)
    if coder_seed not in CODER_SEEDS:
        raise ValueError(f"coder seed must be one of {list(CODER_SEEDS)}")
    if max_parallel_pairs <= 0:
        raise ValueError("max_parallel_pairs must be positive")
    selected = tuple(sorted(set(pair_indexes)))
    if not selected:
        raise ValueError("at least one pair index is required")
    if selected != parse_pair_indexes(
        ",".join(str(index) for index in selected), pair_count=N_PAIRS
    ):
        raise ValueError("invalid pair indexes")

    commands: list[tuple[str, tuple[str, ...]]] = []
    for pair_index in selected:
        args = [
            "confirmatory",
            "physical-pair",
            "--cooperbench",
            str(paths.cooperbench),
            "--artifacts",
            str(paths.artifact_root),
            "--repo-cache",
            str(paths.repo_cache),
            "--workspace",
            str(paths.workspace_root),
            "--seed",
            str(coder_seed),
            "--pair",
            str(pair_index),
            "--repo",
            str(repo_root),
        ]
        if not resume:
            args.append("--no-resume")
        commands.append((f"pair-{pair_index:02d}", python_module_command(*args)))

    result = run_bounded_pair_processes(
        commands,
        max_parallel_pairs=max_parallel_pairs,
    )
    fingerprint = study_fingerprint(study)
    result.update(
        {
            "study_id": study.study_id,
            "study_fingerprint": fingerprint,
            "coder_seed": coder_seed,
            "pair_indexes": list(selected),
            "pair_count": len(selected),
            "claim_plane_runtime_version": _claim_plane_version(),
        }
    )
    digest = hashlib.sha256(
        json.dumps(
            {
                "seed": coder_seed,
                "pairs": selected,
                "max_parallel_pairs": max_parallel_pairs,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:12]
    report = _root(paths, fingerprint) / "batches" / f"batch-{digest}.json"
    result["report"] = str(report)
    _atomic_json(report, result)
    return result


__all__ = ["run_physical_batch", "run_physical_pair"]
