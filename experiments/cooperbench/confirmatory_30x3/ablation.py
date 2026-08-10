"""Deterministic ablation study for the frozen confirmatory workload.

The study reuses the exact frozen 30-pair workload, coder seeds, Planner v1
outputs, and physical-execution instrumentation.  Only the deterministic
admission evidence is varied.  This makes changes in serialization, physical
overlap, pair success, and wall-clock time attributable to a named control-plane
capability rather than to a different task set or planner sample.

The published confirmatory artifacts are immutable and are never overwritten.
Ablation artifacts live under a separate protocol root.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
import tokenize
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from claim_plane.core import (
    DependencyRelation,
    PythonStructuralExtractionError,
    ResourceKind,
    ScopeCommitment,
    SemanticDependencyGraph,
    build_python_dependency_graph,
    extract_python_structure,
)
from claim_plane.swarm import SwarmBudgetPolicy, WorkGraph, compute_concurrency_plan

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
from .config import CODER_SEEDS, N_PAIRS, ConfirmatoryPaths
from .plans import load_plan_bundle, validate_plan_bundle
from .runner import _legacy_pair, load_confirmatory_study

DETERMINISTIC_ABLATION_PROTOCOL = "claim-plane.deterministic-ablation-study.v1"


class AblationProfile(str, Enum):
    """Named deterministic admission configurations used by the study."""

    FULL_V2 = "full_v2"
    FILE_REGION_BASELINE = "file_region_baseline"
    SYMBOLS_WITHOUT_DEPENDENCIES = "symbols_without_dependencies"
    NO_CONTRACT_PROPAGATION = "no_contract_propagation"


DEFAULT_ABLATION_PROFILES = tuple(AblationProfile)


@dataclass(frozen=True, slots=True)
class AblationProfileSpec:
    profile: AblationProfile
    add_symbol_resources: bool
    semantic_graph_mode: str
    description: str


_PROFILE_SPECS: dict[AblationProfile, AblationProfileSpec] = {
    AblationProfile.FULL_V2: AblationProfileSpec(
        profile=AblationProfile.FULL_V2,
        add_symbol_resources=True,
        semantic_graph_mode="full",
        description=(
            "Semantic Resource IR v2 symbols plus the complete Dependency Graph v2."
        ),
    ),
    AblationProfile.FILE_REGION_BASELINE: AblationProfileSpec(
        profile=AblationProfile.FILE_REGION_BASELINE,
        add_symbol_resources=False,
        semantic_graph_mode="none",
        description=(
            "File and declared line-region admission only; semantic evidence is removed."
        ),
    ),
    AblationProfile.SYMBOLS_WITHOUT_DEPENDENCIES: AblationProfileSpec(
        profile=AblationProfile.SYMBOLS_WITHOUT_DEPENDENCIES,
        add_symbol_resources=True,
        semantic_graph_mode="nodes_only",
        description=(
            "Structural symbol identities are retained while semantic dependency edges "
            "are removed."
        ),
    ),
    AblationProfile.NO_CONTRACT_PROPAGATION: AblationProfileSpec(
        profile=AblationProfile.NO_CONTRACT_PROPAGATION,
        add_symbol_resources=True,
        semantic_graph_mode="no_contract_propagation",
        description=(
            "Structural symbols and implementation dependencies remain, while relations "
            "used only by broad contract propagation are removed."
        ),
    ),
}

# Relations that widen a contract/structure change beyond the narrower
# implementation surface. CALLS/READS/TESTS deliberately remain so this profile
# does not collapse all semantic dependency reasoning.
_CONTRACT_PROPAGATION_ONLY_RELATIONS = frozenset(
    {
        DependencyRelation.IMPORTS,
        DependencyRelation.WRITES,
        DependencyRelation.INHERITS,
        DependencyRelation.TYPES,
        DependencyRelation.PUBLIC_API,
    }
)


def parse_ablation_profiles(
    value: str | Sequence[str | AblationProfile],
) -> tuple[AblationProfile, ...]:
    """Parse, validate, and de-duplicate profile names while preserving order."""

    raw: Iterable[str | AblationProfile]
    if isinstance(value, str):
        raw = (item.strip() for item in value.split(",") if item.strip())
    else:
        raw = value
    profiles: list[AblationProfile] = []
    seen: set[AblationProfile] = set()
    for item in raw:
        profile = (
            item if isinstance(item, AblationProfile) else AblationProfile(str(item))
        )
        if profile not in seen:
            seen.add(profile)
            profiles.append(profile)
    if not profiles:
        raise ValueError("at least one ablation profile is required")
    return tuple(profiles)


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


def _git_bytes(root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            stderr or "git command failed while building ablation evidence"
        )
    return completed.stdout


def _python_sources_at_revision(root: Path, revision: str) -> dict[str, str]:
    listing = _git_bytes(root, "ls-tree", "-r", "-z", "--name-only", revision)
    sources: dict[str, str] = {}
    excluded = {".git", ".claim-plane", ".codex", ".venv", "venv", "node_modules"}
    for raw_path in listing.split(b"\0"):
        if not raw_path:
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape")
        parts = tuple(part for part in path.replace("\\", "/").split("/") if part)
        if not parts or any(part in excluded for part in parts):
            continue
        if not path.endswith((".py", ".pyi")):
            continue
        raw = _git_bytes(root, "show", f"{revision}:{path}")
        try:
            encoding, _ = tokenize.detect_encoding(BytesIO(raw).readline)
            sources[path] = raw.decode(encoding)
        except (SyntaxError, UnicodeError) as exc:
            raise PythonStructuralExtractionError(
                f"cannot decode pinned Python source {path}: {exc}", path=path
            ) from exc
    return sources


def _semantic_graph_for_profile(
    sources: Mapping[str, str], profile: AblationProfile
) -> SemanticDependencyGraph | None:
    spec = _PROFILE_SPECS[profile]
    if spec.semantic_graph_mode == "none" or not sources:
        return None
    graph = build_python_dependency_graph(sources)
    if spec.semantic_graph_mode == "full":
        return graph
    if spec.semantic_graph_mode == "nodes_only":
        return SemanticDependencyGraph(
            nodes=graph.nodes,
            edges=(),
            source_digests=graph.source_digests,
            metadata={**graph.metadata, "ablation": profile.value},
        )
    if spec.semantic_graph_mode == "no_contract_propagation":
        return SemanticDependencyGraph(
            nodes=graph.nodes,
            edges=tuple(
                edge
                for edge in graph.edges
                if edge.relation not in _CONTRACT_PROPAGATION_ONLY_RELATIONS
            ),
            source_digests=graph.source_digests,
            metadata={**graph.metadata, "ablation": profile.value},
        )
    raise ValueError(f"unsupported semantic graph mode: {spec.semantic_graph_mode}")


def _access_for_action(action: object) -> str:
    return {
        "delete": "delete",
        "rename": "rename",
    }.get(str(action or "modify").lower(), "write")


def _commitment_for_item(item: Mapping[str, Any]) -> ScopeCommitment:
    return ScopeCommitment(
        str(item.get("commitment", ScopeCommitment.COMMITTED.value)).strip().lower()
    )


def _file_operation(item: Mapping[str, Any]) -> dict[str, Any]:
    path = str(item["path"])
    resource: dict[str, Any] = {"kind": "file", "identifier": path}
    start = int(item.get("line_start", 0) or 0)
    end = int(item.get("line_end", 0) or 0)
    if start > 0 and end > 0:
        resource["region"] = f"lines:{min(start, end)}-{max(start, end)}"
    operation: dict[str, Any] = {
        "access": _access_for_action(item.get("action")),
        "resource": resource,
    }
    commitment = _commitment_for_item(item)
    if commitment is ScopeCommitment.CONTINGENT:
        operation["commitment"] = commitment.value
    return operation


def _symbol_operations(
    item: Mapping[str, Any], sources: Mapping[str, str]
) -> tuple[dict[str, Any], ...]:
    path = str(item.get("path") or "")
    source = sources.get(path)
    start = int(item.get("line_start", 0) or 0)
    end = int(item.get("line_end", 0) or 0)
    if source is None or start <= 0 or end <= 0:
        return ()
    try:
        index = extract_python_structure(source, path=path)
    except PythonStructuralExtractionError:
        return ()
    low, high = min(start, end), max(start, end)
    commitment = _commitment_for_item(item)
    owners = index.owners_for_region(low, high)
    definitions = {item.resource.identity: item for item in index.definitions}
    operations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for owner in owners:
        if owner.kind is not ResourceKind.SYMBOL or owner.identity in seen:
            continue
        seen.add(owner.identity)
        definition = definitions.get(owner.identity)
        touches_definition = bool(
            definition is not None and low <= definition.definition_line <= high
        )
        change_kind = (
            "contract" if touches_definition and owner.signature else "implementation"
        )
        metadata = {
            "path": path,
            "language": owner.language or "python",
            "qualified_identifier": owner.qualified_name or owner.identifier,
            "semantic_source": "planner_declared_region",
            "declared_region_touches_definition": touches_definition,
        }
        if owner.signature:
            metadata["signature"] = owner.signature
        operation: dict[str, Any] = {
            "access": _access_for_action(item.get("action")),
            "resource": {
                "kind": "symbol",
                "identifier": owner.qualified_name or owner.identifier,
                "signature": owner.signature,
                "metadata": metadata,
            },
            "metadata": {"semantic_change_kind": change_kind},
        }
        if commitment is ScopeCommitment.CONTINGENT:
            operation["commitment"] = commitment.value
        operations.append(operation)
    return tuple(operations)


def _work_item_from_plan(
    work_id: str,
    plan: Mapping[str, Any],
    *,
    sources: Mapping[str, str],
    add_symbol_resources: bool,
) -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    for raw in plan.get("files", ()):
        if not isinstance(raw, Mapping) or not raw.get("path"):
            continue
        operations.append(_file_operation(raw))
        if add_symbol_resources:
            operations.extend(_symbol_operations(raw, sources))
    if not operations:
        raise ValueError(f"planner declaration for {work_id} contains no resources")
    return {
        "work_id": work_id,
        "title": f"Ablation worker {work_id}",
        "goal": f"Execute frozen Planner v1 declaration {work_id}",
        "operations": operations,
    }


def _policy() -> SwarmBudgetPolicy:
    return SwarmBudgetPolicy.from_dict(
        {
            "workers": {
                "max_active": 2,
                "max_active_per_work_item": 1,
                "max_work_items": 2,
                "max_total_launches": 8,
            },
            "concurrency": {
                "same_file": "region_safe",
                "unknown_overlap": "serialize",
                "shared_contract": "serialize",
                "schema_change": "serialize",
            },
        }
    )


def deterministic_ablation_verdict(
    repo: str | Path,
    *,
    base_commit: str,
    plan_a: Mapping[str, Any],
    plan_b: Mapping[str, Any],
    profile: AblationProfile | str,
) -> dict[str, Any]:
    """Compute one source-bound deterministic admission verdict for an ablation profile."""

    selected = (
        profile if isinstance(profile, AblationProfile) else AblationProfile(profile)
    )
    spec = _PROFILE_SPECS[selected]
    root = Path(repo).resolve()
    sources = _python_sources_at_revision(root, base_commit)
    semantic_graph: SemanticDependencyGraph | None
    graph_error: str | None = None
    try:
        semantic_graph = _semantic_graph_for_profile(sources, selected)
    except PythonStructuralExtractionError as exc:
        semantic_graph = None
        graph_error = str(exc)

    graph = WorkGraph.from_dict(
        {
            "protocol": "claim-plane.swarm-work-graph.v1",
            "work_items": [
                _work_item_from_plan(
                    "A",
                    plan_a,
                    sources=sources,
                    add_symbol_resources=spec.add_symbol_resources,
                ),
                _work_item_from_plan(
                    "B",
                    plan_b,
                    sources=sources,
                    add_symbol_resources=spec.add_symbol_resources,
                ),
            ],
        }
    )
    concurrency = compute_concurrency_plan(
        graph,
        _policy(),
        semantic_graph=semantic_graph,
    )
    waves = [list(wave.work_ids) for wave in concurrency.waves]
    if concurrency.status.value == "replan_required":
        serialized = True
        allowed = False
        kind = "replan_required"
        serial_order = "A->B"
    elif len(waves) == 1 and set(waves[0]) == {"A", "B"}:
        serialized = False
        allowed = True
        kind = "parallel"
        serial_order = None
    else:
        serialized = True
        allowed = True
        kind = "ordered" if any(len(wave) == 1 for wave in waves) else "serialized"
        flattened = [work_id for wave in waves for work_id in wave]
        serial_order = "->".join(flattened) if flattened else "A->B"

    evidence = {
        "protocol": DETERMINISTIC_ABLATION_PROTOCOL,
        "profile": selected.value,
        "description": spec.description,
        "add_symbol_resources": spec.add_symbol_resources,
        "semantic_graph_mode": spec.semantic_graph_mode,
        "semantic_graph_fingerprint": (
            semantic_graph.fingerprint if semantic_graph is not None else None
        ),
        "semantic_graph_error": graph_error,
        "work_graph_fingerprint": graph.fingerprint(),
        "concurrency_plan": concurrency.to_dict(),
        "execution_waves": waves,
    }
    return {
        "serialized": serialized,
        "kind": kind,
        "allowed": allowed,
        "reason": f"Deterministic ablation profile {selected.value}: {kind}.",
        "valid_for_accuracy": True,
        "serial_order": serial_order,
        "ablation_profile": selected.value,
        "ablation_evidence": evidence,
    }


def _root(paths: ConfirmatoryPaths, fingerprint: str) -> Path:
    return (
        paths.artifact_root
        / "deterministic-ablation-v1"
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


def _runtime_version() -> str:
    try:
        from claim_plane import __version__

        return __version__
    except Exception:  # pragma: no cover - diagnostic only
        return "unknown"


def run_ablation_pair(
    paths: ConfirmatoryPaths,
    *,
    coder_seed: int,
    pair_index: int,
    profiles: Sequence[AblationProfile | str] = DEFAULT_ABLATION_PROFILES,
    repo_root: str | Path = ".",
    resume: bool = True,
) -> dict[str, Any]:
    """Execute one frozen pair once per deterministic admission configuration."""

    selected_profiles = parse_ablation_profiles(profiles)
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
    profile_key = "+".join(profile.value for profile in selected_profiles)
    output_file = (
        output_dir
        / f"result-{hashlib.sha256(profile_key.encode()).hexdigest()[:12]}.json"
    )
    if resume and output_file.exists():
        existing = json.loads(output_file.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            return existing
    if output_file.exists() and not resume:
        raise RuntimeError(
            f"ablation artifact already exists; enable resume or remove {output_file}"
        )

    isolated_workspace = (
        paths.workspace_root
        / "deterministic-ablation-v1"
        / f"seed-{coder_seed}"
        / f"pair-{pair_index:02d}"
    )
    isolated_repo_cache = (
        paths.repo_cache
        / "deterministic-ablation-v1"
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
    repetition = list(study.coder_seeds).index(coder_seed)
    task, _feature_a, _feature_b, base_commit = harness._task_inputs(_legacy_pair(pair))

    rows: list[dict[str, Any]] = []
    study_started_ns = time.time_ns()
    for profile in selected_profiles:
        reset_provider_state()
        reset_agent_traces()
        repo = harness.get_repo(task.clone_url, base_commit)
        verdict = deterministic_ablation_verdict(
            repo,
            base_commit=base_commit,
            plan_a=plan_a,
            plan_b=plan_b,
            profile=profile,
        )
        started_ns = time.time_ns()
        row = harness.run_pair(
            _legacy_pair(pair),
            "claim-plane-static",
            repetition,
            coder_seed=coder_seed,
            frozen_plans=bundle["pairs"],
            physical_parallel=True,
            admission_override=verdict,
            ablation_profile=profile.value,
        )
        finished_ns = time.time_ns()
        row["ablation_profile"] = profile.value
        row["ablation_profile_description"] = _PROFILE_SPECS[profile].description
        row["ablation_wall_time_seconds"] = (finished_ns - started_ns) / 1_000_000_000
        row["ablation_gate"] = verdict
        row["provider_stats"] = _provider_stats()
        row["agent_traces"] = list(AGENT_TRACE_LOGS)
        rows.append(row)
        _atomic_json(output_dir / f"{profile.value}.json", row)
    study_finished_ns = time.time_ns()

    full = next((row for row in rows if row["ablation_profile"] == "full_v2"), None)
    comparisons: list[dict[str, Any]] = []
    if full is not None:
        full_wall = float(full.get("ablation_wall_time_seconds", 0.0) or 0.0)
        for row in rows:
            wall = float(row.get("ablation_wall_time_seconds", 0.0) or 0.0)
            comparisons.append(
                {
                    "profile": row["ablation_profile"],
                    "serialized": bool(row.get("serialized")),
                    "pair_pass": row.get("pair_pass"),
                    "physical_overlap_seconds": float(
                        row.get("physical_overlap_seconds", 0.0) or 0.0
                    ),
                    "wall_time_seconds": wall,
                    "wall_time_delta_vs_full_seconds": wall - full_wall,
                }
            )

    result = {
        "protocol": DETERMINISTIC_ABLATION_PROTOCOL,
        "study_id": study.study_id,
        "study_fingerprint": fingerprint,
        "claim_plane_runtime_version": _runtime_version(),
        "pair_index": pair_index,
        "pair_key": pair.key,
        "coder_seed": coder_seed,
        "coder_seed_index": repetition,
        "profiles": [profile.value for profile in selected_profiles],
        "profile_specs": {
            profile.value: {
                "description": _PROFILE_SPECS[profile].description,
                "add_symbol_resources": _PROFILE_SPECS[profile].add_symbol_resources,
                "semantic_graph_mode": _PROFILE_SPECS[profile].semantic_graph_mode,
            }
            for profile in selected_profiles
        },
        "rows": rows,
        "comparisons_vs_full_v2": comparisons,
        "started_ns": study_started_ns,
        "finished_ns": study_finished_ns,
        "wall_time_seconds": (study_finished_ns - study_started_ns) / 1_000_000_000,
        "environment": runtime_environment(),
        "repo_root": str(Path(repo_root).resolve()),
        "complete": True,
    }
    _atomic_json(output_file, result)
    return result


def run_ablation_batch(
    paths: ConfirmatoryPaths,
    *,
    coder_seed: int,
    pair_indexes: Sequence[int],
    profiles: Sequence[AblationProfile | str] = DEFAULT_ABLATION_PROFILES,
    max_parallel_pairs: int = 6,
    repo_root: str | Path = ".",
    resume: bool = True,
) -> dict[str, Any]:
    """Run independent ablation pairs through the bounded outer worker pool."""

    selected_profiles = parse_ablation_profiles(profiles)
    study = load_confirmatory_study(paths)
    if coder_seed not in CODER_SEEDS:
        raise ValueError(f"coder seed must be one of {list(CODER_SEEDS)}")
    selected = tuple(sorted(set(int(index) for index in pair_indexes)))
    if not selected:
        raise ValueError("at least one pair index is required")
    if selected != parse_pair_indexes(
        ",".join(str(index) for index in selected), pair_count=N_PAIRS
    ):
        raise ValueError("invalid pair indexes")

    profile_arg = ",".join(profile.value for profile in selected_profiles)
    commands: list[tuple[str, tuple[str, ...]]] = []
    for pair_index in selected:
        args = [
            "confirmatory",
            "ablation-pair",
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
            "--profiles",
            profile_arg,
            "--repo",
            str(repo_root),
        ]
        if not resume:
            args.append("--no-resume")
        commands.append((f"pair-{pair_index:02d}", python_module_command(*args)))

    result = run_bounded_pair_processes(commands, max_parallel_pairs=max_parallel_pairs)
    fingerprint = study_fingerprint(study)
    result.update(
        {
            "protocol": DETERMINISTIC_ABLATION_PROTOCOL,
            "study_id": study.study_id,
            "study_fingerprint": fingerprint,
            "claim_plane_runtime_version": _runtime_version(),
            "coder_seed": coder_seed,
            "pair_indexes": list(selected),
            "pair_count": len(selected),
            "profiles": [profile.value for profile in selected_profiles],
        }
    )
    digest = hashlib.sha256(
        json.dumps(
            {
                "seed": coder_seed,
                "pairs": selected,
                "profiles": [profile.value for profile in selected_profiles],
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


__all__ = [
    "AblationProfile",
    "DEFAULT_ABLATION_PROFILES",
    "DETERMINISTIC_ABLATION_PROTOCOL",
    "deterministic_ablation_verdict",
    "parse_ablation_profiles",
    "run_ablation_batch",
    "run_ablation_pair",
]
