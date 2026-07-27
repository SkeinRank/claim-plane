"""Conversion between Planner v1 declarations and Claim Plane intents."""

from __future__ import annotations

from typing import Any

from claim_plane import ChangeIntent, ScopeCommitment
from claim_plane.coordination.admission import AdmissionEngine


def plan_to_intent(
    intent_id: str,
    owner: str,
    plan: dict[str, Any],
    *,
    force_all_committed: bool = False,
    base_commit: str | None = None,
) -> ChangeIntent | None:
    operations: list[dict[str, Any]] = []
    for item in plan.get("files", []):
        path = item.get("path")
        if not path:
            continue

        action = str(item.get("action", "modify")).lower()
        access = {
            "modify": "write",
            "create": "write",
            "delete": "delete",
            "rename": "rename",
        }.get(action, "write")
        commitment = (
            ScopeCommitment.COMMITTED.value
            if force_all_committed
            else str(item.get("commitment", ScopeCommitment.COMMITTED.value)).lower()
        )
        start = int(item.get("line_start", 0) or 0)
        end = int(item.get("line_end", 0) or 0)
        resource: dict[str, Any] = {"kind": "file", "identifier": str(path)}
        if start > 0 and end > 0:
            resource["region"] = f"lines:{min(start, end)}-{max(start, end)}"

        operation: dict[str, Any] = {"access": access, "resource": resource}
        if commitment == ScopeCommitment.CONTINGENT.value:
            operation["commitment"] = commitment
        operations.append(operation)

    if not operations:
        return None

    return ChangeIntent.from_dict(
        {
            "intent_id": intent_id,
            "task_id": intent_id,
            "owner": owner,
            "base_revision": "HEAD",
            "base_commit": base_commit,
            "operations": operations,
        }
    )


def admission_verdict(
    plan_a: dict[str, Any],
    plan_b: dict[str, Any],
    *,
    force_all_committed: bool = False,
) -> dict[str, Any]:
    """Evaluate the same two-declaration gate used by the V8.5 study."""
    intent_a = plan_to_intent(
        "A", "agent-a", plan_a, force_all_committed=force_all_committed
    )
    intent_b = plan_to_intent(
        "B", "agent-b", plan_b, force_all_committed=force_all_committed
    )
    if intent_a is None or intent_b is None:
        return {
            "serialized": True,
            "kind": "declaration_invalid",
            "allowed": False,
            "reason": "At least one declaration was empty or invalid.",
            "valid_for_accuracy": False,
        }

    engine = AdmissionEngine()
    decision_a = engine.evaluate(intent_a, [], {"A", "B"})
    if not decision_a.allowed:
        return {
            "serialized": True,
            "kind": decision_a.kind.value,
            "allowed": False,
            "reason": decision_a.guidance or "Agent A was not admitted.",
            "valid_for_accuracy": True,
        }

    decision_b = engine.evaluate(intent_b, [intent_a], {"A", "B"})
    return {
        "serialized": not decision_b.allowed,
        "kind": decision_b.kind.value,
        "allowed": decision_b.allowed,
        "reason": decision_b.guidance or "; ".join(decision_b.constraints),
        "valid_for_accuracy": True,
    }
