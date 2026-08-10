"""Deterministic v2 confirmatory experiment over the frozen 30x3 workload.

This protocol reuses the exact Paper #2 pair set, coder seeds, Planner v1
outputs, and benchmark revision.  It compares four execution modes:

- naive physical parallelism;
- the historical conservative static Claim Plane admission;
- full deterministic semantic admission v2;
- always-serial execution.

Independent pair processes may run through a bounded outer pool to reduce
turnaround time.  That outer concurrency is recorded as harness throughput and
is never counted as Claim Plane speedup.  Scientific wall-clock comparisons are
paired within the same pair/seed unit against the always-serial baseline.
"""

from __future__ import annotations

import hashlib
import json
import os
import statistics
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..common.identity import study_fingerprint
from ..environment import runtime_environment
from ..paper_6pair import runner as harness
from ..paper_6pair.coder import AGENT_TRACE_LOGS, reset_agent_traces
from ..paper_6pair.provider import STATS as CODER_PROVIDER_STATS, reset_provider_state
from ..physical_parallel import (
    parse_pair_indexes,
    python_module_command,
    run_bounded_pair_processes,
)
from .ablation import AblationProfile, deterministic_ablation_verdict
from .config import CODER_SEEDS, N_PAIRS, ConfirmatoryPaths
from .plans import load_plan_bundle, validate_plan_bundle
from .runner import _legacy_pair, load_confirmatory_study

DETERMINISTIC_CONFIRMATORY_PROTOCOL = "claim-plane.deterministic-confirmatory.v2"


class ConfirmatoryMode(str, Enum):
    """Execution modes compared by the deterministic v2 confirmatory study."""

    NAIVE_PARALLEL = "naive_parallel"
    LEGACY_STATIC = "legacy_static"
    DETERMINISTIC_V2 = "deterministic_v2"
    ALWAYS_SERIAL = "always_serial"


DEFAULT_MODES = tuple(ConfirmatoryMode)


@dataclass(frozen=True, slots=True)
class ModeSpec:
    mode: ConfirmatoryMode
    arm: str
    semantic_v2: bool
    description: str


_MODE_SPECS: dict[ConfirmatoryMode, ModeSpec] = {
    ConfirmatoryMode.NAIVE_PARALLEL: ModeSpec(
        mode=ConfirmatoryMode.NAIVE_PARALLEL,
        arm="parallel",
        semantic_v2=False,
        description="Uncoordinated physical A/B execution followed by Git integration.",
    ),
    ConfirmatoryMode.LEGACY_STATIC: ModeSpec(
        mode=ConfirmatoryMode.LEGACY_STATIC,
        arm="claim-plane-static",
        semantic_v2=False,
        description=(
            "Historical conservative static admission used as the direct Paper #2 control."
        ),
    ),
    ConfirmatoryMode.DETERMINISTIC_V2: ModeSpec(
        mode=ConfirmatoryMode.DETERMINISTIC_V2,
        arm="claim-plane-static",
        semantic_v2=True,
        description=(
            "Full Semantic Resource IR v2, Dependency Graph v2, contract propagation, "
            "and semantic conflict taxonomy admission."
        ),
    ),
    ConfirmatoryMode.ALWAYS_SERIAL: ModeSpec(
        mode=ConfirmatoryMode.ALWAYS_SERIAL,
        arm="always-serial",
        semantic_v2=False,
        description="Serial reliability and wall-clock baseline.",
    ),
}

# Four counterbalanced orders.  Every mode appears once in each ordinal position
# across the four schedules, reducing systematic provider-time/order bias while
# preserving a deterministic protocol.
_EXECUTION_ORDERS: tuple[tuple[ConfirmatoryMode, ...], ...] = (
    (
        ConfirmatoryMode.NAIVE_PARALLEL,
        ConfirmatoryMode.LEGACY_STATIC,
        ConfirmatoryMode.DETERMINISTIC_V2,
        ConfirmatoryMode.ALWAYS_SERIAL,
    ),
    (
        ConfirmatoryMode.LEGACY_STATIC,
        ConfirmatoryMode.ALWAYS_SERIAL,
        ConfirmatoryMode.NAIVE_PARALLEL,
        ConfirmatoryMode.DETERMINISTIC_V2,
    ),
    (
        ConfirmatoryMode.DETERMINISTIC_V2,
        ConfirmatoryMode.NAIVE_PARALLEL,
        ConfirmatoryMode.ALWAYS_SERIAL,
        ConfirmatoryMode.LEGACY_STATIC,
    ),
    (
        ConfirmatoryMode.ALWAYS_SERIAL,
        ConfirmatoryMode.DETERMINISTIC_V2,
        ConfirmatoryMode.LEGACY_STATIC,
        ConfirmatoryMode.NAIVE_PARALLEL,
    ),
)


def parse_confirmatory_modes(
    value: str | Sequence[str | ConfirmatoryMode],
) -> tuple[ConfirmatoryMode, ...]:
    raw: Iterable[str | ConfirmatoryMode]
    if isinstance(value, str):
        raw = (item.strip() for item in value.split(",") if item.strip())
    else:
        raw = value
    modes: list[ConfirmatoryMode] = []
    seen: set[ConfirmatoryMode] = set()
    for item in raw:
        mode = item if isinstance(item, ConfirmatoryMode) else ConfirmatoryMode(str(item))
        if mode not in seen:
            seen.add(mode)
            modes.append(mode)
    if not modes:
        raise ValueError("at least one confirmatory mode is required")
    return tuple(modes)


def parse_coder_seeds(value: str | Sequence[int]) -> tuple[int, ...]:
    if isinstance(value, str):
        values = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    else:
        values = tuple(int(item) for item in value)
    if not values:
        raise ValueError("at least one coder seed is required")
    allowed = set(CODER_SEEDS)
    if any(seed not in allowed for seed in values):
        raise ValueError(f"coder seeds must be drawn from {list(CODER_SEEDS)}")
    return tuple(dict.fromkeys(values))


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
        except (FileNotFoundError, RuntimeError):
            pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_version() -> str:
    try:
        from claim_plane import __version__

        return __version__
    except Exception:  # pragma: no cover - diagnostic only
        return "unknown"


def _root(paths: ConfirmatoryPaths, fingerprint: str) -> Path:
    return (
        paths.artifact_root
        / "deterministic-confirmatory-v2"
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


def _execution_order(
    *,
    fingerprint: str,
    coder_seed: int,
    pair_index: int,
    selected_modes: Sequence[ConfirmatoryMode],
) -> tuple[ConfirmatoryMode, ...]:
    key = f"{fingerprint}|{coder_seed}|{pair_index}".encode("utf-8")
    schedule = _EXECUTION_ORDERS[int(hashlib.sha256(key).hexdigest()[:8], 16) % 4]
    selected = set(selected_modes)
    return tuple(mode for mode in schedule if mode in selected)


def _mode_row(
    *,
    mode: ConfirmatoryMode,
    row: Mapping[str, Any],
    wall_time_seconds: float,
    provider_stats: Mapping[str, Any],
    traces: Sequence[Mapping[str, Any]],
    execution_ordinal: int,
    semantic_gate: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result = dict(row)
    result.update(
        {
            "confirmatory_mode": mode.value,
            "source_arm": _MODE_SPECS[mode].arm,
            "confirmatory_mode_description": _MODE_SPECS[mode].description,
            "deterministic_v2_enabled": _MODE_SPECS[mode].semantic_v2,
            "confirmatory_execution_ordinal": execution_ordinal,
            "confirmatory_wall_time_seconds": wall_time_seconds,
            "provider_stats": dict(provider_stats),
            "agent_traces": list(traces),
        }
    )
    if semantic_gate is not None:
        result["deterministic_v2_gate"] = dict(semantic_gate)
    return result


def run_confirmatory_pair(
    paths: ConfirmatoryPaths,
    *,
    coder_seed: int,
    pair_index: int,
    modes: Sequence[ConfirmatoryMode | str] = DEFAULT_MODES,
    repo_root: str | Path = ".",
    resume: bool = True,
) -> dict[str, Any]:
    """Execute one frozen pair under every selected confirmatory mode."""

    selected_modes = parse_confirmatory_modes(modes)
    study = load_confirmatory_study(paths)
    if coder_seed not in CODER_SEEDS:
        raise ValueError(f"coder seed must be one of {list(CODER_SEEDS)}")
    if not 1 <= pair_index <= len(study.pairs):
        raise ValueError(f"pair index must be within 1..{len(study.pairs)}")

    bundle = load_plan_bundle(paths.frozen_plans_file)
    validate_plan_bundle(bundle, study)
    pair = study.pairs[pair_index - 1]
    pair_id = f"{pair.repo}/task{pair.task_id}/feature{pair.feature_a}+feature{pair.feature_b}"
    pair_payload = bundle["pairs"].get(pair_id)
    if not isinstance(pair_payload, dict):
        raise RuntimeError(f"frozen Planner v1 output missing for {pair_id}")
    plan_a = pair_payload["A"]["plan"]
    plan_b = pair_payload["B"]["plan"]

    fingerprint = study_fingerprint(study)
    output_dir = _pair_dir(
        paths,
        fingerprint=fingerprint,
        coder_seed=coder_seed,
        pair_index=pair_index,
    )
    mode_key = "+".join(sorted(mode.value for mode in selected_modes))
    output_file = output_dir / f"result-{hashlib.sha256(mode_key.encode()).hexdigest()[:12]}.json"
    if resume and output_file.exists():
        existing = json.loads(output_file.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            return existing
    if output_file.exists() and not resume:
        raise RuntimeError(
            f"confirmatory artifact already exists; enable resume or remove {output_file}"
        )

    isolated_paths = ConfirmatoryPaths(
        cooperbench=paths.cooperbench,
        artifact_root=paths.artifact_root,
        repo_cache=(
            paths.repo_cache
            / "deterministic-confirmatory-v2"
            / f"seed-{coder_seed}"
            / f"pair-{pair_index:02d}"
        ),
        workspace_root=(
            paths.workspace_root
            / "deterministic-confirmatory-v2"
            / f"seed-{coder_seed}"
            / f"pair-{pair_index:02d}"
        ),
    )
    harness.configure_runtime(isolated_paths, planner=None, pairs=study.pairs)
    repetition = list(study.coder_seeds).index(coder_seed)
    task, _feature_a, _feature_b, base_commit = harness._task_inputs(_legacy_pair(pair))
    repo = harness.get_repo(task.clone_url, base_commit)
    semantic_gate = deterministic_ablation_verdict(
        repo,
        base_commit=base_commit,
        plan_a=plan_a,
        plan_b=plan_b,
        profile=AblationProfile.FULL_V2,
    )

    order = _execution_order(
        fingerprint=fingerprint,
        coder_seed=coder_seed,
        pair_index=pair_index,
        selected_modes=selected_modes,
    )
    rows: list[dict[str, Any]] = []
    pair_started_ns = time.time_ns()
    for ordinal, mode in enumerate(order, start=1):
        spec = _MODE_SPECS[mode]
        reset_provider_state()
        reset_agent_traces()
        started_ns = time.time_ns()
        row = harness.run_pair(
            _legacy_pair(pair),
            spec.arm,
            repetition,
            coder_seed=coder_seed,
            frozen_plans=bundle["pairs"],
            physical_parallel=True,
            admission_override=semantic_gate if spec.semantic_v2 else None,
            ablation_profile="full_v2" if spec.semantic_v2 else None,
        )
        finished_ns = time.time_ns()
        normalized = _mode_row(
            mode=mode,
            row=row,
            wall_time_seconds=(finished_ns - started_ns) / 1_000_000_000,
            provider_stats=_provider_stats(),
            traces=tuple(AGENT_TRACE_LOGS),
            execution_ordinal=ordinal,
            semantic_gate=semantic_gate if spec.semantic_v2 else None,
        )
        rows.append(normalized)
        _atomic_json(output_dir / f"{mode.value}.json", normalized)
    pair_finished_ns = time.time_ns()

    by_mode = {str(row["confirmatory_mode"]): row for row in rows}
    serial = by_mode.get(ConfirmatoryMode.ALWAYS_SERIAL.value)
    comparisons: list[dict[str, Any]] = []
    if serial is not None:
        serial_wall = float(serial.get("confirmatory_wall_time_seconds", 0.0) or 0.0)
        for mode in selected_modes:
            row = by_mode.get(mode.value)
            if row is None:
                continue
            wall = float(row.get("confirmatory_wall_time_seconds", 0.0) or 0.0)
            comparisons.append(
                {
                    "mode": mode.value,
                    "pair_pass": row.get("pair_pass"),
                    "integration_success": row.get("integration_success"),
                    "serialized": bool(row.get("serialized")),
                    "physical_concurrency_observed": bool(
                        row.get("physical_concurrency_observed")
                    ),
                    "physical_overlap_seconds": float(
                        row.get("physical_overlap_seconds", 0.0) or 0.0
                    ),
                    "wall_time_seconds": wall,
                    "paired_speedup_vs_serial": (
                        serial_wall / wall if serial_wall > 0.0 and wall > 0.0 else None
                    ),
                }
            )

    result = {
        "protocol": DETERMINISTIC_CONFIRMATORY_PROTOCOL,
        "study_id": study.study_id,
        "study_fingerprint": fingerprint,
        "claim_plane_runtime_version": _runtime_version(),
        "frozen_plan_manifest_sha256": (
            _sha256_file(paths.frozen_plan_manifest_file)
            if paths.frozen_plan_manifest_file.exists()
            else None
        ),
        "pair_index": pair_index,
        "pair_key": pair.key,
        "gold_conflict": pair.gold_conflict,
        "coder_seed": coder_seed,
        "coder_seed_index": repetition,
        "modes": [mode.value for mode in selected_modes],
        "execution_order": [mode.value for mode in order],
        "rows": rows,
        "paired_comparisons": comparisons,
        "semantic_v2_gate": semantic_gate,
        "started_ns": pair_started_ns,
        "finished_ns": pair_finished_ns,
        "pair_wall_time_seconds": (pair_finished_ns - pair_started_ns) / 1_000_000_000,
        "environment": runtime_environment(),
        "repo_root": str(Path(repo_root).resolve()),
        "artifact": str(output_file),
        "complete": True,
    }
    _atomic_json(output_file, result)
    return result


def _mean(values: Iterable[float]) -> float | None:
    rows = list(values)
    return sum(rows) / len(rows) if rows else None


def _rate(rows: Sequence[Mapping[str, Any]], field: str) -> float | None:
    if not rows:
        return None
    return sum(1 for row in rows if bool(row.get(field))) / len(rows)


def _mode_summary(rows: Sequence[Mapping[str, Any]], mode: ConfirmatoryMode) -> dict[str, Any]:
    selected = [row for row in rows if row.get("confirmatory_mode") == mode.value]
    walls = [float(row.get("confirmatory_wall_time_seconds", 0.0) or 0.0) for row in selected]
    overlaps = [float(row.get("physical_overlap_seconds", 0.0) or 0.0) for row in selected]
    overlap_fractions = [
        float(row.get("physical_overlap_fraction_of_shorter", 0.0) or 0.0)
        for row in selected
    ]
    costs = [float(row.get("coder_cost", 0.0) or 0.0) for row in selected]
    return {
        "mode": mode.value,
        "description": _MODE_SPECS[mode].description,
        "observations": len(selected),
        "pair_pass_rate": _rate(selected, "pair_pass"),
        "integration_success_rate": _rate(selected, "integration_success"),
        "serialization_rate": _rate(selected, "serialized"),
        "physical_concurrency_rate": _rate(selected, "physical_concurrency_observed"),
        "mean_physical_overlap_seconds": _mean(overlaps),
        "mean_overlap_fraction_of_shorter": _mean(overlap_fractions),
        "mean_wall_time_seconds": _mean(walls),
        "median_wall_time_seconds": statistics.median(walls) if walls else None,
        "mean_coder_cost": _mean(costs),
        "coder_cost_total": sum(costs),
        "scope_promotion_attempts": sum(
            int(row.get("scope_promotion_attempts", 0) or 0) for row in selected
        ),
        "dynamic_restarts": sum(int(row.get("dynamic_restart_count", 0) or 0) for row in selected),
    }


def _collect_pair_results(
    paths: ConfirmatoryPaths,
    *,
    fingerprint: str,
    seeds: Sequence[int],
    pair_indexes: Sequence[int],
    modes: Sequence[ConfirmatoryMode],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    pair_results: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    mode_key = "+".join(sorted(mode.value for mode in modes))
    result_name = f"result-{hashlib.sha256(mode_key.encode()).hexdigest()[:12]}.json"
    for seed in seeds:
        for pair_index in pair_indexes:
            path = _pair_dir(
                paths,
                fingerprint=fingerprint,
                coder_seed=seed,
                pair_index=pair_index,
            ) / result_name
            if not path.exists():
                missing.append(f"seed-{seed}/pair-{pair_index:02d}")
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not payload.get("complete"):
                missing.append(f"seed-{seed}/pair-{pair_index:02d}")
                continue
            pair_results.append(payload)
            payload_rows = payload.get("rows") or []
            if isinstance(payload_rows, list):
                rows.extend(dict(row) for row in payload_rows if isinstance(row, dict))
    return pair_results, rows, missing


def build_confirmatory_report(
    paths: ConfirmatoryPaths,
    *,
    seeds: Sequence[int] = CODER_SEEDS,
    pair_indexes: Sequence[int] = tuple(range(1, N_PAIRS + 1)),
    modes: Sequence[ConfirmatoryMode | str] = DEFAULT_MODES,
    require_complete: bool = False,
) -> dict[str, Any]:
    """Aggregate deterministic v2 pair artifacts without making model calls."""

    selected_modes = parse_confirmatory_modes(modes)
    selected_seeds = parse_coder_seeds(tuple(seeds))
    selected_pairs = tuple(sorted(set(int(index) for index in pair_indexes)))
    if not selected_pairs:
        raise ValueError("at least one pair index is required")
    study = load_confirmatory_study(paths)
    fingerprint = study_fingerprint(study)
    pair_results, rows, missing = _collect_pair_results(
        paths,
        fingerprint=fingerprint,
        seeds=selected_seeds,
        pair_indexes=selected_pairs,
        modes=selected_modes,
    )
    expected_pair_units = len(selected_seeds) * len(selected_pairs)
    expected_rows = expected_pair_units * len(selected_modes)
    if require_complete and missing:
        raise RuntimeError(
            f"deterministic confirmatory matrix is incomplete: {len(missing)} pair units missing"
        )

    summaries = [_mode_summary(rows, mode) for mode in selected_modes]
    paired_speedups: dict[str, list[float]] = {mode.value: [] for mode in selected_modes}
    serial_rows: dict[tuple[str, int], Mapping[str, Any]] = {
        (str(row.get("pair")), int(row.get("coder_seed", -1))): row
        for row in rows
        if row.get("confirmatory_mode") == ConfirmatoryMode.ALWAYS_SERIAL.value
    }
    for row in rows:
        key = (str(row.get("pair")), int(row.get("coder_seed", -1)))
        serial = serial_rows.get(key)
        if serial is None:
            continue
        wall = float(row.get("confirmatory_wall_time_seconds", 0.0) or 0.0)
        serial_wall = float(serial.get("confirmatory_wall_time_seconds", 0.0) or 0.0)
        if wall > 0 and serial_wall > 0:
            paired_speedups[str(row.get("confirmatory_mode"))].append(serial_wall / wall)

    speedup_summary = {
        mode.value: {
            "paired_observations": len(paired_speedups[mode.value]),
            "mean_speedup_vs_serial": _mean(paired_speedups[mode.value]),
            "median_speedup_vs_serial": (
                statistics.median(paired_speedups[mode.value])
                if paired_speedups[mode.value]
                else None
            ),
        }
        for mode in selected_modes
    }

    summary_by_mode = {item["mode"]: item for item in summaries}
    legacy = summary_by_mode.get(ConfirmatoryMode.LEGACY_STATIC.value)
    v2 = summary_by_mode.get(ConfirmatoryMode.DETERMINISTIC_V2.value)
    direct_delta: dict[str, Any] | None = None
    if legacy is not None and v2 is not None:
        def delta(field: str) -> float | None:
            left = legacy.get(field)
            right = v2.get(field)
            if left is None or right is None:
                return None
            return float(right) - float(left)

        direct_delta = {
            "serialization_rate_delta_v2_minus_legacy": delta("serialization_rate"),
            "pair_pass_rate_delta_v2_minus_legacy": delta("pair_pass_rate"),
            "integration_success_rate_delta_v2_minus_legacy": delta(
                "integration_success_rate"
            ),
            "physical_concurrency_rate_delta_v2_minus_legacy": delta(
                "physical_concurrency_rate"
            ),
            "mean_wall_time_seconds_delta_v2_minus_legacy": delta(
                "mean_wall_time_seconds"
            ),
        }

    report = {
        "protocol": DETERMINISTIC_CONFIRMATORY_PROTOCOL,
        "study_id": study.study_id,
        "study_fingerprint": fingerprint,
        "claim_plane_runtime_version": _runtime_version(),
        "frozen_plan_manifest_sha256": (
            _sha256_file(paths.frozen_plan_manifest_file)
            if paths.frozen_plan_manifest_file.exists()
            else None
        ),
        "seeds": list(selected_seeds),
        "pair_indexes": list(selected_pairs),
        "modes": [mode.value for mode in selected_modes],
        "expected_pair_units": expected_pair_units,
        "completed_pair_units": len(pair_results),
        "expected_rows": expected_rows,
        "observed_rows": len(rows),
        "complete": not missing and len(rows) == expected_rows,
        "missing_pair_units": missing,
        "mode_summary": summaries,
        "paired_speedup_vs_serial": speedup_summary,
        "deterministic_v2_vs_legacy_static": direct_delta,
        "outer_concurrency_interpretation": (
            "Outer pair concurrency reduces experiment turnaround only and is excluded "
            "from Claim Plane speedup measurements."
        ),
        "environment": runtime_environment(),
    }
    return report


def run_confirmatory_batch(
    paths: ConfirmatoryPaths,
    *,
    seeds: Sequence[int] = CODER_SEEDS,
    pair_indexes: Sequence[int] = tuple(range(1, N_PAIRS + 1)),
    modes: Sequence[ConfirmatoryMode | str] = DEFAULT_MODES,
    max_parallel_pairs: int = 6,
    repo_root: str | Path = ".",
    resume: bool = True,
) -> dict[str, Any]:
    """Execute selected pair/seed units through the bounded outer pool."""

    selected_modes = parse_confirmatory_modes(modes)
    selected_seeds = parse_coder_seeds(tuple(seeds))
    selected_pairs = tuple(sorted(set(int(index) for index in pair_indexes)))
    if not selected_pairs:
        raise ValueError("at least one pair index is required")
    if selected_pairs != parse_pair_indexes(
        ",".join(str(index) for index in selected_pairs), pair_count=N_PAIRS
    ):
        raise ValueError("invalid pair indexes")
    if max_parallel_pairs <= 0:
        raise ValueError("max_parallel_pairs must be positive")

    mode_arg = ",".join(mode.value for mode in selected_modes)
    commands: list[tuple[str, tuple[str, ...]]] = []
    for seed in selected_seeds:
        for pair_index in selected_pairs:
            args = [
                "confirmatory",
                "final-pair",
                "--cooperbench",
                str(paths.cooperbench),
                "--artifacts",
                str(paths.artifact_root),
                "--repo-cache",
                str(paths.repo_cache),
                "--workspace",
                str(paths.workspace_root),
                "--seed",
                str(seed),
                "--pair",
                str(pair_index),
                "--modes",
                mode_arg,
                "--repo",
                str(repo_root),
            ]
            if not resume:
                args.append("--no-resume")
            commands.append(
                (f"seed-{seed}-pair-{pair_index:02d}", python_module_command(*args))
            )

    started_ns = time.time_ns()
    pool = run_bounded_pair_processes(commands, max_parallel_pairs=max_parallel_pairs)
    finished_ns = time.time_ns()
    study = load_confirmatory_study(paths)
    fingerprint = study_fingerprint(study)
    report = build_confirmatory_report(
        paths,
        seeds=selected_seeds,
        pair_indexes=selected_pairs,
        modes=selected_modes,
        require_complete=False,
    )
    result = {
        **pool,
        "protocol": DETERMINISTIC_CONFIRMATORY_PROTOCOL,
        "study_id": study.study_id,
        "study_fingerprint": fingerprint,
        "claim_plane_runtime_version": _runtime_version(),
        "seeds": list(selected_seeds),
        "pair_indexes": list(selected_pairs),
        "pair_unit_count": len(selected_seeds) * len(selected_pairs),
        "modes": [mode.value for mode in selected_modes],
        "max_parallel_pairs": max_parallel_pairs,
        "batch_started_ns": started_ns,
        "batch_finished_ns": finished_ns,
        "batch_wall_time_seconds": (finished_ns - started_ns) / 1_000_000_000,
        "scientific_report": report,
    }
    digest = hashlib.sha256(
        json.dumps(
            {
                "seeds": selected_seeds,
                "pairs": selected_pairs,
                "modes": [mode.value for mode in selected_modes],
                "max_parallel_pairs": max_parallel_pairs,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:12]
    output = _root(paths, fingerprint) / "batches" / f"batch-{digest}.json"
    result["report"] = str(output)
    _atomic_json(output, result)

    if report["complete"] and set(selected_seeds) == set(CODER_SEEDS) and selected_pairs == tuple(
        range(1, N_PAIRS + 1)
    ) and tuple(selected_modes) == DEFAULT_MODES:
        final_path = _root(paths, fingerprint) / "analysis" / "final-report.json"
        _atomic_json(final_path, report)
        result["final_report"] = str(final_path)
    return result


def confirmatory_status(paths: ConfirmatoryPaths) -> dict[str, Any]:
    """Report completion of the complete 30x3 deterministic v2 matrix offline."""

    try:
        report = build_confirmatory_report(paths, require_complete=False)
    except (FileNotFoundError, RuntimeError):
        return {
            "protocol": DETERMINISTIC_CONFIRMATORY_PROTOCOL,
            "prepared": False,
            "complete": False,
            "expected_pair_units": N_PAIRS * len(CODER_SEEDS),
            "completed_pair_units": 0,
        }
    return {"prepared": True, **report}


def aggregate_confirmatory_experiment(paths: ConfirmatoryPaths) -> dict[str, Any]:
    """Require and seal the complete 30x3 × four-mode result matrix."""

    report = build_confirmatory_report(paths, require_complete=True)
    if report["observed_rows"] != N_PAIRS * len(CODER_SEEDS) * len(DEFAULT_MODES):
        raise RuntimeError("deterministic confirmatory row matrix is incomplete")
    study = load_confirmatory_study(paths)
    fingerprint = study_fingerprint(study)
    analysis_dir = _root(paths, fingerprint) / "analysis"
    final_path = analysis_dir / "final-report.json"
    _atomic_json(final_path, report)
    digest = _sha256_file(final_path)
    manifest = {
        "protocol": DETERMINISTIC_CONFIRMATORY_PROTOCOL,
        "study_fingerprint": fingerprint,
        "final_report": final_path.name,
        "final_report_sha256": digest,
        "rows": report["observed_rows"],
        "pair_units": report["completed_pair_units"],
        "sealed_at_ns": time.time_ns(),
    }
    manifest_path = analysis_dir / "manifest.json"
    _atomic_json(manifest_path, manifest)
    return {
        **report,
        "final_report": str(final_path),
        "manifest": str(manifest_path),
        "final_report_sha256": digest,
    }


__all__ = [
    "ConfirmatoryMode",
    "DEFAULT_MODES",
    "DETERMINISTIC_CONFIRMATORY_PROTOCOL",
    "aggregate_confirmatory_experiment",
    "build_confirmatory_report",
    "confirmatory_status",
    "parse_coder_seeds",
    "parse_confirmatory_modes",
    "run_confirmatory_batch",
    "run_confirmatory_pair",
]
