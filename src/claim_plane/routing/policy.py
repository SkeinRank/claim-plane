"""A transparent baseline router.

This does not execute models. It recommends a worker tier from declared risk,
then relies on the same integration verifier and escalation policy for quality.
"""

from __future__ import annotations

from claim_plane.core.models import (
    AccessMode,
    ChangeIntent,
    ResourceKind,
    RouteRecommendation,
    WorkerTier,
)


def recommend_worker_tier(
    intent: ChangeIntent, *, overlap_count: int = 0
) -> RouteRecommendation:
    score = 0
    reasons: list[str] = []

    operation_count = len(intent.operations)
    if operation_count >= 8:
        score += 3
        reasons.append("large declared change surface")
    elif operation_count >= 4:
        score += 1
        reasons.append("moderate declared change surface")

    public_contracts = sum(
        operation.resource.kind is ResourceKind.CONTRACT and operation.mutating
        for operation in intent.operations
    )
    if public_contracts:
        score += min(4, public_contracts * 2)
        reasons.append(f"{public_contracts} mutating public contract(s)")

    destructive = sum(
        operation.access in {AccessMode.DELETE, AccessMode.RENAME}
        for operation in intent.operations
    )
    if destructive:
        score += destructive * 4
        reasons.append("destructive rename/delete operation")

    if overlap_count:
        score += min(4, overlap_count * 2)
        reasons.append(f"{overlap_count} active overlap(s)")

    if not intent.acceptance:
        score += 2
        reasons.append("no machine-checkable acceptance criteria")
    if not intent.preserves:
        score += 1
        reasons.append("no explicit preserved invariants")

    if intent.metadata.get("security_sensitive"):
        score += 5
        reasons.append("security-sensitive task")
    if intent.metadata.get("migration"):
        score += 3
        reasons.append("data/schema migration")

    if score >= 6:
        tier = WorkerTier.FRONTIER
        fallback = None
    elif score >= 3:
        tier = WorkerTier.STANDARD
        fallback = WorkerTier.FRONTIER
    else:
        tier = WorkerTier.ECONOMY
        fallback = WorkerTier.STANDARD
        if not reasons:
            reasons.append("small bounded task with explicit write surface")

    return RouteRecommendation(
        tier=tier, risk_score=score, reasons=tuple(reasons), fallback_tier=fallback
    )
