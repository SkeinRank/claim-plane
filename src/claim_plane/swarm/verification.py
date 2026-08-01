"""Two-level verification and durable evidence for integrated swarm sessions."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from claim_plane.coordination.admission import parse_line_region
from claim_plane.core import ChangeIntent, IntentOperation, ResourceKind
from claim_plane.core.models import AcceptanceResult
from claim_plane.integration.acceptance import AcceptanceRunner
from claim_plane.integration.collector import GitChangeCollector, _parse_changed_regions
from claim_plane.integration.snapshot import (
    capture_worktree_tree,
    changed_worktree_paths,
)
from claim_plane.integration.verifier import IntegrationVerifier
from claim_plane.swarm.merge_queue import (
    DeterministicMergeQueue,
    MergeEntryState,
    MergeQueueStatus,
)
from claim_plane.swarm.models import SwarmSession, WorkItem

SWARM_VERIFICATION_PROTOCOL = "claim-plane.swarm-verification.v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fingerprint(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise ValueError(
            completed.stderr.strip() or completed.stdout.strip() or "git failed"
        )
    return completed.stdout.strip()


class SwarmVerificationStatus(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    STALE = "stale"


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_GIT_OID_RE = re.compile(r"[0-9a-f]{40,64}\Z")


@dataclass(frozen=True, slots=True)
class VerificationFinding:
    code: str
    message: str
    severity: str = "error"
    work_id: str | None = None
    path: str | None = None

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("verification finding code must not be empty")
        if self.severity not in {"info", "warning", "error"}:
            raise ValueError("verification finding severity is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "work_id": self.work_id,
            "path": self.path,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VerificationFinding":
        return cls(
            code=str(data.get("code") or "unknown"),
            severity=str(data.get("severity") or "error"),
            message=str(data.get("message") or ""),
            work_id=None if data.get("work_id") is None else str(data.get("work_id")),
            path=None if data.get("path") is None else str(data.get("path")),
        )


@dataclass(frozen=True, slots=True)
class WorkVerificationEvidence:
    work_id: str
    run_id: str | None
    source_commit: str | None
    integration_commit: str | None
    changed_paths: tuple[str, ...]
    changed_regions: tuple[Mapping[str, Any], ...]
    acceptance_results: tuple[AcceptanceResult, ...]
    findings: tuple[VerificationFinding, ...]
    verified: bool

    def __post_init__(self) -> None:
        if not self.work_id.strip():
            raise ValueError("work verification work_id must not be empty")
        for value in (self.source_commit, self.integration_commit):
            if value is not None and not _GIT_OID_RE.fullmatch(value.lower()):
                raise ValueError(
                    "work verification commit must be a full Git object id"
                )
        object.__setattr__(
            self, "changed_paths", tuple(sorted(set(self.changed_paths)))
        )
        object.__setattr__(
            self,
            "changed_regions",
            tuple(dict(item) for item in self.changed_regions),
        )
        object.__setattr__(self, "acceptance_results", tuple(self.acceptance_results))
        object.__setattr__(self, "findings", tuple(self.findings))
        if self.verified and any(item.severity == "error" for item in self.findings):
            raise ValueError("verified work evidence cannot contain error findings")

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "run_id": self.run_id,
            "source_commit": self.source_commit,
            "integration_commit": self.integration_commit,
            "changed_paths": list(self.changed_paths),
            "changed_regions": [dict(item) for item in self.changed_regions],
            "acceptance_results": [item.to_dict() for item in self.acceptance_results],
            "findings": [item.to_dict() for item in self.findings],
            "verified": self.verified,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkVerificationEvidence":
        return cls(
            work_id=str(data.get("work_id") or ""),
            run_id=None if data.get("run_id") is None else str(data.get("run_id")),
            source_commit=(
                None
                if data.get("source_commit") is None
                else str(data.get("source_commit"))
            ),
            integration_commit=(
                None
                if data.get("integration_commit") is None
                else str(data.get("integration_commit"))
            ),
            changed_paths=tuple(str(item) for item in data.get("changed_paths") or ()),
            changed_regions=tuple(
                dict(item) for item in data.get("changed_regions") or ()
            ),
            acceptance_results=tuple(
                AcceptanceResult.from_dict(item)
                for item in data.get("acceptance_results") or ()
            ),
            findings=tuple(
                VerificationFinding.from_dict(item)
                for item in data.get("findings") or ()
            ),
            verified=bool(data.get("verified")),
        )


@dataclass(frozen=True, slots=True)
class SwarmVerificationReport:
    session_id: str
    repository_identity: str
    base_commit: str
    graph_version: int
    graph_fingerprint: str
    budget_version: int
    budget_fingerprint: str
    admission_fingerprint: str
    merge_queue_fingerprint: str
    integration_head: str
    status: SwarmVerificationStatus
    work_evidence: tuple[WorkVerificationEvidence, ...]
    root_changed_paths: tuple[str, ...]
    root_acceptance_results: tuple[AcceptanceResult, ...]
    root_report: Mapping[str, Any]
    snapshot_integrity_ok: bool
    acceptance_mutation_paths: tuple[str, ...]
    findings: tuple[VerificationFinding, ...]
    created_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SWARM_VERIFICATION_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SWARM_VERIFICATION_PROTOCOL:
            raise ValueError(
                f"unsupported swarm verification protocol {self.protocol!r}"
            )
        if not self.session_id.strip():
            raise ValueError("swarm verification session_id must not be empty")
        identity = self.repository_identity.lower()
        if not _SHA256_RE.fullmatch(identity):
            raise ValueError("repository_identity must be a SHA-256 digest")
        object.__setattr__(self, "repository_identity", identity)
        base = self.base_commit.lower()
        head = self.integration_head.lower()
        if not _GIT_OID_RE.fullmatch(base) or not _GIT_OID_RE.fullmatch(head):
            raise ValueError("verification commits must be full Git object ids")
        object.__setattr__(self, "base_commit", base)
        object.__setattr__(self, "integration_head", head)
        if self.graph_version <= 0 or self.budget_version <= 0:
            raise ValueError("verification source versions must be positive")
        for name in (
            "graph_fingerprint",
            "budget_fingerprint",
            "admission_fingerprint",
            "merge_queue_fingerprint",
        ):
            value = str(getattr(self, name)).lower()
            if not _SHA256_RE.fullmatch(value):
                raise ValueError(f"{name} must be a SHA-256 digest")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "status", SwarmVerificationStatus(self.status))
        object.__setattr__(
            self,
            "work_evidence",
            tuple(sorted(self.work_evidence, key=lambda item: item.work_id)),
        )
        object.__setattr__(
            self,
            "root_changed_paths",
            tuple(sorted(set(self.root_changed_paths))),
        )
        object.__setattr__(
            self,
            "acceptance_mutation_paths",
            tuple(sorted(set(self.acceptance_mutation_paths))),
        )
        object.__setattr__(self, "root_report", dict(self.root_report))
        object.__setattr__(
            self,
            "root_acceptance_results",
            tuple(self.root_acceptance_results),
        )
        object.__setattr__(self, "findings", tuple(self.findings))
        if not self.created_at.strip():
            raise ValueError("verification created_at must not be empty")
        if self.status is SwarmVerificationStatus.VERIFIED:
            if not self.snapshot_integrity_ok or not all(
                item.verified for item in self.work_evidence
            ):
                raise ValueError(
                    "verified swarm report requires clean work and snapshot evidence"
                )
            if any(item.severity == "error" for item in self.findings):
                raise ValueError("verified swarm report cannot contain error findings")
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def verified(self) -> bool:
        return self.status is SwarmVerificationStatus.VERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "session_id": self.session_id,
            "repository_identity": self.repository_identity,
            "base_commit": self.base_commit,
            "graph_version": self.graph_version,
            "graph_fingerprint": self.graph_fingerprint,
            "budget_version": self.budget_version,
            "budget_fingerprint": self.budget_fingerprint,
            "admission_fingerprint": self.admission_fingerprint,
            "merge_queue_fingerprint": self.merge_queue_fingerprint,
            "integration_head": self.integration_head,
            "status": self.status.value,
            "verified": self.verified,
            "work_evidence": [item.to_dict() for item in self.work_evidence],
            "root_changed_paths": list(self.root_changed_paths),
            "root_acceptance_results": [
                item.to_dict() for item in self.root_acceptance_results
            ],
            "root_report": dict(self.root_report),
            "snapshot_integrity_ok": self.snapshot_integrity_ok,
            "acceptance_mutation_paths": list(self.acceptance_mutation_paths),
            "findings": [item.to_dict() for item in self.findings],
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    def fingerprint(self) -> str:
        return _fingerprint(self.to_dict())

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "verified": self.verified,
            "work_items": len(self.work_evidence),
            "work_verified": sum(1 for item in self.work_evidence if item.verified),
            "root_acceptance_passed": all(
                item.passed for item in self.root_acceptance_results
            ),
            "snapshot_integrity_ok": self.snapshot_integrity_ok,
            "changed_paths": len(self.root_changed_paths),
            "errors": sum(1 for item in self.findings if item.severity == "error"),
            "warnings": sum(1 for item in self.findings if item.severity == "warning"),
            "fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SwarmVerificationReport":
        return cls(
            protocol=str(data.get("protocol") or SWARM_VERIFICATION_PROTOCOL),
            session_id=str(data.get("session_id") or ""),
            repository_identity=str(data.get("repository_identity") or ""),
            base_commit=str(data.get("base_commit") or ""),
            graph_version=int(data.get("graph_version") or 0),
            graph_fingerprint=str(data.get("graph_fingerprint") or ""),
            budget_version=int(data.get("budget_version") or 0),
            budget_fingerprint=str(data.get("budget_fingerprint") or ""),
            admission_fingerprint=str(data.get("admission_fingerprint") or ""),
            merge_queue_fingerprint=str(data.get("merge_queue_fingerprint") or ""),
            integration_head=str(data.get("integration_head") or ""),
            status=SwarmVerificationStatus(data.get("status") or "failed"),
            work_evidence=tuple(
                WorkVerificationEvidence.from_dict(item)
                for item in data.get("work_evidence") or ()
            ),
            root_changed_paths=tuple(
                str(item) for item in data.get("root_changed_paths") or ()
            ),
            root_acceptance_results=tuple(
                AcceptanceResult.from_dict(item)
                for item in data.get("root_acceptance_results") or ()
            ),
            root_report=dict(data.get("root_report") or {}),
            snapshot_integrity_ok=bool(data.get("snapshot_integrity_ok")),
            acceptance_mutation_paths=tuple(
                str(item) for item in data.get("acceptance_mutation_paths") or ()
            ),
            findings=tuple(
                VerificationFinding.from_dict(item)
                for item in data.get("findings") or ()
            ),
            created_at=str(data.get("created_at") or ""),
            metadata=dict(data.get("metadata") or {}),
        )


def _operation_path(operation: IntentOperation) -> str | None:
    resource = operation.resource
    if resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT, ResourceKind.CONFIG}:
        return resource.identifier.replace("\\", "/")
    value = resource.metadata.get("path")
    return None if value is None else str(value).replace("\\", "/")


def _covers(operation: IntentOperation, path: str) -> bool:
    resource = operation.resource
    candidate = _operation_path(operation)
    if candidate is None:
        return False
    if resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}:
        return resource.covers_path(path)
    if any(ch in candidate for ch in "*?["):
        import fnmatch

        return fnmatch.fnmatchcase(path, candidate)
    return path == candidate


def _commit_evidence(
    root: Path, commit: str | None
) -> tuple[tuple[str, ...], tuple[Mapping[str, Any], ...]]:
    if commit is None:
        return (), ()
    paths = tuple(
        sorted(
            set(
                filter(
                    None,
                    _git(
                        root,
                        "diff-tree",
                        "--no-commit-id",
                        "--name-only",
                        "-r",
                        commit,
                    ).splitlines(),
                )
            )
        )
    )
    patch = _git(root, "show", "--format=", "--no-color", "--unified=0", commit, "--")
    regions = tuple(
        {
            "path": item.path,
            "start_line": item.start_line,
            "end_line": item.end_line,
            "old_start_line": item.old_start_line,
            "old_end_line": item.old_end_line,
        }
        for item in _parse_changed_regions(patch)
    )
    return paths, regions


def _verify_work_item(
    root: Path,
    item: WorkItem,
    entry: Any,
    acceptance_results: tuple[AcceptanceResult, ...],
) -> WorkVerificationEvidence:
    integration_commit = (
        entry.integration_commit if entry.source_commit is not None else None
    )
    paths, regions = _commit_evidence(root, integration_commit)
    operations = tuple(op for op in item.operations if op.mutating and op.committed)
    findings: list[VerificationFinding] = []
    for path in paths:
        if not any(_covers(op, path) for op in operations):
            findings.append(
                VerificationFinding(
                    "undeclared_change",
                    (
                        f"Integrated path {path!r} is outside the admitted "
                        "work-item scope."
                    ),
                    work_id=item.work_id,
                    path=path,
                )
            )
    for operation in operations:
        declared = _operation_path(operation)
        if (
            operation.required
            and declared is not None
            and not any(_covers(operation, path) for path in paths)
        ):
            findings.append(
                VerificationFinding(
                    "missing_declared_change",
                    f"Required scope {declared!r} produced no integrated change.",
                    work_id=item.work_id,
                    path=declared,
                )
            )
    for region in regions:
        path = str(region["path"])
        matching = [op for op in operations if _covers(op, path)]
        bounded = [op for op in matching if op.resource.region]
        if bounded:
            start = int(region["start_line"])
            end = int(region["end_line"])
            allowed = False
            for operation in bounded:
                parsed = parse_line_region(str(operation.resource.region))
                if parsed is not None and parsed[0] <= start and end <= parsed[1]:
                    allowed = True
                    break
            if not allowed:
                findings.append(
                    VerificationFinding(
                        "region_violation",
                        (
                            f"Changed lines {start}-{end} in {path!r} exceed "
                            "the admitted region."
                        ),
                        work_id=item.work_id,
                        path=path,
                    )
                )
    for result in acceptance_results:
        if not result.passed:
            findings.append(
                VerificationFinding(
                    "acceptance_failed",
                    f"Acceptance command failed: {result.command}",
                    work_id=item.work_id,
                )
            )
    return WorkVerificationEvidence(
        work_id=item.work_id,
        run_id=entry.run_id,
        source_commit=entry.source_commit,
        integration_commit=entry.integration_commit,
        changed_paths=paths,
        changed_regions=regions,
        acceptance_results=acceptance_results,
        findings=tuple(findings),
        verified=not any(item.severity == "error" for item in findings),
    )


def _restore_integration(root: Path, head: str) -> None:
    subprocess.run(
        ["git", "reset", "--hard", head],
        cwd=root,
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["git", "clean", "-fd"],
        cwd=root,
        capture_output=True,
        check=False,
    )


def _run_acceptance_with_integrity(
    commands: tuple[str, ...],
    integration: Path,
    *,
    timeout_seconds: int,
    baseline_tree: str,
    integration_head: str,
) -> tuple[tuple[AcceptanceResult, ...], tuple[str, ...]]:
    runner = AcceptanceRunner(timeout_seconds=timeout_seconds)
    results: list[AcceptanceResult] = []
    mutation_paths: set[str] = set()
    for command in commands:
        result = runner.run((command,), integration)[0]
        results.append(result)
        if capture_worktree_tree(integration) != baseline_tree:
            mutation_paths.update(changed_worktree_paths(integration))
            _restore_integration(integration, integration_head)
        if not result.passed:
            break
    return tuple(results), tuple(sorted(mutation_paths))


def _root_intent(session: SwarmSession, admissions: Any) -> ChangeIntent:
    operations: list[IntentOperation] = []
    preserves: list[str] = []
    for work_id in session.work_graph.topological_order():
        admission = admissions.admission_map[work_id]
        operations.extend(admission.intent.operations)
        preserves.extend(admission.intent.preserves)
    return ChangeIntent(
        intent_id=f"swarm-root:{session.session_id}:{session.graph_fingerprint[:16]}",
        task_id=f"swarm-root:{session.session_id}",
        owner=f"swarm/{session.session_id}/root-verifier",
        base_revision=session.base_commit,
        base_commit=session.base_commit,
        operations=tuple(operations),
        preserves=tuple(dict.fromkeys(preserves)),
        acceptance=session.root_task.acceptance,
        metadata={
            "swarm_session_id": session.session_id,
            "verification_scope": "integrated_root",
        },
    )


def _queue_fresh(
    queue: DeterministicMergeQueue, session: SwarmSession, admission: Any
) -> bool:
    return (
        queue.repository_identity == session.repository_identity
        and queue.base_commit == session.base_commit
        and queue.graph_version == session.graph_version
        and queue.graph_fingerprint == session.graph_fingerprint
        and queue.budget_version == session.budget_version
        and queue.budget_fingerprint == session.budget_fingerprint
        and queue.admission_fingerprint == admission.fingerprint()
    )


def verify_swarm_session(
    repo: str | Path,
    session_id: str,
    *,
    run_acceptance: bool = True,
    acceptance_timeout: int = 300,
) -> dict[str, Any]:
    from claim_plane.swarm.service import (
        _repository_identity,
        _require_initialized,
        _store,
        _validate_session_id,
        resolve_repository_root,
    )

    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    with _store(root) as store:
        session = store.require(session_id)
        shared = store.get_shared_admission(session_id)
        stored_queue = store.get_merge_queue(session_id)
    if session.repository_identity != _repository_identity(root):
        raise ValueError("swarm session is bound to a different repository identity")
    if shared is None or stored_queue is None:
        raise ValueError(
            "swarm verification requires shared admission and a merge queue"
        )
    admission = shared[0]
    queue = stored_queue[0]
    if not _queue_fresh(queue, session, admission):
        raise ValueError("merge queue is stale for swarm verification")
    if queue.status is not MergeQueueStatus.COMPLETED or any(
        entry.state is not MergeEntryState.INTEGRATED for entry in queue.entries
    ):
        raise ValueError("swarm verification requires a completed merge queue")
    integration = Path(queue.integration_worktree_path).resolve()
    if not integration.is_dir():
        raise ValueError("managed integration worktree is missing")
    if _git(integration, "rev-parse", "HEAD").lower() != queue.integration_head:
        raise ValueError("integration worktree HEAD differs from the merge queue")
    if _git(integration, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("integration worktree must be clean before verification")

    with _store(root) as store:
        store.begin_verification(
            session_id,
            expected_queue_fingerprint=queue.fingerprint(),
            updated_at=_utc_now(),
        )

    tree_before = capture_worktree_tree(integration)
    work_evidence: list[WorkVerificationEvidence] = []
    all_mutation_paths: set[str] = set()
    item_map = session.work_graph.item_map
    for entry in queue.entries:
        results: tuple[AcceptanceResult, ...] = ()
        work_mutation_paths: tuple[str, ...] = ()
        if run_acceptance and item_map[entry.work_id].acceptance:
            results, work_mutation_paths = _run_acceptance_with_integrity(
                item_map[entry.work_id].acceptance,
                integration,
                timeout_seconds=acceptance_timeout,
                baseline_tree=tree_before,
                integration_head=queue.integration_head,
            )
            all_mutation_paths.update(work_mutation_paths)
        evidence = _verify_work_item(
            integration,
            item_map[entry.work_id],
            entry,
            results,
        )
        if work_mutation_paths:
            mutation_finding = VerificationFinding(
                "snapshot_mutation",
                "Work-item acceptance mutated the integration worktree.",
                work_id=entry.work_id,
                path=work_mutation_paths[0],
            )
            evidence = replace(
                evidence,
                findings=(*evidence.findings, mutation_finding),
                verified=False,
            )
        work_evidence.append(evidence)

    root_intent = _root_intent(session, admission)
    manifest = GitChangeCollector().collect(integration, root_intent)
    root_results: tuple[AcceptanceResult, ...] = ()
    root_mutation_paths: tuple[str, ...] = ()
    if run_acceptance and root_intent.acceptance:
        root_results, root_mutation_paths = _run_acceptance_with_integrity(
            root_intent.acceptance,
            integration,
            timeout_seconds=acceptance_timeout,
            baseline_tree=tree_before,
            integration_head=queue.integration_head,
        )
        all_mutation_paths.update(root_mutation_paths)
        manifest = replace(manifest, acceptance_results=root_results)
    integrity = not all_mutation_paths
    mutation_paths = tuple(sorted(all_mutation_paths))
    manifest = replace(
        manifest,
        metadata={
            **manifest.metadata,
            "swarm_verification": True,
            "snapshot_integrity_ok": integrity,
            "acceptance_mutation_paths": list(mutation_paths),
        },
    )
    root_report_obj = IntegrationVerifier().verify(root_intent, manifest)
    root_report = root_report_obj.to_dict()

    findings: list[VerificationFinding] = []
    for evidence in work_evidence:
        findings.extend(evidence.findings)
    for raw in root_report.get("findings") or ():
        if isinstance(raw, Mapping):
            findings.append(
                VerificationFinding(
                    str(raw.get("code") or "root_verification_failed"),
                    str(raw.get("message") or "Root verification failed."),
                    str(raw.get("severity") or "error"),
                    path=(None if raw.get("path") is None else str(raw.get("path"))),
                )
            )
    if not integrity:
        findings.append(
            VerificationFinding(
                "snapshot_mutation",
                "Acceptance commands mutated the integration worktree.",
                path=mutation_paths[0] if mutation_paths else None,
            )
        )

    verified = (
        all(item.verified for item in work_evidence)
        and root_report_obj.clean
        and integrity
    )
    report = SwarmVerificationReport(
        session_id=session_id,
        repository_identity=session.repository_identity,
        base_commit=session.base_commit,
        graph_version=session.graph_version,
        graph_fingerprint=session.graph_fingerprint,
        budget_version=session.budget_version,
        budget_fingerprint=session.budget_fingerprint,
        admission_fingerprint=admission.fingerprint(),
        merge_queue_fingerprint=queue.fingerprint(),
        integration_head=queue.integration_head,
        status=(
            SwarmVerificationStatus.VERIFIED
            if verified
            else SwarmVerificationStatus.FAILED
        ),
        work_evidence=tuple(work_evidence),
        root_changed_paths=tuple(manifest.changed_files),
        root_acceptance_results=root_results,
        root_report=root_report,
        snapshot_integrity_ok=integrity,
        acceptance_mutation_paths=tuple(mutation_paths),
        findings=tuple(findings),
        created_at=_utc_now(),
        metadata={
            "run_acceptance": run_acceptance,
            "acceptance_timeout": acceptance_timeout,
            "integration_worktree": str(integration),
        },
    )
    with _store(root) as store:
        stored, version, changed = store.save_verification(
            report,
            expected_queue_fingerprint=queue.fingerprint(),
        )
    return {
        "session_id": session_id,
        "created": changed,
        "verification_version": version,
        "verification_fingerprint": stored.fingerprint(),
        "verification": stored.to_dict(),
        "summary": stored.summary(),
    }


def get_swarm_verification(repo: str | Path, session_id: str) -> dict[str, Any]:
    from claim_plane.swarm.service import (
        _repository_identity,
        _require_initialized,
        _store,
        _validate_session_id,
        resolve_repository_root,
    )

    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    with _store(root) as store:
        session = store.require(session_id)
        stored = store.get_verification(session_id)
    if session.repository_identity != _repository_identity(root):
        raise ValueError("swarm session is bound to a different repository identity")
    if stored is None:
        raise KeyError(f"swarm session {session_id!r} has no verification evidence")
    report, version = stored
    return {
        "session_id": session_id,
        "created": False,
        "verification_version": version,
        "verification_fingerprint": report.fingerprint(),
        "verification": report.to_dict(),
        "summary": report.summary(),
    }
