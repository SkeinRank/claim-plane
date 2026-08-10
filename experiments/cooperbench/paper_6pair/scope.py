"""Admission, Dynamic Scope setup, and scope-quality metrics for the paper study."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from claim_plane import Plane, ScopeCommitment

from ..planner_v1 import admission_verdict, plan_fingerprint, plan_to_intent


def declared_files(plan: dict[str, Any]) -> list[str]:
    return sorted(
        {str(item["path"]) for item in plan.get("files", []) if item.get("path")}
    )


def declared_committed_files(plan: dict[str, Any]) -> list[str]:
    return sorted(
        {
            str(item["path"])
            for item in plan.get("files", [])
            if item.get("path")
            and str(item.get("commitment", ScopeCommitment.COMMITTED.value)).lower()
            == ScopeCommitment.COMMITTED.value
        }
    )


def declared_contingent_files(plan: dict[str, Any]) -> list[str]:
    return sorted(
        {
            str(item["path"])
            for item in plan.get("files", [])
            if item.get("path")
            and str(item.get("commitment", ScopeCommitment.COMMITTED.value)).lower()
            == ScopeCommitment.CONTINGENT.value
        }
    )


def build_scope_plane(
    plan_a: dict[str, Any],
    plan_b: dict[str, Any],
    *,
    force_all_committed: bool,
    base_commit: str,
) -> dict[str, Any]:
    plane = Plane.open(":memory:")
    intent_a = plan_to_intent(
        "A",
        "agent-a",
        plan_a,
        force_all_committed=force_all_committed,
        base_commit=base_commit,
    )
    intent_b = plan_to_intent(
        "B",
        "agent-b",
        plan_b,
        force_all_committed=force_all_committed,
        base_commit=base_commit,
    )
    if intent_a is None or intent_b is None:
        raise ValueError("Cannot build a scope plane from an empty declaration.")
    decision_a = plane.admit(intent_a)
    decision_b = plane.admit(intent_b)
    return {
        "plane": plane,
        "intent_a": intent_a,
        "intent_b": intent_b,
        "decision_a": decision_a,
        "decision_b": decision_b,
    }


def prepare_threadsafe_scope_registry(
    plan_a: dict[str, Any],
    plan_b: dict[str, Any],
    *,
    force_all_committed: bool,
    base_commit: str,
    db_path: str | Path,
) -> dict[str, Any]:
    """Create two independent Plane connections over one shared SQLite registry.

    The published harness used one in-memory Plane because provider calls were
    sequential. Physical worker threads cannot safely share that SQLite connection,
    so each controller receives its own connection while registry decisions remain
    serialized by SQLite transactions.
    """

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()

    intent_a = plan_to_intent(
        "A",
        "agent-a",
        plan_a,
        force_all_committed=force_all_committed,
        base_commit=base_commit,
    )
    intent_b = plan_to_intent(
        "B",
        "agent-b",
        plan_b,
        force_all_committed=force_all_committed,
        base_commit=base_commit,
    )
    if intent_a is None or intent_b is None:
        raise ValueError("Cannot build a scope plane from an empty declaration.")

    admin = Plane.open(path)
    try:
        decision_a = admin.admit(intent_a)
        decision_b = admin.admit(intent_b)
    finally:
        admin.close()

    return {
        "intent_a": intent_a,
        "intent_b": intent_b,
        "decision_a": decision_a,
        "decision_b": decision_b,
        "db_path": path,
    }


def build_single_scope_plane(
    plan: dict[str, Any],
    *,
    intent_id: str,
    owner: str,
    force_all_committed: bool,
    base_commit: str,
) -> Plane:
    plane = Plane.open(":memory:")
    intent = plan_to_intent(
        intent_id,
        owner,
        plan,
        force_all_committed=force_all_committed,
        base_commit=base_commit,
    )
    if intent is None:
        raise ValueError("Cannot build a scope controller from an empty declaration.")
    decision = plane.admit(intent)
    if not decision.allowed:
        raise RuntimeError(
            "Single-intent scope admission unexpectedly failed: "
            + (decision.guidance or decision.kind.value)
        )
    plane.activate(intent_id)
    return plane


def declared_scope_records(plan: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in plan.get("files", []):
        path = item.get("path")
        if not path:
            continue
        start = int(item.get("line_start", 0) or 0)
        end = int(item.get("line_end", 0) or 0)
        if start > 0 and end > 0:
            start, end = min(start, end), max(start, end)
        else:
            start = end = 0
        records.append(
            {
                "path": str(path),
                "action": str(item.get("action", "modify")).lower(),
                "commitment": str(
                    item.get("commitment", ScopeCommitment.COMMITTED.value)
                ).lower(),
                "line_start": start,
                "line_end": end,
                "coordinate_space": "preimage",
            }
        )
    return records


def _interval_union_length(intervals: Iterable[tuple[int, int]]) -> int:
    normalized = sorted(
        (min(int(a), int(b)), max(int(a), int(b))) for a, b in intervals
    )
    if not normalized:
        return 0
    total = 0
    current_start, current_end = normalized[0]
    for start, end in normalized[1:]:
        if start <= current_end + 1:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start + 1
            current_start, current_end = start, end
    return total + current_end - current_start + 1


def _interval_overlap_length(
    left: Iterable[tuple[int, int]], right: Iterable[tuple[int, int]]
) -> int:
    left_items = sorted((min(int(a), int(b)), max(int(a), int(b))) for a, b in left)
    right_items = sorted((min(int(a), int(b)), max(int(a), int(b))) for a, b in right)
    i = j = overlap = 0
    while i < len(left_items) and j < len(right_items):
        l_start, l_end = left_items[i]
        r_start, r_end = right_items[j]
        start, end = max(l_start, r_start), min(l_end, r_end)
        if start <= end:
            overlap += end - start + 1
        if l_end < r_end:
            i += 1
        else:
            j += 1
    return overlap


def scope_precision_recall(
    declared_records: list[dict[str, Any]] | None,
    actual_records: list[dict[str, Any]] | None,
    *,
    region_evaluable: bool = True,
) -> dict[str, Any]:
    declared_records = declared_records or []
    actual_records = actual_records or []
    declared_files_set = {item["path"] for item in declared_records if item.get("path")}
    actual_files_set = {item["path"] for item in actual_records if item.get("path")}
    file_overlap = declared_files_set & actual_files_set
    file_precision = (
        len(file_overlap) / len(declared_files_set)
        if declared_files_set
        else (1.0 if not actual_files_set else 0.0)
    )
    file_recall = (
        len(file_overlap) / len(actual_files_set)
        if actual_files_set
        else (1.0 if not declared_files_set else 0.0)
    )
    region_precision: float | None = None
    region_recall: float | None = None
    if region_evaluable:
        declared_by_path: dict[str, list[tuple[int, int]]] = {}
        actual_by_path: dict[str, list[tuple[int, int]]] = {}
        for item in declared_records:
            if item.get("path"):
                declared_by_path.setdefault(str(item["path"]), []).append(
                    (int(item.get("line_start", 0)), int(item.get("line_end", 0)))
                )
        for item in actual_records:
            if item.get("path"):
                actual_by_path.setdefault(str(item["path"]), []).append(
                    (int(item.get("line_start", 0)), int(item.get("line_end", 0)))
                )
        declared_total = sum(
            _interval_union_length(value) for value in declared_by_path.values()
        )
        actual_total = sum(_interval_union_length(v) for v in actual_by_path.values())
        overlap_total = sum(
            _interval_overlap_length(
                declared_by_path.get(path, []), actual_by_path.get(path, [])
            )
            for path in set(declared_by_path) | set(actual_by_path)
        )
        region_precision = (
            overlap_total / declared_total
            if declared_total
            else (1.0 if not actual_total else 0.0)
        )
        region_recall = (
            overlap_total / actual_total
            if actual_total
            else (1.0 if not declared_total else 0.0)
        )
    return {
        "file_precision": file_precision,
        "file_recall": file_recall,
        "region_precision": region_precision,
        "region_recall": region_recall,
        "region_evaluable": bool(region_evaluable),
    }


def jaccard(left: Iterable[str] | None, right: Iterable[str] | None) -> float | None:
    left_set, right_set = set(left or []), set(right or [])
    union = left_set | right_set
    return len(left_set & right_set) / len(union) if union else None


def gate_decision_fingerprint(
    plan_a: dict[str, Any], plan_b: dict[str, Any], verdict: dict[str, Any]
) -> str:
    payload = {
        "plan_a": plan_fingerprint(plan_a),
        "plan_b": plan_fingerprint(plan_b),
        "kind": verdict.get("kind"),
        "serialized": bool(verdict.get("serialized")),
        "allowed": bool(verdict.get("allowed")),
    }
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "admission_verdict",
    "build_scope_plane",
    "build_single_scope_plane",
    "prepare_threadsafe_scope_registry",
    "declared_committed_files",
    "declared_contingent_files",
    "declared_files",
    "declared_scope_records",
    "gate_decision_fingerprint",
    "jaccard",
    "plan_fingerprint",
    "scope_precision_recall",
]
