"""Deterministic evidence reports and lifecycle replay for controlled runs.

The evidence layer rebuilds a stable, secret-safe view from durable controlled-run
records and the normalized lifecycle journal. It never repeats provider calls and
never trusts raw agent prose as verification evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from claim_plane.controlled_run import (
    CONTROLLED_RUNS_PATH,
    ControlledRunError,
    load_controlled_run,
)
from claim_plane.project import resolve_project_root
from claim_plane.protocol import (
    LifecycleEvent,
    LifecycleEventStore,
    LifecycleEventType,
    build_lifecycle_report,
)

EVIDENCE_REPORT_PROTOCOL = "claim-plane.evidence-report.v1"
EVIDENCE_REPLAY_PROTOCOL = "claim-plane.evidence-replay.v1"


class EvidenceError(RuntimeError):
    """Evidence cannot be resolved or validated safely."""


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _duration_seconds(started_at: str, finished_at: str) -> float | None:
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        finish = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return max(0.0, round((finish - start).total_seconds(), 6))


def _run_records(root: Path) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    runs_root = root / CONTROLLED_RUNS_PATH
    if not runs_root.is_dir():
        return ()
    for path in sorted(runs_root.glob("*/run.json")):
        run_id = path.parent.name
        try:
            payload = load_controlled_run(root, run_id)
        except (OSError, json.JSONDecodeError, ControlledRunError) as exc:
            raise EvidenceError(
                f"durable controlled run {run_id!r} is corrupt"
            ) from exc
        records.append(payload)
    return tuple(
        sorted(
            records,
            key=lambda item: (
                str(item.get("finished_at") or ""),
                str(item.get("run_id") or ""),
            ),
        )
    )


def resolve_controlled_run(
    root: str | Path, selector: str = "latest"
) -> dict[str, Any]:
    """Resolve an exact run id or the most recently completed durable run."""

    resolved_root = resolve_project_root(root)
    cleaned = selector.strip()
    if not cleaned:
        raise EvidenceError("run selector must not be empty")
    if cleaned != "latest":
        try:
            return load_controlled_run(resolved_root, cleaned)
        except (OSError, json.JSONDecodeError, ControlledRunError) as exc:
            raise EvidenceError(f"controlled run {cleaned!r} is unavailable") from exc
    records = _run_records(resolved_root)
    if not records:
        raise EvidenceError("no durable controlled runs were found")
    return records[-1]


def list_controlled_runs(root: str | Path) -> tuple[dict[str, Any], ...]:
    """Return secret-safe durable run summaries ordered by completion time."""

    resolved_root = resolve_project_root(root)
    return tuple(
        {
            "run_id": item.get("run_id"),
            "adapter": item.get("adapter"),
            "policy": item.get("policy"),
            "outcome": item.get("outcome"),
            "verified": bool(item.get("verified")),
            "started_at": item.get("started_at"),
            "finished_at": item.get("finished_at"),
            "session_id": item.get("session_id"),
            "intent_id": item.get("intent_id"),
        }
        for item in _run_records(resolved_root)
    )


def _events_for_run(root: Path, run: Mapping[str, Any]) -> tuple[LifecycleEvent, ...]:
    session_id = run.get("session_id")
    adapter = run.get("adapter")
    if not isinstance(session_id, str) or not session_id:
        return ()
    if not isinstance(adapter, str) or not adapter:
        return ()
    try:
        with LifecycleEventStore.for_project(root) as store:
            events = store.list_events(adapter=adapter, session_id=session_id)
    except Exception as exc:  # noqa: BLE001
        raise EvidenceError("normalized lifecycle evidence is unavailable") from exc
    expected_run_id = run.get("run_id")
    # Some Codex versions do not propagate the controlled-run environment into
    # every project-local hook. Those native events remain explicitly unbound,
    # while the final verifier seals the session with the durable run id.
    explicit_run_ids = {
        event.run_id
        for event in events
        if isinstance(event.run_id, str) and event.run_id
    }
    foreign_run_ids = sorted(
        run_id for run_id in explicit_run_ids if run_id != expected_run_id
    )
    if foreign_run_ids:
        raise EvidenceError("lifecycle stream is bound to a different controlled run")
    if events and expected_run_id not in explicit_run_ids:
        raise EvidenceError("lifecycle stream is not bound to the controlled run")
    return events


def _event_counts(events: Sequence[LifecycleEvent]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        key = event.event_type.value
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _safe_event_payload(event: LifecycleEvent) -> dict[str, Any]:
    allowed = {
        "status",
        "allowed",
        "verified",
        "state",
        "exit_code",
        "tool_name",
        "paths",
        "changed_paths",
        "authorized_paths",
        "denied_paths",
        "error_code",
        "retryable",
        "ticket_id",
        "resume",
        "source",
    }
    return {
        key: value for key, value in sorted(event.payload.items()) if key in allowed
    }


def _decision_summary(events: Sequence[LifecycleEvent]) -> dict[str, Any]:
    blocked: list[dict[str, Any]] = []
    observed: list[dict[str, Any]] = []
    amendments: list[dict[str, Any]] = []
    for event in events:
        item = {
            "sequence": event.sequence,
            "event_id": event.event_id,
            "timestamp": event.timestamp,
            "intent_id": event.intent_id,
            "intent_version": event.intent_version,
            "payload": _safe_event_payload(event),
        }
        if event.event_type in {
            LifecycleEventType.ADMISSION_DENIED,
            LifecycleEventType.MUTATION_DENIED,
            LifecycleEventType.SCOPE_EXPANSION_DENIED,
        }:
            blocked.append({"type": event.event_type.value, **item})
        elif event.event_type is LifecycleEventType.MUTATION_OBSERVED:
            observed.append({"type": event.event_type.value, **item})
        elif event.event_type in {
            LifecycleEventType.SCOPE_EXPANSION_GRANTED,
            LifecycleEventType.SCOPE_EXPANSION_DENIED,
        }:
            amendments.append({"type": event.event_type.value, **item})
    return {
        "blocked": blocked,
        "observed": observed,
        "amendments": amendments,
        "blocked_count": len(blocked),
        "observed_count": len(observed),
        "amendment_count": len(amendments),
    }


def _guarantee_summary(
    run: Mapping[str, Any], events: Sequence[LifecycleEvent]
) -> dict[str, Any]:
    manifest: Mapping[str, Any] | None = None
    lifecycle = build_lifecycle_report(events) if events else None
    if lifecycle is not None and isinstance(lifecycle.adapter_manifest, Mapping):
        manifest = lifecycle.adapter_manifest
    guarantees = manifest.get("guarantees") if isinstance(manifest, Mapping) else None
    levels: dict[str, list[str]] = {
        "HARD_BLOCKED": [],
        "OBSERVED": [],
        "POST_VERIFIED": [],
        "UNAVAILABLE": [],
    }
    iterable: Iterable[tuple[str, Mapping[str, Any]]]
    if isinstance(guarantees, Mapping):
        iterable = (
            (str(name), declaration)
            for name, declaration in guarantees.items()
            if isinstance(declaration, Mapping)
        )
    elif isinstance(guarantees, Sequence) and not isinstance(guarantees, (str, bytes)):
        iterable = (
            (str(item.get("name") or item.get("guarantee") or "unknown"), item)
            for item in guarantees
            if isinstance(item, Mapping)
        )
    else:
        iterable = ()
    for name, declaration in iterable:
        raw_level = (
            declaration.get("level") or declaration.get("enforcement") or "UNAVAILABLE"
        )
        level = str(raw_level).upper()
        levels.setdefault(level, []).append(str(name))
    return {
        "manifest_digest": run.get("manifest_digest"),
        "levels": {key: sorted(value) for key, value in levels.items()},
    }


def _report_unsigned(root: Path, run: Mapping[str, Any]) -> dict[str, Any]:
    events = _events_for_run(root, run)
    lifecycle = build_lifecycle_report(events) if events else None
    stored_lifecycle = run.get("lifecycle")
    findings: list[dict[str, str]] = []
    if lifecycle is None:
        findings.append(
            {
                "code": "lifecycle_unavailable",
                "message": "the run has no normalized lifecycle stream",
            }
        )
    elif not lifecycle.valid:
        findings.extend(
            {"code": item.code.value, "message": item.message}
            for item in lifecycle.findings
        )
    if isinstance(stored_lifecycle, Mapping) and lifecycle is not None:
        expected = stored_lifecycle.get("head_digest")
        if expected and expected != lifecycle.head_digest:
            findings.append(
                {
                    "code": "lifecycle_head_mismatch",
                    "message": (
                        "durable run record and lifecycle journal have different heads"
                    ),
                }
            )
    completion_value = run.get("completion")
    completion: Mapping[str, Any] = (
        completion_value if isinstance(completion_value, Mapping) else {}
    )
    changes = run.get("changes")
    if not isinstance(changes, Mapping):
        changes = {
            "protocol": "claim-plane.change-summary.v1",
            "available": False,
            "files": [],
            "file_count": int(completion.get("changed_files") or 0),
        }
    acceptance = run.get("acceptance")
    if not isinstance(acceptance, Mapping):
        acceptance = {
            "commands": [],
            "passed": bool(completion.get("acceptance_passed")),
            "evidence": "completion_summary",
        }
    execution = {
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "duration_seconds": _duration_seconds(
            str(run.get("started_at") or ""),
            str(run.get("finished_at") or ""),
        ),
        "runtime_returncode": run.get("runtime_returncode"),
        "exit_code": run.get("exit_code"),
        "usage": dict((run.get("runtime") or {}).get("usage") or {}),
        "runtime_events": dict((run.get("runtime") or {}).get("event_counts") or {}),
        "runtime_errors": int((run.get("runtime") or {}).get("errors") or 0),
    }
    lifecycle_payload: dict[str, Any] | None
    if lifecycle is None:
        lifecycle_payload = None
    else:
        lifecycle_payload = {
            **lifecycle.to_dict(),
            "event_counts": _event_counts(events),
        }
    return {
        "protocol": EVIDENCE_REPORT_PROTOCOL,
        "run_id": run.get("run_id"),
        "outcome": run.get("outcome"),
        "verified": bool(run.get("verified")),
        "task": {
            "sha256": run.get("task_sha256"),
            "length": run.get("task_length"),
        },
        "repository": {
            "root": str(root),
            "start_git": dict(run.get("start_git") or {}),
            "result_git": dict(run.get("result_git") or {}),
        },
        "agent": {
            "adapter": run.get("adapter"),
            "session_id": run.get("session_id"),
            "handshake": dict(run.get("handshake") or {}),
            "runtime": {
                key: value
                for key, value in dict(run.get("runtime") or {}).items()
                if key != "event_counts"
            },
        },
        "intent": {
            "id": run.get("intent_id"),
            "version": run.get("intent_version"),
        },
        "policy": {
            "name": run.get("policy"),
            "effective": dict(run.get("effective_policy") or {}),
            "compatibility": dict(run.get("policy_compatibility") or {}),
            "risk": dict(run.get("risk") or {}),
        },
        "guarantees": _guarantee_summary(run, events),
        "changes": dict(changes),
        "scope": dict(run.get("scope") or {}),
        "acceptance": dict(acceptance),
        "verification": dict(run.get("completion") or {}),
        "decisions": _decision_summary(events),
        "execution": execution,
        "lifecycle": lifecycle_payload,
        "integrity": {
            "valid": not findings,
            "findings": findings,
        },
    }


def build_evidence_report(root: str | Path, selector: str = "latest") -> dict[str, Any]:
    """Build one canonical report from durable run and lifecycle evidence."""

    resolved_root = resolve_project_root(root)
    run = resolve_controlled_run(resolved_root, selector)
    unsigned = _report_unsigned(resolved_root, run)
    return {**unsigned, "evidence_digest": _sha256(unsigned)}


@dataclass(frozen=True, slots=True)
class ReplayEntry:
    sequence: int
    timestamp: str
    event_type: str
    message: str
    intent_id: str | None
    intent_version: int | None
    event_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "message": self.message,
            "intent_id": self.intent_id,
            "intent_version": self.intent_version,
            "event_id": self.event_id,
        }


def _event_message(event: LifecycleEvent) -> str:
    payload = event.payload
    paths = (
        payload.get("paths")
        or payload.get("changed_paths")
        or payload.get("denied_paths")
    )
    path_text = ""
    if isinstance(paths, Sequence) and not isinstance(paths, (str, bytes)):
        clean = [str(item) for item in paths[:3]]
        if clean:
            path_text = " — " + ", ".join(clean)
    messages = {
        LifecycleEventType.SESSION_STARTED: "session started",
        LifecycleEventType.TASK_SUBMITTED: "task submitted",
        LifecycleEventType.INTENT_PROPOSED: "intent proposed",
        LifecycleEventType.ADMISSION_REQUESTED: "admission requested",
        LifecycleEventType.ADMISSION_GRANTED: "intent admitted",
        LifecycleEventType.ADMISSION_DENIED: "intent denied",
        LifecycleEventType.MUTATION_REQUESTED: "mutation requested",
        LifecycleEventType.MUTATION_ALLOWED: "mutation allowed",
        LifecycleEventType.MUTATION_DENIED: "mutation denied",
        LifecycleEventType.MUTATION_OBSERVED: "mutation observed",
        LifecycleEventType.SCOPE_EXPANSION_REQUESTED: "scope expansion requested",
        LifecycleEventType.SCOPE_EXPANSION_GRANTED: "scope expansion admitted",
        LifecycleEventType.SCOPE_EXPANSION_DENIED: "scope expansion denied",
        LifecycleEventType.VERIFICATION_STARTED: "verification started",
        LifecycleEventType.VERIFICATION_COMPLETED: (
            "verification passed"
            if payload.get("verified")
            else "verification did not pass"
        ),
        LifecycleEventType.AGENT_STOPPED: "agent stopped",
        LifecycleEventType.SESSION_ENDED: "session ended",
    }
    return messages[event.event_type] + path_text


def build_evidence_replay(root: str | Path, selector: str = "latest") -> dict[str, Any]:
    """Reconstruct the decision chronology without repeating provider calls."""

    resolved_root = resolve_project_root(root)
    run = resolve_controlled_run(resolved_root, selector)
    events = _events_for_run(resolved_root, run)
    report = build_lifecycle_report(events) if events else None
    if report is None or not report.valid:
        raise EvidenceError(
            "a valid normalized lifecycle stream is required for replay"
        )
    entries = tuple(
        ReplayEntry(
            sequence=event.sequence,
            timestamp=event.timestamp,
            event_type=event.event_type.value,
            message=_event_message(event),
            intent_id=event.intent_id,
            intent_version=event.intent_version,
            event_id=event.event_id,
        )
        for event in events
    )
    unsigned = {
        "protocol": EVIDENCE_REPLAY_PROTOCOL,
        "run_id": run.get("run_id"),
        "adapter": run.get("adapter"),
        "session_id": run.get("session_id"),
        "outcome": run.get("outcome"),
        "event_count": len(entries),
        "head_digest": report.head_digest,
        "entries": [item.to_dict() for item in entries],
    }
    return {**unsigned, "replay_digest": _sha256(unsigned)}


def render_evidence_replay(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Render a stable human-readable chronology from a replay document."""

    lines = [
        f"RUN {payload.get('run_id')} — {payload.get('outcome')}",
    ]
    for entry in payload.get("entries") or ():
        if not isinstance(entry, Mapping):
            continue
        timestamp = str(entry.get("timestamp") or "")
        clock = timestamp[11:19] if len(timestamp) >= 19 else timestamp
        intent = ""
        if entry.get("intent_id"):
            intent = f" [{entry['intent_id']}@{entry.get('intent_version')}]"
        lines.append(
            f"{int(entry.get('sequence') or 0):04d} {clock} "
            f"{entry.get('message')}{intent}"
        )
    lines.append(f"Replay digest: {payload.get('replay_digest')}")
    return tuple(lines)
