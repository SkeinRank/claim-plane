"""Protocol serialization helpers kept separate from SQLite storage."""

from __future__ import annotations

from typing import Any, Mapping

from claim_plane.core.models import (
    AdmissionConflict,
    AdmissionDecision,
    AdmissionKind,
    ChangeIntent,
    IntentOperation,
)


def admission_decision_from_dict(data: Mapping[str, Any]) -> AdmissionDecision:
    conflicts = tuple(
        AdmissionConflict(
            existing_intent_id=str(item["existing_intent_id"]),
            existing_owner=str(item["existing_owner"]),
            kind=AdmissionKind(item["kind"]),
            incoming_operation=IntentOperation.from_dict(item["incoming_operation"]),
            existing_operation=IntentOperation.from_dict(item["existing_operation"]),
            reason=str(item["reason"]),
            blocking=bool(item["blocking"]),
        )
        for item in data.get("conflicts") or ()
    )
    return AdmissionDecision(
        kind=AdmissionKind(data["kind"]),
        allowed=bool(data["allowed"]),
        intent=ChangeIntent.from_dict(data["intent"]),
        conflicts=conflicts,
        constraints=tuple(data.get("constraints") or ()),
        notifications=tuple(data.get("notifications") or ()),
        guidance=str(data.get("guidance") or ""),
    )
