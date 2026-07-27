"""Frozen Planner v1 execution and uncertainty calibration."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from claim_plane import ScopeCommitment

from .policy import (
    CALIBRATION_MAX_TOKENS,
    CALIBRATION_RETRIES,
    CALIBRATION_V2_MAX_MODEL_SELECTED,
    CALIBRATION_V2_PROMPT,
    PLAN_PROMPT,
    PLANNER_FALLBACK_CONTEXT_CHARS,
    PLANNER_FALLBACK_MAX_TOKENS,
    PLANNER_MAX_TOKENS,
    PLANNER_MODEL,
    PLAN_RETRIES,
    RUN_PLANNER_UNCERTAINTY_CALIBRATION,
)
from .provider import CompletionProvider
from .tools import (
    _calibration_v2_candidate_prompt_payload,
    apply_uncertainty_calibration_v2,
    build_uncertainty_candidates_v2,
    read_context,
)


class PlannerExecutionError(RuntimeError):
    """Planner provider/runtime failure distinct from declaration invalidity."""

    def __init__(
        self, message: str, *, provider_failures: list[str] | None = None
    ) -> None:
        super().__init__(message)
        self.provider_failures = provider_failures or []


def extract_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("No JSON object found.")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Planner response is not a JSON object.")
    return payload


def normalize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    files = plan.get("files")
    if not isinstance(files, list):
        raise ValueError("`files` is not a list.")
    if not files:
        raise ValueError("`files` is empty.")

    normalized_files: list[dict[str, Any]] = []
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            raise ValueError(f"`files[{index}]` is not an object.")
        if not item.get("path"):
            raise ValueError(f"`files[{index}].path` is empty.")

        commitment = (
            str(item.get("commitment", ScopeCommitment.COMMITTED.value)).strip().lower()
        )
        if commitment not in {
            ScopeCommitment.COMMITTED.value,
            ScopeCommitment.CONTINGENT.value,
        }:
            raise ValueError(
                f"`files[{index}].commitment` must be `committed` or `contingent`."
            )

        normalized = dict(item)
        normalized["commitment"] = commitment
        normalized_files.append(normalized)

    return {**plan, "files": normalized_files}


def canonical_plan_payload(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Stable order-insensitive representation of one planner declaration."""
    items: list[dict[str, Any]] = []
    for item in plan.get("files", []):
        path = item.get("path")
        if not path:
            continue
        start = int(item.get("line_start", 0) or 0)
        end = int(item.get("line_end", 0) or 0)
        if start > 0 and end > 0:
            start, end = min(start, end), max(start, end)
        items.append(
            {
                "path": str(path),
                "action": str(item.get("action", "modify")).lower(),
                "commitment": str(
                    item.get("commitment", ScopeCommitment.COMMITTED.value)
                ).lower(),
                "line_start": start,
                "line_end": end,
            }
        )
    return sorted(
        items,
        key=lambda item: (
            item["path"],
            item["action"],
            item["commitment"],
            item["line_start"],
            item["line_end"],
        ),
    )


def plan_fingerprint(plan: dict[str, Any]) -> str:
    encoded = json.dumps(
        canonical_plan_payload(plan),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PlannerV1:
    """Exact research planner policy used by the final V8.5 calibration."""

    policy_version = "planner-v1"
    model = PLANNER_MODEL

    def __init__(self, provider: CompletionProvider) -> None:
        self.provider = provider
        self._shared_cache: dict[str, dict[str, Any]] = {}

    def get_plan(
        self, tree: str | Path, feature_dir: str | Path, *, seed: int
    ) -> dict[str, Any]:
        tree = Path(tree)
        feature_dir = Path(feature_dir)
        feature = (feature_dir / "feature.md").read_text(
            encoding="utf-8", errors="replace"
        )[:14_000]
        full_context = read_context(tree, feature_dir)

        parse_error = ""
        logical_cost = 0.0
        logical_latency = 0.0
        cache_hits = 0
        last_result: dict[str, Any] | None = None
        provider_failures: list[str] = []
        response_count = 0

        for attempt in range(PLAN_RETRIES):
            compact_retry = attempt > 0
            context = (
                full_context[:PLANNER_FALLBACK_CONTEXT_CHARS]
                if compact_retry
                else full_context
            )
            retry_notes: list[str] = []
            if compact_retry:
                retry_notes.append(
                    "RETRY MODE: return only the smallest valid JSON declaration. "
                    "Do not explain, reason aloud, or include Markdown."
                )
            if parse_error:
                retry_notes.append(
                    "The previous response was invalid. "
                    f"Parser/schema error: {parse_error}"
                )
            if provider_failures:
                retry_notes.append(
                    "A previous model/provider call failed before a usable response. "
                    "Keep the declaration concise."
                )

            max_tokens = (
                PLANNER_FALLBACK_MAX_TOKENS if compact_retry else PLANNER_MAX_TOKENS
            )
            try:
                completion = self.provider.complete(
                    [
                        {
                            "role": "user",
                            "content": PLAN_PROMPT
                            % (feature, context, "\n".join(retry_notes)),
                        }
                    ],
                    model=PLANNER_MODEL,
                    seed=seed + attempt,
                    max_tokens=max_tokens,
                    role="planner",
                    phase="plan_fallback" if compact_retry else "plan",
                )
            except Exception as exc:
                provider_failures.append(str(exc))
                continue

            response_count += 1
            last_result = completion.to_dict()
            logical_cost += completion.cost
            logical_latency += completion.latency_seconds
            cache_hits += int(completion.cached)
            try:
                plan = normalize_plan(extract_json_object(completion.content))
                return {
                    "plan": plan,
                    "valid": True,
                    "attempts": attempt + 1,
                    "logical_cost": logical_cost,
                    "logical_latency": logical_latency,
                    "cache_hits": cache_hits,
                    "last_result": last_result,
                    "provider_failures": provider_failures,
                }
            except Exception as exc:
                parse_error = str(exc)

        if response_count == 0:
            last = provider_failures[-1] if provider_failures else "unknown"
            raise PlannerExecutionError(
                "Planner failed before producing any usable response after "
                f"{PLAN_RETRIES} planner-level attempts. Last failure: {last}",
                provider_failures=provider_failures,
            )

        return {
            "plan": {"files": []},
            "valid": False,
            "attempts": PLAN_RETRIES,
            "parse_error": parse_error,
            "logical_cost": logical_cost,
            "logical_latency": logical_latency,
            "cache_hits": cache_hits,
            "last_result": last_result,
            "provider_failures": provider_failures,
        }

    def calibrate_uncertainty(
        self,
        tree: str | Path,
        feature_dir: str | Path,
        primary_plan: dict[str, Any],
        *,
        seed: int,
    ) -> dict[str, Any]:
        tree = Path(tree)
        feature_dir = Path(feature_dir)
        if not RUN_PLANNER_UNCERTAINTY_CALIBRATION or not primary_plan.get("files"):
            return {
                "plan": copy.deepcopy(primary_plan),
                "valid": True,
                "applied": False,
                "attempts": 0,
                "logical_cost": 0.0,
                "logical_latency": 0.0,
                "cache_hits": 0,
                "downgraded_count": 0,
                "added_contingent_count": 0,
                "auto_added_count": 0,
                "model_selected_count": 0,
                "candidate_count": 0,
                "selected_candidate_ids": [],
                "auto_candidate_ids": [],
                "model_selected_candidate_ids": [],
                "selected_candidate_kinds": {},
                "provider_failures": [],
                "parse_error": None,
            }

        feature_text = (feature_dir / "feature.md").read_text(
            encoding="utf-8", errors="replace"
        )[:14_000]
        candidates = build_uncertainty_candidates_v2(tree, feature_text, primary_plan)
        prompt_candidates = _calibration_v2_candidate_prompt_payload(candidates)

        logical_cost = 0.0
        logical_latency = 0.0
        cache_hits = 0
        provider_failures: list[str] = []
        parse_error: str | None = None

        for attempt in range(CALIBRATION_RETRIES):
            retry_note = (
                ""
                if attempt == 0
                else (
                    "RETRY MODE: return one valid compact JSON object only. "
                    f"Previous parser error: {parse_error or 'unknown'}"
                )
            )
            try:
                completion = self.provider.complete(
                    [
                        {
                            "role": "user",
                            "content": CALIBRATION_V2_PROMPT
                            % (
                                CALIBRATION_V2_MAX_MODEL_SELECTED,
                                feature_text,
                                json.dumps(primary_plan, indent=2, ensure_ascii=False),
                                json.dumps(
                                    prompt_candidates, indent=2, ensure_ascii=False
                                ),
                                retry_note,
                            ),
                        }
                    ],
                    model=PLANNER_MODEL,
                    seed=seed + 80_001 + attempt,
                    max_tokens=CALIBRATION_MAX_TOKENS,
                    role="planner",
                    phase="uncertainty_calibration_v2",
                )
            except Exception as exc:
                provider_failures.append(str(exc))
                continue

            logical_cost += completion.cost
            logical_latency += completion.latency_seconds
            cache_hits += int(completion.cached)

            try:
                payload = extract_json_object(completion.content)
                applied = apply_uncertainty_calibration_v2(
                    primary_plan, candidates, payload
                )
                return {
                    "plan": applied["plan"],
                    "valid": True,
                    "applied": True,
                    "attempts": attempt + 1,
                    "logical_cost": logical_cost,
                    "logical_latency": logical_latency,
                    "cache_hits": cache_hits,
                    "downgraded_count": applied["downgraded_count"],
                    "added_contingent_count": applied["added_contingent_count"],
                    "auto_added_count": applied["auto_added_count"],
                    "model_selected_count": applied["model_selected_count"],
                    "candidate_count": len(candidates),
                    "selected_candidate_ids": applied["selected_candidate_ids"],
                    "auto_candidate_ids": applied["auto_candidate_ids"],
                    "model_selected_candidate_ids": applied[
                        "model_selected_candidate_ids"
                    ],
                    "selected_candidate_kinds": applied["selected_candidate_kinds"],
                    "provider_failures": provider_failures,
                    "parse_error": None,
                }
            except Exception as exc:
                parse_error = str(exc)

        fallback = apply_uncertainty_calibration_v2(
            primary_plan,
            candidates,
            {"downgrade_item_indices": [], "selected_candidate_ids": []},
        )
        return {
            "plan": fallback["plan"],
            "valid": False,
            "applied": True,
            "attempts": CALIBRATION_RETRIES,
            "logical_cost": logical_cost,
            "logical_latency": logical_latency,
            "cache_hits": cache_hits,
            "downgraded_count": 0,
            "added_contingent_count": fallback["added_contingent_count"],
            "auto_added_count": fallback["auto_added_count"],
            "model_selected_count": 0,
            "candidate_count": len(candidates),
            "selected_candidate_ids": fallback["selected_candidate_ids"],
            "auto_candidate_ids": fallback["auto_candidate_ids"],
            "model_selected_candidate_ids": [],
            "selected_candidate_kinds": fallback["selected_candidate_kinds"],
            "provider_failures": provider_failures,
            "parse_error": parse_error,
        }

    def get_calibrated_plan(
        self, tree: str | Path, feature_dir: str | Path, *, seed: int
    ) -> dict[str, Any]:
        primary = self.get_plan(tree, feature_dir, seed=seed)
        if not primary["valid"]:
            return {
                **primary,
                "primary_plan": copy.deepcopy(primary["plan"]),
                "primary_logical_cost": primary["logical_cost"],
                "primary_logical_latency": primary["logical_latency"],
                "calibration_valid": None,
                "calibration_applied": False,
                "calibration_attempts": 0,
                "calibration_logical_cost": 0.0,
                "calibration_logical_latency": 0.0,
                "calibration_downgraded_count": 0,
                "calibration_added_contingent_count": 0,
                "calibration_auto_added_count": 0,
                "calibration_model_selected_count": 0,
                "calibration_candidate_count": 0,
                "calibration_selected_candidate_ids": [],
                "calibration_auto_candidate_ids": [],
                "calibration_model_selected_candidate_ids": [],
                "calibration_selected_candidate_kinds": {},
                "shared_plan_cache_hit": False,
            }

        calibration = self.calibrate_uncertainty(
            tree, feature_dir, primary["plan"], seed=seed
        )
        return {
            **primary,
            "plan": calibration["plan"],
            "primary_plan": copy.deepcopy(primary["plan"]),
            "logical_cost": float(primary["logical_cost"])
            + float(calibration["logical_cost"]),
            "logical_latency": float(primary["logical_latency"])
            + float(calibration["logical_latency"]),
            "cache_hits": int(primary["cache_hits"]) + int(calibration["cache_hits"]),
            "primary_logical_cost": primary["logical_cost"],
            "primary_logical_latency": primary["logical_latency"],
            "calibration_valid": calibration["valid"],
            "calibration_applied": calibration["applied"],
            "calibration_attempts": calibration["attempts"],
            "calibration_logical_cost": calibration["logical_cost"],
            "calibration_logical_latency": calibration["logical_latency"],
            "calibration_downgraded_count": calibration["downgraded_count"],
            "calibration_added_contingent_count": calibration["added_contingent_count"],
            "calibration_auto_added_count": calibration.get("auto_added_count", 0),
            "calibration_model_selected_count": calibration.get(
                "model_selected_count", 0
            ),
            "calibration_candidate_count": calibration.get("candidate_count", 0),
            "calibration_selected_candidate_ids": calibration.get(
                "selected_candidate_ids", []
            ),
            "calibration_auto_candidate_ids": calibration.get("auto_candidate_ids", []),
            "calibration_model_selected_candidate_ids": calibration.get(
                "model_selected_candidate_ids", []
            ),
            "calibration_selected_candidate_kinds": calibration.get(
                "selected_candidate_kinds", {}
            ),
            "calibration_provider_failures": calibration["provider_failures"],
            "calibration_parse_error": calibration["parse_error"],
            "shared_plan_cache_hit": False,
        }

    def get_shared_calibrated_plan(
        self,
        cache_key: str,
        tree: str | Path,
        feature_dir: str | Path,
        *,
        seed: int,
    ) -> dict[str, Any]:
        """Return byte-for-byte identical planner output to static and dynamic arms."""
        if cache_key in self._shared_cache:
            cached = copy.deepcopy(self._shared_cache[cache_key])
            cached["shared_plan_cache_hit"] = True
            return cached
        result = self.get_calibrated_plan(tree, feature_dir, seed=seed)
        self._shared_cache[cache_key] = copy.deepcopy(result)
        return result
