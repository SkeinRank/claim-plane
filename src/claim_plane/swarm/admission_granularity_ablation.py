"""Deterministic admission-granularity ablation for Claim Plane swarm planning.

This module measures the admission effect of the 9B--9D authority stack without
launching workers.  Every profile consumes the same immutable work graph, budget,
semantic graph, and commutativity proofs.  Candidate blocking is disabled during
this ablation so differences are attributable to authority granularity and policy
refinement rather than pair prefiltering.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from claim_plane.core import CommutativityProof, SemanticDependencyGraph
from claim_plane.swarm.admission_attribution import (
    AdmissionDecisionAttributionReport,
    AdmissionPairDisposition,
)
from claim_plane.swarm.budget import SwarmBudgetPolicy
from claim_plane.swarm.concurrency import ConcurrencyPlan, compute_concurrency_plan
from claim_plane.swarm.models import WorkGraph

ADMISSION_GRANULARITY_ABLATION_PROTOCOL = (
    "claim-plane.admission-granularity-ablation.v1"
)


class AdmissionGranularityProfile(str, Enum):
    """Ordered authority/policy stages measured by the ablation."""

    BROAD_DECLARED = "broad_declared"
    SYMBOL_PROJECTION = "symbol_projection"
    DEPENDENCY_NARROWING = "dependency_narrowing"
    REFINED_POLICY = "refined_policy"


ADMISSION_GRANULARITY_PROFILE_ORDER = tuple(AdmissionGranularityProfile)


def _canonical_json(payload: Mapping[str, Any] | list[Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(payload: Mapping[str, Any] | list[Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _validate_digest(
    name: str, value: str | None, *, optional: bool = False
) -> str | None:
    if value is None and optional:
        return None
    text = str(value or "").lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return text


def _worker_authority_fingerprint(graph: WorkGraph) -> str:
    payload = [
        {
            "work_id": item.work_id,
            "operations": [
                operation.to_dict()
                for operation in item.operations
                if operation.committed and operation.mutating
            ],
        }
        for item in sorted(graph.work_items, key=lambda item: item.work_id)
    ]
    return _sha256(payload)


@dataclass(frozen=True, slots=True)
class AdmissionGranularityProfileResult:
    """Static concurrency result for one ablation profile."""

    profile: AdmissionGranularityProfile
    plan_fingerprint: str
    analysis_graph_fingerprint: str
    attribution_fingerprint: str
    status: str
    pair_count: int
    parallel_eligible_pairs: int
    dependency_ordered_pairs: int
    serialized_pairs: int
    denied_pairs: int
    wave_count: int
    peak_concurrency: int
    mean_wave_width: float
    parallel_eligibility_rate: float
    narrowed_work_items: int
    closed_dependency_work_items: int
    fail_closed_dependency_work_items: int
    policy_released_serializations: int
    constraint_reason_counts: Mapping[str, int] = field(default_factory=dict)
    primary_reason_counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile", AdmissionGranularityProfile(self.profile))
        for name in (
            "plan_fingerprint",
            "analysis_graph_fingerprint",
            "attribution_fingerprint",
        ):
            object.__setattr__(self, name, _validate_digest(name, getattr(self, name)))
        if not str(self.status).strip():
            raise ValueError("ablation profile status must not be empty")
        for name in (
            "pair_count",
            "parallel_eligible_pairs",
            "dependency_ordered_pairs",
            "serialized_pairs",
            "denied_pairs",
            "wave_count",
            "peak_concurrency",
            "narrowed_work_items",
            "closed_dependency_work_items",
            "fail_closed_dependency_work_items",
            "policy_released_serializations",
        ):
            if int(getattr(self, name)) < 0:
                raise ValueError(f"{name} must be non-negative")
        if (
            self.parallel_eligible_pairs
            + self.dependency_ordered_pairs
            + self.serialized_pairs
            + self.denied_pairs
            != self.pair_count
        ):
            raise ValueError("ablation disposition counts must cover every pair")
        if self.mean_wave_width < 0:
            raise ValueError("mean_wave_width must be non-negative")
        if not 0.0 <= self.parallel_eligibility_rate <= 1.0:
            raise ValueError("parallel_eligibility_rate must be between zero and one")
        object.__setattr__(
            self,
            "constraint_reason_counts",
            dict(
                sorted(
                    (str(k), int(v))
                    for k, v in self.constraint_reason_counts.items()
                    if int(v)
                )
            ),
        )
        object.__setattr__(
            self,
            "primary_reason_counts",
            dict(
                sorted(
                    (str(k), int(v))
                    for k, v in self.primary_reason_counts.items()
                    if int(v)
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.value,
            "plan_fingerprint": self.plan_fingerprint,
            "analysis_graph_fingerprint": self.analysis_graph_fingerprint,
            "attribution_fingerprint": self.attribution_fingerprint,
            "status": self.status,
            "pair_count": self.pair_count,
            "parallel_eligible_pairs": self.parallel_eligible_pairs,
            "dependency_ordered_pairs": self.dependency_ordered_pairs,
            "serialized_pairs": self.serialized_pairs,
            "denied_pairs": self.denied_pairs,
            "wave_count": self.wave_count,
            "peak_concurrency": self.peak_concurrency,
            "mean_wave_width": self.mean_wave_width,
            "parallel_eligibility_rate": self.parallel_eligibility_rate,
            "narrowed_work_items": self.narrowed_work_items,
            "closed_dependency_work_items": self.closed_dependency_work_items,
            "fail_closed_dependency_work_items": self.fail_closed_dependency_work_items,
            "policy_released_serializations": self.policy_released_serializations,
            "constraint_reason_counts": dict(self.constraint_reason_counts),
            "primary_reason_counts": dict(self.primary_reason_counts),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdmissionGranularityProfileResult":
        return cls(
            profile=AdmissionGranularityProfile(data["profile"]),
            plan_fingerprint=str(data["plan_fingerprint"]),
            analysis_graph_fingerprint=str(data["analysis_graph_fingerprint"]),
            attribution_fingerprint=str(data["attribution_fingerprint"]),
            status=str(data["status"]),
            pair_count=int(data["pair_count"]),
            parallel_eligible_pairs=int(data["parallel_eligible_pairs"]),
            dependency_ordered_pairs=int(data["dependency_ordered_pairs"]),
            serialized_pairs=int(data["serialized_pairs"]),
            denied_pairs=int(data["denied_pairs"]),
            wave_count=int(data["wave_count"]),
            peak_concurrency=int(data["peak_concurrency"]),
            mean_wave_width=float(data["mean_wave_width"]),
            parallel_eligibility_rate=float(data["parallel_eligibility_rate"]),
            narrowed_work_items=int(data["narrowed_work_items"]),
            closed_dependency_work_items=int(data["closed_dependency_work_items"]),
            fail_closed_dependency_work_items=int(
                data["fail_closed_dependency_work_items"]
            ),
            policy_released_serializations=int(data["policy_released_serializations"]),
            constraint_reason_counts=dict(data.get("constraint_reason_counts") or {}),
            primary_reason_counts=dict(data.get("primary_reason_counts") or {}),
        )


@dataclass(frozen=True, slots=True)
class AdmissionGranularityTransition:
    """Pairwise decision changes between adjacent ablation stages."""

    from_profile: AdmissionGranularityProfile
    to_profile: AdmissionGranularityProfile
    changed_pairs: int
    newly_parallel_pairs: int
    released_serializations: int
    newly_serialized_pairs: int
    newly_denied_pairs: int
    released_denials: int
    ordering_changes: int
    parallel_pair_delta: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "from_profile", AdmissionGranularityProfile(self.from_profile)
        )
        object.__setattr__(
            self, "to_profile", AdmissionGranularityProfile(self.to_profile)
        )
        for name in (
            "changed_pairs",
            "newly_parallel_pairs",
            "released_serializations",
            "newly_serialized_pairs",
            "newly_denied_pairs",
            "released_denials",
            "ordering_changes",
        ):
            if int(getattr(self, name)) < 0:
                raise ValueError(f"{name} must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_profile": self.from_profile.value,
            "to_profile": self.to_profile.value,
            "changed_pairs": self.changed_pairs,
            "newly_parallel_pairs": self.newly_parallel_pairs,
            "released_serializations": self.released_serializations,
            "newly_serialized_pairs": self.newly_serialized_pairs,
            "newly_denied_pairs": self.newly_denied_pairs,
            "released_denials": self.released_denials,
            "ordering_changes": self.ordering_changes,
            "parallel_pair_delta": self.parallel_pair_delta,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdmissionGranularityTransition":
        return cls(
            from_profile=AdmissionGranularityProfile(data["from_profile"]),
            to_profile=AdmissionGranularityProfile(data["to_profile"]),
            changed_pairs=int(data["changed_pairs"]),
            newly_parallel_pairs=int(data["newly_parallel_pairs"]),
            released_serializations=int(data["released_serializations"]),
            newly_serialized_pairs=int(data["newly_serialized_pairs"]),
            newly_denied_pairs=int(data["newly_denied_pairs"]),
            released_denials=int(data["released_denials"]),
            ordering_changes=int(data["ordering_changes"]),
            parallel_pair_delta=int(data["parallel_pair_delta"]),
        )


@dataclass(frozen=True, slots=True)
class AdmissionGranularityAblationReport:
    """Source-bound deterministic report comparing all admission granularity stages."""

    work_graph_fingerprint: str
    worker_authority_fingerprint: str
    budget_fingerprint: str
    profiles: tuple[AdmissionGranularityProfileResult, ...]
    transitions: tuple[AdmissionGranularityTransition, ...]
    semantic_graph_fingerprint: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = ADMISSION_GRANULARITY_ABLATION_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != ADMISSION_GRANULARITY_ABLATION_PROTOCOL:
            raise ValueError(
                f"unsupported admission granularity ablation {self.protocol!r}"
            )
        for name in (
            "work_graph_fingerprint",
            "worker_authority_fingerprint",
            "budget_fingerprint",
        ):
            object.__setattr__(self, name, _validate_digest(name, getattr(self, name)))
        object.__setattr__(
            self,
            "semantic_graph_fingerprint",
            _validate_digest(
                "semantic_graph_fingerprint",
                self.semantic_graph_fingerprint,
                optional=True,
            ),
        )
        profiles = tuple(
            item
            if isinstance(item, AdmissionGranularityProfileResult)
            else AdmissionGranularityProfileResult.from_dict(item)
            for item in self.profiles
        )
        if (
            tuple(item.profile for item in profiles)
            != ADMISSION_GRANULARITY_PROFILE_ORDER
        ):
            raise ValueError("ablation profiles must use the canonical stage order")
        transitions = tuple(
            item
            if isinstance(item, AdmissionGranularityTransition)
            else AdmissionGranularityTransition.from_dict(item)
            for item in self.transitions
        )
        expected = tuple(
            zip(
                ADMISSION_GRANULARITY_PROFILE_ORDER,
                ADMISSION_GRANULARITY_PROFILE_ORDER[1:],
            )
        )
        actual = tuple((item.from_profile, item.to_profile) for item in transitions)
        if actual != expected:
            raise ValueError(
                "ablation transitions must connect adjacent canonical profiles"
            )
        object.__setattr__(self, "profiles", profiles)
        object.__setattr__(self, "transitions", transitions)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def profile_map(
        self,
    ) -> dict[AdmissionGranularityProfile, AdmissionGranularityProfileResult]:
        return {item.profile: item for item in self.profiles}

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_dict(include_fingerprint=False))

    def _summary_core(self) -> dict[str, Any]:
        baseline = self.profiles[0]
        final = self.profiles[-1]
        return {
            "profile_count": len(self.profiles),
            "pair_count": final.pair_count,
            "baseline_parallel_eligible_pairs": baseline.parallel_eligible_pairs,
            "final_parallel_eligible_pairs": final.parallel_eligible_pairs,
            "parallel_pair_gain": (
                final.parallel_eligible_pairs - baseline.parallel_eligible_pairs
            ),
            "baseline_peak_concurrency": baseline.peak_concurrency,
            "final_peak_concurrency": final.peak_concurrency,
            "released_serializations": sum(
                item.released_serializations for item in self.transitions
            ),
            "released_denials": sum(item.released_denials for item in self.transitions),
        }

    def summary(self) -> dict[str, Any]:
        return {**self._summary_core(), "fingerprint": self.fingerprint}

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload = {
            "protocol": self.protocol,
            "work_graph_fingerprint": self.work_graph_fingerprint,
            "worker_authority_fingerprint": self.worker_authority_fingerprint,
            "budget_fingerprint": self.budget_fingerprint,
            "semantic_graph_fingerprint": self.semantic_graph_fingerprint,
            "profiles": [item.to_dict() for item in self.profiles],
            "transitions": [item.to_dict() for item in self.transitions],
            "summary": self.summary() if include_fingerprint else self._summary_core(),
            "metadata": dict(self.metadata),
        }
        if include_fingerprint:
            return {"fingerprint": self.fingerprint, **payload}
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdmissionGranularityAblationReport":
        report = cls(
            protocol=str(
                data.get("protocol") or ADMISSION_GRANULARITY_ABLATION_PROTOCOL
            ),
            work_graph_fingerprint=str(data["work_graph_fingerprint"]),
            worker_authority_fingerprint=str(data["worker_authority_fingerprint"]),
            budget_fingerprint=str(data["budget_fingerprint"]),
            semantic_graph_fingerprint=(
                str(data["semantic_graph_fingerprint"])
                if data.get("semantic_graph_fingerprint") is not None
                else None
            ),
            profiles=tuple(
                AdmissionGranularityProfileResult.from_dict(item)
                for item in data.get("profiles") or ()
            ),
            transitions=tuple(
                AdmissionGranularityTransition.from_dict(item)
                for item in data.get("transitions") or ()
            ),
            metadata=dict(data.get("metadata") or {}),
        )
        supplied = data.get("fingerprint")
        if supplied is not None and str(supplied) != report.fingerprint:
            raise ValueError("admission granularity ablation fingerprint mismatch")
        summary = data.get("summary")
        if isinstance(summary, Mapping) and dict(summary) != report.summary():
            raise ValueError("admission granularity ablation summary mismatch")
        return report


def _profile_result(
    profile: AdmissionGranularityProfile,
    plan: ConcurrencyPlan,
    attribution: AdmissionDecisionAttributionReport,
) -> AdmissionGranularityProfileResult:
    summary = attribution.summary()
    reason_counts: dict[str, int] = {}
    for constraint in plan.constraints:
        for reason in constraint.reasons:
            reason_counts[reason.value] = reason_counts.get(reason.value, 0) + 1
    wave_widths = [len(wave.work_ids) for wave in plan.waves]
    mean_wave_width = (sum(wave_widths) / len(wave_widths)) if wave_widths else 0.0
    pair_count = int(summary["pair_count"])
    parallel_pairs = int(summary["parallel_eligible_pairs"])
    projection_summary = plan.metadata.get("symbol_authority_projection_summary")
    narrowing_summary = plan.metadata.get("dependency_authority_narrowing_summary")
    refinement_summary = plan.metadata.get("conflict_policy_refinement_summary")
    projection_active = profile is not AdmissionGranularityProfile.BROAD_DECLARED
    narrowing_active = profile in {
        AdmissionGranularityProfile.DEPENDENCY_NARROWING,
        AdmissionGranularityProfile.REFINED_POLICY,
    }
    refinement_active = profile is AdmissionGranularityProfile.REFINED_POLICY
    return AdmissionGranularityProfileResult(
        profile=profile,
        plan_fingerprint=plan.fingerprint(),
        analysis_graph_fingerprint=str(
            plan.metadata["admission_analysis_graph_fingerprint"]
        ),
        attribution_fingerprint=attribution.fingerprint,
        status=plan.status.value,
        pair_count=pair_count,
        parallel_eligible_pairs=parallel_pairs,
        dependency_ordered_pairs=int(summary["dependency_ordered_pairs"]),
        serialized_pairs=int(summary["serialized_pairs"]),
        denied_pairs=int(summary["denied_pairs"]),
        wave_count=len(plan.waves),
        peak_concurrency=plan.peak_concurrency,
        mean_wave_width=mean_wave_width,
        parallel_eligibility_rate=(parallel_pairs / pair_count if pair_count else 1.0),
        narrowed_work_items=(
            int(projection_summary.get("narrowed_work_items", 0))
            if projection_active and isinstance(projection_summary, Mapping)
            else 0
        ),
        closed_dependency_work_items=(
            int(narrowing_summary.get("closed_work_items", 0))
            if narrowing_active and isinstance(narrowing_summary, Mapping)
            else 0
        ),
        fail_closed_dependency_work_items=(
            int(narrowing_summary.get("fail_closed_work_items", 0))
            if narrowing_active and isinstance(narrowing_summary, Mapping)
            else 0
        ),
        policy_released_serializations=(
            int(refinement_summary.get("released_serializations", 0))
            if refinement_active and isinstance(refinement_summary, Mapping)
            else 0
        ),
        constraint_reason_counts=reason_counts,
        primary_reason_counts=dict(summary["primary_reason_counts"]),
    )


def _transition(
    from_profile: AdmissionGranularityProfile,
    to_profile: AdmissionGranularityProfile,
    before: AdmissionDecisionAttributionReport,
    after: AdmissionDecisionAttributionReport,
) -> AdmissionGranularityTransition:
    before_map = {
        frozenset((item.left_id, item.right_id)): item for item in before.pairs
    }
    after_map = {frozenset((item.left_id, item.right_id)): item for item in after.pairs}
    if set(before_map) != set(after_map):
        raise ValueError("ablation profile attribution pair sets differ")
    changed = newly_parallel = released_serializations = newly_serialized = 0
    newly_denied = released_denials = ordering_changes = 0
    for key in sorted(before_map, key=lambda item: tuple(sorted(item))):
        left = before_map[key]
        right = after_map[key]
        left_order = (left.before_id, left.after_id)
        right_order = (right.before_id, right.after_id)
        if left.disposition != right.disposition or left_order != right_order:
            changed += 1
        if (
            right.disposition is AdmissionPairDisposition.PARALLEL_ELIGIBLE
            and left.disposition is not AdmissionPairDisposition.PARALLEL_ELIGIBLE
        ):
            newly_parallel += 1
        if (
            left.disposition is AdmissionPairDisposition.SERIALIZED
            and right.disposition is AdmissionPairDisposition.PARALLEL_ELIGIBLE
        ):
            released_serializations += 1
        if (
            right.disposition is AdmissionPairDisposition.SERIALIZED
            and left.disposition is not AdmissionPairDisposition.SERIALIZED
        ):
            newly_serialized += 1
        if (
            right.disposition is AdmissionPairDisposition.DENIED
            and left.disposition is not AdmissionPairDisposition.DENIED
        ):
            newly_denied += 1
        if (
            left.disposition is AdmissionPairDisposition.DENIED
            and right.disposition is not AdmissionPairDisposition.DENIED
        ):
            released_denials += 1
        ordered_dispositions = {
            AdmissionPairDisposition.ORDERED_BY_DEPENDENCY,
            AdmissionPairDisposition.SERIALIZED,
        }
        if (
            left_order != right_order
            and left.disposition in ordered_dispositions
            and right.disposition in ordered_dispositions
        ):
            ordering_changes += 1
    parallel_delta = (
        sum(
            item.disposition is AdmissionPairDisposition.PARALLEL_ELIGIBLE
            for item in after.pairs
        )
        - sum(
            item.disposition is AdmissionPairDisposition.PARALLEL_ELIGIBLE
            for item in before.pairs
        )
    )
    return AdmissionGranularityTransition(
        from_profile=from_profile,
        to_profile=to_profile,
        changed_pairs=changed,
        newly_parallel_pairs=newly_parallel,
        released_serializations=released_serializations,
        newly_serialized_pairs=newly_serialized,
        newly_denied_pairs=newly_denied,
        released_denials=released_denials,
        ordering_changes=ordering_changes,
        parallel_pair_delta=parallel_delta,
    )


def run_admission_granularity_ablation(
    graph: WorkGraph,
    policy: SwarmBudgetPolicy,
    *,
    semantic_graph: SemanticDependencyGraph | None = None,
    commutativity_proofs: Iterable[CommutativityProof] = (),
    graph_version: int = 1,
    budget_version: int = 1,
) -> AdmissionGranularityAblationReport:
    """Measure each authority granularity stage on identical source-bound inputs."""

    proofs = tuple(commutativity_proofs)
    plans: dict[AdmissionGranularityProfile, ConcurrencyPlan] = {}
    attributions: dict[
        AdmissionGranularityProfile, AdmissionDecisionAttributionReport
    ] = {}
    profile_results: list[AdmissionGranularityProfileResult] = []
    for profile in ADMISSION_GRANULARITY_PROFILE_ORDER:
        plan = compute_concurrency_plan(
            graph,
            policy,
            graph_version=graph_version,
            budget_version=budget_version,
            semantic_graph=semantic_graph,
            commutativity_proofs=proofs,
            candidate_blocking_enabled=False,
            admission_granularity_stage=profile.value,
        )
        if plan.graph_fingerprint != graph.fingerprint():
            raise ValueError("ablation plan is not bound to the supplied work graph")
        if plan.budget_fingerprint != policy.fingerprint():
            raise ValueError("ablation plan is not bound to the supplied budget")
        attribution_payload = plan.metadata.get("admission_attribution")
        if not isinstance(attribution_payload, Mapping):
            raise ValueError("ablation plan is missing admission attribution")
        attribution = AdmissionDecisionAttributionReport.from_dict(attribution_payload)
        plans[profile] = plan
        attributions[profile] = attribution
        profile_results.append(_profile_result(profile, plan, attribution))

    transitions = tuple(
        _transition(left, right, attributions[left], attributions[right])
        for left, right in zip(
            ADMISSION_GRANULARITY_PROFILE_ORDER,
            ADMISSION_GRANULARITY_PROFILE_ORDER[1:],
        )
    )
    return AdmissionGranularityAblationReport(
        work_graph_fingerprint=graph.fingerprint(),
        worker_authority_fingerprint=_worker_authority_fingerprint(graph),
        budget_fingerprint=policy.fingerprint(),
        semantic_graph_fingerprint=(
            semantic_graph.fingerprint if semantic_graph is not None else None
        ),
        profiles=tuple(profile_results),
        transitions=transitions,
        metadata={
            "scope": "deterministic-admission-only",
            "worker_mutation_authority_preserved": True,
            "candidate_blocking_enabled": False,
            "profile_order": [
                item.value for item in ADMISSION_GRANULARITY_PROFILE_ORDER
            ],
            "measurement": "same-input-counterfactual",
            "physical_execution": False,
            "next_validation": "confirmatory-physical-benchmark",
        },
    )


__all__ = [
    "ADMISSION_GRANULARITY_ABLATION_PROTOCOL",
    "ADMISSION_GRANULARITY_PROFILE_ORDER",
    "AdmissionGranularityProfile",
    "AdmissionGranularityProfileResult",
    "AdmissionGranularityTransition",
    "AdmissionGranularityAblationReport",
    "run_admission_granularity_ablation",
]
