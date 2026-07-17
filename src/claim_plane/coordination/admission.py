"""Sound pre-write admission for partially overlapping agent tasks."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Iterable

from claim_plane.core.models import (
    AccessMode,
    AdmissionConflict,
    AdmissionDecision,
    AdmissionKind,
    ChangeIntent,
    IntentOperation,
    ResourceKind,
    highest_admission_kind,
)


@dataclass(frozen=True, slots=True)
class PairAssessment:
    kind: AdmissionKind
    reason: str
    blocking: bool
    constraint: str | None = None
    notification: str | None = None


class AdmissionEngine:
    """Conservative intent admission.

    Known overlapping writes fail closed.  Parallel execution is allowed only
    for proven disjoint regions, read dependencies, documentation/tests under a
    concept-bound contract, or matching contracts bound to the same concept.
    """

    def evaluate(
        self,
        incoming: ChangeIntent,
        active: Iterable[ChangeIntent],
        known_intent_ids: Iterable[str] = (),
    ) -> AdmissionDecision:
        conflicts: list[AdmissionConflict] = []
        constraints: list[str] = []
        notifications: list[str] = []
        active = tuple(active)
        known = set(known_intent_ids) | {item.intent_id for item in active}

        missing_dependencies = sorted(set(incoming.dependencies) - known)
        if missing_dependencies:
            return AdmissionDecision(
                kind=AdmissionKind.REJECT,
                intent=incoming,
                allowed=False,
                constraints=("Declare dependencies only after their intents exist.",),
                guidance="Missing dependency intents: "
                + ", ".join(missing_dependencies),
            )

        ambiguous = [
            operation.resource.identifier
            for operation in incoming.admission_operations
            if operation.mutating
            and operation.resource.metadata.get("semantic_status") == "ambiguous"
        ]
        if ambiguous:
            return AdmissionDecision(
                kind=AdmissionKind.REJECT,
                intent=incoming,
                allowed=False,
                constraints=(
                    "Resolve ambiguous terminology before mutating shared state.",
                ),
                guidance="Ambiguous semantic identity: "
                + ", ".join(sorted(set(ambiguous))),
            )

        semantic_meta = incoming.metadata.get("semantic_resolution") or {}
        if semantic_meta.get("required") and not semantic_meta.get("enabled"):
            return AdmissionDecision(
                kind=AdmissionKind.REJECT,
                intent=incoming,
                allowed=False,
                constraints=("Restore the required semantic identity layer.",),
                guidance="Semantic mode was requested but Agent Lexicon is unavailable.",
            )

        deprecated = [
            operation.resource.identifier
            for operation in incoming.admission_operations
            if operation.mutating and operation.resource.metadata.get("deprecated")
        ]
        if deprecated:
            constraints.append(
                "Replace deprecated surfaces with canonical terms: "
                + ", ".join(sorted(set(deprecated)))
            )

        for existing in active:
            if existing.intent_id == incoming.intent_id:
                continue
            for incoming_op in incoming.admission_operations:
                for existing_op in existing.admission_operations:
                    assessment = self._assess(
                        incoming, incoming_op, existing, existing_op
                    )
                    if assessment is None:
                        continue
                    conflicts.append(
                        AdmissionConflict(
                            existing_intent_id=existing.intent_id,
                            existing_owner=existing.owner,
                            kind=assessment.kind,
                            incoming_operation=incoming_op,
                            existing_operation=existing_op,
                            reason=assessment.reason,
                            blocking=assessment.blocking,
                        )
                    )
                    if assessment.constraint:
                        constraints.append(assessment.constraint)
                    if assessment.notification:
                        notifications.append(assessment.notification)

        kind = highest_admission_kind(conflict.kind for conflict in conflicts)
        allowed = not any(conflict.blocking for conflict in conflicts)
        if not conflicts:
            kind = AdmissionKind.INDEPENDENT

        if allowed and conflicts:
            guidance = (
                f"Intent {incoming.intent_id} may run in parallel under "
                f"{len(set(constraints))} verified constraint(s)."
            )
        elif allowed:
            guidance = f"Intent {incoming.intent_id} is independent of active work."
        else:
            blocking_ids = sorted(
                {c.existing_intent_id for c in conflicts if c.blocking}
            )
            guidance = (
                f"Intent {incoming.intent_id} is not admitted. Replan or serialize against: "
                + ", ".join(blocking_ids)
            )

        return AdmissionDecision(
            kind=kind,
            intent=incoming,
            allowed=allowed,
            conflicts=tuple(conflicts),
            constraints=tuple(dict.fromkeys(constraints)),
            notifications=tuple(dict.fromkeys(notifications)),
            guidance=guidance,
        )

    def _assess(
        self,
        incoming_intent: ChangeIntent,
        incoming: IntentOperation,
        existing_intent: ChangeIntent,
        existing: IntentOperation,
    ) -> PairAssessment | None:
        overlap = _resource_overlap(incoming, existing)
        if overlap == "none":
            return None
        if incoming.access is AccessMode.READ and existing.access is AccessMode.READ:
            return None
        incoming_base = incoming_intent.base_commit or incoming_intent.base_revision
        existing_base = existing_intent.base_commit or existing_intent.base_revision
        if incoming_base != existing_base and (incoming.mutating or existing.mutating):
            return PairAssessment(
                AdmissionKind.REPLAN,
                (
                    "Overlapping intents use different base commits/revisions: "
                    f"{incoming_base} vs {existing_base}."
                ),
                True,
                "Rebase both intents onto one common revision before parallel execution.",
            )

        resource = incoming.resource.identifier
        existing_resource = existing.resource.identifier
        if incoming.access.destructive or existing.access.destructive:
            return PairAssessment(
                AdmissionKind.REPLAN,
                f"Destructive operation overlaps {existing_intent.intent_id}: {resource} / {existing_resource}.",
                True,
                "A rename/delete invalidates dependents and requires re-admission.",
                f"Notify {existing_intent.owner}: destructive change targets {existing_resource}.",
            )

        if (
            incoming.resource.kind is ResourceKind.CONTRACT
            or existing.resource.kind is ResourceKind.CONTRACT
        ):
            return self._assess_contract(
                incoming_intent, incoming, existing_intent, existing
            )

        if incoming.access is AccessMode.READ or existing.access is AccessMode.READ:
            reader = (
                incoming_intent
                if incoming.access is AccessMode.READ
                else existing_intent
            )
            writer = (
                existing_intent
                if incoming.access is AccessMode.READ
                else incoming_intent
            )
            key = incoming.resource.semantic_key or existing.resource.semantic_key
            return PairAssessment(
                AdmissionKind.NOTIFY_ON_CHANGE,
                f"{reader.intent_id} reads a resource mutated by {writer.intent_id}.",
                False,
                f"Record {writer.intent_id} as a premise of {reader.intent_id} for {key}.",
                f"Invalidate {reader.intent_id} if {key} changes.",
            )

        if incoming.resource.kind in {
            ResourceKind.FILE,
            ResourceKind.DOCUMENT,
        } or existing.resource.kind in {
            ResourceKind.FILE,
            ResourceKind.DOCUMENT,
        }:
            return self._assess_path(
                incoming_intent, incoming, existing_intent, existing, overlap
            )

        if {incoming.access, existing.access} & {AccessMode.DOCUMENT, AccessMode.TEST}:
            concept_key = incoming.resource.semantic_key
            signature = _shared_contract_signature(
                incoming_intent, existing_intent, concept_key
            )
            if signature:
                return PairAssessment(
                    AdmissionKind.COMPATIBLE_OVERLAP,
                    f"Documentation/test work and implementation share concept-bound contract `{signature}`.",
                    False,
                    f"All outputs must preserve `{signature}`.",
                    f"Notify both owners if `{signature}` changes.",
                )
            return PairAssessment(
                AdmissionKind.REQUIRES_STUB,
                f"Documentation/test overlap for {resource} has no concept-bound contract.",
                True,
                "Bind a contract to the shared concept before parallel execution.",
            )

        if (
            incoming.resource.kind is ResourceKind.CONCEPT
            or existing.resource.kind is ResourceKind.CONCEPT
        ):
            concept_key = incoming.resource.semantic_key
            signature = _shared_contract_signature(
                incoming_intent, existing_intent, concept_key
            )
            if signature:
                return PairAssessment(
                    AdmissionKind.COMPATIBLE_OVERLAP,
                    f"Both intents mutate one concept under concept-bound contract `{signature}`.",
                    False,
                    f"Both workers must preserve `{signature}`.",
                    f"Invalidate both workers if `{signature}` changes.",
                )
            return PairAssessment(
                AdmissionKind.REQUIRES_STUB,
                f"Both intents mutate concept {resource} without a shared concept-bound contract.",
                True,
                "Publish a contract stub bound to this concept or split the write surfaces.",
            )

        return PairAssessment(
            AdmissionKind.SERIALIZE,
            f"Concurrent writes overlap resource {resource}.",
            True,
            "Assign one writer or define provably disjoint bounded regions.",
        )

    def _assess_contract(
        self,
        incoming_intent: ChangeIntent,
        incoming: IntentOperation,
        existing_intent: ChangeIntent,
        existing: IntentOperation,
    ) -> PairAssessment:
        # A contract only coordinates resources when both declarations identify
        # the same contract and the same subject concept.
        if not _same_contract(incoming, existing):
            return PairAssessment(
                AdmissionKind.REQUIRES_STUB,
                (
                    f"Contract declarations overlap semantically but are not bound to the same subject: "
                    f"{incoming.resource.identifier} / {existing.resource.identifier}."
                ),
                True,
                "Set subject_concept_id on both contract declarations.",
            )

        incoming_signature = incoming.resource.signature
        existing_signature = existing.resource.signature
        if incoming.access is AccessMode.READ or existing.access is AccessMode.READ:
            producer = (
                existing_intent
                if incoming.access is AccessMode.READ
                else incoming_intent
            )
            consumer = (
                incoming_intent
                if incoming.access is AccessMode.READ
                else existing_intent
            )
            signature = (
                incoming_signature or existing_signature or incoming.resource.identifier
            )
            mismatch = bool(
                incoming_signature
                and existing_signature
                and incoming_signature != existing_signature
            )
            producer_is_incoming_amendment = (
                incoming.mutating
                and incoming_intent.metadata.get("_amendment") is True
                and existing.access is AccessMode.READ
            )
            if mismatch and not producer_is_incoming_amendment:
                return PairAssessment(
                    AdmissionKind.REPLAN,
                    (
                        f"Consumer and producer declare different contract versions for "
                        f"{incoming.resource.identifier}: `{incoming_signature}` vs `{existing_signature}`."
                    ),
                    True,
                    "Refresh the consumer premise or amend the producer with dependent invalidation.",
                )
            reason = (
                f"{producer.intent_id} changes a consumed contract; {consumer.intent_id} must become stale."
                if mismatch
                else f"{consumer.intent_id} consumes contract produced or changed by {producer.intent_id}."
            )
            return PairAssessment(
                AdmissionKind.CONTRACT_DEPENDENCY,
                reason,
                False,
                f"Consumer must compile/test against `{signature}`.",
                f"Invalidate {consumer.intent_id} if `{signature}` changes.",
            )
        if (
            incoming_signature
            and existing_signature
            and incoming_signature != existing_signature
        ):
            return PairAssessment(
                AdmissionKind.REPLAN,
                (
                    f"Contract mismatch for {incoming.resource.identifier}: "
                    f"`{incoming_signature}` vs `{existing_signature}`."
                ),
                True,
                "Negotiate one signature before workers continue.",
            )
        if incoming_signature and existing_signature:
            return PairAssessment(
                AdmissionKind.PARALLEL_WITH_CONSTRAINT,
                f"Both intents use matching concept-bound contract `{incoming_signature}`.",
                False,
                f"Neither worker may change `{incoming_signature}` without amendment and re-admission.",
                f"Contract updates must be broadcast to {incoming_intent.owner} and {existing_intent.owner}.",
            )
        return PairAssessment(
            AdmissionKind.REQUIRES_STUB,
            f"Concurrent contract work for {incoming.resource.identifier} has no published signature.",
            True,
            "Publish a signature/stub before admitting dependent workers.",
        )

    def _assess_path(
        self,
        incoming_intent: ChangeIntent,
        incoming: IntentOperation,
        existing_intent: ChangeIntent,
        existing: IntentOperation,
        overlap: str,
    ) -> PairAssessment:
        if overlap == "same-resource-disjoint-region":
            return PairAssessment(
                AdmissionKind.PARALLEL_WITH_CONSTRAINT,
                f"Both intents touch {incoming.resource.identifier} in disjoint line regions.",
                False,
                (
                    f"{incoming_intent.intent_id} stays in {incoming.resource.region}; "
                    f"{existing_intent.intent_id} stays in {existing.resource.region}; Git hunks are verified."
                ),
            )
        if overlap in {"scope-exact-overlap", "scope-overlap"}:
            return PairAssessment(
                AdmissionKind.SERIALIZE,
                (
                    f"Write scopes overlap and include at least one common path: "
                    f"{incoming.resource.identifier} / {existing.resource.identifier}."
                ),
                True,
                "Narrow both intents to disjoint concrete files before parallel execution.",
            )
        return PairAssessment(
            AdmissionKind.SERIALIZE,
            f"Two writers target the same file/region: {incoming.resource.identifier}.",
            True,
            "Split into verified disjoint regions or assign one writer plus reviewer.",
        )


def _contract_entries(intent: ChangeIntent, concept_key: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for operation in intent.admission_operations:
        resource = operation.resource
        if resource.kind is not ResourceKind.CONTRACT or not resource.signature:
            continue
        if resource.subject_key != concept_key:
            continue
        entries[resource.semantic_key] = resource.signature
    return entries


def _shared_contract_signature(
    a: ChangeIntent, b: ChangeIntent, concept_key: str
) -> str | None:
    a_contracts = _contract_entries(a, concept_key)
    b_contracts = _contract_entries(b, concept_key)
    for key in sorted(set(a_contracts) & set(b_contracts)):
        if a_contracts[key] == b_contracts[key]:
            return a_contracts[key]
    return None


def _same_contract(a: IntentOperation, b: IntentOperation) -> bool:
    ar = a.resource
    br = b.resource
    return (
        ar.kind is ResourceKind.CONTRACT
        and br.kind is ResourceKind.CONTRACT
        and ar.semantic_key == br.semantic_key
        and ar.subject_key is not None
        and ar.subject_key == br.subject_key
    )


def _resource_overlap(a: IntentOperation, b: IntentOperation) -> str:
    ar = a.resource
    br = b.resource
    path_kinds = {ResourceKind.FILE, ResourceKind.DOCUMENT}
    if ar.kind in path_kinds and br.kind in path_kinds:
        return _path_overlap(ar.identifier, br.identifier, ar.region, br.region)

    semantic_kinds = {
        ResourceKind.SYMBOL,
        ResourceKind.CONCEPT,
        ResourceKind.CONFIG,
        ResourceKind.ROUTE,
        ResourceKind.SCHEMA,
    }
    if ar.kind is ResourceKind.CONTRACT and br.kind is ResourceKind.CONTRACT:
        return "same-resource" if _same_contract(a, b) else "none"
    if ar.kind in semantic_kinds and br.kind in semantic_kinds:
        return "same-resource" if ar.semantic_key == br.semantic_key else "none"
    # Contract-to-concept relationships are evaluated through the concept/concept
    # pair and concept-bound contract lookup, not as an extra pairwise conflict.
    if {ar.kind, br.kind} == {ResourceKind.CONTRACT, ResourceKind.CONCEPT}:
        return "none"
    return "none"


def _path_overlap(a: str, b: str, a_region: str | None, b_region: str | None) -> str:
    a = a.replace("\\", "/").lstrip("./").rstrip("/")
    b = b.replace("\\", "/").lstrip("./").rstrip("/")
    a_pattern = any(ch in a for ch in "*?[")
    b_pattern = any(ch in b for ch in "*?[")

    if not a_pattern and not b_pattern:
        if a != b:
            return "none"
        regions = _regions_overlap(a_region, b_region)
        if regions is False:
            return "same-resource-disjoint-region"
        return "same-resource"

    if a_pattern and not b_pattern:
        return "scope-exact-overlap" if fnmatch.fnmatchcase(b, a) else "none"
    if b_pattern and not a_pattern:
        return "scope-exact-overlap" if fnmatch.fnmatchcase(a, b) else "none"

    # Glob/glob intersection is undecidable in general.  Prefixes before the
    # first wildcard make many disjoint cases obvious; all others fail closed.
    a_prefix = _glob_prefix(a)
    b_prefix = _glob_prefix(b)
    if (
        a_prefix
        and b_prefix
        and not (a_prefix.startswith(b_prefix) or b_prefix.startswith(a_prefix))
    ):
        return "none"
    return "scope-overlap"


def _glob_prefix(pattern: str) -> str:
    indexes = [pattern.find(ch) for ch in "*?[" if pattern.find(ch) >= 0]
    return pattern[: min(indexes)] if indexes else pattern


def _regions_overlap(a: str | None, b: str | None) -> bool | None:
    if not a or not b:
        return None
    parsed_a = parse_line_region(a)
    parsed_b = parse_line_region(b)
    if parsed_a is None or parsed_b is None:
        return None
    a_start, a_end = parsed_a
    b_start, b_end = parsed_b
    return not (a_end < b_start or b_end < a_start)


def parse_line_region(value: str) -> tuple[int, int] | None:
    match = re.search(
        r"(?:lines?\s*[:=]?\s*)?(\d+)\s*[-:]\s*(\d+)", value, re.IGNORECASE
    )
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    return (min(start, end), max(start, end))
