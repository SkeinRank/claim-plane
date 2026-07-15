"""Immutable verification, neutral integration, and bounded repair loops."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import sys
import shutil
import subprocess
import tempfile
import time
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from claim_plane.core.models import (
    AcceptanceResult,
    ObservedAccess,
    ChangeManifest,
    IntegrationReport,
    RepairAction,
    RepairActionKind,
    RepairPlan,
)
from claim_plane.integration.acceptance import AcceptanceRunner
from claim_plane.integration.observation import (
    ObservationPolicy,
    load_observation_trace,
    observation_digest,
)
from claim_plane.integration.sandbox import (
    SandboxPolicy,
    resolve_sandbox_command,
    sanitized_environment,
)
from claim_plane.integration.signing import sign_payload, sign_payload_ed25519
from claim_plane.integration.snapshot import (
    capture_worktree_tree,
    changed_worktree_paths,
    create_commit_from_tree,
    diff_objects,
    freeze_worktree,
    materialize_snapshot,
    remove_materialized_snapshot,
    sha256_bytes,
    write_patch,
)

if TYPE_CHECKING:
    from claim_plane.core.plane import Plane


@dataclass(frozen=True, slots=True)
class WorkerTarget:
    intent_id: str
    repo_path: str
    repair_command: str | None = None
    observation_trace: str | None = None
    observation_session_id: str | None = None

    def __post_init__(self) -> None:
        if not self.intent_id.strip():
            raise ValueError("worker intent_id must not be empty")
        if not self.repo_path.strip():
            raise ValueError("worker repo_path must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "repo_path": self.repo_path,
            "repair_command": self.repair_command,
            "observation_trace": self.observation_trace,
            "observation_session_id": self.observation_session_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkerTarget":
        return cls(
            intent_id=str(data["intent_id"]),
            repo_path=str(data["repo_path"]),
            repair_command=(
                str(data["repair_command"])
                if data.get("repair_command") is not None
                else None
            ),
            observation_trace=(
                str(data["observation_trace"])
                if data.get("observation_trace") is not None
                else None
            ),
            observation_session_id=(
                str(data["observation_session_id"])
                if data.get("observation_session_id") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class IntegrationRunSpec:
    run_id: str
    base_repo: str
    base_revision: str
    workers: tuple[WorkerTarget, ...]
    base_commit: str | None = None
    integration_commands: tuple[str, ...] = ()
    max_attempts: int = 1
    repair_timeout: int = 900
    integration_timeout: int = 600
    run_worker_acceptance: bool = True
    require_clean_worker_acceptance: bool = True
    require_clean_integration_commands: bool = True
    artifact_dir: str = ".claim-plane/runs"
    keep_integration_worktree: bool = False
    complete_on_success: bool = False
    result_ref: str | None = None
    worker_sandbox: SandboxPolicy = field(default_factory=SandboxPolicy)
    integration_sandbox: SandboxPolicy = field(default_factory=SandboxPolicy)
    repair_sandbox: SandboxPolicy = field(default_factory=SandboxPolicy)
    observation_policy: ObservationPolicy = field(default_factory=ObservationPolicy)
    observation_key_env: str | None = None
    broker_key_env: str | None = None
    evidence_signing_key_env: str | None = None
    evidence_signing_method: str = "hmac-sha256"
    evidence_key_id: str = "default"

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")
        if not self.base_repo.strip():
            raise ValueError("base_repo must not be empty")
        if not self.base_revision.strip():
            raise ValueError("base_revision must not be empty")
        if self.base_commit is not None:
            commit = self.base_commit.strip().lower()
            if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
                raise ValueError("base_commit must be a full hexadecimal object id")
            object.__setattr__(self, "base_commit", commit)
        object.__setattr__(
            self,
            "worker_sandbox",
            self.worker_sandbox
            if isinstance(self.worker_sandbox, SandboxPolicy)
            else SandboxPolicy.from_dict(self.worker_sandbox),
        )
        object.__setattr__(
            self,
            "integration_sandbox",
            self.integration_sandbox
            if isinstance(self.integration_sandbox, SandboxPolicy)
            else SandboxPolicy.from_dict(self.integration_sandbox),
        )
        object.__setattr__(
            self,
            "repair_sandbox",
            self.repair_sandbox
            if isinstance(self.repair_sandbox, SandboxPolicy)
            else SandboxPolicy.from_dict(self.repair_sandbox),
        )
        object.__setattr__(
            self,
            "observation_policy",
            self.observation_policy
            if isinstance(self.observation_policy, ObservationPolicy)
            else ObservationPolicy.from_dict(self.observation_policy),
        )
        if self.evidence_signing_method not in {"hmac-sha256", "ed25519"}:
            raise ValueError("unsupported evidence signing method")
        workers = tuple(
            worker
            if isinstance(worker, WorkerTarget)
            else WorkerTarget.from_dict(worker)
            for worker in self.workers
        )
        if not workers:
            raise ValueError("workers must not be empty")
        ids = [worker.intent_id for worker in workers]
        if len(ids) != len(set(ids)):
            raise ValueError("worker intent_id values must be unique")
        object.__setattr__(self, "workers", workers)
        object.__setattr__(
            self,
            "integration_commands",
            tuple(
                command.strip()
                for command in self.integration_commands
                if command.strip()
            ),
        )
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if self.repair_timeout <= 0 or self.integration_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if self.result_ref is not None:
            ref = self.result_ref.strip()
            if not ref.startswith("refs/claim-plane/"):
                raise ValueError("result_ref must be under refs/claim-plane/")
            object.__setattr__(self, "result_ref", ref)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "base_repo": self.base_repo,
            "base_revision": self.base_revision,
            "base_commit": self.base_commit,
            "workers": [worker.to_dict() for worker in self.workers],
            "integration_commands": list(self.integration_commands),
            "max_attempts": self.max_attempts,
            "repair_timeout": self.repair_timeout,
            "integration_timeout": self.integration_timeout,
            "run_worker_acceptance": self.run_worker_acceptance,
            "require_clean_worker_acceptance": self.require_clean_worker_acceptance,
            "require_clean_integration_commands": self.require_clean_integration_commands,
            "artifact_dir": self.artifact_dir,
            "keep_integration_worktree": self.keep_integration_worktree,
            "complete_on_success": self.complete_on_success,
            "result_ref": self.result_ref,
            "worker_sandbox": self.worker_sandbox.to_dict(),
            "integration_sandbox": self.integration_sandbox.to_dict(),
            "repair_sandbox": self.repair_sandbox.to_dict(),
            "observation_policy": self.observation_policy.to_dict(),
            "observation_key_env": self.observation_key_env,
            "broker_key_env": self.broker_key_env,
            "evidence_signing_key_env": self.evidence_signing_key_env,
            "evidence_signing_method": self.evidence_signing_method,
            "evidence_key_id": self.evidence_key_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "IntegrationRunSpec":
        return cls(
            run_id=str(data["run_id"]),
            base_repo=str(data["base_repo"]),
            base_revision=str(data["base_revision"]),
            base_commit=(str(data["base_commit"]) if data.get("base_commit") else None),
            workers=tuple(
                WorkerTarget.from_dict(item) for item in data.get("workers") or ()
            ),
            integration_commands=tuple(data.get("integration_commands") or ()),
            max_attempts=int(data.get("max_attempts", 1)),
            repair_timeout=int(data.get("repair_timeout", 900)),
            integration_timeout=int(data.get("integration_timeout", 600)),
            run_worker_acceptance=bool(data.get("run_worker_acceptance", True)),
            require_clean_worker_acceptance=bool(
                data.get("require_clean_worker_acceptance", True)
            ),
            require_clean_integration_commands=bool(
                data.get("require_clean_integration_commands", True)
            ),
            artifact_dir=str(data.get("artifact_dir") or ".claim-plane/runs"),
            keep_integration_worktree=bool(
                data.get("keep_integration_worktree", False)
            ),
            complete_on_success=bool(data.get("complete_on_success", False)),
            result_ref=(
                str(data["result_ref"]) if data.get("result_ref") is not None else None
            ),
            worker_sandbox=SandboxPolicy.from_dict(data.get("worker_sandbox")),
            integration_sandbox=SandboxPolicy.from_dict(
                data.get("integration_sandbox")
            ),
            repair_sandbox=SandboxPolicy.from_dict(data.get("repair_sandbox")),
            observation_policy=ObservationPolicy.from_dict(
                data.get("observation_policy")
            ),
            observation_key_env=(
                str(data["observation_key_env"])
                if data.get("observation_key_env")
                else None
            ),
            broker_key_env=(
                str(data["broker_key_env"]) if data.get("broker_key_env") else None
            ),
            evidence_signing_key_env=(
                str(data["evidence_signing_key_env"])
                if data.get("evidence_signing_key_env")
                else None
            ),
            evidence_key_id=str(data.get("evidence_key_id") or "default"),
            evidence_signing_method=str(
                data.get("evidence_signing_method") or "hmac-sha256"
            ),
        )


@dataclass(frozen=True, slots=True)
class CommandExecution:
    intent_id: str
    command: str
    returncode: int
    duration_ms: int
    stdout_tail: str = ""
    stderr_tail: str = ""
    sandbox_backend: str = "none"
    sandbox_enforced: bool = False

    @property
    def passed(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "command": self.command,
            "returncode": self.returncode,
            "passed": self.passed,
            "duration_ms": self.duration_ms,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "sandbox_backend": self.sandbox_backend,
            "sandbox_enforced": self.sandbox_enforced,
        }


@dataclass(frozen=True, slots=True)
class WorkerEvidence:
    intent_id: str
    source_repo: str
    base_commit: str
    snapshot_commit: str
    snapshot_tree: str
    patch_path: str
    patch_sha256: str
    patch_size: int
    manifest_path: str
    manifest_sha256: str
    manifest_file_sha256: str
    observation_trace_sha256: str | None
    observation_session_id: str | None
    observation_session_digest: str | None
    observation_trusted: bool
    broker_instance_id: str | None
    broker_instance_digest: str | None
    broker_policy_digest: str | None
    broker_expected_tree_hash: str | None
    acceptance_tree_before: str
    acceptance_tree_after: str
    acceptance_immutable: bool
    mutation_paths: tuple[str, ...]
    manifest: ChangeManifest = field(repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "source_repo": self.source_repo,
            "base_commit": self.base_commit,
            "snapshot_commit": self.snapshot_commit,
            "snapshot_tree": self.snapshot_tree,
            "patch_path": self.patch_path,
            "patch_sha256": self.patch_sha256,
            "patch_size": self.patch_size,
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "manifest_file_sha256": self.manifest_file_sha256,
            "observation_trace_sha256": self.observation_trace_sha256,
            "observation_session_id": self.observation_session_id,
            "observation_session_digest": self.observation_session_digest,
            "observation_trusted": self.observation_trusted,
            "broker_instance_id": self.broker_instance_id,
            "broker_instance_digest": self.broker_instance_digest,
            "broker_policy_digest": self.broker_policy_digest,
            "broker_expected_tree_hash": self.broker_expected_tree_hash,
            "acceptance_tree_before": self.acceptance_tree_before,
            "acceptance_tree_after": self.acceptance_tree_after,
            "acceptance_immutable": self.acceptance_immutable,
            "mutation_paths": list(self.mutation_paths),
        }


@dataclass(frozen=True, slots=True)
class MergeSimulation:
    applied: bool
    error: str | None = None
    failed_worker: str | None = None
    worktree_path: str | None = None
    worktree_kept: bool = False
    acceptance_results: tuple[AcceptanceResult, ...] = ()
    applied_order: tuple[str, ...] = ()
    tree_before_commands: str | None = None
    tree_after_commands: str | None = None
    commands_immutable: bool = True
    mutation_paths: tuple[str, ...] = ()
    result_tree: str | None = None
    result_commit: str | None = None
    result_ref: str | None = None
    result_patch_path: str | None = None
    result_patch_sha256: str | None = None

    @property
    def clean(self) -> bool:
        return (
            self.applied
            and self.error is None
            and self.commands_immutable
            and all(result.passed for result in self.acceptance_results)
            and self.result_tree is not None
            and self.result_commit is not None
            and self.result_patch_sha256 is not None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "clean": self.clean,
            "error": self.error,
            "failed_worker": self.failed_worker,
            "worktree_path": self.worktree_path,
            "worktree_kept": self.worktree_kept,
            "acceptance_results": [
                result.to_dict() for result in self.acceptance_results
            ],
            "applied_order": list(self.applied_order),
            "tree_before_commands": self.tree_before_commands,
            "tree_after_commands": self.tree_after_commands,
            "commands_immutable": self.commands_immutable,
            "mutation_paths": list(self.mutation_paths),
            "result_tree": self.result_tree,
            "result_commit": self.result_commit,
            "result_ref": self.result_ref,
            "result_patch_path": self.result_patch_path,
            "result_patch_sha256": self.result_patch_sha256,
        }


@dataclass(frozen=True, slots=True)
class IntegrationAttempt:
    number: int
    reports: Mapping[str, IntegrationReport]
    repair_plans: Mapping[str, RepairPlan]
    merge: MergeSimulation | None = None
    repair_executions: tuple[CommandExecution, ...] = ()
    worker_evidence: Mapping[str, WorkerEvidence] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return (
            bool(self.reports)
            and all(report.clean for report in self.reports.values())
            and self.merge is not None
            and self.merge.clean
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "clean": self.clean,
            "reports": {
                intent_id: report.to_dict()
                for intent_id, report in self.reports.items()
            },
            "repair_plans": {
                intent_id: plan.to_dict()
                for intent_id, plan in self.repair_plans.items()
            },
            "merge": self.merge.to_dict() if self.merge else None,
            "repair_executions": [item.to_dict() for item in self.repair_executions],
            "worker_evidence": {
                intent_id: evidence.to_dict()
                for intent_id, evidence in self.worker_evidence.items()
            },
        }


@dataclass(frozen=True, slots=True)
class IntegrationRunResult:
    run_id: str
    attempts: tuple[IntegrationAttempt, ...]
    stopped_reason: str
    artifact_dir: str
    evidence_path: str | None = None
    evidence_sha256: str | None = None
    evidence_file_sha256: str | None = None
    evidence_signature_path: str | None = None
    result_commit: str | None = None
    result_tree: str | None = None
    result_patch_path: str | None = None
    result_patch_sha256: str | None = None

    @property
    def clean(self) -> bool:
        return bool(self.attempts) and self.attempts[-1].clean

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": "claim-plane.integration-run.v4",
            "run_id": self.run_id,
            "clean": self.clean,
            "stopped_reason": self.stopped_reason,
            "artifact_dir": self.artifact_dir,
            "evidence_path": self.evidence_path,
            "evidence_sha256": self.evidence_sha256,
            "evidence_file_sha256": self.evidence_file_sha256,
            "evidence_signature_path": self.evidence_signature_path,
            "result_commit": self.result_commit,
            "result_tree": self.result_tree,
            "result_patch_path": self.result_patch_path,
            "result_patch_sha256": self.result_patch_sha256,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


class IntegrationRunner:
    """Freeze, verify and integrate exact worker artifacts.

    Every attempt uses one immutable patch per worker.  The manifest is
    collected from the corresponding frozen commit, acceptance runs against a
    detached snapshot, and neutral integration applies those same patch bytes.
    """

    def __init__(self, plane: "Plane") -> None:
        self._plane = plane

    def run(self, spec: IntegrationRunSpec) -> IntegrationRunResult:
        base_repo = Path(spec.base_repo).resolve()
        base_commit = self._validate_spec(spec, base_repo)
        artifact_root = Path(spec.artifact_dir)
        if not artifact_root.is_absolute():
            artifact_root = base_repo / artifact_root
        run_dir = artifact_root / spec.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(run_dir / "spec.json", spec.to_dict())

        attempts: list[IntegrationAttempt] = []
        stopped_reason = "max_attempts_exhausted"
        final_evidence_path: str | None = None
        final_evidence_sha256: str | None = None
        final_evidence_file_sha256: str | None = None
        final_evidence_signature_path: str | None = None
        final_merge: MergeSimulation | None = None

        for number in range(1, spec.max_attempts + 1):
            attempt_dir = run_dir / f"attempt-{number:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            ordered_workers = self._ordered_workers(spec)
            worker_evidence = {
                worker.intent_id: self._freeze_worker(
                    spec,
                    worker,
                    attempt_dir / "workers" / worker.intent_id,
                    base_commit=base_commit,
                )
                for worker in ordered_workers
            }
            manifests = tuple(
                worker_evidence[worker.intent_id].manifest for worker in ordered_workers
            )
            reports = self._plane.verify_batch(manifests)
            repair_plans = {
                intent_id: self._plane.repair_plan(report)
                for intent_id, report in reports.items()
                if not report.clean
            }

            merge: MergeSimulation | None = None
            if all(report.clean for report in reports.values()):
                merge = self._integrate_exact_artifacts(
                    spec,
                    attempt_dir,
                    base_commit=base_commit,
                    workers=ordered_workers,
                    evidence=worker_evidence,
                )
                if not merge.clean:
                    for worker in ordered_workers:
                        repair_plans.setdefault(
                            worker.intent_id,
                            _merge_repair_plan(worker.intent_id, merge),
                        )

            preliminary = IntegrationAttempt(
                number=number,
                reports=reports,
                repair_plans=repair_plans,
                merge=merge,
                worker_evidence=worker_evidence,
            )
            self._write_attempt(attempt_dir, preliminary)
            (
                evidence_path,
                evidence_sha256,
                evidence_file_sha256,
                evidence_signature_path,
            ) = self._write_evidence_bundle(
                spec,
                attempt_dir,
                base_commit=base_commit,
                attempt=preliminary,
            )

            if preliminary.clean:
                attempts.append(preliminary)
                stopped_reason = "clean_integration"
                final_evidence_path = evidence_path
                final_evidence_sha256 = evidence_sha256
                final_evidence_file_sha256 = evidence_file_sha256
                final_evidence_signature_path = evidence_signature_path
                final_merge = merge
                if spec.complete_on_success:
                    self._complete_successful_intents(spec)
                break

            if number >= spec.max_attempts:
                attempts.append(preliminary)
                break

            repair_executions = self._run_repairs(
                spec,
                attempt_dir,
                reports=reports,
                repair_plans=repair_plans,
                merge=merge,
                attempt_number=number,
            )
            attempt = replace(preliminary, repair_executions=repair_executions)
            self._write_attempt(attempt_dir, attempt)
            attempts.append(attempt)
            if not repair_executions:
                stopped_reason = "repair_adapter_unavailable"
                break
            if any(not execution.passed for execution in repair_executions):
                stopped_reason = "repair_command_failed"
                break

        result = IntegrationRunResult(
            run_id=spec.run_id,
            attempts=tuple(attempts),
            stopped_reason=stopped_reason,
            artifact_dir=str(run_dir),
            evidence_path=final_evidence_path,
            evidence_sha256=final_evidence_sha256,
            evidence_file_sha256=final_evidence_file_sha256,
            evidence_signature_path=final_evidence_signature_path,
            result_commit=final_merge.result_commit if final_merge else None,
            result_tree=final_merge.result_tree if final_merge else None,
            result_patch_path=final_merge.result_patch_path if final_merge else None,
            result_patch_sha256=(
                final_merge.result_patch_sha256 if final_merge else None
            ),
        )
        _write_json(run_dir / "result.json", result.to_dict())
        return result

    def _validate_spec(self, spec: IntegrationRunSpec, base_repo: Path) -> str:
        if _run(["git", "rev-parse", "--git-dir"], cwd=base_repo).returncode != 0:
            raise ValueError(f"base_repo is not a Git worktree: {base_repo}")
        if spec.base_commit is None and not re.fullmatch(
            r"[0-9a-fA-F]{40,64}", spec.base_revision
        ):
            raise ValueError(
                "integration base uses a mutable base_revision; provide an explicit base_commit pin"
            )

        base_commit = (spec.base_commit or spec.base_revision).lower()
        available = _run(
            ["git", "cat-file", "-e", f"{base_commit}^{{commit}}"], cwd=base_repo
        )
        if available.returncode != 0:
            raise ValueError(
                f"pinned integration base commit {base_commit!r} is not available in {base_repo}"
            )

        for worker in spec.workers:
            intent = self._plane.intent(worker.intent_id)
            if intent is None:
                raise KeyError(f"unknown intent: {worker.intent_id}")
            pinned_intent = intent.base_commit or (
                intent.base_revision.lower()
                if re.fullmatch(r"[0-9a-fA-F]{40,64}", intent.base_revision)
                else None
            )
            if pinned_intent != base_commit:
                raise ValueError(
                    f"{worker.intent_id} is not pinned to integration base commit {base_commit}"
                )
            worker_repo = Path(worker.repo_path).resolve()
            available = _run(
                ["git", "cat-file", "-e", f"{base_commit}^{{commit}}"], cwd=worker_repo
            )
            if available.returncode != 0:
                raise ValueError(
                    f"worker {worker.intent_id} does not contain pinned base commit {base_commit}"
                )
        return base_commit

    def _ordered_workers(self, spec: IntegrationRunSpec) -> tuple[WorkerTarget, ...]:
        graph = self._plane.dependency_graph()
        raw_order = graph.get("topological_order")
        topological_order = (
            tuple(str(item) for item in raw_order)
            if isinstance(raw_order, (list, tuple))
            else ()
        )
        graph_order: dict[str, int] = {
            intent_id: index for index, intent_id in enumerate(topological_order)
        }
        original = {
            worker.intent_id: index for index, worker in enumerate(spec.workers)
        }
        return tuple(
            sorted(
                spec.workers,
                key=lambda worker: (
                    graph_order.get(worker.intent_id, len(graph_order)),
                    original[worker.intent_id],
                ),
            )
        )

    def _freeze_worker(
        self,
        spec: IntegrationRunSpec,
        worker: WorkerTarget,
        worker_dir: Path,
        *,
        base_commit: str,
    ) -> WorkerEvidence:
        worker_dir.mkdir(parents=True, exist_ok=True)
        snapshot = freeze_worktree(
            worker.repo_path,
            base_commit,
            message=f"Claim Plane snapshot {spec.run_id}:{worker.intent_id}",
        )
        patch_path = worker_dir / "worker.patch"
        patch_sha256 = write_patch(patch_path, snapshot.patch_bytes)
        if patch_sha256 != snapshot.patch_sha256:
            raise RuntimeError("worker patch digest changed while being persisted")

        frozen = materialize_snapshot(worker.repo_path, snapshot.snapshot_commit)
        try:
            manifest = self._plane.collect_git_manifest(worker.intent_id, frozen)
            trace_items: tuple = ()
            trace_sha: str | None = None
            observation_session_id: str | None = None
            observation_session_digest: str | None = None
            observation_trusted = False
            broker_instance_id: str | None = None
            broker_instance_digest: str | None = None
            broker_policy_digest: str | None = None
            broker_expected_tree_hash: str | None = None
            policy = spec.observation_policy
            if worker.observation_session_id:
                if not spec.observation_key_env:
                    raise ValueError(
                        "observation_session_id requires observation_key_env"
                    )
                value = os.environ.get(spec.observation_key_env)
                if not value:
                    raise ValueError(
                        f"observation key environment variable {spec.observation_key_env!r} is not set"
                    )
                verified = self._plane.verify_observation_session(
                    worker.observation_session_id, key=value.encode("utf-8")
                )
                session = verified["session"]
                if session["intent_id"] != worker.intent_id:
                    raise ValueError(
                        f"observation session {worker.observation_session_id} belongs to {session['intent_id']}, not {worker.intent_id}"
                    )
                if not verified["valid"]:
                    raise ValueError(
                        "invalid trusted observation session: "
                        + "; ".join(verified["errors"])
                    )
                if policy.require_complete and not session["complete"]:
                    raise ValueError("observation session is sealed but incomplete")
                if session["coverage"] not in policy.allowed_coverages:
                    raise ValueError(
                        f"observation coverage {session['coverage']!r} is not allowed"
                    )
                if (
                    policy.mode == "brokered"
                    and session["coverage"] != "brokered_proxy"
                ):
                    raise ValueError(
                        "brokered observation policy requires brokered_proxy coverage"
                    )
                trace_items = tuple(
                    ObservedAccess.from_dict(item) for item in verified["accesses"]
                )
                if policy.mode == "brokered":
                    if not spec.broker_key_env:
                        raise ValueError(
                            "brokered observation policy requires broker_key_env"
                        )
                    broker_value = os.environ.get(spec.broker_key_env)
                    if not broker_value:
                        raise ValueError(
                            f"broker key environment variable {spec.broker_key_env!r} is not set"
                        )
                    broker_verified = self._plane.verify_broker_session(
                        worker.observation_session_id,
                        broker_key=broker_value.encode("utf-8"),
                    )
                    if not broker_verified["valid"]:
                        raise ValueError(
                            "invalid broker attestation: "
                            + "; ".join(broker_verified["errors"])
                        )
                    broker_instance = broker_verified["instance"]
                    assert isinstance(broker_instance, dict)
                    if (
                        Path(str(broker_instance["root_path"])).resolve()
                        != Path(worker.repo_path).resolve()
                    ):
                        raise ValueError(
                            "broker instance repository root does not match worker repo"
                        )
                    if str(broker_instance["base_commit"]) != base_commit:
                        raise ValueError(
                            "broker instance base commit does not match integration base"
                        )
                    if str(broker_instance["intent_id"]) != worker.intent_id:
                        raise ValueError("broker instance belongs to another intent")
                    broker_instance_id = str(broker_instance["instance_id"])
                    broker_policy_digest = str(broker_instance["policy_digest"])
                    broker_instance_digest = str(broker_verified.get("digest") or "")
                    broker_expected_tree_hash = str(
                        broker_instance.get("expected_tree_hash") or ""
                    )
                    if snapshot.tree_hash != broker_expected_tree_hash:
                        raise ValueError(
                            "worker snapshot does not match the broker-derived tree: "
                            f"expected {broker_expected_tree_hash}, got {snapshot.tree_hash}"
                        )
                    invalid_events = [
                        item
                        for item in trace_items
                        if item.metadata.get("broker_protocol")
                        != "claim-plane.broker.v2"
                        or item.metadata.get("broker_instance_id") != broker_instance_id
                        or item.metadata.get("broker_policy_digest")
                        != broker_policy_digest
                    ]
                    if invalid_events:
                        raise ValueError(
                            "brokered observation session contains unbound broker events"
                        )
                    if not trace_items:
                        raise ValueError(
                            "brokered observation session must contain at least one broker event"
                        )
                trace_sha = observation_digest(trace_items)
                observation_session_id = worker.observation_session_id
                observation_session_digest = str(verified["digest"])
                observation_trusted = True
            elif worker.observation_trace:
                if policy.mode in {"trusted", "brokered"}:
                    raise ValueError(
                        "trusted observation policy rejects editable file traces; use observation_session_id"
                    )
                trace_items = load_observation_trace(worker.observation_trace)
                trace_sha = observation_digest(trace_items) if trace_items else None
            elif policy.mode in {"required", "trusted", "brokered"}:
                raise ValueError(
                    f"observation policy {policy.mode!r} requires evidence for {worker.intent_id}"
                )
            manifest = replace(
                manifest,
                base_commit=base_commit,
                observed_accesses=trace_items,
                metadata={
                    **manifest.metadata,
                    "observation_trace_sha256": trace_sha,
                    "observation_trace_count": len(trace_items),
                    "observation_session_id": observation_session_id,
                    "observation_session_digest": observation_session_digest,
                    "observation_trusted": observation_trusted,
                    "observation_policy": policy.mode,
                    "broker_instance_id": broker_instance_id,
                    "broker_instance_digest": broker_instance_digest,
                    "broker_policy_digest": broker_policy_digest,
                    "broker_expected_tree_hash": broker_expected_tree_hash,
                },
            )
            acceptance_results: tuple[AcceptanceResult, ...] = ()
            tree_after = snapshot.tree_hash
            mutation_paths: tuple[str, ...] = ()
            immutable = True
            intent = self._plane.intent(worker.intent_id)
            if spec.run_worker_acceptance and intent is not None and intent.acceptance:
                acceptance_results = AcceptanceRunner(
                    timeout_seconds=spec.integration_timeout,
                    sandbox_policy=spec.worker_sandbox,
                ).run(intent.acceptance, frozen)
                tree_after = capture_worktree_tree(frozen)
                immutable = tree_after == snapshot.tree_hash
                if not immutable:
                    mutation_paths = _tree_diff_paths(
                        frozen, snapshot.tree_hash, tree_after
                    )
            integrity_ok = immutable or not spec.require_clean_worker_acceptance
            manifest = replace(
                manifest,
                acceptance_results=acceptance_results,
                metadata={
                    **manifest.metadata,
                    "verified_snapshot_commit": snapshot.snapshot_commit,
                    "verified_snapshot_tree": snapshot.tree_hash,
                    "verified_patch_sha256": patch_sha256,
                    "verified_patch_size": snapshot.patch_size,
                    "acceptance_executed": bool(acceptance_results),
                    "snapshot_integrity_ok": integrity_ok,
                    "snapshot_tree_after_acceptance": tree_after,
                    "acceptance_mutation_paths": list(mutation_paths),
                },
            )
        finally:
            remove_materialized_snapshot(worker.repo_path, frozen)

        manifest_payload = manifest.to_dict()
        manifest_bytes = _canonical_json_bytes(manifest_payload)
        manifest_path = worker_dir / "manifest.json"
        manifest_file_bytes = _pretty_json_bytes(manifest_payload)
        manifest_path.write_bytes(manifest_file_bytes)
        manifest_sha256 = sha256_bytes(manifest_bytes)
        manifest_file_sha256 = sha256_bytes(manifest_file_bytes)
        (worker_dir / "manifest.sha256").write_text(
            f"{manifest_file_sha256}  manifest.json\n", encoding="utf-8"
        )
        (worker_dir / "manifest.canonical.sha256").write_text(
            f"{manifest_sha256}  manifest canonical payload\n", encoding="utf-8"
        )
        (worker_dir / "worker.patch.sha256").write_text(
            f"{patch_sha256}  worker.patch\n", encoding="utf-8"
        )
        return WorkerEvidence(
            intent_id=worker.intent_id,
            source_repo=snapshot.source_repo,
            base_commit=snapshot.base_commit,
            snapshot_commit=snapshot.snapshot_commit,
            snapshot_tree=snapshot.tree_hash,
            patch_path=str(patch_path),
            patch_sha256=patch_sha256,
            patch_size=snapshot.patch_size,
            manifest_path=str(manifest_path),
            manifest_sha256=manifest_sha256,
            manifest_file_sha256=manifest_file_sha256,
            observation_trace_sha256=(
                manifest.metadata.get("observation_trace_sha256") or None
            ),
            observation_session_id=(
                manifest.metadata.get("observation_session_id") or None
            ),
            observation_session_digest=(
                manifest.metadata.get("observation_session_digest") or None
            ),
            observation_trusted=bool(
                manifest.metadata.get("observation_trusted", False)
            ),
            broker_instance_id=(manifest.metadata.get("broker_instance_id") or None),
            broker_instance_digest=(
                manifest.metadata.get("broker_instance_digest") or None
            ),
            broker_policy_digest=(
                manifest.metadata.get("broker_policy_digest") or None
            ),
            broker_expected_tree_hash=(
                manifest.metadata.get("broker_expected_tree_hash") or None
            ),
            acceptance_tree_before=snapshot.tree_hash,
            acceptance_tree_after=tree_after,
            acceptance_immutable=integrity_ok,
            mutation_paths=mutation_paths,
            manifest=manifest,
        )

    def _integrate_exact_artifacts(
        self,
        spec: IntegrationRunSpec,
        attempt_dir: Path,
        *,
        base_commit: str,
        workers: tuple[WorkerTarget, ...],
        evidence: Mapping[str, WorkerEvidence],
    ) -> MergeSimulation:
        base_repo = Path(spec.base_repo).resolve()
        temp_root = Path(
            tempfile.mkdtemp(prefix="claim-plane-integration-", dir=base_repo.parent)
        )
        worktree = temp_root / "worktree"
        added = False
        applied_order: list[str] = []
        try:
            result = _run(
                ["git", "worktree", "add", "--detach", str(worktree), base_commit],
                cwd=base_repo,
            )
            if result.returncode != 0:
                return MergeSimulation(
                    False,
                    error=result.stderr.strip() or result.stdout.strip(),
                )
            added = True

            for worker in workers:
                frozen = evidence[worker.intent_id]
                patch = Path(frozen.patch_path).read_bytes()
                if sha256_bytes(patch) != frozen.patch_sha256:
                    return MergeSimulation(
                        False,
                        error="persisted worker patch failed SHA-256 verification",
                        failed_worker=worker.intent_id,
                        worktree_path=str(worktree),
                        worktree_kept=spec.keep_integration_worktree,
                        applied_order=tuple(applied_order),
                    )
                if not patch:
                    applied_order.append(worker.intent_id)
                    continue
                applied = subprocess.run(
                    [
                        "git",
                        "apply",
                        "--3way",
                        "--index",
                        "--whitespace=nowarn",
                        "-",
                    ],
                    cwd=worktree,
                    input=patch,
                    capture_output=True,
                    check=False,
                )
                if applied.returncode != 0:
                    apply_error = applied.stderr.decode(
                        "utf-8", errors="replace"
                    ).strip()
                    return MergeSimulation(
                        False,
                        error=apply_error or "git apply --3way failed",
                        failed_worker=worker.intent_id,
                        worktree_path=str(worktree),
                        worktree_kept=spec.keep_integration_worktree,
                        applied_order=tuple(applied_order),
                    )
                applied_order.append(worker.intent_id)

            result_tree = _git_text(worktree, "write-tree").strip()
            acceptance = AcceptanceRunner(
                timeout_seconds=spec.integration_timeout,
                sandbox_policy=spec.integration_sandbox,
            ).run(spec.integration_commands, worktree)
            tree_after = capture_worktree_tree(worktree)
            immutable = tree_after == result_tree
            mutation_paths = (
                () if immutable else _tree_diff_paths(worktree, result_tree, tree_after)
            )
            commands_immutable = (
                immutable or not spec.require_clean_integration_commands
            )
            command_failure = not all(item.passed for item in acceptance)
            error: str | None
            if command_failure:
                error = "integration acceptance failed"
            elif not commands_immutable:
                error = (
                    "integration commands mutated the verified result: "
                    + ", ".join(mutation_paths or ("unknown paths",))
                )
            else:
                error = None

            result_commit: str | None = None
            result_patch_path: str | None = None
            result_patch_sha256: str | None = None
            result_ref: str | None = None
            if error is None:
                result_commit = create_commit_from_tree(
                    base_repo,
                    result_tree,
                    base_commit,
                    message=f"Claim Plane verified integration {spec.run_id}",
                )
                result_patch = diff_objects(base_repo, base_commit, result_commit)
                path = attempt_dir / "result.patch"
                result_patch_sha256 = write_patch(path, result_patch)
                result_patch_path = str(path)
                (attempt_dir / "result.patch.sha256").write_text(
                    f"{result_patch_sha256}  result.patch\n", encoding="utf-8"
                )
                if spec.result_ref:
                    updated = _run(
                        ["git", "update-ref", spec.result_ref, result_commit],
                        cwd=base_repo,
                    )
                    if updated.returncode != 0:
                        error = updated.stderr.strip() or "could not update result_ref"
                    else:
                        result_ref = spec.result_ref

            return MergeSimulation(
                True,
                error=error,
                worktree_path=str(worktree),
                worktree_kept=spec.keep_integration_worktree,
                acceptance_results=acceptance,
                applied_order=tuple(applied_order),
                tree_before_commands=result_tree,
                tree_after_commands=tree_after,
                commands_immutable=commands_immutable,
                mutation_paths=mutation_paths,
                result_tree=result_tree if error is None else None,
                result_commit=result_commit if error is None else None,
                result_ref=result_ref if error is None else None,
                result_patch_path=result_patch_path if error is None else None,
                result_patch_sha256=(result_patch_sha256 if error is None else None),
            )
        finally:
            if added and not spec.keep_integration_worktree:
                removed = _run(
                    ["git", "worktree", "remove", "--force", str(worktree)],
                    cwd=base_repo,
                )
                if removed.returncode != 0:
                    shutil.rmtree(worktree, ignore_errors=True)
                    _run(["git", "worktree", "prune"], cwd=base_repo)
                shutil.rmtree(temp_root, ignore_errors=True)
            elif not added:
                shutil.rmtree(temp_root, ignore_errors=True)

    def _write_evidence_bundle(
        self,
        spec: IntegrationRunSpec,
        attempt_dir: Path,
        *,
        base_commit: str,
        attempt: IntegrationAttempt,
    ) -> tuple[str, str, str, str | None]:
        base_tree = _git_text(
            Path(spec.base_repo).resolve(), "rev-parse", f"{base_commit}^{{tree}}"
        ).strip()
        payload = {
            "protocol": "claim-plane.verified-evidence.v4",
            "run_id": spec.run_id,
            "attempt": attempt.number,
            "base_revision": spec.base_revision,
            "base_commit": base_commit,
            "base_tree": base_tree,
            "spec_sha256": sha256_bytes(_canonical_json_bytes(spec.to_dict())),
            "worker_order": (
                list(attempt.merge.applied_order)
                if attempt.merge is not None
                else list(attempt.worker_evidence)
            ),
            "workers": {
                intent_id: item.to_dict()
                for intent_id, item in attempt.worker_evidence.items()
            },
            "reports": {
                intent_id: report.to_dict()
                for intent_id, report in attempt.reports.items()
            },
            "merge": attempt.merge.to_dict() if attempt.merge else None,
            "clean": attempt.clean,
            "provenance": {
                "claim_plane_version": _package_version(),
                "claim_plane_source_sha256": _source_bundle_sha256(),
                "schema_bundle_sha256": _schema_bundle_sha256(),
                "policy_bundle_sha256": sha256_bytes(
                    _canonical_json_bytes(
                        {
                            "worker_sandbox": spec.worker_sandbox.to_dict(),
                            "integration_sandbox": spec.integration_sandbox.to_dict(),
                            "repair_sandbox": spec.repair_sandbox.to_dict(),
                            "observation_policy": spec.observation_policy.to_dict(),
                        }
                    )
                ),
                "python_version": platform.python_version(),
                "python_implementation": platform.python_implementation(),
                "platform": platform.platform(),
                "executable_sha256": _path_sha256(Path(sys.executable)),
                "worker_sandbox": spec.worker_sandbox.to_dict(),
                "integration_sandbox": spec.integration_sandbox.to_dict(),
                "repair_sandbox": spec.repair_sandbox.to_dict(),
                "observation_policy": spec.observation_policy.to_dict(),
                "evidence_signing_method": spec.evidence_signing_method,
            },
        }
        path = attempt_dir / "evidence.json"
        content = _pretty_json_bytes(payload)
        path.write_bytes(content)
        digest = sha256_bytes(_canonical_json_bytes(payload))
        file_digest = sha256_bytes(content)
        (attempt_dir / "evidence.sha256").write_text(
            f"{file_digest}  evidence.json\n", encoding="utf-8"
        )
        (attempt_dir / "evidence.canonical.sha256").write_text(
            f"{digest}  evidence canonical payload\n", encoding="utf-8"
        )
        signature_path: str | None = None
        if spec.evidence_signing_key_env:
            value = os.environ.get(spec.evidence_signing_key_env)
            if not value:
                raise RuntimeError(
                    f"evidence signing key environment variable {spec.evidence_signing_key_env!r} is not set"
                )
            if spec.evidence_signing_method == "ed25519":
                signature = sign_payload_ed25519(
                    payload,
                    private_key_pem=value.encode("utf-8"),
                    key_id=spec.evidence_key_id,
                )
            else:
                signature = sign_payload(
                    payload, key=value.encode("utf-8"), key_id=spec.evidence_key_id
                )
            target = attempt_dir / "evidence.sig.json"
            _write_json(target, signature)
            signature_path = str(target)
        return str(path), digest, file_digest, signature_path

    def _run_repairs(
        self,
        spec: IntegrationRunSpec,
        attempt_dir: Path,
        *,
        reports: Mapping[str, IntegrationReport],
        repair_plans: Mapping[str, RepairPlan],
        merge: MergeSimulation | None,
        attempt_number: int,
    ) -> tuple[CommandExecution, ...]:
        executions: list[CommandExecution] = []
        for worker in spec.workers:
            plan = repair_plans.get(worker.intent_id)
            if plan is None or not worker.repair_command:
                continue
            report_path = attempt_dir / f"{worker.intent_id}.report.json"
            plan_path = attempt_dir / f"{worker.intent_id}.repair.json"
            report = reports.get(worker.intent_id)
            _write_json(report_path, report.to_dict() if report else {})
            _write_json(plan_path, plan.to_dict())
            values: dict[str, str | int] = {
                "intent_id": worker.intent_id,
                "repo": str(Path(worker.repo_path).resolve()),
                "attempt": attempt_number,
                "report": str(report_path),
                "repair_plan": str(plan_path),
                "merge_error": merge.error if merge and merge.error else "",
            }
            command = worker.repair_command.format(**values)
            extra_env: dict[str, str] = {
                "CLAIM_PLANE_INTENT_ID": worker.intent_id,
                "CLAIM_PLANE_REPO": str(values["repo"]),
                "CLAIM_PLANE_ATTEMPT": str(attempt_number),
                "CLAIM_PLANE_REPORT": str(report_path),
                "CLAIM_PLANE_REPAIR_PLAN": str(plan_path),
                "CLAIM_PLANE_MERGE_ERROR": str(values["merge_error"]),
            }
            started = time.monotonic()
            backend = "unavailable"
            enforced = False
            try:
                sandbox = resolve_sandbox_command(
                    command, Path(worker.repo_path).resolve(), spec.repair_sandbox
                )
                backend = sandbox.backend
                enforced = sandbox.enforced
                completed = subprocess.run(
                    sandbox.argv,
                    cwd=Path(worker.repo_path).resolve(),
                    text=True,
                    capture_output=True,
                    timeout=spec.repair_timeout,
                    env=sanitized_environment(
                        extra_env,
                        allowlist=spec.repair_sandbox.environment_allowlist,
                    ),
                    check=False,
                )
                returncode = completed.returncode
                stdout = completed.stdout
                stderr = completed.stderr
            except RuntimeError as exc:
                returncode = 125
                stdout = ""
                stderr = str(exc)
            except subprocess.TimeoutExpired as exc:
                returncode = 124
                stdout = _text(exc.stdout)
                stderr = _text(exc.stderr) + f"\nTimed out after {spec.repair_timeout}s"
            executions.append(
                CommandExecution(
                    intent_id=worker.intent_id,
                    command=command,
                    returncode=returncode,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    stdout_tail=stdout[-4000:],
                    stderr_tail=stderr[-4000:],
                    sandbox_backend=backend,
                    sandbox_enforced=enforced,
                )
            )
        return tuple(executions)

    @staticmethod
    def _write_attempt(path: Path, attempt: IntegrationAttempt) -> None:
        _write_json(path / "attempt.json", attempt.to_dict())

    def _complete_successful_intents(self, spec: IntegrationRunSpec) -> None:
        records = {record["intent_id"]: record for record in self._plane.intents()}
        for worker in spec.workers:
            record = records.get(worker.intent_id)
            if record and record["state"] in {"admitted", "active"}:
                self._plane.complete(worker.intent_id)


def _package_version() -> str:
    try:
        return importlib.metadata.version("claim-plane")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.1+source"


def _path_sha256(path: Path) -> str | None:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError:
        return None


def _directory_bundle_sha256(root: Path, patterns: tuple[str, ...]) -> str | None:
    if not root.exists():
        return None
    digest = hashlib.sha256()
    files: list[Path] = []
    for pattern in patterns:
        files.extend(path for path in root.rglob(pattern) if path.is_file())
    unique = sorted(set(files), key=lambda item: item.relative_to(root).as_posix())
    if not unique:
        return None
    for path in unique:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _source_bundle_sha256() -> str | None:
    package_root = Path(__file__).resolve().parents[1]
    return _directory_bundle_sha256(package_root, ("*.py", "py.typed"))


def _schema_bundle_sha256() -> str | None:
    project_root = Path(__file__).resolve().parents[3]
    return _directory_bundle_sha256(project_root / "schemas", ("*.json",))


def _merge_repair_plan(intent_id: str, merge: MergeSimulation) -> RepairPlan:
    message = merge.error or "Verified integration could not compose worker patches."
    if merge.applied:
        action = RepairAction(
            RepairActionKind.RERUN_ACCEPTANCE,
            f"Repair the integrated behavior and rerun the integration commands: {message}",
            priority=1,
        )
    else:
        action = RepairAction(
            RepairActionKind.SERIALIZE,
            (
                "Repair the cross-worktree integration conflict without expanding "
                f"the declared intent. Integration error: {message}"
            ),
            related_intents=(merge.failed_worker,) if merge.failed_worker else (),
            priority=1,
        )
    return RepairPlan(
        intent_id,
        (action,),
        rerun_checks=("claim-plane integrate <spec.json>",),
    )


def _tree_diff_paths(repo: Path, left_tree: str, right_tree: str) -> tuple[str, ...]:
    completed = _run(
        ["git", "diff", "--name-only", left_tree, right_tree, "--"], cwd=repo
    )
    if completed.returncode == 0:
        paths = tuple(
            line.strip() for line in completed.stdout.splitlines() if line.strip()
        )
        if paths:
            return paths
    return changed_worktree_paths(repo)


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def _git_text(repo: Path, *args: str) -> str:
    completed = _run(["git", *args], cwd=repo)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"git {' '.join(args)} failed")
    return completed.stdout


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _pretty_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_pretty_json_bytes(payload))


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
