"""Explicit storage contracts for Claim Plane control-plane backends.

The public protocols form a complete structural boundary. SQLite remains the
permanent single-host Community backend; a network backend must
implement the same claim, intent, observation, broker, and verification
transactions before it can be injected into :class:`claim_plane.core.Plane`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable

from claim_plane.core.models import (
    AccessMode,
    AdmissionDecision,
    ChangeIntent,
    Claim,
    ClaimType,
    ObservedAccess,
    Verdict,
)
from claim_plane.core.registry import ClaimRegistry

AdmissionEvaluator = Callable[
    [ChangeIntent, list[ChangeIntent], set[str]], AdmissionDecision
]


class ClaimStore(Protocol):
    def arbitrate_claim(
        self, claim: Claim, *, canonical_key: str | None = None
    ) -> Verdict: ...

    def incumbent(self, claim_type: ClaimType, key: str) -> Any | None: ...

    def all_grants(self) -> list[dict[str, Any]]: ...

    def decision_log(self) -> list[dict[str, Any]]: ...

    def release(self, owner: str) -> int: ...

    def renew_claims(self, owner: str, lease_seconds: int = 900) -> int: ...


class IntentStore(Protocol):
    def admit_intent(
        self, intent: ChangeIntent, evaluator: AdmissionEvaluator
    ) -> AdmissionDecision: ...

    def amend_intent(
        self,
        intent: ChangeIntent,
        evaluator: AdmissionEvaluator,
        *,
        expected_version: int | None = None,
    ) -> AdmissionDecision: ...

    def promote_contingent_operations(
        self,
        intent_id: str,
        *,
        path: str,
        modes: Iterable[AccessMode],
        evaluator: AdmissionEvaluator,
        expected_version: int | None = None,
        broker_instance_id: str | None = None,
        broker_key: bytes | None = None,
    ) -> AdmissionDecision: ...

    def invalidate_dependents(
        self, producer_intent_id: str, resource_keys: list[str], *, reason: str
    ) -> list[str]: ...

    def dependency_graph(self) -> dict[str, object]: ...

    def notices(
        self, intent_id: str, *, pending_only: bool = True
    ) -> list[dict[str, Any]]: ...

    def acknowledge_notice(self, notice_id: int) -> None: ...

    def activate_intent(self, intent_id: str) -> None: ...

    def complete_intent(self, intent_id: str) -> None: ...

    def release_intent(self, intent_id: str) -> None: ...

    def heartbeat_intent(self, intent_id: str, lease_seconds: int = 900) -> None: ...

    def get_intent(self, intent_id: str) -> ChangeIntent | None: ...

    def get_intent_record(self, intent_id: str) -> dict[str, Any] | None: ...

    def list_intents(self, *, active_only: bool = False) -> list[dict[str, Any]]: ...

    def active_intents(self) -> list[ChangeIntent]: ...

    def coordination_events(self) -> list[dict[str, Any]]: ...


class ObservationStore(Protocol):
    def start_observation_session(
        self,
        session_id: str,
        intent_id: str,
        *,
        monitor_id: str,
        key_id: str = "default",
        coverage: str = "tool_proxy",
        required_tools: Iterable[str] = (),
    ) -> dict[str, Any]: ...

    def record_observation_event(
        self, session_id: str, access: ObservedAccess, *, key: bytes
    ) -> dict[str, Any]: ...

    def seal_observation_session(
        self, session_id: str, *, key: bytes, complete: bool = True
    ) -> dict[str, Any]: ...

    def observation_session(self, session_id: str) -> dict[str, Any]: ...

    def verify_observation_session(
        self, session_id: str, *, key: bytes
    ) -> dict[str, Any]: ...


class BrokerStore(Protocol):
    def register_broker_instance(
        self,
        *,
        instance_id: str,
        intent_id: str,
        session_id: str,
        monitor_id: str,
        key_id: str,
        root_path: str,
        repo_identity: str,
        base_commit: str,
        initial_tree_hash: str,
        writer_lease_seconds: int,
        policy: Mapping[str, object],
        binary_digest: str,
        broker_key: bytes,
        required_tools: Iterable[str] = (),
    ) -> dict[str, Any]: ...

    def broker_instance(self, instance_id: str) -> dict[str, Any]: ...

    def validate_broker_instance(
        self,
        instance_id: str,
        *,
        broker_key: bytes,
        current_tree_hash: str | None = None,
    ) -> dict[str, Any]: ...

    def stop_broker_instance(
        self, instance_id: str, *, broker_key: bytes
    ) -> dict[str, Any]: ...

    def verify_broker_instance(
        self, instance_id: str, *, broker_key: bytes
    ) -> dict[str, Any]: ...

    def broker_operation_for_request(
        self, instance_id: str, request_id: str
    ) -> dict[str, Any] | None: ...

    def prepare_broker_operation(
        self,
        *,
        operation_id: str,
        instance_id: str,
        request_id: str,
        operation: str,
        mode: AccessMode,
        path: str,
        target_path: str | None,
        payload: Mapping[str, object],
        broker_key: bytes,
        fencing_token: int,
        pre_tree_hash: str | None = None,
    ) -> dict[str, Any]: ...

    def commit_broker_operation(
        self,
        operation_id: str,
        *,
        accesses: Iterable[ObservedAccess],
        response: Mapping[str, object],
        observation_key: bytes,
        broker_key: bytes,
        fencing_token: int,
        post_tree_hash: str | None = None,
    ) -> dict[str, Any]: ...

    def fail_broker_operation(
        self,
        operation_id: str,
        *,
        state: str,
        error: str,
        broker_key: bytes,
    ) -> dict[str, Any]: ...

    def pending_broker_operations(self, instance_id: str) -> list[dict[str, Any]]: ...

    def verify_broker_session(
        self, session_id: str, *, broker_key: bytes
    ) -> dict[str, Any]: ...


class VerificationStore(Protocol):
    def record_verification(
        self, intent_id: str, report: Mapping[str, object]
    ) -> None: ...

    def export_audit(self, path: str | Path) -> None: ...


@runtime_checkable
class PlaneStore(
    ClaimStore,
    IntentStore,
    ObservationStore,
    BrokerStore,
    VerificationStore,
    Protocol,
):
    """Complete state-store contract consumed by the public Plane facade."""

    backend_name: str
    single_host: bool

    def close(self) -> None: ...


PLANE_STORE_METHODS = (
    "acknowledge_notice",
    "activate_intent",
    "active_intents",
    "admit_intent",
    "all_grants",
    "amend_intent",
    "arbitrate_claim",
    "broker_instance",
    "broker_operation_for_request",
    "close",
    "commit_broker_operation",
    "complete_intent",
    "coordination_events",
    "decision_log",
    "dependency_graph",
    "export_audit",
    "fail_broker_operation",
    "get_intent",
    "get_intent_record",
    "heartbeat_intent",
    "incumbent",
    "invalidate_dependents",
    "list_intents",
    "notices",
    "observation_session",
    "pending_broker_operations",
    "prepare_broker_operation",
    "promote_contingent_operations",
    "record_observation_event",
    "record_verification",
    "register_broker_instance",
    "release",
    "release_intent",
    "renew_claims",
    "seal_observation_session",
    "start_observation_session",
    "stop_broker_instance",
    "validate_broker_instance",
    "verify_broker_instance",
    "verify_broker_session",
    "verify_observation_session",
)


def validate_plane_store(store: object) -> PlaneStore:
    """Fail early when a proposed backend does not implement the full contract."""

    missing = [
        name for name in PLANE_STORE_METHODS if not callable(getattr(store, name, None))
    ]
    for attribute in ("backend_name", "single_host"):
        if not hasattr(store, attribute):
            missing.append(attribute)
    if missing:
        raise TypeError(
            "incomplete PlaneStore backend; missing: " + ", ".join(sorted(missing))
        )
    return store  # type: ignore[return-value]


class SQLitePlaneStore(ClaimRegistry):
    """Authoritative single-host store used by Community/local deployments."""

    backend_name = "sqlite"
    single_host = True

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        super().__init__(db_path)
