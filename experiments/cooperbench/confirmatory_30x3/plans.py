"""Freeze Planner v1 declarations once and reuse them across all coder seeds."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from ..common import PairRef, StudySpec
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


def validate_plan_bundle(bundle: Mapping[str, Any], study: StudySpec) -> None:
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
    expected = {pair.key for pair in study.pairs}
    if set(pair_payloads) != expected:
        missing = sorted(expected - set(pair_payloads))
        extra = sorted(set(pair_payloads) - expected)
        raise RuntimeError(
            f"frozen plan set is incomplete or mismatched; missing={missing}, extra={extra}"
        )
    for pair in study.pairs:
        pair_payload = pair_payloads[pair.key]
        if not isinstance(pair_payload, dict) or set(pair_payload) < {"A", "B"}:
            raise RuntimeError(f"frozen plans missing A/B declaration for {pair.key}")
        for agent in ("A", "B"):
            result = pair_payload[agent]
            if not isinstance(result, dict) or not isinstance(result.get("plan"), dict):
                raise RuntimeError(
                    f"invalid frozen planner result for {pair.key}/{agent}"
                )
            if not bool(result.get("valid")):
                raise RuntimeError(
                    f"invalid planner declaration frozen for {pair.key}/{agent}"
                )
            expected_seed = pair_plan_seed(pair, agent)
            if int(result.get("confirmatory_plan_seed", -1)) != expected_seed:
                raise RuntimeError(f"planner seed mismatch for {pair.key}/{agent}")
            if result.get("confirmatory_plan_fingerprint") != plan_fingerprint(
                result["plan"]
            ):
                raise RuntimeError(
                    f"planner fingerprint mismatch for {pair.key}/{agent}"
                )


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


def freeze_plans(
    paths: ConfirmatoryPaths,
    study: StudySpec,
    tasks: Mapping[tuple[str, int], TaskInfo],
) -> dict[str, Any]:
    """Create or resume the one-time Planner v1 freeze for all 30 pairs."""
    paths.protocol_dir.mkdir(parents=True, exist_ok=True)
    if paths.frozen_plans_file.exists():
        bundle = load_plan_bundle(paths.frozen_plans_file)
        header = dict(bundle)
        pair_payloads = header.pop("pairs", None)
        expected_header = _empty_bundle(study)
        expected_header.pop("pairs")
        if header != expected_header:
            raise RuntimeError(
                "existing frozen plan checkpoint belongs to a different protocol"
            )
        if not isinstance(pair_payloads, dict):
            raise RuntimeError("existing frozen plan checkpoint is malformed")
        bundle["pairs"] = pair_payloads
    else:
        bundle = _empty_bundle(study)

    provider = OpenRouterClient()
    planner = PlannerV1(provider)
    pair_payloads = bundle["pairs"]

    for pair in study.pairs:
        existing = pair_payloads.get(pair.key)
        if isinstance(existing, dict) and set(existing) >= {"A", "B"}:
            continue
        task = tasks[(pair.repo, pair.task_id)]
        repo = get_repo(task.clone_url, task.base_commit, paths.repo_cache)
        result_by_agent: dict[str, dict[str, Any]] = {}
        for agent, feature_id in (("A", pair.feature_a), ("B", pair.feature_b)):
            seed = pair_plan_seed(pair, agent)
            result = planner.get_calibrated_plan(
                repo, task.features[feature_id], seed=seed
            )
            if not bool(result.get("valid")):
                raise RuntimeError(
                    f"Planner v1 produced an invalid declaration for {pair.key}/{agent}: "
                    f"{result.get('error') or result.get('parse_error')}"
                )
            frozen = copy.deepcopy(result)
            frozen["confirmatory_plan_seed"] = seed
            frozen["confirmatory_plan_fingerprint"] = plan_fingerprint(frozen["plan"])
            result_by_agent[agent] = frozen
        pair_payloads[pair.key] = result_by_agent
        _atomic_json(paths.frozen_plans_file, bundle)

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
        old = json.loads(paths.frozen_plan_manifest_file.read_text(encoding="utf-8"))
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
    return manifest
