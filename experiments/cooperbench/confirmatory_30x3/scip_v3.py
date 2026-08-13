"""SCIP ablation and Physical Parallel Benchmark v3.

The protocol reuses the frozen CooperBench 30x3 workload and Planner v1 declarations
while isolating the code-intelligence changes introduced after the deterministic-v2
study.  Five paired execution profiles are compared:

* serial reliability baseline;
* naive physical parallelism;
* builtin deterministic Claim Plane graph without candidate blocking;
* required cold SCIP evidence without candidate blocking;
* warm SCIP evidence with revision caches and affected-subgraph candidate blocking.

Outer pair-process concurrency only reduces experiment turnaround.  Scientific speedup
is computed within each pair/seed unit against its serial execution.  SCIP profiles never
silently fall back to builtin analysis: unavailable or stale provider evidence invalidates
the unit so the experiment cannot label a builtin result as SCIP-backed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from claim_plane.code_intelligence import (
    ScipDependencyGraphError,
    ScipIndexError,
    ScipIndexManager,
    ScipSemanticResourceError,
    SemanticGraphRevisionCache,
    SemanticGraphSnapshot,
    assert_semantic_graph_fresh,
    build_scip_dependency_graph,
    build_scip_semantic_resource_index,
    refresh_python_dependency_graph_incrementally,
)
from claim_plane.core import SemanticDependencyGraph
from claim_plane.swarm import WorkGraph, compute_concurrency_plan
from claim_plane.swarm.service import _merge_semantic_dependency_graphs, _repository_identity

from ..common.identity import study_fingerprint
from ..environment import runtime_environment
from ..paper_6pair import runner as harness
from ..paper_6pair.coder import AGENT_TRACE_LOGS, reset_agent_traces
from ..paper_6pair.provider import STATS as CODER_PROVIDER_STATS, reset_provider_state
from ..physical_parallel import parse_pair_indexes, python_module_command, run_bounded_pair_processes
from .ablation import (
    _intent_ast_anchor_evidence,
    _policy,
    _python_sources_at_revision,
    _work_item_from_plan,
)
from .config import CODER_SEEDS, N_PAIRS, ConfirmatoryPaths
from .final import parse_coder_seeds
from .plans import load_plan_bundle, validate_plan_bundle
from .runner import _legacy_pair, load_confirmatory_study

SCIP_PHYSICAL_V3_PROTOCOL = "claim-plane.scip-ablation-physical-benchmark.v3"
SCIP_PHYSICAL_V3_RESULT_REVISION = 2


class ScipV3Profile(str, Enum):
    SERIAL = "serial"
    NAIVE_PARALLEL = "naive_parallel"
    BUILTIN_GRAPH = "builtin_graph"
    SCIP_GRAPH_COLD = "scip_graph_cold"
    SCIP_CACHE_BLOCKING = "scip_cache_blocking"


class ScipV3ExecutionOutcome(str, Enum):
    SUCCESS = "success"
    AGENT_FAILURE = "agent_failure"
    INTEGRATION_FAILURE = "integration_failure"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    POLICY_BLOCK = "policy_block"


DEFAULT_SCIP_V3_PROFILES = tuple(ScipV3Profile)


@dataclass(frozen=True, slots=True)
class ProfileSpec:
    profile: ScipV3Profile
    arm: str
    graph_mode: str | None
    candidate_blocking: bool
    cache_mode: str
    description: str


_PROFILE_SPECS = {
    ScipV3Profile.SERIAL: ProfileSpec(
        ScipV3Profile.SERIAL,
        "always-serial",
        None,
        False,
        "none",
        "Always-serial reliability and wall-clock baseline.",
    ),
    ScipV3Profile.NAIVE_PARALLEL: ProfileSpec(
        ScipV3Profile.NAIVE_PARALLEL,
        "parallel",
        None,
        False,
        "none",
        "Uncoordinated physical A/B execution followed by Git integration.",
    ),
    ScipV3Profile.BUILTIN_GRAPH: ProfileSpec(
        ScipV3Profile.BUILTIN_GRAPH,
        "claim-plane-static",
        "builtin",
        False,
        "uncached",
        "Builtin deterministic semantic graph without SCIP or candidate blocking.",
    ),
    ScipV3Profile.SCIP_GRAPH_COLD: ProfileSpec(
        ScipV3Profile.SCIP_GRAPH_COLD,
        "claim-plane-static",
        "scip",
        False,
        "cold",
        "Required cold SCIP evidence merged with the builtin graph; blocking disabled.",
    ),
    ScipV3Profile.SCIP_CACHE_BLOCKING: ProfileSpec(
        ScipV3Profile.SCIP_CACHE_BLOCKING,
        "claim-plane-static",
        "scip",
        True,
        "warm",
        "Warm revision caches plus SCIP evidence and affected-subgraph candidate blocking.",
    ),
}


def parse_scip_v3_profiles(
    value: str | Sequence[str | ScipV3Profile],
) -> tuple[ScipV3Profile, ...]:
    raw: Iterable[str | ScipV3Profile]
    if isinstance(value, str):
        raw = (item.strip() for item in value.split(",") if item.strip())
    else:
        raw = value
    profiles: list[ScipV3Profile] = []
    seen: set[ScipV3Profile] = set()
    for item in raw:
        profile = item if isinstance(item, ScipV3Profile) else ScipV3Profile(str(item))
        if profile not in seen:
            profiles.append(profile)
            seen.add(profile)
    if not profiles:
        raise ValueError("at least one SCIP v3 profile is required")
    return tuple(profiles)


def _runtime_version() -> str:
    try:
        from claim_plane import __version__

        return __version__
    except Exception:  # pragma: no cover - diagnostics only
        return "unknown"


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git failed")
    return completed.stdout.strip()


def _root(paths: ConfirmatoryPaths, fingerprint: str) -> Path:
    return (
        paths.artifact_root
        / "scip-ablation-physical-v3"
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


def _work_graph(
    plan_a: Mapping[str, Any],
    plan_b: Mapping[str, Any],
    *,
    sources: Mapping[str, str],
) -> WorkGraph:
    return WorkGraph.from_dict(
        {
            "protocol": "claim-plane.swarm-work-graph.v1",
            "work_items": [
                _work_item_from_plan("A", plan_a, sources=sources, add_symbol_resources=True),
                _work_item_from_plan("B", plan_b, sources=sources, add_symbol_resources=True),
            ],
        }
    )


def _verdict_from_graph(
    graph: WorkGraph,
    semantic_graph: SemanticDependencyGraph,
    *,
    profile: ScipV3Profile,
    candidate_blocking: bool,
) -> tuple[dict[str, Any], float]:
    started = time.perf_counter_ns()
    concurrency = compute_concurrency_plan(
        graph,
        _policy(),
        semantic_graph=semantic_graph,
        candidate_blocking_enabled=candidate_blocking,
    )
    admission_seconds = (time.perf_counter_ns() - started) / 1_000_000_000
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
            "reason": f"SCIP Physical v3 profile {profile.value}: {kind}.",
            "valid_for_accuracy": True,
            "serial_order": serial_order,
            "scip_v3_profile": profile.value,
            "scip_v3_evidence": {
                "protocol": SCIP_PHYSICAL_V3_PROTOCOL,
                "profile": profile.value,
                "semantic_graph_fingerprint": semantic_graph.fingerprint,
                "semantic_graph_revision": semantic_graph.metadata.get("source_revision"),
                "code_intelligence_sources": semantic_graph.metadata.get(
                    "code_intelligence_sources"
                ),
                "candidate_blocking_enabled": candidate_blocking,
                "work_graph_fingerprint": graph.fingerprint(),
                "intent_ast_anchors": _intent_ast_anchor_evidence(graph),
                "concurrency_plan": concurrency.to_dict(),
                "execution_waves": waves,
            },
        },
        admission_seconds,
    )


def _fresh_builtin_graph(
    sources: Mapping[str, str], *, revision: str
) -> SemanticDependencyGraph:
    graph, _ = refresh_python_dependency_graph_incrementally(
        None, sources, revision=revision
    )
    assert_semantic_graph_fresh(graph, expected_revision=revision)
    return graph


def _cached_builtin_graph(
    repo: Path,
    sources: Mapping[str, str],
    *,
    revision: str,
    cache_root: Path,
) -> tuple[SemanticDependencyGraph, bool]:
    cache = SemanticGraphRevisionCache(cache_root / "semantic-graphs")
    identity = _repository_identity(repo)
    exact = cache.load_exact(identity, revision)
    current_digests = {
        path: hashlib.sha256(source.encode("utf-8")).hexdigest()
        for path, source in sorted(sources.items())
    }
    if exact is not None and exact.graph.source_digests == current_digests:
        assert_semantic_graph_fresh(exact.graph, expected_revision=revision)
        return exact.graph, True
    previous = cache.load_latest(identity)
    graph, _ = refresh_python_dependency_graph_incrementally(
        None if previous is None else previous.graph,
        sources,
        revision=revision,
    )
    assert_semantic_graph_fresh(graph, expected_revision=revision)
    cache.store(
        SemanticGraphSnapshot(
            repository_identity=identity,
            revision=revision,
            graph=graph,
        )
    )
    return graph, False


def _required_scip_graph(
    repo: Path,
    builtin: SemanticDependencyGraph,
    *,
    revision: str,
    cache_root: Path,
    force: bool,
) -> tuple[SemanticDependencyGraph, dict[str, Any]]:
    head = _git(repo, "rev-parse", "HEAD").lower()
    dirty = bool(_git(repo, "status", "--porcelain", "--untracked-files=normal"))
    if head != revision.lower() or dirty:
        raise RuntimeError(
            "SCIP v3 requires a clean checkout exactly matching the frozen base revision"
        )
    index_started = time.perf_counter_ns()
    try:
        artifact = ScipIndexManager().index_repository(
            repo,
            revision=revision,
            cache_root=cache_root / "scip",
            force=force,
        )
    except ScipIndexError as exc:
        raise RuntimeError(
            "SCIP v3 requires working scip-python; provider fallback is forbidden"
        ) from exc
    index_seconds = (time.perf_counter_ns() - index_started) / 1_000_000_000

    decode_started = time.perf_counter_ns()
    try:
        scip_index = build_scip_semantic_resource_index(artifact)
        raw = build_scip_dependency_graph(scip_index)
    except (ScipSemanticResourceError, ScipDependencyGraphError) as exc:
        raise RuntimeError("SCIP v3 provider evidence is invalid") from exc
    decode_seconds = (time.perf_counter_ns() - decode_started) / 1_000_000_000
    scip_graph = SemanticDependencyGraph(
        nodes=raw.nodes,
        edges=raw.edges,
        source_digests=raw.source_digests,
        metadata={
            **dict(raw.metadata),
            "source_revision": revision.lower(),
            "source_mode": "checked_out_workspace",
            "freshness_fence": "validated",
        },
    )
    assert_semantic_graph_fresh(
        scip_graph,
        expected_revision=revision,
        expected_workspace_fingerprint=artifact.workspace_fingerprint,
    )
    merge_started = time.perf_counter_ns()
    merged = _merge_semantic_dependency_graphs(builtin, scip_graph)
    merge_seconds = (time.perf_counter_ns() - merge_started) / 1_000_000_000
    return merged, {
        "scip_index_seconds": index_seconds,
        "scip_decode_graph_seconds": decode_seconds,
        "graph_merge_seconds": merge_seconds,
        "scip_cache_hit": artifact.cache_hit,
        "scip_cache_key": artifact.cache_key,
        "scip_artifact_sha256": artifact.sha256,
        "scip_artifact_size_bytes": artifact.size_bytes,
        "scip_indexer_id": artifact.indexer_id,
        "scip_indexer_version": artifact.indexer_version,
        "workspace_fingerprint": artifact.workspace_fingerprint,
    }


def build_pair_admission_profiles(
    repo: str | Path,
    *,
    base_commit: str,
    plan_a: Mapping[str, Any],
    plan_b: Mapping[str, Any],
    cache_root: str | Path,
    profiles: Sequence[ScipV3Profile | str] = (
        ScipV3Profile.BUILTIN_GRAPH,
        ScipV3Profile.SCIP_GRAPH_COLD,
        ScipV3Profile.SCIP_CACHE_BLOCKING,
    ),
) -> dict[ScipV3Profile, dict[str, Any]]:
    """Precompute graph/admission evidence and cold/warm control-plane timings."""

    selected = set(parse_scip_v3_profiles(profiles))
    graph_profiles = {
        ScipV3Profile.BUILTIN_GRAPH,
        ScipV3Profile.SCIP_GRAPH_COLD,
        ScipV3Profile.SCIP_CACHE_BLOCKING,
    }
    unsupported = selected - graph_profiles
    if unsupported:
        raise ValueError(
            "admission profile builder accepts graph-backed profiles only: "
            + ", ".join(sorted(item.value for item in unsupported))
        )
    root = Path(repo).resolve()
    cache = Path(cache_root).resolve()
    shared_started = time.perf_counter_ns()
    sources = _python_sources_at_revision(root, base_commit)
    graph = _work_graph(plan_a, plan_b, sources=sources)
    shared_projection_seconds = (
        time.perf_counter_ns() - shared_started
    ) / 1_000_000_000
    results: dict[ScipV3Profile, dict[str, Any]] = {}

    if ScipV3Profile.BUILTIN_GRAPH in selected:
        builtin_started = time.perf_counter_ns()
        builtin = _fresh_builtin_graph(sources, revision=base_commit)
        builtin_build_seconds = (
            time.perf_counter_ns() - builtin_started
        ) / 1_000_000_000
        verdict, admission_seconds = _verdict_from_graph(
            graph,
            builtin,
            profile=ScipV3Profile.BUILTIN_GRAPH,
            candidate_blocking=False,
        )
        results[ScipV3Profile.BUILTIN_GRAPH] = {
            "verdict": verdict,
            "timing": {
                "shared_projection_seconds": shared_projection_seconds,
                "builtin_graph_seconds": builtin_build_seconds,
                "admission_seconds": admission_seconds,
                "control_plane_seconds": (
                    shared_projection_seconds
                    + builtin_build_seconds
                    + admission_seconds
                ),
                "builtin_cache_hit": False,
                "scip_cache_hit": None,
            },
        }

    if not selected.intersection(
        {ScipV3Profile.SCIP_GRAPH_COLD, ScipV3Profile.SCIP_CACHE_BLOCKING}
    ):
        return results

    # Cold is measured first and seeds the exact same isolated revision cache used by
    # the warm profile. Remove stale benchmark-local cache state so --no-resume and a
    # fresh pair run reproduce the cold/warm distinction deterministically.
    shutil.rmtree(cache, ignore_errors=True)
    cache.mkdir(parents=True, exist_ok=True)
    cold_builtin_started = time.perf_counter_ns()
    cold_builtin, cold_builtin_hit = _cached_builtin_graph(
        root, sources, revision=base_commit, cache_root=cache
    )
    cold_builtin_seconds = (time.perf_counter_ns() - cold_builtin_started) / 1_000_000_000
    cold_graph, cold_scip = _required_scip_graph(
        root, cold_builtin, revision=base_commit, cache_root=cache, force=True
    )
    cold_verdict, cold_admission_seconds = _verdict_from_graph(
        graph,
        cold_graph,
        profile=ScipV3Profile.SCIP_GRAPH_COLD,
        candidate_blocking=False,
    )
    cold_control = (
        shared_projection_seconds
        + cold_builtin_seconds
        + float(cold_scip["scip_index_seconds"])
        + float(cold_scip["scip_decode_graph_seconds"])
        + float(cold_scip["graph_merge_seconds"])
        + cold_admission_seconds
    )
    if ScipV3Profile.SCIP_GRAPH_COLD in selected:
        results[ScipV3Profile.SCIP_GRAPH_COLD] = {
            "verdict": cold_verdict,
            "timing": {
                "shared_projection_seconds": shared_projection_seconds,
                "builtin_graph_seconds": cold_builtin_seconds,
                "builtin_cache_hit": cold_builtin_hit,
                **cold_scip,
                "admission_seconds": cold_admission_seconds,
                "control_plane_seconds": cold_control,
            },
        }

    if ScipV3Profile.SCIP_CACHE_BLOCKING not in selected:
        return results

    warm_builtin_started = time.perf_counter_ns()
    warm_builtin, warm_builtin_hit = _cached_builtin_graph(
        root, sources, revision=base_commit, cache_root=cache
    )
    warm_builtin_seconds = (time.perf_counter_ns() - warm_builtin_started) / 1_000_000_000
    warm_graph, warm_scip = _required_scip_graph(
        root, warm_builtin, revision=base_commit, cache_root=cache, force=False
    )
    warm_verdict, warm_admission_seconds = _verdict_from_graph(
        graph,
        warm_graph,
        profile=ScipV3Profile.SCIP_CACHE_BLOCKING,
        candidate_blocking=True,
    )
    warm_control = (
        shared_projection_seconds
        + warm_builtin_seconds
        + float(warm_scip["scip_index_seconds"])
        + float(warm_scip["scip_decode_graph_seconds"])
        + float(warm_scip["graph_merge_seconds"])
        + warm_admission_seconds
    )
    results[ScipV3Profile.SCIP_CACHE_BLOCKING] = {
        "verdict": warm_verdict,
        "timing": {
            "shared_projection_seconds": shared_projection_seconds,
            "builtin_graph_seconds": warm_builtin_seconds,
            "builtin_cache_hit": warm_builtin_hit,
            **warm_scip,
            "admission_seconds": warm_admission_seconds,
            "control_plane_seconds": warm_control,
        },
    }
    return results


def _provider_stats() -> dict[str, Any]:
    return {
        "api_attempts": CODER_PROVIDER_STATS.api_attempts,
        "http_200_responses": CODER_PROVIDER_STATS.http_200_responses,
        "accepted_responses": CODER_PROVIDER_STATS.accepted_responses,
        "actual_cost": CODER_PROVIDER_STATS.actual_cost,
        "cost_by_role": dict(CODER_PROVIDER_STATS.cost_by_role),
    }


def _execution_order(
    *, fingerprint: str, coder_seed: int, pair_index: int, profiles: Sequence[ScipV3Profile]
) -> tuple[ScipV3Profile, ...]:
    # A deterministic five-way rotation avoids pinning one profile to one provider-time
    # position while keeping the graph cold/warm measurements outside coder execution.
    canonical = DEFAULT_SCIP_V3_PROFILES
    key = f"{fingerprint}|{coder_seed}|{pair_index}|scip-v3".encode("utf-8")
    offset = int(hashlib.sha256(key).hexdigest()[:8], 16) % len(canonical)
    rotated = canonical[offset:] + canonical[:offset]
    selected = set(profiles)
    return tuple(profile for profile in rotated if profile in selected)


def _mean_active_agents(row: Mapping[str, Any]) -> float:
    timing = row.get("physical_timing")
    if isinstance(timing, Mapping):
        union = float(timing.get("union_seconds", 0.0) or 0.0)
        a = timing.get("agent_a")
        b = timing.get("agent_b")
        if union > 0 and isinstance(a, Mapping) and isinstance(b, Mapping):
            total = float(a.get("duration_seconds", 0.0) or 0.0) + float(
                b.get("duration_seconds", 0.0) or 0.0
            )
            return total / union
    return 1.0 if bool(row.get("serialized")) else 0.0


def _critical_path_seconds(row: Mapping[str, Any]) -> float:
    physical = float(row.get("physical_union_seconds", 0.0) or 0.0)
    if physical > 0:
        return physical
    return float(row.get("coder_latency_critical", 0.0) or 0.0)


def _measurement_validity(row: Mapping[str, Any]) -> dict[str, Any]:
    """Classify whether a completed attempt is valid for wall-clock speedup.

    Correctness outcomes remain observable for every attempt.  Only truncated attempts
    caused by agent/protocol, harness/planner, or policy-enforcement failures are excluded
    from timing claims so a fast failure can never become an apparent speedup.  A normal
    integration failure remains timing-eligible because both agent executions completed
    and the failed integration is itself the measured naive-parallel outcome.
    """

    if bool(row.get("agent_execution_failure")):
        return {
            "execution_outcome": ScipV3ExecutionOutcome.AGENT_FAILURE.value,
            "speedup_eligible": False,
            "speedup_exclusion_reason": "agent_execution_failure",
        }
    if bool(row.get("harness_failure")) or bool(row.get("planner_failure")):
        reason = "harness_failure" if bool(row.get("harness_failure")) else "planner_failure"
        return {
            "execution_outcome": ScipV3ExecutionOutcome.INFRASTRUCTURE_FAILURE.value,
            "speedup_eligible": False,
            "speedup_exclusion_reason": reason,
        }
    if bool(row.get("scope_enforcement_failure")):
        return {
            "execution_outcome": ScipV3ExecutionOutcome.POLICY_BLOCK.value,
            "speedup_eligible": False,
            "speedup_exclusion_reason": "scope_enforcement_failure",
        }
    if row.get("integration_success") is False:
        return {
            "execution_outcome": ScipV3ExecutionOutcome.INTEGRATION_FAILURE.value,
            "speedup_eligible": True,
            "speedup_exclusion_reason": None,
        }
    return {
        "execution_outcome": ScipV3ExecutionOutcome.SUCCESS.value,
        "speedup_eligible": True,
        "speedup_exclusion_reason": None,
    }


def _annotate_measurement_validity(row: Mapping[str, Any]) -> dict[str, Any]:
    annotated = dict(row)
    validity = _measurement_validity(annotated)
    # Recompute rather than trusting persisted derived fields. This makes 0.45.2 able to
    # re-aggregate 0.45.0/0.45.1 raw artifacts without rerunning model calls.
    annotated.update(validity)
    return annotated


def _paired_speedup(
    serial: Mapping[str, Any], row: Mapping[str, Any]
) -> tuple[float | None, float | None, str | None]:
    serial_validity = _measurement_validity(serial)
    row_validity = _measurement_validity(row)
    if not bool(serial_validity["speedup_eligible"]):
        return (
            None,
            None,
            f"serial:{serial_validity['speedup_exclusion_reason']}",
        )
    if not bool(row_validity["speedup_eligible"]):
        return None, None, str(row_validity["speedup_exclusion_reason"])

    execution = float(row.get("execution_wall_time_seconds", 0.0) or 0.0)
    serial_execution = float(serial.get("execution_wall_time_seconds", 0.0) or 0.0)
    e2e = float(row.get("end_to_end_wall_time_seconds", 0.0) or 0.0)
    serial_e2e = float(serial.get("end_to_end_wall_time_seconds", 0.0) or 0.0)
    if execution <= 0 or serial_execution <= 0:
        return None, None, "non_positive_execution_wall_time"
    if e2e <= 0 or serial_e2e <= 0:
        return None, None, "non_positive_end_to_end_wall_time"
    return serial_execution / execution, serial_e2e / e2e, None


def run_scip_v3_pair(
    paths: ConfirmatoryPaths,
    *,
    coder_seed: int,
    pair_index: int,
    profiles: Sequence[ScipV3Profile | str] = DEFAULT_SCIP_V3_PROFILES,
    repo_root: str | Path = ".",
    resume: bool = True,
) -> dict[str, Any]:
    selected = parse_scip_v3_profiles(profiles)
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
        if not isinstance(payload, dict):
            raise RuntimeError(f"invalid SCIP v3 resume artifact: {output_file}")
        expected_profiles = [profile.value for profile in selected]
        if (
            payload.get("protocol") != SCIP_PHYSICAL_V3_PROTOCOL
            or payload.get("result_revision") != SCIP_PHYSICAL_V3_RESULT_REVISION
            or payload.get("coder_seed") != coder_seed
            or payload.get("pair_index") != pair_index
            or payload.get("profiles") != expected_profiles
            or payload.get("complete") is not True
        ):
            raise RuntimeError(
                "SCIP v3 resume artifact does not match the current result contract: "
                f"{output_file}"
            )
        return payload
    if output_file.exists() and not resume:
        raise RuntimeError(f"SCIP v3 artifact already exists; remove {output_file} or resume")

    isolated_paths = ConfirmatoryPaths(
        cooperbench=paths.cooperbench,
        artifact_root=paths.artifact_root,
        repo_cache=paths.repo_cache / "scip-v3" / f"seed-{coder_seed}" / f"pair-{pair_index:02d}",
        workspace_root=paths.workspace_root / "scip-v3" / f"seed-{coder_seed}" / f"pair-{pair_index:02d}",
    )
    harness.configure_runtime(isolated_paths, planner=None, pairs=study.pairs)
    repetition = list(study.coder_seeds).index(coder_seed)
    task, _feature_a, _feature_b, base_commit = harness._task_inputs(_legacy_pair(pair))
    repo = harness.get_repo(task.clone_url, base_commit)

    needed_graph_profiles = {
        profile for profile in selected if _PROFILE_SPECS[profile].graph_mode is not None
    }
    graph_profiles: dict[ScipV3Profile, dict[str, Any]] = {}
    if needed_graph_profiles:
        graph_profiles = build_pair_admission_profiles(
            repo,
            base_commit=base_commit,
            plan_a=plan_a,
            plan_b=plan_b,
            cache_root=output_dir / "code-intelligence-cache",
            profiles=tuple(needed_graph_profiles),
        )

    order = _execution_order(
        fingerprint=fingerprint,
        coder_seed=coder_seed,
        pair_index=pair_index,
        profiles=selected,
    )
    rows: list[dict[str, Any]] = []
    pair_started = time.time_ns()
    for ordinal, profile in enumerate(order, start=1):
        spec = _PROFILE_SPECS[profile]
        gate_record = graph_profiles.get(profile)
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
                "scip_v3_profile": profile.value,
                "scip_v3_description": spec.description,
                "scip_v3_execution_ordinal": ordinal,
                "execution_wall_time_seconds": execution_seconds,
                "control_plane_wall_time_seconds": control_seconds,
                "end_to_end_wall_time_seconds": execution_seconds + control_seconds,
                "critical_path_seconds": _critical_path_seconds(row),
                "mean_active_agents": _mean_active_agents(row),
                "control_plane": timing,
                "scip_v3_gate": gate,
                "provider_stats": _provider_stats(),
                "agent_traces": list(AGENT_TRACE_LOGS),
            }
        )
        rows.append(normalized)
        _atomic_json(output_dir / f"{profile.value}.json", normalized)
    pair_finished = time.time_ns()

    by_profile = {str(row["scip_v3_profile"]): row for row in rows}
    serial = by_profile.get(ScipV3Profile.SERIAL.value)
    comparisons: list[dict[str, Any]] = []
    if serial is not None:
        for profile in selected:
            row = by_profile.get(profile.value)
            if row is None:
                continue
            execution = float(row.get("execution_wall_time_seconds", 0.0) or 0.0)
            e2e = float(row.get("end_to_end_wall_time_seconds", 0.0) or 0.0)
            execution_speedup, e2e_speedup, exclusion_reason = _paired_speedup(
                serial, row
            )
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
                    "speedup_eligible": exclusion_reason is None,
                    "speedup_exclusion_reason": exclusion_reason,
                    "execution_wall_time_seconds": execution,
                    "control_plane_wall_time_seconds": row.get(
                        "control_plane_wall_time_seconds"
                    ),
                    "end_to_end_wall_time_seconds": e2e,
                    "speedup_vs_serial_execution": execution_speedup,
                    "speedup_vs_serial_end_to_end": e2e_speedup,
                }
            )

    result = {
        "protocol": SCIP_PHYSICAL_V3_PROTOCOL,
        "result_revision": SCIP_PHYSICAL_V3_RESULT_REVISION,
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


def _profile_summary(rows: Sequence[Mapping[str, Any]], profile: ScipV3Profile) -> dict[str, Any]:
    selected = [
        _annotate_measurement_validity(row)
        for row in rows
        if row.get("scip_v3_profile") == profile.value
    ]
    timing_selected = [row for row in selected if bool(row.get("speedup_eligible"))]
    execution = [
        float(row.get("execution_wall_time_seconds", 0.0) or 0.0)
        for row in timing_selected
    ]
    control = [
        float(row.get("control_plane_wall_time_seconds", 0.0) or 0.0)
        for row in timing_selected
    ]
    e2e = [
        float(row.get("end_to_end_wall_time_seconds", 0.0) or 0.0)
        for row in timing_selected
    ]
    critical = [
        float(row.get("critical_path_seconds", 0.0) or 0.0)
        for row in timing_selected
    ]
    active = [float(row.get("mean_active_agents", 0.0) or 0.0) for row in timing_selected]
    outcome_counts = {outcome.value: 0 for outcome in ScipV3ExecutionOutcome}
    for row in selected:
        outcome_counts[str(row["execution_outcome"])] += 1
    raw_execution = [
        float(row.get("execution_wall_time_seconds", 0.0) or 0.0) for row in selected
    ]
    return {
        "profile": profile.value,
        "description": _PROFILE_SPECS[profile].description,
        "observations": len(selected),
        "timing_observations": len(timing_selected),
        "excluded_timing_observations": len(selected) - len(timing_selected),
        "execution_outcome_counts": outcome_counts,
        "pair_pass_rate": _rate(selected, "pair_pass"),
        "integration_success_rate": _rate(selected, "integration_success"),
        "serialization_rate": _rate(selected, "serialized"),
        "physical_concurrency_rate": _rate(selected, "physical_concurrency_observed"),
        "mean_active_agents": _mean(active),
        "mean_critical_path_seconds": _mean(critical),
        "mean_execution_wall_time_seconds": _mean(execution),
        "median_execution_wall_time_seconds": statistics.median(execution) if execution else None,
        "mean_control_plane_wall_time_seconds": _mean(control),
        "mean_end_to_end_wall_time_seconds": _mean(e2e),
        "mean_attempt_wall_time_seconds": _mean(raw_execution),
        "scip_cache_hit_rate": (
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


def _legacy_result_name(profiles: Sequence[ScipV3Profile]) -> str:
    key = "+".join(sorted(profile.value for profile in profiles))
    return f"result-{hashlib.sha256(key.encode()).hexdigest()[:12]}.json"


def _result_name(profiles: Sequence[ScipV3Profile]) -> str:
    key = "+".join(sorted(profile.value for profile in profiles))
    revisioned = f"{key}|result-revision={SCIP_PHYSICAL_V3_RESULT_REVISION}"
    return f"result-{hashlib.sha256(revisioned.encode()).hexdigest()[:12]}.json"


def _paired_speedup_summary(
    rows: Sequence[Mapping[str, Any]],
    profiles: Sequence[ScipV3Profile],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    serial_rows = {
        (int(row.get("pair_index", -1)), int(row.get("coder_seed", -1))): row
        for row in rows
        if row.get("scip_v3_profile") == ScipV3Profile.SERIAL.value
    }
    paired: dict[str, list[float]] = {profile.value: [] for profile in profiles}
    paired_e2e: dict[str, list[float]] = {profile.value: [] for profile in profiles}
    exclusions: list[dict[str, Any]] = []
    exclusion_counts: dict[str, dict[str, int]] = {profile.value: {} for profile in profiles}
    for row in rows:
        profile = str(row.get("scip_v3_profile"))
        if profile not in paired:
            continue
        serial = serial_rows.get(
            (int(row.get("pair_index", -1)), int(row.get("coder_seed", -1)))
        )
        if serial is None:
            reason = "missing_paired_serial"
            exclusion_counts[profile][reason] = exclusion_counts[profile].get(reason, 0) + 1
            exclusions.append(
                {
                    "profile": profile,
                    "coder_seed": row.get("coder_seed"),
                    "pair_index": row.get("pair_index"),
                    "pair": row.get("pair"),
                    "reason": reason,
                }
            )
            continue
        execution_speedup, e2e_speedup, reason = _paired_speedup(serial, row)
        if reason is not None:
            exclusion_counts[profile][reason] = exclusion_counts[profile].get(reason, 0) + 1
            exclusions.append(
                {
                    "profile": profile,
                    "coder_seed": row.get("coder_seed"),
                    "pair_index": row.get("pair_index"),
                    "pair": row.get("pair"),
                    "reason": reason,
                    "serial_execution_outcome": _measurement_validity(serial)[
                        "execution_outcome"
                    ],
                    "profile_execution_outcome": _measurement_validity(row)[
                        "execution_outcome"
                    ],
                }
            )
            continue
        assert execution_speedup is not None
        assert e2e_speedup is not None
        paired[profile].append(execution_speedup)
        paired_e2e[profile].append(e2e_speedup)
    speedups = {
        profile.value: {
            "paired_observations": len(paired[profile.value]),
            "valid_speedup_observations": len(paired[profile.value]),
            "excluded_speedup_observations": sum(
                exclusion_counts[profile.value].values()
            ),
            "exclusion_reasons": dict(sorted(exclusion_counts[profile.value].items())),
            "mean_execution_speedup_vs_serial": _mean(paired[profile.value]),
            "median_execution_speedup_vs_serial": (
                statistics.median(paired[profile.value]) if paired[profile.value] else None
            ),
            "mean_end_to_end_speedup_vs_serial": _mean(paired_e2e[profile.value]),
            "median_end_to_end_speedup_vs_serial": (
                statistics.median(paired_e2e[profile.value])
                if paired_e2e[profile.value]
                else None
            ),
        }
        for profile in profiles
    }
    return speedups, exclusions


def build_scip_v3_report(
    paths: ConfirmatoryPaths,
    *,
    seeds: Sequence[int] = CODER_SEEDS,
    pair_indexes: Sequence[int] = tuple(range(1, N_PAIRS + 1)),
    profiles: Sequence[ScipV3Profile | str] = DEFAULT_SCIP_V3_PROFILES,
    require_complete: bool = False,
    allow_legacy: bool = False,
) -> dict[str, Any]:
    selected_profiles = parse_scip_v3_profiles(profiles)
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
    name = _result_name(selected_profiles)
    legacy_name = _legacy_result_name(selected_profiles)
    completed_units = 0
    legacy_artifact_units = 0
    execution_environments_by_fingerprint: dict[str, dict[str, Any]] = {}
    for seed in selected_seeds:
        for pair_index in selected_pairs:
            pair_dir = _pair_dir(
                paths, fingerprint=fingerprint, coder_seed=seed, pair_index=pair_index
            )
            path = pair_dir / name
            using_legacy = False
            if not path.exists() and allow_legacy:
                legacy_path = pair_dir / legacy_name
                if legacy_path.exists():
                    path = legacy_path
                    using_legacy = True
            if not path.exists():
                missing.append(f"seed-{seed}/pair-{pair_index:02d}")
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not payload.get("complete"):
                missing.append(f"seed-{seed}/pair-{pair_index:02d}")
                continue
            completed_units += 1
            if using_legacy:
                legacy_artifact_units += 1
            payload_environment = payload.get("environment")
            if isinstance(payload_environment, dict):
                encoded_environment = json.dumps(
                    payload_environment, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                environment_fingerprint = hashlib.sha256(encoded_environment).hexdigest()
                execution_environments_by_fingerprint.setdefault(
                    environment_fingerprint, dict(payload_environment)
                )
            payload_rows = payload.get("rows") or []
            if isinstance(payload_rows, list):
                for row in payload_rows:
                    if not isinstance(row, dict):
                        continue
                    normalized_row = dict(row)
                    normalized_row["coder_seed"] = seed
                    normalized_row["pair_index"] = pair_index
                    normalized_row.setdefault("pair_key", payload.get("pair_key"))
                    rows.append(_annotate_measurement_validity(normalized_row))
    if require_complete and missing:
        raise RuntimeError(f"SCIP v3 matrix is incomplete: {len(missing)} pair units missing")

    speedups, exclusions = _paired_speedup_summary(rows, selected_profiles)
    execution_environments = [
        execution_environments_by_fingerprint[key]
        for key in sorted(execution_environments_by_fingerprint)
    ]
    if require_complete and legacy_artifact_units:
        raise RuntimeError(
            "SCIP v3 final aggregation refuses legacy result artifacts; rerun those pair/seed units "
            f"under result revision {SCIP_PHYSICAL_V3_RESULT_REVISION}"
        )
    if require_complete and len(execution_environments) != 1:
        raise RuntimeError(
            "SCIP v3 final aggregation requires exactly one execution environment; "
            f"observed {len(execution_environments)}"
        )
    aggregation_environment = runtime_environment()
    expected_units = len(selected_seeds) * len(selected_pairs)
    expected_rows = expected_units * len(selected_profiles)
    return {
        "protocol": SCIP_PHYSICAL_V3_PROTOCOL,
        "result_revision": SCIP_PHYSICAL_V3_RESULT_REVISION,
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
        "profile_summary": [_profile_summary(rows, profile) for profile in selected_profiles],
        "paired_speedup_vs_serial": speedups,
        "speedup_exclusions": exclusions,
        "cold_warm_interpretation": (
            "scip_graph_cold forces a new SCIP index in an isolated per-pair cache; "
            "scip_cache_blocking reuses that exact revision cache and enables affected-subgraph blocking."
        ),
        "outer_concurrency_interpretation": (
            "Outer pair concurrency reduces experiment turnaround only and is excluded from Claim Plane speedup."
        ),
        "legacy_artifact_units": legacy_artifact_units,
        "execution_environment_count": len(execution_environments),
        "execution_environments": execution_environments,
        "aggregation_environment": aggregation_environment,
        "environment": (
            execution_environments[0]
            if len(execution_environments) == 1
            else aggregation_environment
        ),
    }


def run_scip_v3_batch(
    paths: ConfirmatoryPaths,
    *,
    seeds: Sequence[int] = (101,),
    pair_indexes: Sequence[int] = tuple(range(1, 7)),
    profiles: Sequence[ScipV3Profile | str] = DEFAULT_SCIP_V3_PROFILES,
    max_parallel_pairs: int = 6,
    repo_root: str | Path = ".",
    resume: bool = True,
) -> dict[str, Any]:
    selected_profiles = parse_scip_v3_profiles(profiles)
    selected_seeds = parse_coder_seeds(tuple(seeds))
    selected_pairs = tuple(sorted(set(int(index) for index in pair_indexes)))
    if max_parallel_pairs <= 0:
        raise ValueError("max_parallel_pairs must be positive")
    parse_pair_indexes(
        ",".join(str(index) for index in selected_pairs), pair_count=N_PAIRS
    )
    profile_arg = ",".join(profile.value for profile in selected_profiles)
    commands = []
    for seed in selected_seeds:
        for pair_index in selected_pairs:
            args = [
                "confirmatory",
                "scip-v3-pair",
                "--cooperbench", str(paths.cooperbench),
                "--artifacts", str(paths.artifact_root),
                "--repo-cache", str(paths.repo_cache),
                "--workspace", str(paths.workspace_root),
                "--seed", str(seed),
                "--pair", str(pair_index),
                "--profiles", profile_arg,
                "--repo", str(repo_root),
            ]
            if not resume:
                args.append("--no-resume")
            commands.append((f"seed-{seed}-pair-{pair_index:02d}", python_module_command(*args)))
    started = time.time_ns()
    pool = run_bounded_pair_processes(commands, max_parallel_pairs=max_parallel_pairs)
    finished = time.time_ns()
    report = build_scip_v3_report(
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
        "protocol": SCIP_PHYSICAL_V3_PROTOCOL,
        "result_revision": SCIP_PHYSICAL_V3_RESULT_REVISION,
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
        json.dumps(
            {
                "seeds": selected_seeds,
                "pairs": selected_pairs,
                "profiles": [profile.value for profile in selected_profiles],
                "max_parallel_pairs": max_parallel_pairs,
                "result_revision": SCIP_PHYSICAL_V3_RESULT_REVISION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:12]
    output = _root(paths, fingerprint) / "batches" / f"batch-{digest}.json"
    result["report"] = str(output)
    _atomic_json(output, result)
    return result


def scip_v3_status(paths: ConfirmatoryPaths) -> dict[str, Any]:
    try:
        return {
            "prepared": True,
            **build_scip_v3_report(paths, require_complete=False, allow_legacy=True),
        }
    except (FileNotFoundError, RuntimeError):
        return {
            "protocol": SCIP_PHYSICAL_V3_PROTOCOL,
            "prepared": False,
            "complete": False,
            "expected_pair_units": N_PAIRS * len(CODER_SEEDS),
            "completed_pair_units": 0,
        }


def aggregate_scip_v3(paths: ConfirmatoryPaths) -> dict[str, Any]:
    report = build_scip_v3_report(paths, require_complete=True)
    study = load_confirmatory_study(paths)
    fingerprint = study_fingerprint(study)
    analysis = _root(paths, fingerprint) / "analysis"
    final = analysis / "final-report.json"
    _atomic_json(final, report)
    manifest = {
        "protocol": SCIP_PHYSICAL_V3_PROTOCOL,
        "result_revision": SCIP_PHYSICAL_V3_RESULT_REVISION,
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
    "DEFAULT_SCIP_V3_PROFILES",
    "SCIP_PHYSICAL_V3_PROTOCOL",
    "SCIP_PHYSICAL_V3_RESULT_REVISION",
    "ScipV3Profile",
    "ScipV3ExecutionOutcome",
    "aggregate_scip_v3",
    "build_pair_admission_profiles",
    "build_scip_v3_report",
    "parse_scip_v3_profiles",
    "run_scip_v3_batch",
    "run_scip_v3_pair",
    "scip_v3_status",
]
