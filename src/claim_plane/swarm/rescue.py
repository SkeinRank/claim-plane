"""Bounded, fail-closed recovery for deterministic swarm integration.

This layer never guesses a merge resolution. It can retry a durable snapshot after
transient integration failure, or prepare a serial re-execution on the current
integration head when the previous parallel result can no longer be integrated
safely. Authority violations and unresolved semantic ambiguity remain blocked for
explicit replanning or human review.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from claim_plane.swarm.integration_v2 import IntegrationReason
from claim_plane.swarm.merge_queue import DeterministicMergeQueue, MergeEntryState
from claim_plane.swarm.service import (
    _registered_worktrees,
    _require_initialized,
    _store,
    _validate_session_id,
    resolve_repository_root,
)

INTEGRATION_RESCUE_PROTOCOL = "claim-plane.integration-rescue.v1"
_RECOVERY_ACTION = "integration_rescue"


class RescueDisposition(str, Enum):
    RETRY_SNAPSHOT = "retry_snapshot"
    SERIAL_RERUN = "serial_rerun"
    REPLAN_REQUIRED = "replan_required"
    MANUAL = "manual"


class RescueReason(str, Enum):
    TRANSIENT_INTEGRATION_ERROR = "transient_integration_error"
    TEXTUAL_INTEGRATION_CONFLICT = "textual_integration_conflict"
    STALE_ORDERED_DEPENDENCY = "stale_ordered_dependency"
    AUTHORITY_VIOLATION = "authority_violation"
    SEMANTIC_AMBIGUITY = "semantic_ambiguity"
    POST_APPLY_MISMATCH = "post_apply_mismatch"
    REPAIR_BUDGET_EXHAUSTED = "repair_budget_exhausted"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


@dataclass(frozen=True, slots=True)
class IntegrationRescueDecision:
    event_id: str
    session_id: str
    work_id: str
    source_run_id: str | None
    queue_fingerprint: str
    integration_head: str
    disposition: RescueDisposition
    reason: RescueReason
    attempt: int
    max_attempts: int
    prepared: bool
    conflict_paths: tuple[str, ...] = ()
    detail: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now)
    protocol: str = INTEGRATION_RESCUE_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != INTEGRATION_RESCUE_PROTOCOL:
            raise ValueError(
                f"unsupported integration rescue protocol {self.protocol!r}"
            )
        if not self.event_id or not self.session_id or not self.work_id:
            raise ValueError("integration rescue identity fields must not be empty")
        if self.attempt <= 0:
            raise ValueError("integration rescue attempt must be positive")
        if self.max_attempts < 0:
            raise ValueError("integration rescue max_attempts must be non-negative")
        object.__setattr__(self, "disposition", RescueDisposition(self.disposition))
        object.__setattr__(self, "reason", RescueReason(self.reason))
        object.__setattr__(
            self, "conflict_paths", tuple(sorted(set(self.conflict_paths)))
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "work_id": self.work_id,
            "source_run_id": self.source_run_id,
            "queue_fingerprint": self.queue_fingerprint,
            "integration_head": self.integration_head,
            "disposition": self.disposition.value,
            "reason": self.reason.value,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "prepared": self.prepared,
            "conflict_paths": list(self.conflict_paths),
            "detail": self.detail,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "fingerprint": self.fingerprint(),
        }

    def fingerprint(self) -> str:
        payload = {
            "protocol": self.protocol,
            "session_id": self.session_id,
            "work_id": self.work_id,
            "source_run_id": self.source_run_id,
            "queue_fingerprint": self.queue_fingerprint,
            "integration_head": self.integration_head,
            "disposition": self.disposition.value,
            "reason": self.reason.value,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "prepared": self.prepared,
            "conflict_paths": list(self.conflict_paths),
            "detail": self.detail,
            "metadata": dict(self.metadata),
        }
        return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "IntegrationRescueDecision":
        return cls(
            protocol=str(data.get("protocol") or INTEGRATION_RESCUE_PROTOCOL),
            event_id=str(data.get("event_id") or ""),
            session_id=str(data.get("session_id") or ""),
            work_id=str(data.get("work_id") or ""),
            source_run_id=(
                None
                if data.get("source_run_id") is None
                else str(data.get("source_run_id"))
            ),
            queue_fingerprint=str(data.get("queue_fingerprint") or ""),
            integration_head=str(data.get("integration_head") or ""),
            disposition=RescueDisposition(data.get("disposition") or "manual"),
            reason=RescueReason(data.get("reason") or "semantic_ambiguity"),
            attempt=int(data.get("attempt") or 0),
            max_attempts=int(data.get("max_attempts") or 0),
            prepared=bool(data.get("prepared")),
            conflict_paths=tuple(data.get("conflict_paths") or ()),
            detail=str(data.get("detail") or ""),
            metadata=dict(data.get("metadata") or {}),
            created_at=str(data.get("created_at") or ""),
        )


def _rescue_payloads(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for event in events:
        if event.get("action") != _RECOVERY_ACTION:
            continue
        rescue = (event.get("metadata") or {}).get("rescue")
        if isinstance(rescue, dict):
            payloads.append(dict(rescue))
    return payloads


def superseded_rescue_run_ids(events: list[dict[str, Any]]) -> set[str]:
    """Return successful runs deliberately superseded by prepared serial rescue."""

    result: set[str] = set()
    for payload in _rescue_payloads(events):
        if not payload.get("prepared"):
            continue
        if payload.get("disposition") != RescueDisposition.SERIAL_RERUN.value:
            continue
        run_id = payload.get("source_run_id")
        if run_id:
            result.add(str(run_id))
    return result


def effective_runs_for_rescue(
    records: list[Any], events: list[dict[str, Any]]
) -> list[Any]:
    superseded = superseded_rescue_run_ids(events)
    return [
        record
        for record in records
        if getattr(record, "run_id", None) not in superseded
    ]


def _attempt_count(events: list[dict[str, Any]], work_id: str) -> int:
    return sum(1 for item in _rescue_payloads(events) if item.get("work_id") == work_id)


def _integration_reasons(entry: Any) -> set[str]:
    evidence = entry.integration_evidence or {}
    return {str(value) for value in (evidence.get("reasons") or ())}


def _stale_ordered_only(entry: Any) -> bool:
    evidence = entry.integration_evidence or {}
    checks = [
        item
        for item in (evidence.get("semantic_checks") or ())
        if not item.get("allowed")
    ]
    if not checks:
        return False
    return all(
        item.get("kind") == "ordered"
        and bool(item.get("declared_dependency"))
        and not bool(item.get("source_base_matches_integration_head"))
        for item in checks
    )


def _classify(entry: Any) -> tuple[RescueDisposition, RescueReason, str]:
    reasons = _integration_reasons(entry)
    authority = {
        IntegrationReason.UNDECLARED_PATH.value,
        IntegrationReason.REGION_VIOLATION.value,
        IntegrationReason.SEMANTIC_SCOPE_VIOLATION.value,
    }
    post_apply = {
        IntegrationReason.STRUCTURAL_EXTRACTION_FAILED.value,
        IntegrationReason.STAGED_PATH_MISMATCH.value,
        IntegrationReason.STAGED_SEMANTIC_MISMATCH.value,
    }
    if "<integration-error>" in entry.conflict_paths:
        return (
            RescueDisposition.RETRY_SNAPSHOT,
            RescueReason.TRANSIENT_INTEGRATION_ERROR,
            "retry the already bounded worker snapshot without changing authority",
        )
    if reasons & authority:
        return (
            RescueDisposition.MANUAL,
            RescueReason.AUTHORITY_VIOLATION,
            "actual mutations exceeded admitted authority; automatic rescue is forbidden",
        )
    if reasons & post_apply:
        return (
            RescueDisposition.MANUAL,
            RescueReason.POST_APPLY_MISMATCH,
            "post-apply evidence no longer matches the admitted mutation surface",
        )
    if reasons & {
        IntegrationReason.ACTUAL_SEMANTIC_CONFLICT.value,
        IntegrationReason.ACTUAL_SEMANTIC_UNKNOWN.value,
    }:
        if _stale_ordered_only(entry):
            return (
                RescueDisposition.SERIAL_RERUN,
                RescueReason.STALE_ORDERED_DEPENDENCY,
                "rerun the consumer from the current integration head so its premise is fresh",
            )
        return (
            RescueDisposition.REPLAN_REQUIRED,
            RescueReason.SEMANTIC_AMBIGUITY,
            "semantic conflict or ambiguity is not repairable by deterministic replay",
        )
    return (
        RescueDisposition.SERIAL_RERUN,
        RescueReason.TEXTUAL_INTEGRATION_CONFLICT,
        "textual overlap is rerun serially on the current integration head",
    )


def _reset_managed_worker(
    root: Path, queue: DeterministicMergeQueue, work_id: str
) -> None:
    with _store(root) as store:
        worktrees = store.list_worktrees(queue.session_id)
        active = [
            record
            for record in store.list_codex_runs(queue.session_id, work_id=work_id)
            if record.state.active
        ]
    if active:
        raise ValueError("cannot prepare serial rescue while the worker is active")
    record = next((item for item in worktrees if item.work_id == work_id), None)
    if record is None:
        raise ValueError(f"managed worktree {work_id!r} is missing")
    path = Path(record.worktree_path).resolve()
    registered = _registered_worktrees(root)
    git_record = registered.get(path)
    if git_record is None:
        raise ValueError("refusing to reset an unregistered worker worktree")
    actual_branch = git_record.get("branch", "").removeprefix("refs/heads/")
    if actual_branch != record.branch:
        raise ValueError("managed worker branch ownership changed")
    reset = subprocess.run(
        ["git", "reset", "--hard", queue.integration_head],
        cwd=path,
        text=True,
        capture_output=True,
        check=False,
    )
    if reset.returncode != 0:
        raise ValueError(
            reset.stderr.strip() or reset.stdout.strip() or "worker reset failed"
        )
    clean = subprocess.run(
        ["git", "clean", "-fd", "-e", ".claim-plane/", "-e", ".codex/hooks.json"],
        cwd=path,
        text=True,
        capture_output=True,
        check=False,
    )
    if clean.returncode != 0:
        raise ValueError(
            clean.stderr.strip() or clean.stdout.strip() or "worker clean failed"
        )


def _recovery_event(decision: IntegrationRescueDecision) -> dict[str, Any]:
    return {
        "protocol": "claim-plane.swarm-recovery.v1",
        "event_id": decision.event_id,
        "session_id": decision.session_id,
        "action": _RECOVERY_ACTION,
        "run_id": decision.source_run_id,
        "work_id": decision.work_id,
        "detail": decision.detail,
        "created_at": decision.created_at,
        "metadata": {"rescue": decision.to_dict()},
    }


def plan_integration_rescue(
    repo: str | Path, session_id: str, *, work_id: str | None = None
) -> IntegrationRescueDecision:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    with _store(root) as store:
        session = store.require(session_id)
        queue_data = store.get_merge_queue(session_id)
        events = store.list_recovery_events(session_id)
    if queue_data is None:
        raise ValueError("swarm session has no deterministic merge queue")
    queue, _ = queue_data
    conflicts = [
        entry for entry in queue.entries if entry.state is MergeEntryState.CONFLICT
    ]
    if work_id is not None:
        conflicts = [entry for entry in conflicts if entry.work_id == work_id]
    if not conflicts:
        raise ValueError("merge queue has no matching conflicted work item")
    entry = min(conflicts, key=lambda item: item.order)
    attempt = _attempt_count(events, entry.work_id) + 1
    max_attempts = session.budget_policy.retries.max_repairs_per_work_item
    disposition, reason, detail = _classify(entry)
    if attempt > max_attempts:
        disposition = RescueDisposition.MANUAL
        reason = RescueReason.REPAIR_BUDGET_EXHAUSTED
        detail = "bounded integration repair budget is exhausted; explicit review is required"
    return IntegrationRescueDecision(
        event_id=f"rescue-{secrets.token_hex(12)}",
        session_id=session_id,
        work_id=entry.work_id,
        source_run_id=entry.run_id,
        queue_fingerprint=queue.fingerprint(),
        integration_head=queue.integration_head,
        disposition=disposition,
        reason=reason,
        attempt=attempt,
        max_attempts=max_attempts,
        prepared=False,
        conflict_paths=entry.conflict_paths,
        detail=detail,
        metadata={
            "source_commit": entry.source_commit,
            "integration_evidence_fingerprint": (
                None
                if not entry.integration_evidence
                else entry.integration_evidence.get("fingerprint")
            ),
            "target_branch_mutated": False,
        },
    )


def rescue_swarm_integration(
    repo: str | Path, session_id: str, *, work_id: str | None = None
) -> dict[str, Any]:
    root = resolve_repository_root(repo)
    decision = plan_integration_rescue(root, session_id, work_id=work_id)
    if decision.disposition in {
        RescueDisposition.MANUAL,
        RescueDisposition.REPLAN_REQUIRED,
    }:
        with _store(root) as store:
            store.save_recovery_event(_recovery_event(decision))
        return {"prepared": False, "decision": decision.to_dict()}

    with _store(root) as store:
        queue_data = store.get_merge_queue(session_id)
    if queue_data is None:
        raise ValueError("swarm session has no deterministic merge queue")
    queue, _ = queue_data
    if queue.fingerprint() != decision.queue_fingerprint:
        raise ValueError("merge queue changed before integration rescue")

    if decision.disposition is RescueDisposition.SERIAL_RERUN:
        _reset_managed_worker(root, queue, decision.work_id)
        replacement_state = MergeEntryState.PENDING
        detail = "serial rescue prepared on the current integration head"
    else:
        replacement_state = MergeEntryState.READY
        detail = "bounded integration snapshot retry prepared"

    prepared = IntegrationRescueDecision(
        event_id=decision.event_id,
        session_id=decision.session_id,
        work_id=decision.work_id,
        source_run_id=decision.source_run_id,
        queue_fingerprint=decision.queue_fingerprint,
        integration_head=decision.integration_head,
        disposition=decision.disposition,
        reason=decision.reason,
        attempt=decision.attempt,
        max_attempts=decision.max_attempts,
        prepared=True,
        conflict_paths=decision.conflict_paths,
        detail=decision.detail,
        metadata=decision.metadata,
        created_at=decision.created_at,
    )
    with _store(root) as store:
        updated, version = store.apply_merge_rescue(
            session_id,
            decision.work_id,
            replacement_state=replacement_state,
            preserve_source_commit=(
                decision.disposition is RescueDisposition.RETRY_SNAPSHOT
            ),
            event_payload=_recovery_event(prepared),
            expected_queue_fingerprint=decision.queue_fingerprint,
            detail=detail,
            updated_at=_utc_now(),
        )
    return {
        "prepared": True,
        "queue_version": version,
        "decision": prepared.to_dict(),
        "merge_queue": updated.to_dict(),
        "summary": updated.summary(),
    }


def list_integration_rescues(repo: str | Path, session_id: str) -> list[dict[str, Any]]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    with _store(root) as store:
        events = store.list_recovery_events(session_id)
    return _rescue_payloads(events)
