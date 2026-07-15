"""Deterministic targeted repair plans derived from verifier findings."""

from __future__ import annotations

from claim_plane.core.models import (
    FindingCode,
    IntegrationReport,
    RepairAction,
    RepairActionKind,
    RepairPlan,
)


def build_repair_plan(report: IntegrationReport) -> RepairPlan:
    actions: list[RepairAction] = []
    for finding in report.findings:
        if finding.code is FindingCode.UNDECLARED_CHANGE:
            actions.append(
                RepairAction(
                    RepairActionKind.REDECLARE_SCOPE,
                    f"Revert {finding.path} or amend the intent before continuing.",
                    paths=(finding.path,) if finding.path else (),
                    priority=10,
                )
            )
        elif finding.code is FindingCode.REGION_VIOLATION:
            actions.append(
                RepairAction(
                    RepairActionKind.REPAIR_REGION,
                    "Move the edit back into the admitted region or submit an amended bounded region.",
                    paths=(finding.path,) if finding.path else (),
                    priority=8,
                )
            )
        elif finding.code is FindingCode.CONTRACT_MISMATCH:
            actions.append(
                RepairAction(
                    RepairActionKind.ALIGN_CONTRACT,
                    "Align implementation with the admitted concept-bound signature; amend first if the contract itself must change.",
                    paths=(finding.path,) if finding.path else (),
                    identifiers=(finding.identifier,) if finding.identifier else (),
                    priority=5,
                )
            )
        elif finding.code is FindingCode.CONTRACT_MISSING:
            actions.append(
                RepairAction(
                    RepairActionKind.ADD_MISSING_ARTIFACT,
                    "Implement the declared contract or amend the intent to make it optional.",
                    identifiers=(finding.identifier,) if finding.identifier else (),
                    priority=20,
                )
            )
        elif finding.code is FindingCode.STALE_BASE:
            actions.append(
                RepairAction(
                    RepairActionKind.PIN_BASE,
                    "Pin the exact base commit, rebase onto it, and request re-admission if contracts or regions changed.",
                    priority=1,
                )
            )
        elif finding.code in {
            FindingCode.UNDECLARED_READ,
            FindingCode.OBSERVED_DEPENDENCY_MISSING,
        }:
            actions.append(
                RepairAction(
                    RepairActionKind.DECLARE_READ,
                    "Declare the observed read premise and its producer dependency, then re-admit before continuing.",
                    paths=(finding.path,) if finding.path else (),
                    related_intents=(finding.related_intent_id,)
                    if finding.related_intent_id
                    else (),
                    priority=2,
                )
            )
        elif finding.code in {
            FindingCode.DEPENDENCY_MISSING,
            FindingCode.DEPENDENCY_STALE,
        }:
            actions.append(
                RepairAction(
                    RepairActionKind.REFRESH_DEPENDENCY,
                    "Refresh the producer premise, amend the consumer intent, and re-admit before resuming.",
                    related_intents=(finding.related_intent_id,)
                    if finding.related_intent_id
                    else (),
                    priority=2,
                )
            )
        elif finding.code is FindingCode.CROSS_INTENT_COLLISION:
            actions.append(
                RepairAction(
                    RepairActionKind.SERIALIZE,
                    "Stop overlapping writers, split the concrete surface or publish a shared concept-bound contract, then re-admit.",
                    paths=(finding.path,) if finding.path else (),
                    identifiers=(finding.identifier,) if finding.identifier else (),
                    related_intents=(finding.related_intent_id,)
                    if finding.related_intent_id
                    else (),
                    priority=3,
                )
            )
        elif finding.code is FindingCode.SNAPSHOT_MUTATION:
            actions.append(
                RepairAction(
                    RepairActionKind.RERUN_ACCEPTANCE,
                    "Revert acceptance-command mutations, then re-freeze and verify the worker snapshot.",
                    paths=(finding.path,) if finding.path else (),
                    priority=1,
                )
            )
        elif finding.code is FindingCode.ACCEPTANCE_FAILED:
            actions.append(
                RepairAction(
                    RepairActionKind.RERUN_ACCEPTANCE,
                    "Fix the failing acceptance command and rerun verification with --run-acceptance.",
                    identifiers=(finding.identifier,) if finding.identifier else (),
                    priority=15,
                )
            )
        elif finding.code is FindingCode.MISSING_DECLARED_CHANGE:
            actions.append(
                RepairAction(
                    RepairActionKind.ADD_MISSING_ARTIFACT,
                    "Complete the required declared change or amend the intent to remove it.",
                    paths=(finding.path,) if finding.path else (),
                    priority=30,
                )
            )
        else:
            actions.append(
                RepairAction(
                    RepairActionKind.HUMAN_REVIEW,
                    finding.message,
                    paths=(finding.path,) if finding.path else (),
                    identifiers=(finding.identifier,) if finding.identifier else (),
                    related_intents=(finding.related_intent_id,)
                    if finding.related_intent_id
                    else (),
                    priority=50,
                )
            )

    unique: dict[tuple[object, ...], RepairAction] = {}
    for action in actions:
        marker = (
            action.kind,
            action.instruction,
            action.paths,
            action.identifiers,
            action.related_intents,
        )
        unique.setdefault(marker, action)
    return RepairPlan(
        report.intent_id,
        tuple(
            sorted(unique.values(), key=lambda item: (item.priority, item.kind.value))
        ),
    )
