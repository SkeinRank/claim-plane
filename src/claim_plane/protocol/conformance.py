"""Reusable adapter conformance reporting and guarantee verification.

The conformance layer executes one canonical scenario set through an adapter-specific
fixture.  Adapters may translate their own runtime payloads, but scenario identities,
result semantics, and capability-claim checks remain shared.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from claim_plane.protocol.capabilities import (
    AdapterCapabilityManifest,
    EnforcementLevel,
)

ADAPTER_CONFORMANCE_PROTOCOL = "claim-plane.adapter-conformance.v1"
ADAPTER_CONFORMANCE_VERSION = "1.0"


class ConformanceScenario(str, Enum):
    """Canonical behavior every adapter must expose to the conformance kit."""

    DECLARED_MUTATION_SUCCEEDS = "declared_mutation_succeeds"
    UNDECLARED_MUTATION_DENIED = "undeclared_mutation_denied"
    LEGITIMATE_AMENDMENT_ATOMIC = "legitimate_amendment_admitted_atomically"
    REJECTED_AMENDMENT_PRESERVES_AUTHORITY = (
        "rejected_amendment_preserves_old_authority"
    )
    STALE_INTENT_VERSION_DENIED = "stale_intent_version_denied"
    EXPIRED_LEASE_DENIED = "expired_lease_denied"
    DUPLICATE_EVENT_IDEMPOTENT = "duplicate_event_idempotent"
    OUT_OF_ORDER_EVENT_FAILS_CLOSED = "out_of_order_event_fails_closed"
    CRASH_RESUME_SAFE = "adapter_crash_resumes_safely"
    CANCELLATION_REVOKES_AUTHORITY = "cancellation_revokes_authority"
    COMPLETION_DETECTS_UNCOVERED_MUTATION = "completion_detects_uncovered_mutation"
    CORRUPT_STATE_CANNOT_VERIFY = "corrupt_state_cannot_produce_verified"
    SECRET_VALUES_ABSENT = "secret_values_absent_from_evidence"


CANONICAL_CONFORMANCE_SCENARIOS = tuple(ConformanceScenario)


class ConformanceStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ConformanceObservation:
    """Adapter-specific evidence returned by one scenario implementation."""

    detail: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise ValueError("conformance observation detail must not be empty")
        object.__setattr__(self, "detail", self.detail.strip())
        object.__setattr__(self, "evidence", dict(self.evidence))


@dataclass(frozen=True, slots=True)
class ConformanceScenarioResult:
    scenario: ConformanceScenario
    status: ConformanceStatus
    detail: str
    duration_ms: int
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario", ConformanceScenario(self.scenario))
        object.__setattr__(self, "status", ConformanceStatus(self.status))
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")
        object.__setattr__(self, "evidence", dict(self.evidence))

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario.value,
            "status": self.status.value,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class GuaranteeConformance:
    guarantee: str
    declared_level: EnforcementLevel
    scenarios: tuple[ConformanceScenario, ...]
    verified: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "guarantee": self.guarantee,
            "declared_level": self.declared_level.value,
            "scenarios": [item.value for item in self.scenarios],
            "verified": self.verified,
            "detail": self.detail,
        }


_DEFAULT_GUARANTEE_SCENARIOS: dict[str, tuple[ConformanceScenario, ...]] = {
    "undeclared_tool_write": (ConformanceScenario.UNDECLARED_MUTATION_DENIED,),
    "bypassed_host_write": (ConformanceScenario.COMPLETION_DETECTS_UNCOVERED_MUTATION,),
    "subagent_mutation": (ConformanceScenario.COMPLETION_DETECTS_UNCOVERED_MUTATION,),
    "completion_verification": (
        ConformanceScenario.COMPLETION_DETECTS_UNCOVERED_MUTATION,
        ConformanceScenario.CORRUPT_STATE_CANNOT_VERIFY,
    ),
    "corrupted_session_state": (
        ConformanceScenario.OUT_OF_ORDER_EVENT_FAILS_CLOSED,
        ConformanceScenario.CORRUPT_STATE_CANNOT_VERIFY,
    ),
    "stale_intent_version": (ConformanceScenario.STALE_INTENT_VERSION_DENIED,),
    "cancellation_revokes_authority": (
        ConformanceScenario.CANCELLATION_REVOKES_AUTHORITY,
    ),
}


@runtime_checkable
class AdapterConformanceDriver(Protocol):
    """Runtime-specific fixture consumed by the shared conformance runner."""

    name: str

    def manifest(self) -> AdapterCapabilityManifest: ...

    def run(self, scenario: ConformanceScenario) -> ConformanceObservation: ...


@dataclass(frozen=True, slots=True)
class AdapterConformanceReport:
    adapter: str
    adapter_version: str
    runtime_name: str
    runtime_version: str | None
    manifest_digest: str
    results: tuple[ConformanceScenarioResult, ...]
    guarantees: tuple[GuaranteeConformance, ...]
    protocol: str = ADAPTER_CONFORMANCE_PROTOCOL
    conformance_version: str = ADAPTER_CONFORMANCE_VERSION

    @property
    def passed(self) -> bool:
        return all(item.status is ConformanceStatus.PASSED for item in self.results)

    @property
    def claims_verified(self) -> bool:
        return all(item.verified for item in self.guarantees)

    @property
    def compatible(self) -> bool:
        return self.passed and self.claims_verified

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "protocol": self.protocol,
            "conformance_version": self.conformance_version,
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "runtime": {
                "name": self.runtime_name,
                "version": self.runtime_version,
            },
            "manifest_digest": self.manifest_digest,
            "compatible": self.compatible,
            "scenarios": [item.to_dict() for item in self.results],
            "guarantees": [item.to_dict() for item in self.guarantees],
            "summary": {
                "passed": sum(
                    item.status is ConformanceStatus.PASSED for item in self.results
                ),
                "failed": sum(
                    item.status is ConformanceStatus.FAILED for item in self.results
                ),
                "skipped": sum(
                    item.status is ConformanceStatus.SKIPPED for item in self.results
                ),
                "total": len(self.results),
                "claims_verified": self.claims_verified,
            },
        }
        if include_digest:
            payload["digest"] = self.digest()
        return payload

    def digest(self) -> str:
        raw = json.dumps(
            self.to_dict(include_digest=False),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _guarantee_results(
    manifest: AdapterCapabilityManifest,
    by_scenario: Mapping[ConformanceScenario, ConformanceScenarioResult],
    guarantee_scenarios: Mapping[str, tuple[ConformanceScenario, ...]],
) -> tuple[GuaranteeConformance, ...]:
    outcomes: list[GuaranteeConformance] = []
    for name, declaration in sorted(manifest.guarantees.items()):
        if declaration.level is EnforcementLevel.UNAVAILABLE:
            outcomes.append(
                GuaranteeConformance(
                    guarantee=name,
                    declared_level=declaration.level,
                    scenarios=(),
                    verified=True,
                    detail="Unavailable behavior makes no enforcement claim.",
                )
            )
            continue
        scenarios = guarantee_scenarios.get(name, ())
        if not scenarios:
            outcomes.append(
                GuaranteeConformance(
                    guarantee=name,
                    declared_level=declaration.level,
                    scenarios=(),
                    verified=False,
                    detail=(
                        "No conformance scenario is bound to this available guarantee."
                    ),
                )
            )
            continue
        missing = [item for item in scenarios if item not in by_scenario]
        failed = [
            item
            for item in scenarios
            if item in by_scenario
            and by_scenario[item].status is not ConformanceStatus.PASSED
        ]
        verified = not missing and not failed
        if missing:
            detail = "Missing scenarios: " + ", ".join(item.value for item in missing)
        elif failed:
            detail = "Failed scenarios: " + ", ".join(item.value for item in failed)
        else:
            detail = "Declared behavior is covered by passing conformance scenarios."
        outcomes.append(
            GuaranteeConformance(
                guarantee=name,
                declared_level=declaration.level,
                scenarios=scenarios,
                verified=verified,
                detail=detail,
            )
        )
    return tuple(outcomes)


def run_adapter_conformance(
    driver: AdapterConformanceDriver,
    *,
    scenarios: tuple[ConformanceScenario, ...] = CANONICAL_CONFORMANCE_SCENARIOS,
    guarantee_scenarios: Mapping[
        str, tuple[ConformanceScenario, ...]
    ] = _DEFAULT_GUARANTEE_SCENARIOS,
) -> AdapterConformanceReport:
    """Execute the canonical suite and verify every available guarantee claim."""

    manifest = driver.manifest()
    if manifest.adapter != driver.name:
        raise ValueError(
            f"conformance driver {driver.name!r} returned manifest for "
            f"{manifest.adapter!r}"
        )
    results: list[ConformanceScenarioResult] = []
    for scenario in scenarios:
        started = time.monotonic()
        try:
            observation = driver.run(scenario)
        except NotImplementedError as exc:
            status = ConformanceStatus.SKIPPED
            detail = str(exc) or "Scenario is not implemented."
            evidence: Mapping[str, Any] = {}
        except Exception as exc:  # noqa: BLE001 - failures belong in the report
            status = ConformanceStatus.FAILED
            detail = f"{type(exc).__name__}: {exc}"
            evidence = {}
        else:
            status = ConformanceStatus.PASSED
            detail = observation.detail
            evidence = observation.evidence
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        results.append(
            ConformanceScenarioResult(
                scenario=scenario,
                status=status,
                detail=detail,
                duration_ms=duration_ms,
                evidence=evidence,
            )
        )
    by_scenario = {item.scenario: item for item in results}
    claims = _guarantee_results(manifest, by_scenario, guarantee_scenarios)
    return AdapterConformanceReport(
        adapter=manifest.adapter,
        adapter_version=manifest.adapter_version,
        runtime_name=manifest.runtime.name,
        runtime_version=manifest.runtime.version,
        manifest_digest=manifest.digest(),
        results=tuple(results),
        guarantees=claims,
    )
