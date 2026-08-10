"""Deterministic concurrency conformance suite.

The suite exercises the deterministic concurrency stack without launching agents or
executing repository code.  Canonical fixtures cover coarse path admission, semantic
same-file admission, cross-file dependency ordering, explicit commutativity,
fail-closed unknowns, bounded amendments, and stale-runtime recovery.

The report is intended to be stable experimental evidence.  Scenario identities and
metric definitions are versioned so later benchmark work can distinguish a control-
plane regression from a change in the workload itself.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Mapping

from claim_plane.core import (
    AccessMode,
    ChangeIntent,
    CommutativityProof,
    IntentOperation,
    Plane,
    ResourceKind,
    ResourceRef,
    ScopeCommitment,
    build_python_dependency_graph,
)
from claim_plane.swarm.budget import SwarmBudgetPolicy
from claim_plane.swarm.concurrency import (
    ConcurrencyConstraintReason,
    ConcurrencyPlan,
    compute_concurrency_plan,
)
from claim_plane.swarm.models import WorkGraph

DETERMINISTIC_CONCURRENCY_CONFORMANCE_PROTOCOL = (
    "claim-plane.deterministic-concurrency-conformance.v1"
)
DETERMINISTIC_CONCURRENCY_CONFORMANCE_VERSION = "1.0"

_BASE = "a" * 40
_REFRESHED_BASE = "b" * 40


class ConformanceScenario(str, Enum):
    DIFFERENT_FILES_INDEPENDENT = "different_files_independent"
    SAME_FILE_DIFFERENT_FUNCTIONS = "same_file_different_functions"
    SAME_CLASS_DIFFERENT_METHODS = "same_class_different_methods"
    EXPLICIT_COMMUTATIVITY = "explicit_commutativity"
    SHARED_RETURN_TYPE_ORDERED = "shared_return_type_ordered"
    PRODUCER_CONSUMER_ORDERED = "producer_consumer_ordered"
    PUBLIC_API_CHANGE_ORDERED = "public_api_change_ordered"
    SCHEMA_CHANGE_SERIALIZED = "schema_change_serialized"
    CONFLICTING_RENAME_SERIALIZED = "conflicting_rename_serialized"
    HIDDEN_SEMANTIC_DEPENDENCY_FAILS_CLOSED = "hidden_semantic_dependency_fails_closed"
    MISSING_SEMANTIC_ROOT_FAILS_CLOSED = "missing_semantic_root_fails_closed"
    LATE_SCOPE_EXPANSION_RECOVERED = "late_scope_expansion_recovered"
    ORDERED_SCOPE_EXPANSION_BLOCKED = "ordered_scope_expansion_blocked"
    STALE_WORKER_REFRESH_RESUME = "stale_worker_refresh_resume"


CANONICAL_CONCURRENCY_SCENARIOS = tuple(ConformanceScenario)


class ConformanceExpectation(str, Enum):
    PARALLEL = "parallel"
    ORDERED = "ordered"
    SERIALIZED = "serialized"
    FAIL_CLOSED = "fail_closed"
    AMENDMENT_ADMITTED = "amendment_admitted"
    AMENDMENT_BLOCKED = "amendment_blocked"
    RECOVERED = "recovered"


class ConformanceStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DeterministicConcurrencyScenarioResult:
    scenario: ConformanceScenario
    expectation: ConformanceExpectation
    observed: ConformanceExpectation | None
    status: ConformanceStatus
    detail: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario", ConformanceScenario(self.scenario))
        object.__setattr__(
            self, "expectation", ConformanceExpectation(self.expectation)
        )
        if self.observed is not None:
            object.__setattr__(self, "observed", ConformanceExpectation(self.observed))
        object.__setattr__(self, "status", ConformanceStatus(self.status))
        object.__setattr__(self, "evidence", dict(self.evidence))

    @property
    def passed(self) -> bool:
        return self.status is ConformanceStatus.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario.value,
            "expectation": self.expectation.value,
            "observed": self.observed.value if self.observed is not None else None,
            "status": self.status.value,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class DeterministicConcurrencyMetrics:
    safe_parallel_recall: float
    false_parallel_rate: float
    unnecessary_serialization_rate: float
    ordered_dependency_accuracy: float
    amendment_recovery_rate: float
    counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "safe_parallel_recall",
            "false_parallel_rate",
            "unnecessary_serialization_rate",
            "ordered_dependency_accuracy",
            "amendment_recovery_rate",
        ):
            value = float(getattr(self, name))
            if value < 0.0 or value > 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "counts", dict(self.counts))

    def to_dict(self) -> dict[str, Any]:
        return {
            "safe_parallel_recall": self.safe_parallel_recall,
            "false_parallel_rate": self.false_parallel_rate,
            "unnecessary_serialization_rate": self.unnecessary_serialization_rate,
            "ordered_dependency_accuracy": self.ordered_dependency_accuracy,
            "amendment_recovery_rate": self.amendment_recovery_rate,
            "counts": dict(self.counts),
        }


@dataclass(frozen=True, slots=True)
class DeterministicConcurrencyConformanceReport:
    results: tuple[DeterministicConcurrencyScenarioResult, ...]
    metrics: DeterministicConcurrencyMetrics
    protocol: str = DETERMINISTIC_CONCURRENCY_CONFORMANCE_PROTOCOL
    conformance_version: str = DETERMINISTIC_CONCURRENCY_CONFORMANCE_VERSION

    def __post_init__(self) -> None:
        if self.protocol != DETERMINISTIC_CONCURRENCY_CONFORMANCE_PROTOCOL:
            raise ValueError(
                f"unsupported concurrency conformance protocol {self.protocol!r}"
            )
        if self.conformance_version != DETERMINISTIC_CONCURRENCY_CONFORMANCE_VERSION:
            raise ValueError(
                f"unsupported concurrency conformance version {self.conformance_version!r}"
            )
        object.__setattr__(self, "results", tuple(self.results))

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.results)

    @property
    def fingerprint(self) -> str:
        raw = json.dumps(
            self.to_dict(include_fingerprint=False),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload = {
            "protocol": self.protocol,
            "conformance_version": self.conformance_version,
            "passed": self.passed,
            "summary": {
                "passed": sum(item.passed for item in self.results),
                "failed": sum(not item.passed for item in self.results),
                "total": len(self.results),
            },
            "metrics": self.metrics.to_dict(),
            "scenarios": [item.to_dict() for item in self.results],
        }
        if include_fingerprint:
            return {"fingerprint": self.fingerprint, **payload}
        return payload


@dataclass(frozen=True, slots=True)
class _Case:
    scenario: ConformanceScenario
    expectation: ConformanceExpectation
    runner: Callable[[], tuple[ConformanceExpectation, Mapping[str, Any], str]]
    safe_parallel: bool = False
    unsafe_parallel: bool = False
    ordered: bool = False
    amendment_recovery: bool = False


def _policy(
    *,
    same_file: str = "region_safe",
    unknown_overlap: str = "serialize",
    schema_change: str = "serialize",
) -> SwarmBudgetPolicy:
    return SwarmBudgetPolicy.from_dict(
        {
            "workers": {
                "max_active": 2,
                "max_active_per_work_item": 1,
                "max_work_items": 8,
                "max_total_launches": 16,
            },
            "concurrency": {
                "same_file": same_file,
                "unknown_overlap": unknown_overlap,
                "shared_contract": "serialize",
                "schema_change": schema_change,
            },
        }
    )


def _symbol_operation(
    path: str,
    qualified: str,
    *,
    change_kind: str = "implementation",
    access: str = "write",
    state: bool = False,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "path": path,
        "language": "python",
        "qualified_identifier": qualified,
    }
    if state:
        metadata["state"] = True
    return {
        "access": access,
        "resource": {
            "kind": "symbol",
            "identifier": qualified,
            "metadata": metadata,
        },
        "metadata": {"semantic_change_kind": change_kind},
    }


def _work_item(
    work_id: str,
    path: str,
    qualified: str | None,
    *,
    change_kind: str = "implementation",
    schema_change: bool = False,
    access: str = "write",
    state: bool = False,
) -> dict[str, object]:
    operations: list[dict[str, object]] = [
        {"access": "write", "resource": {"kind": "file", "identifier": path}}
    ]
    if qualified is not None:
        operations.append(
            _symbol_operation(
                path,
                qualified,
                change_kind=change_kind,
                access=access,
                state=state,
            )
        )
    return {
        "work_id": work_id,
        "title": work_id,
        "goal": f"Conformance scenario {work_id}",
        "operations": operations,
        "metadata": {"schema_change": schema_change} if schema_change else {},
    }


def _work_graph(*items: dict[str, object]) -> WorkGraph:
    return WorkGraph.from_dict(
        {"protocol": "claim-plane.swarm-work-graph.v1", "work_items": list(items)}
    )


def _plan_observation(plan: ConcurrencyPlan) -> ConformanceExpectation:
    if plan.status.value == "replan_required":
        return ConformanceExpectation.FAIL_CLOSED
    if len(plan.waves) == 1 and len(plan.waves[0].work_ids) == 2:
        return ConformanceExpectation.PARALLEL
    if any(
        ConcurrencyConstraintReason.SEMANTIC_ORDER in item.reasons
        for item in plan.constraints
    ):
        return ConformanceExpectation.ORDERED
    return ConformanceExpectation.SERIALIZED


def _plan_case(
    sources: Mapping[str, str] | None,
    left: dict[str, object],
    right: dict[str, object],
    *,
    policy: SwarmBudgetPolicy | None = None,
    proofs: tuple[CommutativityProof, ...] = (),
) -> tuple[ConformanceExpectation, Mapping[str, Any], str]:
    semantic_graph = (
        build_python_dependency_graph(sources) if sources is not None else None
    )
    plan = compute_concurrency_plan(
        _work_graph(left, right),
        policy or _policy(),
        semantic_graph=semantic_graph,
        commutativity_proofs=proofs,
    )
    observed = _plan_observation(plan)
    evidence = {
        "waves": [list(item.work_ids) for item in plan.waves],
        "constraints": [item.to_dict() for item in plan.constraints],
        "same_file_admissions": list(plan.metadata.get("same_file_admissions") or ()),
        "semantic_graph_fingerprint": plan.metadata.get("semantic_graph_fingerprint"),
    }
    return observed, evidence, f"planner observed {observed.value}"


def _different_files_independent() -> tuple[
    ConformanceExpectation, Mapping[str, Any], str
]:
    sources = {
        "left.py": "def left():\n    return 1\n",
        "right.py": "def right():\n    return 2\n",
    }
    return _plan_case(
        sources,
        _work_item("left", "left.py", "left"),
        _work_item("right", "right.py", "right"),
    )


def _same_file_different_functions() -> tuple[
    ConformanceExpectation, Mapping[str, Any], str
]:
    sources = {"app.py": "def first():\n    return 1\n\ndef second():\n    return 2\n"}
    return _plan_case(
        sources,
        _work_item("first", "app.py", "first"),
        _work_item("second", "app.py", "second"),
    )


def _same_class_different_methods() -> tuple[
    ConformanceExpectation, Mapping[str, Any], str
]:
    sources = {
        "parser.py": (
            "class Parser:\n"
            "    def parse(self, value: str) -> str:\n"
            "        return value.strip()\n\n"
            "    def validate(self, value: str) -> bool:\n"
            "        return bool(value)\n"
        )
    }
    return _plan_case(
        sources,
        _work_item("parse", "parser.py", "Parser.parse"),
        _work_item("validate", "parser.py", "Parser.validate"),
    )


def _explicit_commutativity() -> tuple[ConformanceExpectation, Mapping[str, Any], str]:
    sources = {"state.py": "STATE = set()\n"}
    identity = "symbol:state.py#STATE"
    proof = CommutativityProof(
        left_identity=identity,
        right_identity=identity,
        basis="conformance-distinct-set-additions",
        metadata={"rule": "canonical_fixture"},
    )
    return _plan_case(
        sources,
        _work_item("left", "state.py", "STATE", change_kind="state", state=True),
        _work_item("right", "state.py", "STATE", change_kind="state", state=True),
        proofs=(proof,),
    )


def _shared_return_type_ordered() -> tuple[
    ConformanceExpectation, Mapping[str, Any], str
]:
    sources = {
        "data.py": (
            "from dataclasses import dataclass\n\n"
            "@dataclass\n"
            "class Result:\n"
            "    value: str\n"
        ),
        "service.py": (
            "from data import Result\n\n"
            "def make(value: str) -> Result:\n"
            "    return Result(value)\n"
        ),
        "consumer.py": (
            "from service import make\n\n"
            "def render(value: str) -> str:\n"
            "    return make(value).value\n"
        ),
    }
    return _plan_case(
        sources,
        _work_item("result-contract", "data.py", "Result", change_kind="contract"),
        _work_item("render", "consumer.py", "render"),
    )


def _producer_consumer_ordered() -> tuple[
    ConformanceExpectation, Mapping[str, Any], str
]:
    sources = {
        "producer.py": "def parse(value: str) -> str:\n    return value\n",
        "consumer.py": (
            "from producer import parse\n\n"
            "def consume(value: str) -> str:\n"
            "    return parse(value)\n"
        ),
    }
    return _plan_case(
        sources,
        _work_item("producer", "producer.py", "parse", change_kind="contract"),
        _work_item("consumer", "consumer.py", "consume"),
    )


def _public_api_change_ordered() -> tuple[
    ConformanceExpectation, Mapping[str, Any], str
]:
    sources = {
        "api.py": "def normalize(value: str) -> str:\n    return value.strip()\n",
        "feature.py": (
            "from api import normalize\n\n"
            "def render(value: str) -> str:\n"
            "    return normalize(value)\n"
        ),
    }
    return _plan_case(
        sources,
        _work_item("api", "api.py", "normalize", change_kind="contract"),
        _work_item("feature", "feature.py", "render"),
    )


def _schema_change_serialized() -> tuple[
    ConformanceExpectation, Mapping[str, Any], str
]:
    sources = {
        "schema.py": "class Record:\n    pass\n",
        "worker.py": "def work():\n    return 1\n",
    }
    return _plan_case(
        sources,
        _work_item(
            "schema", "schema.py", "Record", change_kind="contract", schema_change=True
        ),
        _work_item("worker", "worker.py", "work"),
    )


def _conflicting_rename_serialized() -> tuple[
    ConformanceExpectation, Mapping[str, Any], str
]:
    sources = {"app.py": "def value():\n    return 1\n"}
    return _plan_case(
        sources,
        _work_item("left", "app.py", "value", change_kind="structure", access="rename"),
        _work_item("right", "app.py", "value", change_kind="implementation"),
    )


def _hidden_semantic_dependency_fails_closed() -> tuple[
    ConformanceExpectation, Mapping[str, Any], str
]:
    sources = {
        "left.py": "def left(value):\n    return missing(value)\n",
        "right.py": "def right(value):\n    return missing(value + 1)\n",
    }
    return _plan_case(
        sources,
        _work_item("left", "left.py", "left"),
        _work_item("right", "right.py", "right"),
    )


def _missing_semantic_root_fails_closed() -> tuple[
    ConformanceExpectation, Mapping[str, Any], str
]:
    sources = {"app.py": "def first():\n    return 1\n\ndef second():\n    return 2\n"}
    return _plan_case(
        sources,
        _work_item("first", "app.py", None),
        _work_item("second", "app.py", None),
    )


def _intent_symbol(
    name: str, *, commitment: ScopeCommitment = ScopeCommitment.COMMITTED
) -> IntentOperation:
    return IntentOperation(
        AccessMode.WRITE,
        ResourceRef(
            ResourceKind.SYMBOL,
            name,
            metadata={"path": "app.py", "qualified_identifier": name},
        ),
        commitment=commitment,
    )


def _intent(
    intent_id: str,
    *operations: IntentOperation,
    base_commit: str = _BASE,
) -> ChangeIntent:
    return ChangeIntent(
        intent_id=intent_id,
        task_id=intent_id,
        owner=f"agent-{intent_id}",
        base_revision=base_commit,
        base_commit=base_commit,
        operations=tuple(operations),
    )


def _late_scope_expansion_recovered() -> tuple[
    ConformanceExpectation, Mapping[str, Any], str
]:
    semantic_graph = build_python_dependency_graph(
        {
            "app.py": (
                "def produce():\n    return 1\n\n"
                "def consume():\n    return produce()\n\n"
                "def side():\n    return 2\n"
            )
        }
    )
    current = _intent("left", _intent_symbol("produce"))
    candidate = replace(
        current, operations=(*current.operations, _intent_symbol("side"))
    )
    plane = Plane.open(":memory:")
    try:
        admitted = plane.admit(current)
        if not admitted.allowed:
            raise RuntimeError("canonical current intent was not admitted")
        plane.activate(current.intent_id)
        execution = plane.amend_bounded(candidate, semantic_graph, expected_version=2)
        observed = (
            ConformanceExpectation.AMENDMENT_ADMITTED
            if execution.allowed
            else ConformanceExpectation.AMENDMENT_BLOCKED
        )
        stored_intent = plane.intent("left")
        evidence = {
            "assessment": execution.assessment.to_dict(),
            "admission": execution.admission.to_dict(),
            "operation_count": (
                len(stored_intent.operations) if stored_intent is not None else 0
            ),
        }
        return (
            observed,
            evidence,
            "bounded disjoint scope expansion was re-admitted atomically",
        )
    finally:
        plane.close()


def _ordered_scope_expansion_blocked() -> tuple[
    ConformanceExpectation, Mapping[str, Any], str
]:
    semantic_graph = build_python_dependency_graph(
        {
            "app.py": (
                "def produce():\n    return 1\n\n"
                "def consume():\n    return produce()\n\n"
                "def side():\n    return 2\n"
            )
        }
    )
    current = _intent("left", _intent_symbol("side"))
    candidate = replace(
        current, operations=(*current.operations, _intent_symbol("produce"))
    )
    active = _intent("right", _intent_symbol("consume"))
    plane = Plane.open(":memory:")
    try:
        if not plane.admit(current).allowed:
            raise RuntimeError("canonical current intent was not admitted")
        plane.activate(current.intent_id)
        if not plane.admit(active).allowed:
            raise RuntimeError("canonical active consumer was not admitted")
        plane.activate(active.intent_id)
        execution = plane.amend_bounded(candidate, semantic_graph, expected_version=2)
        observed = (
            ConformanceExpectation.AMENDMENT_BLOCKED
            if not execution.allowed and execution.assessment.requires_ordering
            else ConformanceExpectation.AMENDMENT_ADMITTED
        )
        stored = plane.intent("left")
        evidence = {
            "assessment": execution.assessment.to_dict(),
            "authority_preserved": bool(stored and len(stored.operations) == 1),
        }
        return (
            observed,
            evidence,
            "ordered expansion remained blocked without fresh execution premise",
        )
    finally:
        plane.close()


def _stale_worker_refresh_resume() -> tuple[
    ConformanceExpectation, Mapping[str, Any], str
]:
    original = _intent("worker", _intent_symbol("side"))
    refreshed = _intent("worker", _intent_symbol("side"), base_commit=_REFRESHED_BASE)
    plane = Plane.open(":memory:")
    try:
        if not plane.admit(original).allowed:
            raise RuntimeError("canonical worker was not admitted")
        plane.activate("worker")
        fences = plane.pause_runtime(
            "worker",
            reason="conformance_ordered_dependency",
            resource_keys=("symbol:app.py#side",),
        )
        decision, recovery = plane.refresh_runtime(refreshed)
        if not decision.allowed or recovery is None:
            observed = ConformanceExpectation.FAIL_CLOSED
            evidence = {"fences": fences, "decision": decision.to_dict()}
            return observed, evidence, "stale worker refresh was not admitted"
        resumed = plane.resume_runtime("worker")
        observed = ConformanceExpectation.RECOVERED
        evidence = {
            "fences": fences,
            "refresh": recovery.to_dict(),
            "resume": resumed.to_dict(),
            "state": next(
                item["state"]
                for item in plane.intents()
                if item["intent_id"] == "worker"
            ),
        }
        return (
            observed,
            evidence,
            "stale worker refreshed on a new base and resumed explicitly",
        )
    finally:
        plane.close()


def _canonical_cases() -> tuple[_Case, ...]:
    return (
        _Case(
            ConformanceScenario.DIFFERENT_FILES_INDEPENDENT,
            ConformanceExpectation.PARALLEL,
            _different_files_independent,
            safe_parallel=True,
        ),
        _Case(
            ConformanceScenario.SAME_FILE_DIFFERENT_FUNCTIONS,
            ConformanceExpectation.PARALLEL,
            _same_file_different_functions,
            safe_parallel=True,
        ),
        _Case(
            ConformanceScenario.SAME_CLASS_DIFFERENT_METHODS,
            ConformanceExpectation.PARALLEL,
            _same_class_different_methods,
            safe_parallel=True,
        ),
        _Case(
            ConformanceScenario.EXPLICIT_COMMUTATIVITY,
            ConformanceExpectation.PARALLEL,
            _explicit_commutativity,
            safe_parallel=True,
        ),
        _Case(
            ConformanceScenario.SHARED_RETURN_TYPE_ORDERED,
            ConformanceExpectation.ORDERED,
            _shared_return_type_ordered,
            unsafe_parallel=True,
            ordered=True,
        ),
        _Case(
            ConformanceScenario.PRODUCER_CONSUMER_ORDERED,
            ConformanceExpectation.ORDERED,
            _producer_consumer_ordered,
            unsafe_parallel=True,
            ordered=True,
        ),
        _Case(
            ConformanceScenario.PUBLIC_API_CHANGE_ORDERED,
            ConformanceExpectation.ORDERED,
            _public_api_change_ordered,
            unsafe_parallel=True,
            ordered=True,
        ),
        _Case(
            ConformanceScenario.SCHEMA_CHANGE_SERIALIZED,
            ConformanceExpectation.SERIALIZED,
            _schema_change_serialized,
            unsafe_parallel=True,
        ),
        _Case(
            ConformanceScenario.CONFLICTING_RENAME_SERIALIZED,
            ConformanceExpectation.SERIALIZED,
            _conflicting_rename_serialized,
            unsafe_parallel=True,
        ),
        _Case(
            ConformanceScenario.HIDDEN_SEMANTIC_DEPENDENCY_FAILS_CLOSED,
            ConformanceExpectation.SERIALIZED,
            _hidden_semantic_dependency_fails_closed,
            unsafe_parallel=True,
        ),
        _Case(
            ConformanceScenario.MISSING_SEMANTIC_ROOT_FAILS_CLOSED,
            ConformanceExpectation.SERIALIZED,
            _missing_semantic_root_fails_closed,
            unsafe_parallel=True,
        ),
        _Case(
            ConformanceScenario.LATE_SCOPE_EXPANSION_RECOVERED,
            ConformanceExpectation.AMENDMENT_ADMITTED,
            _late_scope_expansion_recovered,
            amendment_recovery=True,
        ),
        _Case(
            ConformanceScenario.ORDERED_SCOPE_EXPANSION_BLOCKED,
            ConformanceExpectation.AMENDMENT_BLOCKED,
            _ordered_scope_expansion_blocked,
            amendment_recovery=True,
        ),
        _Case(
            ConformanceScenario.STALE_WORKER_REFRESH_RESUME,
            ConformanceExpectation.RECOVERED,
            _stale_worker_refresh_resume,
            amendment_recovery=True,
        ),
    )


def _ratio(numerator: int, denominator: int) -> float:
    return 1.0 if denominator == 0 else numerator / denominator


def _metrics(
    cases: tuple[_Case, ...],
    results: tuple[DeterministicConcurrencyScenarioResult, ...],
) -> DeterministicConcurrencyMetrics:
    by_scenario = {item.scenario: item for item in results}
    safe = [case for case in cases if case.safe_parallel]
    unsafe = [case for case in cases if case.unsafe_parallel]
    ordered = [case for case in cases if case.ordered]
    recovery = [case for case in cases if case.amendment_recovery]

    safe_parallel = sum(
        by_scenario[case.scenario].observed is ConformanceExpectation.PARALLEL
        for case in safe
    )
    false_parallel = sum(
        by_scenario[case.scenario].observed is ConformanceExpectation.PARALLEL
        for case in unsafe
    )
    ordered_correct = sum(
        by_scenario[case.scenario].observed is ConformanceExpectation.ORDERED
        for case in ordered
    )
    recovery_correct = sum(by_scenario[case.scenario].passed for case in recovery)
    return DeterministicConcurrencyMetrics(
        safe_parallel_recall=_ratio(safe_parallel, len(safe)),
        false_parallel_rate=_ratio(false_parallel, len(unsafe)) if unsafe else 0.0,
        unnecessary_serialization_rate=_ratio(len(safe) - safe_parallel, len(safe))
        if safe
        else 0.0,
        ordered_dependency_accuracy=_ratio(ordered_correct, len(ordered)),
        amendment_recovery_rate=_ratio(recovery_correct, len(recovery)),
        counts={
            "safe_parallel_cases": len(safe),
            "unsafe_parallel_cases": len(unsafe),
            "ordered_cases": len(ordered),
            "amendment_recovery_cases": len(recovery),
        },
    )


def run_deterministic_concurrency_conformance(
    *, scenarios: tuple[ConformanceScenario, ...] = CANONICAL_CONCURRENCY_SCENARIOS
) -> DeterministicConcurrencyConformanceReport:
    """Run the canonical deterministic concurrency suite in memory."""

    selected = {ConformanceScenario(item) for item in scenarios}
    cases = tuple(case for case in _canonical_cases() if case.scenario in selected)
    if not cases:
        raise ValueError(
            "deterministic concurrency conformance requires at least one scenario"
        )
    results: list[DeterministicConcurrencyScenarioResult] = []
    for case in cases:
        try:
            observed, evidence, detail = case.runner()
        except Exception as exc:  # noqa: BLE001 - scenario failures belong in report evidence
            results.append(
                DeterministicConcurrencyScenarioResult(
                    scenario=case.scenario,
                    expectation=case.expectation,
                    observed=None,
                    status=ConformanceStatus.FAILED,
                    detail=f"{type(exc).__name__}: {exc}",
                    evidence={},
                )
            )
            continue
        passed = observed is case.expectation
        results.append(
            DeterministicConcurrencyScenarioResult(
                scenario=case.scenario,
                expectation=case.expectation,
                observed=observed,
                status=ConformanceStatus.PASSED if passed else ConformanceStatus.FAILED,
                detail=detail
                if passed
                else f"expected {case.expectation.value}; {detail}",
                evidence=evidence,
            )
        )
    frozen = tuple(results)
    return DeterministicConcurrencyConformanceReport(
        results=frozen,
        metrics=_metrics(cases, frozen),
    )


__all__ = [
    "CANONICAL_CONCURRENCY_SCENARIOS",
    "DETERMINISTIC_CONCURRENCY_CONFORMANCE_PROTOCOL",
    "DETERMINISTIC_CONCURRENCY_CONFORMANCE_VERSION",
    "ConformanceExpectation",
    "ConformanceScenario",
    "ConformanceStatus",
    "DeterministicConcurrencyConformanceReport",
    "DeterministicConcurrencyMetrics",
    "DeterministicConcurrencyScenarioResult",
    "run_deterministic_concurrency_conformance",
]
