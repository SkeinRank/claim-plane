"""Freeze Planner v1 declarations once and reuse them across all coder seeds."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from ..common import PairRef, ProgressUnit, ResearchProgress, StudySpec
from ..common.identity import study_fingerprint
from ..paper_6pair.dataset import TaskInfo, get_repo
from ..planner_v1 import (
    OpenRouterClient,
    PLANNER_MODEL,
    PLANNER_POLICY_FINGERPRINT,
    PLANNER_POLICY_VERSION,
    PlannerV1,
    plan_fingerprint,
)
from .config import ConfirmatoryPaths, PLANNER_FREEZE_SEED

SCHEMA_VERSION = 1


def pair_plan_seed(pair: PairRef, agent: str) -> int:
    normalized = agent.upper()
    if normalized not in {"A", "B"}:
        raise ValueError("agent must be A or B")
    payload = (
        f"V9|planner-freeze|seed={PLANNER_FREEZE_SEED}|{pair.repo}|"
        f"{pair.task_id}|{pair.feature_a}|{pair.feature_b}|agent={normalized}"
    )
    return int(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8], 16)


def planner_unit_id(pair: PairRef, agent: str) -> str:
    normalized = agent.upper()
    if normalized not in {"A", "B"}:
        raise ValueError("agent must be A or B")
    return f"{pair.key}/{normalized}"


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


def load_plan_bundle(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("frozen plan bundle must be one JSON object")
    return payload


def _empty_bundle(study: StudySpec) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "study_fingerprint": study_fingerprint(study),
        "planner_policy_version": PLANNER_POLICY_VERSION,
        "planner_policy_fingerprint": PLANNER_POLICY_FINGERPRINT,
        "planner_model": PLANNER_MODEL,
        "planner_freeze_seed": PLANNER_FREEZE_SEED,
        "pairs": {},
    }


def _validate_agent_result(
    pair: PairRef,
    agent: str,
    result: object,
) -> None:
    if not isinstance(result, dict) or not isinstance(result.get("plan"), dict):
        raise RuntimeError(f"invalid frozen planner result for {pair.key}/{agent}")
    if not bool(result.get("valid")):
        raise RuntimeError(f"invalid planner declaration frozen for {pair.key}/{agent}")
    expected_seed = pair_plan_seed(pair, agent)
    if int(result.get("confirmatory_plan_seed", -1)) != expected_seed:
        raise RuntimeError(f"planner seed mismatch for {pair.key}/{agent}")
    if result.get("confirmatory_plan_fingerprint") != plan_fingerprint(result["plan"]):
        raise RuntimeError(f"planner fingerprint mismatch for {pair.key}/{agent}")


def validate_plan_checkpoint(bundle: Mapping[str, Any], study: StudySpec) -> None:
    """Validate a complete or partially durable planner-freeze checkpoint."""
    if int(bundle.get("schema_version", 0)) != SCHEMA_VERSION:
        raise RuntimeError("unsupported frozen plan bundle schema")
    if bundle.get("study_fingerprint") != study_fingerprint(study):
        raise RuntimeError("frozen plans belong to a different study declaration")
    if bundle.get("planner_policy_version") != PLANNER_POLICY_VERSION:
        raise RuntimeError("frozen plans use a different Planner v1 policy version")
    if bundle.get("planner_policy_fingerprint") != PLANNER_POLICY_FINGERPRINT:
        raise RuntimeError("frozen plans use a different Planner v1 policy fingerprint")
    if bundle.get("planner_model") != PLANNER_MODEL:
        raise RuntimeError("frozen plans use a different planner model")
    if int(bundle.get("planner_freeze_seed", -1)) != PLANNER_FREEZE_SEED:
        raise RuntimeError("frozen plans use a different planner freeze seed")

    pair_payloads = bundle.get("pairs")
    if not isinstance(pair_payloads, dict):
        raise RuntimeError("frozen plan bundle has no pair mapping")

    pair_by_key = {pair.key: pair for pair in study.pairs}
    extra_pairs = sorted(set(pair_payloads) - set(pair_by_key))
    if extra_pairs:
        raise RuntimeError(f"frozen plans contain unexpected pairs: {extra_pairs}")

    for pair_key, pair_payload in pair_payloads.items():
        if not isinstance(pair_payload, dict):
            raise RuntimeError(f"invalid frozen pair payload for {pair_key}")
        extra_agents = sorted(set(pair_payload) - {"A", "B"})
        if extra_agents:
            raise RuntimeError(
                f"frozen plans contain unexpected agents for {pair_key}: {extra_agents}"
            )
        pair = pair_by_key[pair_key]
        for agent, result in pair_payload.items():
            _validate_agent_result(pair, agent, result)


def validate_plan_bundle(bundle: Mapping[str, Any], study: StudySpec) -> None:
    validate_plan_checkpoint(bundle, study)
    pair_payloads = bundle["pairs"]
    expected = {pair.key for pair in study.pairs}
    if set(pair_payloads) != expected:
        missing = sorted(expected - set(pair_payloads))
        extra = sorted(set(pair_payloads) - expected)
        raise RuntimeError(
            f"frozen plan set is incomplete or mismatched; missing={missing}, extra={extra}"
        )
    for pair in study.pairs:
        pair_payload = pair_payloads[pair.key]
        if set(pair_payload) < {"A", "B"}:
            raise RuntimeError(f"frozen plans missing A/B declaration for {pair.key}")


def planner_checkpoint_status(
    path: str | Path,
    study: StudySpec,
    *,
    manifest_exists: bool = False,
) -> dict[str, Any]:
    """Return durable Planner freeze progress without making network calls."""
    expected = len(study.pairs) * 2
    target = Path(path)
    if not target.exists():
        return {
            "state": "not-started",
            "completed_units": 0,
            "expected_units": expected,
        }
    try:
        bundle = load_plan_bundle(target)
        validate_plan_checkpoint(bundle, study)
    except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
        return {
            "state": "invalid",
            "completed_units": 0,
            "expected_units": expected,
            "error": str(exc),
        }

    completed = 0
    for pair in study.pairs:
        pair_payload = bundle["pairs"].get(pair.key, {})
        if not isinstance(pair_payload, dict):
            continue
        completed += sum(agent in pair_payload for agent in ("A", "B"))

    complete_bundle = completed == expected
    state = "complete" if complete_bundle and manifest_exists else "in-progress"
    return {
        "state": state,
        "completed_units": completed,
        "expected_units": expected,
    }


def _load_or_create_checkpoint(
    paths: ConfirmatoryPaths, study: StudySpec
) -> dict[str, Any]:
    if not paths.frozen_plans_file.exists():
        return _empty_bundle(study)
    bundle = load_plan_bundle(paths.frozen_plans_file)
    validate_plan_checkpoint(bundle, study)
    return bundle


def _progress_history(
    study: StudySpec,
    pair_payloads: Mapping[str, Any],
) -> tuple[set[str], dict[str, float], dict[str, float]]:
    completed: set[str] = set()
    durations: dict[str, float] = {}
    costs: dict[str, float] = {}
    for pair in study.pairs:
        payload = pair_payloads.get(pair.key, {})
        if not isinstance(payload, dict):
            continue
        for agent in ("A", "B"):
            result = payload.get(agent)
            if not isinstance(result, dict):
                continue
            _validate_agent_result(pair, agent, result)
            unit_id = planner_unit_id(pair, agent)
            completed.add(unit_id)
            duration = float(
                result.get("planner_wall_time_seconds")
                or result.get("logical_latency")
                or 0.0
            )
            if duration > 0:
                durations[unit_id] = duration
            cost = float(result.get("logical_cost", 0.0) or 0.0)
            if cost >= 0:
                costs[unit_id] = cost
    return completed, durations, costs


def freeze_plans(
    paths: ConfirmatoryPaths,
    study: StudySpec,
    tasks: Mapping[tuple[str, int], TaskInfo],
) -> dict[str, Any]:
    """Create or resume the one-time Planner v1 freeze for all 30 pairs."""
    paths.protocol_dir.mkdir(parents=True, exist_ok=True)
    bundle = _load_or_create_checkpoint(paths, study)
    pair_payloads = bundle["pairs"]
    completed, historical_durations, historical_costs = _progress_history(
        study, pair_payloads
    )

    units = tuple(
        ProgressUnit(
            unit_id=planner_unit_id(pair, agent),
            label=(
                f"{pair.key} · planner {agent} · "
                f"feature {pair.feature_a if agent == 'A' else pair.feature_b}"
            ),
            arm="planner",
        )
        for pair in study.pairs
        for agent in ("A", "B")
    )
    progress = ResearchProgress(
        f"confirmatory planner freeze · seed {PLANNER_FREEZE_SEED}",
        units,
        completed_units=completed,
        historical_durations=historical_durations,
        historical_costs=historical_costs,
        unit_noun="plans",
    )
    progress.start()
    progress.phase(
        1,
        2,
        "freeze Planner v1 declarations",
        detail=f"{len(study.pairs)} pairs · shared across {len(study.coder_seeds)} coder seeds",
    )

    provider = OpenRouterClient()
    planner = PlannerV1(provider)
    try:
        for pair in study.pairs:
            pair_payload = pair_payloads.setdefault(pair.key, {})
            if not isinstance(pair_payload, dict):
                raise RuntimeError(f"invalid frozen pair payload for {pair.key}")
            task = tasks[(pair.repo, pair.task_id)]
            repo: Path | None = None
            for agent, feature_id in (("A", pair.feature_a), ("B", pair.feature_b)):
                unit_id = planner_unit_id(pair, agent)
                existing = pair_payload.get(agent)
                if existing is not None:
                    _validate_agent_result(pair, agent, existing)
                    continue

                progress.start_unit(unit_id)
                started = time.monotonic()
                try:
                    if repo is None:
                        repo = get_repo(
                            task.clone_url, task.base_commit, paths.repo_cache
                        )
                    seed = pair_plan_seed(pair, agent)
                    result = planner.get_calibrated_plan(
                        repo, task.features[feature_id], seed=seed
                    )
                    if not bool(result.get("valid")):
                        raise RuntimeError(
                            "Planner v1 produced an invalid declaration for "
                            f"{pair.key}/{agent}: "
                            f"{result.get('error') or result.get('parse_error')}"
                        )
                    frozen = copy.deepcopy(result)
                    frozen["confirmatory_plan_seed"] = seed
                    frozen["confirmatory_plan_fingerprint"] = plan_fingerprint(
                        frozen["plan"]
                    )
                    wall_time = max(0.0, time.monotonic() - started)
                    frozen["planner_wall_time_seconds"] = wall_time
                    _validate_agent_result(pair, agent, frozen)
                    pair_payload[agent] = frozen
                    _atomic_json(paths.frozen_plans_file, bundle)
                    progress.complete_unit(
                        unit_id,
                        duration_seconds=wall_time,
                        result="FROZEN",
                        cost=float(frozen.get("logical_cost", 0.0) or 0.0),
                    )
                except BaseException as exc:
                    progress.fail_unit(unit_id, exc)
                    raise

        progress.phase(2, 2, "validate frozen plan set and write manifest")
        validate_plan_bundle(bundle, study)
        manifest_rows: list[dict[str, Any]] = []
        total_cost = 0.0
        for pair in study.pairs:
            payload = pair_payloads[pair.key]
            pair_cost = sum(
                float(payload[agent].get("logical_cost", 0.0) or 0.0)
                for agent in ("A", "B")
            )
            pair_latency = max(
                float(payload[agent].get("logical_latency", 0.0) or 0.0)
                for agent in ("A", "B")
            )
            total_cost += pair_cost
            manifest_rows.append(
                {
                    "pair": pair.key,
                    "gold_conflict": pair.gold_conflict,
                    "planner_seed_a": payload["A"]["confirmatory_plan_seed"],
                    "planner_seed_b": payload["B"]["confirmatory_plan_seed"],
                    "plan_a_fingerprint": payload["A"]["confirmatory_plan_fingerprint"],
                    "plan_b_fingerprint": payload["B"]["confirmatory_plan_fingerprint"],
                    "pair_planner_logical_cost": pair_cost,
                    "pair_planner_logical_latency": pair_latency,
                }
            )
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "study_fingerprint": study_fingerprint(study),
            "planner_policy_version": PLANNER_POLICY_VERSION,
            "planner_policy_fingerprint": PLANNER_POLICY_FINGERPRINT,
            "planner_model": PLANNER_MODEL,
            "planner_freeze_seed": PLANNER_FREEZE_SEED,
            "pair_count": len(study.pairs),
            "total_planner_logical_cost": total_cost,
            "rows": manifest_rows,
            "provider_stats_for_this_process": {
                "api_attempts": provider.stats.api_attempts,
                "http_200_responses": provider.stats.http_200_responses,
                "accepted_responses": provider.stats.accepted_responses,
                "actual_cost": provider.stats.actual_cost,
                "planner_cost": provider.stats.planner_cost,
            },
        }
        if paths.frozen_plan_manifest_file.exists():
            old = json.loads(
                paths.frozen_plan_manifest_file.read_text(encoding="utf-8")
            )
            stable_old = dict(old)
            stable_new = dict(manifest)
            stable_old.pop("provider_stats_for_this_process", None)
            stable_new.pop("provider_stats_for_this_process", None)
            if stable_old != stable_new:
                raise RuntimeError(
                    "existing frozen plan manifest differs from the frozen set"
                )
        else:
            _atomic_json(paths.frozen_plan_manifest_file, manifest)
        progress.finish(
            detail="plans frozen once and shared across coder seeds 101/202/303"
        )
        return manifest
    finally:
        progress.close()
