"""Confirmatory physical benchmark for admission-granularity stages.

This benchmark is the physical counterpart to Admission Granularity Ablation.  It
reuses the exact frozen CooperBench 30x3 workload, coder seeds, Planner v1
declarations, and physical overlap instrumentation.  Six paired profiles are run:

* serial reliability baseline;
* naive uncoordinated parallelism;
* broad declared authority;
* symbol-scoped authority projection;
* dependency-aware authority narrowing;
* refined conflict policy.

All four controlled admission profiles consume the same pinned WorkGraph and the
same warm SCIP-backed SemanticDependencyGraph with candidate blocking disabled.
That keeps the comparison causal: only the admission-granularity stage changes.
The cold cache-prime step is recorded but excluded from profile end-to-end timing;
each controlled profile is charged the same standalone warm graph-preparation cost
plus its own admission-decision time.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import statistics
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from claim_plane.swarm import (
    AdmissionGranularityProfile,
    WorkGraph,
    compute_concurrency_plan,
    run_admission_granularity_ablation,
)

from ..common.identity import study_fingerprint
from ..environment import runtime_environment
from ..paper_6pair import runner as harness
from ..paper_6pair.coder import AGENT_TRACE_LOGS, reset_agent_traces
from ..physical_parallel import (
    parse_pair_indexes,
    python_module_command,
    run_bounded_pair_processes,
)
from .ablation import _intent_ast_anchor_evidence, _policy, _python_sources_at_revision
from .config import CODER_SEEDS, N_PAIRS, ConfirmatoryPaths
from .final import parse_coder_seeds
from .plans import load_plan_bundle, validate_plan_bundle
from .runner import _legacy_pair, load_confirmatory_study
from .scip_v3 import (
    ScipV3ExecutionOutcome,
    _annotate_measurement_validity,
    _critical_path_seconds,
    _fresh_builtin_graph,
    _mean_active_agents,
    _measurement_validity,
    _paired_speedup,
    _provider_stats,
    _required_scip_graph,
    _runtime_version,
    _sha256_file,
    _work_graph,
)

ADMISSION_PHYSICAL_V1_PROTOCOL = (
    "claim-plane.admission-granularity-physical-benchmark.v1"
)
ADMISSION_PHYSICAL_V1_RESULT_REVISION = 1


class AdmissionPhysicalProfile(str, Enum):
    SERIAL = "serial"
    NAIVE_PARALLEL = "naive_parallel"
    BROAD_DECLARED = "broad_declared"
    SYMBOL_PROJECTION = "symbol_projection"
    DEPENDENCY_NARROWING = "dependency_narrowing"
    REFINED_POLICY = "refined_policy"


DEFAULT_ADMISSION_PHYSICAL_PROFILES = tuple(AdmissionPhysicalProfile)
_CONTROLLED_PROFILES = (
    AdmissionPhysicalProfile.BROAD_DECLARED,
    AdmissionPhysicalProfile.SYMBOL_PROJECTION,
    AdmissionPhysicalProfile.DEPENDENCY_NARROWING,
    AdmissionPhysicalProfile.REFINED_POLICY,
)
_CONTROLLED_STAGE = {
    AdmissionPhysicalProfile.BROAD_DECLARED: AdmissionGranularityProfile.BROAD_DECLARED,
    AdmissionPhysicalProfile.SYMBOL_PROJECTION: AdmissionGranularityProfile.SYMBOL_PROJECTION,
    AdmissionPhysicalProfile.DEPENDENCY_NARROWING: AdmissionGranularityProfile.DEPENDENCY_NARROWING,
    AdmissionPhysicalProfile.REFINED_POLICY: AdmissionGranularityProfile.REFINED_POLICY,
}


@dataclass(frozen=True, slots=True)
class ProfileSpec:
    profile: AdmissionPhysicalProfile
    arm: str
    stage: AdmissionGranularityProfile | None
    description: str


_PROFILE_SPECS = {
    AdmissionPhysicalProfile.SERIAL: ProfileSpec(
        AdmissionPhysicalProfile.SERIAL,
        "always-serial",
        None,
        "Always-serial reliability and wall-clock baseline.",
    ),
    AdmissionPhysicalProfile.NAIVE_PARALLEL: ProfileSpec(
        AdmissionPhysicalProfile.NAIVE_PARALLEL,
        "parallel",
        None,
        "Uncoordinated physical A/B execution followed by Git integration.",
    ),
    AdmissionPhysicalProfile.BROAD_DECLARED: ProfileSpec(
        AdmissionPhysicalProfile.BROAD_DECLARED,
        "claim-plane-static",
        AdmissionGranularityProfile.BROAD_DECLARED,
        "Claim Plane admission using broad planner-declared authority.",
    ),
    AdmissionPhysicalProfile.SYMBOL_PROJECTION: ProfileSpec(
        AdmissionPhysicalProfile.SYMBOL_PROJECTION,
        "claim-plane-static",
        AdmissionGranularityProfile.SYMBOL_PROJECTION,
        "Claim Plane admission with Symbol-Scoped Authority Projection v2.",
    ),
    AdmissionPhysicalProfile.DEPENDENCY_NARROWING: ProfileSpec(
        AdmissionPhysicalProfile.DEPENDENCY_NARROWING,
        "claim-plane-static",
        AdmissionGranularityProfile.DEPENDENCY_NARROWING,
        "Claim Plane admission with dependency-closed authority evidence.",
    ),
    AdmissionPhysicalProfile.REFINED_POLICY: ProfileSpec(
        AdmissionPhysicalProfile.REFINED_POLICY,
        "claim-plane-static",
        AdmissionGranularityProfile.REFINED_POLICY,
        "Production admission with refined evidence-backed conflict policy.",
    ),
}


def parse_admission_physical_profiles(
    value: str | Sequence[str | AdmissionPhysicalProfile],
) -> tuple[AdmissionPhysicalProfile, ...]:
    raw: Iterable[str | AdmissionPhysicalProfile]
    if isinstance(value, str):
        raw = (item.strip() for item in value.split(",") if item.strip())
    else:
        raw = value
    profiles: list[AdmissionPhysicalProfile] = []
    seen: set[AdmissionPhysicalProfile] = set()
    for item in raw:
        profile = (
            item
            if isinstance(item, AdmissionPhysicalProfile)
            else AdmissionPhysicalProfile(str(item))
        )
        if profile not in seen:
            profiles.append(profile)
            seen.add(profile)
    if not profiles:
        raise ValueError("at least one admission physical profile is required")
    return tuple(profiles)


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


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
        / "admission-granularity-physical-v1"
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


def _result_name(profiles: Sequence[AdmissionPhysicalProfile]) -> str:
    key = "+".join(sorted(profile.value for profile in profiles))
    revisioned = f"{key}|result-revision={ADMISSION_PHYSICAL_V1_RESULT_REVISION}"
    return f"result-{hashlib.sha256(revisioned.encode()).hexdigest()[:12]}.json"


def _execution_order(
    *,
    fingerprint: str,
    coder_seed: int,
    pair_index: int,
    profiles: Sequence[AdmissionPhysicalProfile],
) -> tuple[AdmissionPhysicalProfile, ...]:
    canonical = DEFAULT_ADMISSION_PHYSICAL_PROFILES
    key = f"{fingerprint}|{coder_seed}|{pair_index}|admission-physical-v1".encode()
    offset = int(hashlib.sha256(key).hexdigest()[:8], 16) % len(canonical)
    rotated = canonical[offset:] + canonical[:offset]
    selected = set(profiles)
    return tuple(profile for profile in rotated if profile in selected)


def _verdict_from_stage(
    graph: WorkGraph,
    semantic_graph: Any,
    *,
    profile: AdmissionPhysicalProfile,
    ablation_fingerprint: str,
    ablation_profile_result: Mapping[str, Any],
) -> tuple[dict[str, Any], float]:
    stage = _CONTROLLED_STAGE[profile]
    started = time.perf_counter_ns()
    concurrency = compute_concurrency_plan(
        graph,
        _policy(),
        semantic_graph=semantic_graph,
        candidate_blocking_enabled=False,
        admission_granularity_stage=stage.value,
    )
    admission_seconds = (time.perf_counter_ns() - started) / 1_000_000_000
    if concurrency.fingerprint() != str(ablation_profile_result["plan_fingerprint"]):
        raise RuntimeError(
            f"physical admission plan diverged from 9E ablation for {profile.value}"
        )
    waves = [list(wave.work_ids) for wave in concurrency.waves]
    if concurrency.status.value == "replan_required":
        serialized, allowed, kind, serial_order = True, False, "replan_required", "A->B"
    elif len(waves) == 1 and set(waves[0]) == {"A", "B"}:
        serialized, allowed, kind, serial_order = False, True, "parallel", None
    else:
        flattened = [work_id for wave in waves for work_id in wave]
        serialized = True
        allowed = True
        kind = "ordered" if any(len(wave) == 1 for wave in waves) else "serialized"
        serial_order = "->".join(flattened) if flattened else "A->B"
    return (
        {
            "serialized": serialized,
            "kind": kind,
            "allowed": allowed,
            "reason": f"Admission Physical v1 profile {profile.value}: {kind}.",
            "valid_for_accuracy": True,
            "serial_order": serial_order,
            "admission_physical_profile": profile.value,
            "admission_physical_evidence": {
                "protocol": ADMISSION_PHYSICAL_V1_PROTOCOL,
                "profile": profile.value,
                "stage": stage.value,
                "candidate_blocking_enabled": False,
                "work_graph_fingerprint": graph.fingerprint(),
                "semantic_graph_fingerprint": semantic_graph.fingerprint,
                "semantic_graph_revision": semantic_graph.metadata.get("source_revision"),
                "code_intelligence_sources": semantic_graph.metadata.get(
                    "code_intelligence_sources"
                ),
                "ablation_fingerprint": ablation_fingerprint,
                "ablation_profile_result": dict(ablation_profile_result),
                "intent_ast_anchors": _intent_ast_anchor_evidence(graph),
                "concurrency_plan": concurrency.to_dict(),
                "execution_waves": waves,
            },
        },
        admission_seconds,
    )


def build_pair_admission_profiles(
    repo: str | Path,
    *,
    base_commit: str,
    plan_a: Mapping[str, Any],
    plan_b: Mapping[str, Any],
    cache_root: str | Path,
    profiles: Sequence[AdmissionPhysicalProfile | str] = _CONTROLLED_PROFILES,
) -> dict[AdmissionPhysicalProfile, dict[str, Any]]:
    """Build one shared warm SCIP graph and derive every selected 9E gate from it."""

    selected = parse_admission_physical_profiles(profiles)
    unsupported = set(selected) - set(_CONTROLLED_PROFILES)
    if unsupported:
        raise ValueError(
            "admission profile builder accepts controlled profiles only: "
            + ", ".join(sorted(item.value for item in unsupported))
        )
    root = Path(repo).resolve()
    cache = Path(cache_root).resolve()
    shutil.rmtree(cache, ignore_errors=True)
    cache.mkdir(parents=True, exist_ok=True)

    projection_started = time.perf_counter_ns()
    sources = _python_sources_at_revision(root, base_commit)
    graph = _work_graph(plan_a, plan_b, sources=sources)
    projection_seconds = (time.perf_counter_ns() - projection_started) / 1_000_000_000

    builtin_started = time.perf_counter_ns()
    builtin = _fresh_builtin_graph(sources, revision=base_commit)
    builtin_seconds = (time.perf_counter_ns() - builtin_started) / 1_000_000_000

    # Prime an isolated exact-revision SCIP cache outside scientific profile timing.
    seed_started = time.perf_counter_ns()
    _seed_graph, seed_scip = _required_scip_graph(
        root, builtin, revision=base_commit, cache_root=cache, force=True
    )
    cache_seed_seconds = (time.perf_counter_ns() - seed_started) / 1_000_000_000

    warm_started = time.perf_counter_ns()
    warm_graph, warm_scip = _required_scip_graph(
        root, builtin, revision=base_commit, cache_root=cache, force=False
    )
    warm_graph_seconds = (time.perf_counter_ns() - warm_started) / 1_000_000_000
    if warm_scip.get("scip_cache_hit") is not True:
        raise RuntimeError("Admission Physical v1 requires an exact warm SCIP cache hit")

    audit_started = time.perf_counter_ns()
    ablation = run_admission_granularity_ablation(
        graph,
        _policy(),
        semantic_graph=warm_graph,
    )
    audit_seconds = (time.perf_counter_ns() - audit_started) / 1_000_000_000
    ablation_payload = ablation.to_dict()
    ablation_fingerprint = ablation.fingerprint
    profile_results = {
        str(item["profile"]): item for item in ablation_payload["profiles"]
    }

    shared_graph_seconds = projection_seconds + builtin_seconds + warm_graph_seconds
    results: dict[AdmissionPhysicalProfile, dict[str, Any]] = {}
    for profile in selected:
        stage = _CONTROLLED_STAGE[profile]
        static_result = profile_results[stage.value]
        verdict, admission_seconds = _verdict_from_stage(
            graph,
            warm_graph,
            profile=profile,
            ablation_fingerprint=ablation_fingerprint,
            ablation_profile_result=static_result,
        )
        results[profile] = {
            "verdict": verdict,
            "timing": {
                "shared_projection_seconds": projection_seconds,
                "builtin_graph_seconds": builtin_seconds,
                "scip_cache_seed_seconds_excluded": cache_seed_seconds,
                "scip_seed_artifact_sha256": seed_scip.get("scip_artifact_sha256"),
                "scip_seed_cache_hit": seed_scip.get("scip_cache_hit"),
                "scip_index_seconds": warm_scip["scip_index_seconds"],
                "scip_decode_graph_seconds": warm_scip["scip_decode_graph_seconds"],
                "graph_merge_seconds": warm_scip["graph_merge_seconds"],
                "scip_cache_hit": warm_scip["scip_cache_hit"],
                "scip_cache_key": warm_scip["scip_cache_key"],
                "scip_artifact_sha256": warm_scip["scip_artifact_sha256"],
                "scip_artifact_size_bytes": warm_scip["scip_artifact_size_bytes"],
                "scip_indexer_id": warm_scip["scip_indexer_id"],
                "scip_indexer_version": warm_scip["scip_indexer_version"],
                "workspace_fingerprint": warm_scip["workspace_fingerprint"],
                "warm_scip_graph_total_seconds": warm_graph_seconds,
                "shared_warm_graph_preparation_seconds": shared_graph_seconds,
                "admission_seconds": admission_seconds,
                "ablation_audit_seconds_excluded": audit_seconds,
                "control_plane_seconds": shared_graph_seconds + admission_seconds,
            },
            "static_ablation": {
                "protocol": ablation.protocol,
                "fingerprint": ablation_fingerprint,
                "profile": static_result,
                "summary": ablation.summary(),
            },
        }
    return results


def run_admission_physical_pair(
    paths: ConfirmatoryPaths,
    *,
    coder_seed: int,
    pair_index: int,
    profiles: Sequence[AdmissionPhysicalProfile | str] = DEFAULT_ADMISSION_PHYSICAL_PROFILES,
    repo_root: str | Path = ".",
    resume: bool = True,
) -> dict[str, Any]:
    selected = parse_admission_physical_profiles(profiles)
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
    plan_a, plan_b = pair_payload["A"]["plan"], pair_payload["B"]["plan"]

    fingerprint = study_fingerprint(study)
    output_dir = _pair_dir(
        paths, fingerprint=fingerprint, coder_seed=coder_seed, pair_index=pair_index
    )
    output_file = output_dir / _result_name(selected)
    if resume and output_file.exists():
        payload = json.loads(output_file.read_text(encoding="utf-8"))
        expected_profiles = [profile.value for profile in selected]
        if (
            not isinstance(payload, dict)
            or payload.get("protocol") != ADMISSION_PHYSICAL_V1_PROTOCOL
            or payload.get("result_revision") != ADMISSION_PHYSICAL_V1_RESULT_REVISION
            or payload.get("study_fingerprint") != fingerprint
            or payload.get("coder_seed") != coder_seed
            or payload.get("pair_index") != pair_index
            or payload.get("profiles") != expected_profiles
            or payload.get("complete") is not True
        ):
            raise RuntimeError(
                "Admission Physical v1 resume artifact does not match the current result contract: "
                f"{output_file}"
            )
        return payload
    if output_file.exists() and not resume:
        raise RuntimeError(
            f"Admission Physical v1 artifact already exists; remove {output_file} or resume"
        )

    isolated_paths = ConfirmatoryPaths(
        cooperbench=paths.cooperbench,
        artifact_root=paths.artifact_root,
        repo_cache=(
            paths.repo_cache
            / "admission-physical-v1"
            / f"seed-{coder_seed}"
            / f"pair-{pair_index:02d}"
        ),
        workspace_root=(
            paths.workspace_root
            / "admission-physical-v1"
            / f"seed-{coder_seed}"
            / f"pair-{pair_index:02d}"
        ),
    )
    harness.configure_runtime(isolated_paths, planner=None, pairs=study.pairs)
    repetition = list(study.coder_seeds).index(coder_seed)
    task, _feature_a, _feature_b, base_commit = harness._task_inputs(_legacy_pair(pair))
    repo = harness.get_repo(task.clone_url, base_commit)
    pair_started = time.time_ns()

    controlled = tuple(profile for profile in selected if profile in _CONTROLLED_PROFILES)
    gates: dict[AdmissionPhysicalProfile, dict[str, Any]] = {}
    if controlled:
        gates = build_pair_admission_profiles(
            repo,
            base_commit=base_commit,
            plan_a=plan_a,
            plan_b=plan_b,
            cache_root=output_dir / "code-intelligence-cache",
            profiles=controlled,
        )

    order = _execution_order(
        fingerprint=fingerprint,
        coder_seed=coder_seed,
        pair_index=pair_index,
        profiles=selected,
    )
    rows: list[dict[str, Any]] = []
    for ordinal, profile in enumerate(order, start=1):
        spec = _PROFILE_SPECS[profile]
        gate_record = gates.get(profile)
        gate = None if gate_record is None else gate_record["verdict"]
        timing = {} if gate_record is None else dict(gate_record["timing"])
        reset_provider_state()
        reset_agent_traces()
        started = time.time_ns()
        row = harness.run_pair(
            _legacy_pair(pair),
            spec.arm,
            repetition,
            coder_seed=coder_seed,
            frozen_plans=bundle["pairs"],
            physical_parallel=True,
            admission_override=gate,
            ablation_profile=profile.value if gate is not None else None,
        )
        finished = time.time_ns()
        execution_seconds = (finished - started) / 1_000_000_000
        control_seconds = float(timing.get("control_plane_seconds", 0.0) or 0.0)
        normalized = _annotate_measurement_validity(
            {
                **dict(row),
                "coder_seed": coder_seed,
                "pair_index": pair_index,
                "pair_key": pair.key,
                "admission_physical_profile": profile.value,
                "admission_physical_description": spec.description,
                "admission_physical_execution_ordinal": ordinal,
                "admission_granularity_stage": (
                    None if spec.stage is None else spec.stage.value
                ),
                "execution_wall_time_seconds": execution_seconds,
                "control_plane_wall_time_seconds": control_seconds,
                "end_to_end_wall_time_seconds": execution_seconds + control_seconds,
                "critical_path_seconds": _critical_path_seconds(row),
                "mean_active_agents": _mean_active_agents(row),
                "control_plane": timing,
                "admission_physical_gate": gate,
                "static_ablation": (
                    None if gate_record is None else gate_record["static_ablation"]
                ),
                "provider_stats": _provider_stats(),
                "agent_traces": list(AGENT_TRACE_LOGS),
            }
        )
        rows.append(normalized)
        _atomic_json(output_dir / f"{profile.value}.json", normalized)
    pair_finished = time.time_ns()

    by_profile = {str(row["admission_physical_profile"]): row for row in rows}
    serial = by_profile.get(AdmissionPhysicalProfile.SERIAL.value)
    broad = by_profile.get(AdmissionPhysicalProfile.BROAD_DECLARED.value)
    comparisons: list[dict[str, Any]] = []
    for profile in selected:
        row = by_profile.get(profile.value)
        if row is None:
            continue
        serial_execution = serial_e2e = serial_reason = None
        broad_execution = broad_e2e = broad_reason = None
        if serial is not None:
            serial_execution, serial_e2e, serial_reason = _paired_speedup(serial, row)
        if broad is not None:
            broad_execution, broad_e2e, broad_reason = _paired_speedup(broad, row)
        validity = _measurement_validity(row)
        comparisons.append(
            {
                "profile": profile.value,
                "pair_pass": row.get("pair_pass"),
                "integration_success": row.get("integration_success"),
                "serialized": bool(row.get("serialized")),
                "physical_concurrency_observed": bool(
                    row.get("physical_concurrency_observed")
                ),
                "execution_outcome": validity["execution_outcome"],
                "execution_wall_time_seconds": row.get("execution_wall_time_seconds"),
                "control_plane_wall_time_seconds": row.get(
                    "control_plane_wall_time_seconds"
                ),
                "end_to_end_wall_time_seconds": row.get("end_to_end_wall_time_seconds"),
                "speedup_vs_serial_execution": serial_execution,
                "speedup_vs_serial_end_to_end": serial_e2e,
                "serial_speedup_exclusion_reason": serial_reason,
                "speedup_vs_broad_execution": broad_execution,
                "speedup_vs_broad_end_to_end": broad_e2e,
                "broad_speedup_exclusion_reason": broad_reason,
            }
        )

    result = {
        "protocol": ADMISSION_PHYSICAL_V1_PROTOCOL,
        "result_revision": ADMISSION_PHYSICAL_V1_RESULT_REVISION,
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
        "profiles": [profile.value for profile in selected],
        "execution_order": [profile.value for profile in order],
        "rows": rows,
        "paired_comparisons": comparisons,
        "started_ns": pair_started,
        "finished_ns": pair_finished,
        "pair_wall_time_seconds": (pair_finished - pair_started) / 1_000_000_000,
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


def _profile_summary(
    rows: Sequence[Mapping[str, Any]], profile: AdmissionPhysicalProfile
) -> dict[str, Any]:
    selected = [
        _annotate_measurement_validity(row)
        for row in rows
        if row.get("admission_physical_profile") == profile.value
    ]
    timing_selected = [row for row in selected if bool(row.get("speedup_eligible"))]
    outcome_counts = {outcome.value: 0 for outcome in ScipV3ExecutionOutcome}
    for row in selected:
        outcome_counts[str(row["execution_outcome"])] += 1
    execution = [float(row.get("execution_wall_time_seconds", 0.0) or 0.0) for row in timing_selected]
    e2e = [float(row.get("end_to_end_wall_time_seconds", 0.0) or 0.0) for row in timing_selected]
    control = [float(row.get("control_plane_wall_time_seconds", 0.0) or 0.0) for row in timing_selected]
    critical = [float(row.get("critical_path_seconds", 0.0) or 0.0) for row in timing_selected]
    active = [float(row.get("mean_active_agents", 0.0) or 0.0) for row in timing_selected]
    raw_execution = [float(row.get("execution_wall_time_seconds", 0.0) or 0.0) for row in selected]
    static_parallel = [
        row
        for row in selected
        if isinstance(row.get("admission_physical_gate"), Mapping)
    ]
    return {
        "profile": profile.value,
        "description": _PROFILE_SPECS[profile].description,
        "admission_granularity_stage": (
            None
            if _PROFILE_SPECS[profile].stage is None
            else _PROFILE_SPECS[profile].stage.value
        ),
        "observations": len(selected),
        "timing_observations": len(timing_selected),
        "excluded_timing_observations": len(selected) - len(timing_selected),
        "execution_outcome_counts": outcome_counts,
        "pair_pass_rate": _rate(selected, "pair_pass"),
        "integration_success_rate": _rate(selected, "integration_success"),
        "serialization_rate": _rate(selected, "serialized"),
        "physical_concurrency_rate": _rate(selected, "physical_concurrency_observed"),
        "static_parallel_eligibility_rate": (
            None
            if not static_parallel
            else sum(
                1
                for row in static_parallel
                if not bool(row["admission_physical_gate"].get("serialized"))
                and bool(row["admission_physical_gate"].get("allowed"))
            )
            / len(static_parallel)
        ),
        "mean_active_agents": _mean(active),
        "mean_critical_path_seconds": _mean(critical),
        "mean_execution_wall_time_seconds": _mean(execution),
        "median_execution_wall_time_seconds": statistics.median(execution) if execution else None,
        "mean_control_plane_wall_time_seconds": _mean(control),
        "mean_end_to_end_wall_time_seconds": _mean(e2e),
        "median_end_to_end_wall_time_seconds": statistics.median(e2e) if e2e else None,
        "mean_attempt_wall_time_seconds": _mean(raw_execution),
        "warm_scip_cache_hit_rate": (
            None
            if not [
                row["control_plane"]
                for row in selected
                if isinstance(row.get("control_plane"), Mapping)
                and row["control_plane"].get("scip_cache_hit") is not None
            ]
            else _rate(
                [
                    row["control_plane"]
                    for row in selected
                    if isinstance(row.get("control_plane"), Mapping)
                    and row["control_plane"].get("scip_cache_hit") is not None
                ],
                "scip_cache_hit",
            )
        ),
    }


def _paired_speedup_summary(
    rows: Sequence[Mapping[str, Any]],
    profiles: Sequence[AdmissionPhysicalProfile],
    *,
    baseline: AdmissionPhysicalProfile,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    baseline_rows = {
        (int(row.get("pair_index", -1)), int(row.get("coder_seed", -1))): row
        for row in rows
        if row.get("admission_physical_profile") == baseline.value
    }
    paired: dict[str, list[float]] = {profile.value: [] for profile in profiles}
    paired_e2e: dict[str, list[float]] = {profile.value: [] for profile in profiles}
    exclusions: list[dict[str, Any]] = []
    exclusion_counts: dict[str, dict[str, int]] = {profile.value: {} for profile in profiles}
    for row in rows:
        profile = str(row.get("admission_physical_profile"))
        if profile not in paired:
            continue
        key = (int(row.get("pair_index", -1)), int(row.get("coder_seed", -1)))
        reference = baseline_rows.get(key)
        if reference is None:
            reason = f"missing_paired_{baseline.value}"
            exclusion_counts[profile][reason] = exclusion_counts[profile].get(reason, 0) + 1
            exclusions.append(
                {
                    "baseline": baseline.value,
                    "profile": profile,
                    "coder_seed": row.get("coder_seed"),
                    "pair_index": row.get("pair_index"),
                    "pair": row.get("pair"),
                    "reason": reason,
                }
            )
            continue
        execution_speedup, e2e_speedup, reason = _paired_speedup(reference, row)
        if reason is not None:
            exclusion_counts[profile][reason] = exclusion_counts[profile].get(reason, 0) + 1
            exclusions.append(
                {
                    "baseline": baseline.value,
                    "profile": profile,
                    "coder_seed": row.get("coder_seed"),
                    "pair_index": row.get("pair_index"),
                    "pair": row.get("pair"),
                    "reason": reason,
                }
            )
            continue
        assert execution_speedup is not None and e2e_speedup is not None
        paired[profile].append(execution_speedup)
        paired_e2e[profile].append(e2e_speedup)
    return (
        {
            profile.value: {
                "baseline": baseline.value,
                "paired_observations": len(paired[profile.value]),
                "valid_speedup_observations": len(paired[profile.value]),
                "excluded_speedup_observations": sum(
                    exclusion_counts[profile.value].values()
                ),
                "exclusion_reasons": dict(sorted(exclusion_counts[profile.value].items())),
                "mean_execution_speedup": _mean(paired[profile.value]),
                "median_execution_speedup": (
                    statistics.median(paired[profile.value])
                    if paired[profile.value]
                    else None
                ),
                "mean_end_to_end_speedup": _mean(paired_e2e[profile.value]),
                "median_end_to_end_speedup": (
                    statistics.median(paired_e2e[profile.value])
                    if paired_e2e[profile.value]
                    else None
                ),
            }
            for profile in profiles
        },
        exclusions,
    )


def _stage_transition_summary(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[int, int], dict[str, Mapping[str, Any]]] = {}
    for row in rows:
        key = (int(row.get("pair_index", -1)), int(row.get("coder_seed", -1)))
        by_key.setdefault(key, {})[str(row.get("admission_physical_profile"))] = row
    summaries: list[dict[str, Any]] = []
    for left, right in zip(_CONTROLLED_PROFILES, _CONTROLLED_PROFILES[1:]):
        paired = [
            (profile_rows[left.value], profile_rows[right.value])
            for profile_rows in by_key.values()
            if left.value in profile_rows and right.value in profile_rows
        ]
        released = sum(
            bool(a.get("serialized")) and not bool(b.get("serialized")) for a, b in paired
        )
        newly_serialized = sum(
            not bool(a.get("serialized")) and bool(b.get("serialized")) for a, b in paired
        )
        summaries.append(
            {
                "from_profile": left.value,
                "to_profile": right.value,
                "paired_observations": len(paired),
                "released_serializations": released,
                "newly_serialized_pairs": newly_serialized,
                "physical_concurrency_rate_delta": (
                    None
                    if not paired
                    else sum(bool(b.get("physical_concurrency_observed")) for _, b in paired)
                    / len(paired)
                    - sum(bool(a.get("physical_concurrency_observed")) for a, _ in paired)
                    / len(paired)
                ),
                "integration_success_rate_delta": (
                    None
                    if not paired
                    else sum(bool(b.get("integration_success")) for _, b in paired)
                    / len(paired)
                    - sum(bool(a.get("integration_success")) for a, _ in paired)
                    / len(paired)
                ),
                "pair_pass_rate_delta": (
                    None
                    if not paired
                    else sum(bool(b.get("pair_pass")) for _, b in paired) / len(paired)
                    - sum(bool(a.get("pair_pass")) for a, _ in paired) / len(paired)
                ),
            }
        )
    return summaries


def build_admission_physical_report(
    paths: ConfirmatoryPaths,
    *,
    seeds: Sequence[int] = CODER_SEEDS,
    pair_indexes: Sequence[int] = tuple(range(1, N_PAIRS + 1)),
    profiles: Sequence[AdmissionPhysicalProfile | str] = DEFAULT_ADMISSION_PHYSICAL_PROFILES,
    require_complete: bool = False,
) -> dict[str, Any]:
    selected_profiles = parse_admission_physical_profiles(profiles)
    selected_seeds = parse_coder_seeds(tuple(seeds))
    selected_pairs = tuple(sorted(set(int(index) for index in pair_indexes)))
    if not selected_pairs:
        raise ValueError("at least one pair index is required")
    parse_pair_indexes(
        ",".join(str(index) for index in selected_pairs), pair_count=N_PAIRS
    )
    study = load_confirmatory_study(paths)
    fingerprint = study_fingerprint(study)
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    result_name = _result_name(selected_profiles)
    completed_units = 0
    execution_environments_by_fingerprint: dict[str, dict[str, Any]] = {}
    for seed in selected_seeds:
        for pair_index in selected_pairs:
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
            if (
                not isinstance(payload, dict)
                or payload.get("protocol") != ADMISSION_PHYSICAL_V1_PROTOCOL
                or payload.get("result_revision") != ADMISSION_PHYSICAL_V1_RESULT_REVISION
                or payload.get("study_fingerprint") != fingerprint
                or payload.get("coder_seed") != seed
                or payload.get("pair_index") != pair_index
                or payload.get("profiles") != [profile.value for profile in selected_profiles]
                or payload.get("complete") is not True
            ):
                missing.append(f"seed-{seed}/pair-{pair_index:02d}")
                continue
            completed_units += 1
            environment = payload.get("environment")
            if isinstance(environment, dict):
                digest = hashlib.sha256(_canonical_json(environment)).hexdigest()
                execution_environments_by_fingerprint.setdefault(digest, dict(environment))
            for row in payload.get("rows") or []:
                if not isinstance(row, dict):
                    continue
                normalized = dict(row)
                normalized["coder_seed"] = seed
                normalized["pair_index"] = pair_index
                normalized.setdefault("pair_key", payload.get("pair_key"))
                rows.append(_annotate_measurement_validity(normalized))
    if require_complete and missing:
        raise RuntimeError(
            f"Admission Physical v1 matrix is incomplete: {len(missing)} pair units missing"
        )

    serial_speedups, serial_exclusions = _paired_speedup_summary(
        rows, selected_profiles, baseline=AdmissionPhysicalProfile.SERIAL
    )
    broad_speedups: dict[str, dict[str, Any]] = {}
    broad_exclusions: list[dict[str, Any]] = []
    if AdmissionPhysicalProfile.BROAD_DECLARED in selected_profiles:
        broad_speedups, broad_exclusions = _paired_speedup_summary(
            rows, selected_profiles, baseline=AdmissionPhysicalProfile.BROAD_DECLARED
        )
    environments = [
        execution_environments_by_fingerprint[key]
        for key in sorted(execution_environments_by_fingerprint)
    ]
    if require_complete and len(environments) != 1:
        raise RuntimeError(
            "Admission Physical v1 final aggregation requires exactly one execution environment; "
            f"observed {len(environments)}"
        )
    expected_units = len(selected_seeds) * len(selected_pairs)
    expected_rows = expected_units * len(selected_profiles)
    aggregation_environment = runtime_environment()
    return {
        "protocol": ADMISSION_PHYSICAL_V1_PROTOCOL,
        "result_revision": ADMISSION_PHYSICAL_V1_RESULT_REVISION,
        "study_id": study.study_id,
        "study_fingerprint": fingerprint,
        "claim_plane_runtime_version": _runtime_version(),
        "seeds": list(selected_seeds),
        "pair_indexes": list(selected_pairs),
        "profiles": [profile.value for profile in selected_profiles],
        "expected_pair_units": expected_units,
        "completed_pair_units": completed_units,
        "expected_rows": expected_rows,
        "observed_rows": len(rows),
        "complete": not missing and len(rows) == expected_rows,
        "missing_pair_units": missing,
        "profile_summary": [
            _profile_summary(rows, profile) for profile in selected_profiles
        ],
        "paired_speedup_vs_serial": serial_speedups,
        "paired_speedup_vs_broad_declared": broad_speedups,
        "speedup_exclusions": serial_exclusions + broad_exclusions,
        "stage_transition_summary": _stage_transition_summary(rows),
        "causal_interpretation": (
            "All controlled profiles share one source-bound warm SCIP graph and disable "
            "candidate blocking; only admission granularity changes between stages."
        ),
        "cache_interpretation": (
            "A pair-local cold SCIP prime is recorded but excluded from scientific timing. "
            "Every controlled profile is charged the same standalone warm graph-preparation "
            "cost plus its own admission time."
        ),
        "outer_concurrency_interpretation": (
            "Outer pair concurrency reduces experiment turnaround only and is excluded "
            "from inner A/B speedup."
        ),
        "execution_environment_count": len(environments),
        "execution_environments": environments,
        "aggregation_environment": aggregation_environment,
        "environment": environments[0] if len(environments) == 1 else aggregation_environment,
    }


def run_admission_physical_batch(
    paths: ConfirmatoryPaths,
    *,
    seeds: Sequence[int] = (101,),
    pair_indexes: Sequence[int] = tuple(range(1, 7)),
    profiles: Sequence[AdmissionPhysicalProfile | str] = DEFAULT_ADMISSION_PHYSICAL_PROFILES,
    max_parallel_pairs: int = 2,
    repo_root: str | Path = ".",
    resume: bool = True,
) -> dict[str, Any]:
    selected_profiles = parse_admission_physical_profiles(profiles)
    selected_seeds = parse_coder_seeds(tuple(seeds))
    selected_pairs = tuple(sorted(set(int(index) for index in pair_indexes)))
    if max_parallel_pairs <= 0:
        raise ValueError("max_parallel_pairs must be positive")
    parse_pair_indexes(
        ",".join(str(index) for index in selected_pairs), pair_count=N_PAIRS
    )
    profile_arg = ",".join(profile.value for profile in selected_profiles)
    commands: list[tuple[str, list[str]]] = []
    for seed in selected_seeds:
        for pair_index in selected_pairs:
            args = [
                "confirmatory",
                "admission-v1-pair",
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
                "--profiles",
                profile_arg,
                "--repo",
                str(repo_root),
            ]
            if not resume:
                args.append("--no-resume")
            commands.append(
                (f"seed-{seed}-pair-{pair_index:02d}", python_module_command(*args))
            )
    started = time.time_ns()
    pool = run_bounded_pair_processes(
        commands, max_parallel_pairs=max_parallel_pairs
    )
    finished = time.time_ns()
    report = build_admission_physical_report(
        paths,
        seeds=selected_seeds,
        pair_indexes=selected_pairs,
        profiles=selected_profiles,
        require_complete=False,
    )
    study = load_confirmatory_study(paths)
    fingerprint = study_fingerprint(study)
    result = {
        **pool,
        "protocol": ADMISSION_PHYSICAL_V1_PROTOCOL,
        "result_revision": ADMISSION_PHYSICAL_V1_RESULT_REVISION,
        "study_id": study.study_id,
        "study_fingerprint": fingerprint,
        "claim_plane_runtime_version": _runtime_version(),
        "seeds": list(selected_seeds),
        "pair_indexes": list(selected_pairs),
        "profiles": [profile.value for profile in selected_profiles],
        "max_parallel_pairs": max_parallel_pairs,
        "batch_wall_time_seconds": (finished - started) / 1_000_000_000,
        "scientific_report": report,
    }
    digest = hashlib.sha256(
        _canonical_json(
            {
                "seeds": selected_seeds,
                "pairs": selected_pairs,
                "profiles": [profile.value for profile in selected_profiles],
                "max_parallel_pairs": max_parallel_pairs,
                "result_revision": ADMISSION_PHYSICAL_V1_RESULT_REVISION,
            }
        )
    ).hexdigest()[:12]
    output = _root(paths, fingerprint) / "batches" / f"batch-{digest}.json"
    result["report"] = str(output)
    _atomic_json(output, result)
    return result


def admission_physical_status(paths: ConfirmatoryPaths) -> dict[str, Any]:
    try:
        return {
            "prepared": True,
            **build_admission_physical_report(paths, require_complete=False),
        }
    except (FileNotFoundError, RuntimeError):
        return {
            "protocol": ADMISSION_PHYSICAL_V1_PROTOCOL,
            "prepared": False,
            "complete": False,
            "expected_pair_units": N_PAIRS * len(CODER_SEEDS),
            "completed_pair_units": 0,
        }


def aggregate_admission_physical(paths: ConfirmatoryPaths) -> dict[str, Any]:
    report = build_admission_physical_report(paths, require_complete=True)
    study = load_confirmatory_study(paths)
    fingerprint = study_fingerprint(study)
    analysis = _root(paths, fingerprint) / "analysis"
    final = analysis / "final-report.json"
    _atomic_json(final, report)
    manifest = {
        "protocol": ADMISSION_PHYSICAL_V1_PROTOCOL,
        "result_revision": ADMISSION_PHYSICAL_V1_RESULT_REVISION,
        "study_fingerprint": fingerprint,
        "final_report": final.name,
        "final_report_sha256": _sha256_file(final),
        "rows": report["observed_rows"],
        "pair_units": report["completed_pair_units"],
        "sealed_at_ns": time.time_ns(),
    }
    manifest_path = analysis / "manifest.json"
    _atomic_json(manifest_path, manifest)
    return {
        **report,
        "final_report": str(final),
        "manifest": str(manifest_path),
        "final_report_sha256": manifest["final_report_sha256"],
    }


__all__ = [
    "ADMISSION_PHYSICAL_V1_PROTOCOL",
    "ADMISSION_PHYSICAL_V1_RESULT_REVISION",
    "DEFAULT_ADMISSION_PHYSICAL_PROFILES",
    "AdmissionPhysicalProfile",
    "aggregate_admission_physical",
    "admission_physical_status",
    "build_admission_physical_report",
    "build_pair_admission_profiles",
    "parse_admission_physical_profiles",
    "run_admission_physical_batch",
    "run_admission_physical_pair",
]
