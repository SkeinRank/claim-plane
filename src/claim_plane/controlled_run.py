"""One-command controlled execution for coding-agent adapters.

The controlled runner owns preflight, process lifetime, cancellation, final Git
binding, and durable run state. Runtime-specific lifecycle events still cross the
versioned adapter protocol; the runner never grants mutation authority directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import IO, Any, Callable, Mapping, Protocol, TextIO

from claim_plane.console import ConsoleRenderer
from claim_plane.determinism import build_determinism_record
from claim_plane.exit_codes import ExitCode
from claim_plane.policy import EffectivePolicy, PolicyAction, resolve_policy
from claim_plane.project import load_project_config, resolve_project_root
from claim_plane.protocol import (
    AdapterHandshake,
    AdapterOperation,
    AdapterRequest,
    AgentAdapter,
    LifecycleEventStore,
    require_adapter_policy,
)

CONTROLLED_RUN_PROTOCOL = "claim-plane.controlled-run.v1"
CONTROLLED_RUN_ENV = "CLAIM_PLANE_CONTROLLED_RUN_ID"
CONTROLLED_POLICY_ENV = "CLAIM_PLANE_CONTROLLED_POLICY"
CONTROLLED_POLICY_MANIFEST_ENV = "CLAIM_PLANE_CONTROLLED_POLICY_MANIFEST"
CONTROLLED_INITIAL_SCOPE_ENV = "CLAIM_PLANE_CONTROLLED_INITIAL_SCOPE"
CONTROLLED_SCOPE_LOCK_ENV = "CLAIM_PLANE_CONTROLLED_SCOPE_LOCK"
CONTROLLED_INTERACTIVE_ENV = "CLAIM_PLANE_CONTROLLED_INTERACTIVE"
CONTROLLED_RUNS_PATH = Path(".claim-plane/runs")


class ControlledRunOutcome(str, Enum):
    """Terminal state exposed by the one-command runner."""

    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    FAILED = "FAILED"


class ControlledRunError(RuntimeError):
    """Base controlled-run failure."""


class ControlledRunPreflightError(ControlledRunError):
    """The run could not start without weakening configured guarantees."""


class ProcessLike(Protocol):
    pid: int
    stdout: IO[str] | None
    stderr: IO[str] | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


@dataclass(frozen=True, slots=True)
class GitState:
    """Digest-bound projection of one repository worktree state."""

    head_commit: str
    head_tree: str
    branch: str
    status_sha256: str
    diff_sha256: str
    untracked: Mapping[str, str]
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "head_commit": self.head_commit,
            "head_tree": self.head_tree,
            "branch": self.branch,
            "status_sha256": self.status_sha256,
            "diff_sha256": self.diff_sha256,
            "untracked": dict(self.untracked),
            "digest": self.digest,
        }


@dataclass(slots=True)
class RuntimeSummary:
    """Secret-safe aggregate of a Codex JSONL stream."""

    session_id: str | None = None
    event_counts: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    final_message: str | None = None
    final_message_sha256: str | None = None
    final_message_length: int = 0
    usage: dict[str, int | float] = field(default_factory=dict)

    def observe(self, payload: Mapping[str, Any]) -> None:
        event_type = str(payload.get("type") or "unknown")
        self.event_counts[event_type] = self.event_counts.get(event_type, 0) + 1
        if event_type == "thread.started":
            thread_id = payload.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                self.session_id = thread_id.strip()
        if event_type in {"error", "turn.failed"}:
            self.errors += 1
        item = payload.get("item")
        if (
            event_type == "item.completed"
            and isinstance(item, Mapping)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            self.final_message = str(item["text"])
        usage = payload.get("usage")
        if isinstance(usage, Mapping):
            for key, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.usage[str(key)] = value

    def seal(self) -> None:
        if self.final_message is None:
            return
        encoded = self.final_message.encode("utf-8")
        self.final_message_sha256 = hashlib.sha256(encoded).hexdigest()
        self.final_message_length = len(self.final_message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "event_counts": dict(sorted(self.event_counts.items())),
            "errors": self.errors,
            "final_message_sha256": self.final_message_sha256,
            "final_message_length": self.final_message_length,
            "usage": dict(sorted(self.usage.items())),
        }


@dataclass(frozen=True, slots=True)
class ControlledRunResult:
    """Durable result of one bounded agent execution."""

    run_id: str
    adapter: str
    policy: str
    root: str
    started_at: str
    finished_at: str
    outcome: ControlledRunOutcome
    exit_code: int
    runtime_returncode: int | None
    task_sha256: str
    task_length: int
    session_id: str | None
    intent_id: str | None
    intent_version: int | None
    start_git: GitState
    result_git: GitState
    manifest_digest: str
    handshake: Mapping[str, Any]
    policy_compatibility: Mapping[str, Any]
    effective_policy: Mapping[str, Any]
    risk: Mapping[str, Any]
    runtime: Mapping[str, Any]
    completion: Mapping[str, Any]
    changes: Mapping[str, Any]
    acceptance: Mapping[str, Any]
    scope: Mapping[str, Any]
    lifecycle: Mapping[str, Any] | None
    determinism: Mapping[str, Any]
    cancellation: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    protocol: str = CONTROLLED_RUN_PROTOCOL

    @property
    def verified(self) -> bool:
        return self.outcome is ControlledRunOutcome.VERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "run_id": self.run_id,
            "adapter": self.adapter,
            "policy": self.policy,
            "root": self.root,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "outcome": self.outcome.value,
            "verified": self.verified,
            "exit_code": self.exit_code,
            "runtime_returncode": self.runtime_returncode,
            "task_sha256": self.task_sha256,
            "task_length": self.task_length,
            "session_id": self.session_id,
            "intent_id": self.intent_id,
            "intent_version": self.intent_version,
            "start_git": self.start_git.to_dict(),
            "result_git": self.result_git.to_dict(),
            "manifest_digest": self.manifest_digest,
            "handshake": dict(self.handshake),
            "policy_compatibility": dict(self.policy_compatibility),
            "effective_policy": dict(self.effective_policy),
            "risk": dict(self.risk),
            "runtime": dict(self.runtime),
            "completion": dict(self.completion),
            "changes": dict(self.changes),
            "acceptance": dict(self.acceptance),
            "scope": dict(self.scope),
            "lifecycle": dict(self.lifecycle) if self.lifecycle is not None else None,
            "determinism": dict(self.determinism),
            "cancellation": (
                dict(self.cancellation) if self.cancellation is not None else None
            ),
            "error": dict(self.error) if self.error is not None else None,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def controlled_run_path(root: str | Path, run_id: str) -> Path:
    """Return the private durable result path for one controlled run."""

    return Path(root) / CONTROLLED_RUNS_PATH / run_id / "run.json"


def load_controlled_run(root: str | Path, run_id: str) -> dict[str, Any]:
    """Load one persisted run result and validate its basic identity."""

    path = controlled_run_path(root, run_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ControlledRunError(f"{path} must contain a JSON object")
    if payload.get("protocol") != CONTROLLED_RUN_PROTOCOL:
        raise ControlledRunError(f"unsupported controlled run protocol in {path}")
    if payload.get("run_id") != run_id:
        raise ControlledRunError(f"controlled run identity mismatch in {path}")
    return payload


def _git(root: Path, *args: str, binary: bool = False) -> str | bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=not binary,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr
        stdout = completed.stdout
        if isinstance(stderr, bytes):
            detail = stderr.decode("utf-8", errors="replace").strip()
        else:
            detail = stderr.strip()
        if not detail:
            if isinstance(stdout, bytes):
                detail = stdout.decode("utf-8", errors="replace").strip()
            else:
                detail = stdout.strip()
        raise ControlledRunError(detail or "git command failed")
    return completed.stdout


def _untracked_digest(root: Path) -> dict[str, str]:
    raw = _git(root, "ls-files", "--others", "--exclude-standard", "-z", binary=True)
    assert isinstance(raw, bytes)
    result: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        relative = item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        path = root / relative
        try:
            if path.is_symlink():
                digest = hashlib.sha256(os.readlink(path).encode("utf-8")).hexdigest()
                result[relative] = f"symlink:{digest}"
            elif path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                result[relative] = f"file:{digest}"
            elif path.is_dir():
                result[relative] = "dir"
            else:
                result[relative] = "other"
        except OSError as exc:
            result[relative] = (
                "error:" + hashlib.sha256(str(exc).encode("utf-8")).hexdigest()
            )
    return dict(sorted(result.items()))


def capture_git_state(root_or_child: str | Path) -> GitState:
    """Capture one deterministic worktree binding without mutating Git state."""

    root = resolve_project_root(root_or_child)
    head_commit = str(_git(root, "rev-parse", "--verify", "HEAD^{commit}")).strip()
    head_tree = str(_git(root, "rev-parse", "--verify", "HEAD^{tree}")).strip()
    branch = str(_git(root, "rev-parse", "--abbrev-ref", "HEAD")).strip() or "HEAD"
    status = _git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "-z",
        binary=True,
    )
    diff = _git(root, "diff", "--binary", "HEAD", "--", binary=True)
    assert isinstance(status, bytes)
    assert isinstance(diff, bytes)
    untracked = _untracked_digest(root)
    unsigned = {
        "head_commit": head_commit,
        "head_tree": head_tree,
        "branch": branch,
        "status_sha256": hashlib.sha256(status).hexdigest(),
        "diff_sha256": hashlib.sha256(diff).hexdigest(),
        "untracked": untracked,
    }
    digest = hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest()
    return GitState(
        head_commit=head_commit,
        head_tree=head_tree,
        branch=branch,
        status_sha256=hashlib.sha256(status).hexdigest(),
        diff_sha256=hashlib.sha256(diff).hexdigest(),
        untracked=untracked,
        digest=digest,
    )


def _path_fingerprint(path: Path) -> str:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return "missing"
    if path.is_symlink():
        target = os.readlink(path).encode("utf-8")
        return "symlink:" + hashlib.sha256(target).hexdigest()
    if path.is_file():
        return "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if path.is_dir():
        return "dir"
    return f"other:{stat.st_mode}"


def _dirty_worktree_baseline(root: Path) -> dict[str, str]:
    """Bind user-authored worktree state that predates a controlled run."""

    tracked = subprocess.run(
        ["git", "diff", "--name-only", "-z", "HEAD", "--"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if tracked.returncode != 0:
        detail = tracked.stderr.decode("utf-8", errors="replace").strip()
        raise ControlledRunError(detail or "could not inspect tracked worktree changes")
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if untracked.returncode != 0:
        detail = untracked.stderr.decode("utf-8", errors="replace").strip()
        raise ControlledRunError(
            detail or "could not inspect untracked worktree changes"
        )

    baseline: dict[str, str] = {}
    for raw in tracked.stdout.split(b"\0") + untracked.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        # Claim Plane local evidence is excluded by Git; project-local connector files
        # can remain visible but are ignored when their exact fingerprint is unchanged.
        baseline[relative] = _path_fingerprint(root / relative)
    return dict(sorted(baseline.items()))


def _unchanged_from_baseline(
    root: Path, relative: str, baseline: Mapping[str, str]
) -> bool:
    expected = baseline.get(relative)
    return expected is not None and _path_fingerprint(root / relative) == expected


def _request(
    operation: AdapterOperation,
    *,
    adapter: str,
    root: Path,
    run_id: str,
    session_id: str | None = None,
    intent_id: str | None = None,
    intent_version: int | None = None,
    timeout_seconds: float = 30.0,
    payload: Mapping[str, Any] | None = None,
) -> AdapterRequest:
    material = _canonical_json(
        {
            "operation": operation.value,
            "adapter": adapter,
            "run_id": run_id,
            "session_id": session_id,
            "intent_id": intent_id,
            "intent_version": intent_version,
            "payload": dict(payload or {}),
        }
    )
    request_id = "controlled-" + hashlib.sha256(material.encode("utf-8")).hexdigest()
    return AdapterRequest.create(
        operation,
        adapter=adapter,
        project_root=str(root),
        request_id=request_id,
        session_id=session_id,
        run_id=run_id,
        intent_id=intent_id,
        intent_version=intent_version,
        timeout_seconds=timeout_seconds,
        payload=payload,
    )


def _doctor_ready(
    adapter: AgentAdapter, *, root: Path, run_id: str
) -> Mapping[str, Any]:
    response = adapter.doctor(
        _request(
            AdapterOperation.DOCTOR,
            adapter=adapter.name,
            root=root,
            run_id=run_id,
        )
    )
    payload = dict(response.payload)
    if not payload.get("ready"):
        failures = [
            str(item.get("name"))
            for item in payload.get("checks") or ()
            if isinstance(item, Mapping) and item.get("status") == "error"
        ]
        detail = ", ".join(failures) or "adapter doctor reported action required"
        raise ControlledRunPreflightError(f"adapter is not ready: {detail}")
    return payload


def _configured_policy(
    root: Path, adapter: str, explicit: str | None
) -> EffectivePolicy:
    config = load_project_config(root)
    adapters = config.get("adapters")
    settings = adapters.get(adapter) if isinstance(adapters, Mapping) else None
    if not isinstance(settings, Mapping) or not settings.get("enabled"):
        raise ControlledRunPreflightError(
            f"adapter {adapter!r} is not enrolled; run 'claim-plane connect {adapter}'"
        )
    selected = str(explicit or settings.get("policy") or "guarded")
    risk = config.get("risk")
    try:
        return resolve_policy(
            selected,
            risk=risk if isinstance(risk, Mapping) else None,
            source="command_line" if explicit is not None else "project_config",
            metadata={"adapter": adapter},
        )
    except (TypeError, ValueError) as exc:
        raise ControlledRunPreflightError(f"invalid effective policy: {exc}") from exc


def _changed_paths(
    root: Path,
    start_commit: str,
    result_git: GitState,
    *,
    preexisting: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "diff", "--name-only", "-z", start_commit, "--"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ControlledRunError(detail or "could not classify changed paths")
    tracked = {
        item.decode("utf-8", errors="surrogateescape")
        for item in completed.stdout.split(b"\0")
        if item
    }
    candidates = tracked | set(result_git.untracked)
    baseline = preexisting or {}
    return tuple(
        sorted(
            path
            for path in candidates
            if not _unchanged_from_baseline(root, path, baseline)
        )
    )


_HUNK_RE = re.compile(
    rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@",
    re.MULTILINE,
)


def _change_summary(
    root: Path,
    start_git: GitState,
    result_git: GitState,
    *,
    preexisting: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Capture final file and hunk metadata without storing source content."""

    base_commit = start_git.head_commit
    status_process = subprocess.run(
        [
            "git",
            "diff",
            "--name-status",
            "--no-renames",
            "-z",
            base_commit,
            "--",
        ],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if status_process.returncode != 0:
        detail = status_process.stderr.decode("utf-8", errors="replace").strip()
        raise ControlledRunError(detail or "could not summarize changed files")
    status_parts = status_process.stdout.split(b"\0")
    statuses: dict[str, str] = {}
    index = 0
    while index + 1 < len(status_parts):
        raw_status = status_parts[index]
        raw_path = status_parts[index + 1]
        index += 2
        if not raw_status or not raw_path:
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        statuses[path] = raw_status.decode("ascii", errors="replace")[:1] or "M"

    numstat_process = subprocess.run(
        [
            "git",
            "diff",
            "--numstat",
            "--no-renames",
            "-z",
            base_commit,
            "--",
        ],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if numstat_process.returncode != 0:
        detail = numstat_process.stderr.decode("utf-8", errors="replace").strip()
        raise ControlledRunError(detail or "could not summarize diff statistics")
    stats: dict[str, tuple[int | None, int | None]] = {}
    for raw in numstat_process.stdout.split(b"\0"):
        if not raw:
            continue
        parts = raw.split(b"\t", 2)
        if len(parts) != 3:
            continue
        added_raw, deleted_raw, path_raw = parts
        path = path_raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        added = None if added_raw == b"-" else int(added_raw)
        deleted = None if deleted_raw == b"-" else int(deleted_raw)
        stats[path] = (added, deleted)

    files: list[dict[str, Any]] = []
    baseline = preexisting or {}
    tracked_paths = sorted(
        path
        for path in (set(statuses) | set(stats))
        if not _unchanged_from_baseline(root, path, baseline)
    )
    for relative in tracked_paths:
        patch_process = subprocess.run(
            [
                "git",
                "diff",
                "--binary",
                "--no-ext-diff",
                "--no-renames",
                "--unified=0",
                base_commit,
                "--",
                relative,
            ],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if patch_process.returncode != 0:
            detail = patch_process.stderr.decode("utf-8", errors="replace").strip()
            raise ControlledRunError(detail or f"could not summarize {relative}")
        hunks = []
        for match in _HUNK_RE.finditer(patch_process.stdout):
            hunks.append(
                {
                    "old_start": int(match.group(1)),
                    "old_lines": int(match.group(2) or b"1"),
                    "new_start": int(match.group(3)),
                    "new_lines": int(match.group(4) or b"1"),
                }
            )
        added, deleted = stats.get(relative, (0, 0))
        files.append(
            {
                "path": relative,
                "status": statuses.get(relative, "M"),
                "additions": added,
                "deletions": deleted,
                "binary": added is None or deleted is None,
                "patch_sha256": hashlib.sha256(patch_process.stdout).hexdigest(),
                "hunks": hunks,
            }
        )

    changed_untracked = {
        relative_path: descriptor
        for relative_path, descriptor in result_git.untracked.items()
        if start_git.untracked.get(relative_path) != descriptor
        and not _unchanged_from_baseline(root, relative_path, baseline)
    }
    for relative, descriptor in sorted(changed_untracked.items()):
        untracked_path = root / relative
        additions: int | None = None
        binary = True
        if untracked_path.is_file():
            try:
                data = untracked_path.read_bytes()
            except OSError:
                data = b""
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                pass
            else:
                binary = False
                additions = len(text.splitlines())
        files.append(
            {
                "path": relative,
                "status": "A",
                "additions": additions,
                "deletions": 0 if additions is not None else None,
                "binary": binary,
                "patch_sha256": descriptor.split(":", 1)[-1],
                "hunks": [],
            }
        )
    files.sort(key=lambda item: str(item["path"]))
    total_additions = sum(
        int(item["additions"]) for item in files if item["additions"] is not None
    )
    total_deletions = sum(
        int(item["deletions"]) for item in files if item["deletions"] is not None
    )
    total_hunks = sum(len(item["hunks"]) for item in files)
    unsigned = {
        "protocol": "claim-plane.change-summary.v1",
        "available": True,
        "base_commit": base_commit,
        "result_commit": result_git.head_commit,
        "file_count": len(files),
        "total_additions": total_additions,
        "total_deletions": total_deletions,
        "total_hunks": total_hunks,
        "files": files,
    }
    return {
        **unsigned,
        "digest": hashlib.sha256(_canonical_json(unsigned).encode("utf-8")).hexdigest(),
    }


_OSS_PILOT_ACCEPTANCE_RESULT_MARKER = "CLAIM_PLANE_OSS_ACCEPTANCE_RESULT="


def _oss_pilot_acceptance_metadata(result: Mapping[str, Any]) -> dict[str, Any] | None:
    for stream_field in ("stdout_tail", "stderr_tail"):
        text = result.get(stream_field)
        if not isinstance(text, str):
            continue
        for line in reversed(text.splitlines()):
            if not line.startswith(_OSS_PILOT_ACCEPTANCE_RESULT_MARKER):
                continue
            try:
                payload = json.loads(line[len(_OSS_PILOT_ACCEPTANCE_RESULT_MARKER) :])
            except json.JSONDecodeError:
                return None
            if isinstance(payload, dict):
                return payload
    return None


def _acceptance_summary(root: Path, completion: Mapping[str, Any]) -> dict[str, Any]:
    config = load_project_config(root)
    acceptance = config.get("acceptance")
    commands = acceptance.get("commands") if isinstance(acceptance, Mapping) else ()
    safe_commands = [
        str(item) for item in commands or () if isinstance(item, str) and item.strip()
    ]
    result_items: list[dict[str, Any]] = []
    classification = "PASS" if completion.get("acceptance_passed") else "COMMAND_FAILED"
    log_dir: str | None = None
    for raw in completion.get("acceptance_results") or ():
        if not isinstance(raw, Mapping):
            continue
        metadata = _oss_pilot_acceptance_metadata(raw)
        item = {
            "command": str(raw.get("command") or ""),
            "returncode": int(raw.get("returncode") or 0),
            "passed": bool(raw.get("passed")),
            "duration_ms": int(raw.get("duration_ms") or 0),
            "stdout_tail": str(raw.get("stdout_tail") or ""),
            "stderr_tail": str(raw.get("stderr_tail") or ""),
        }
        if metadata is not None:
            metadata_classification = str(metadata.get("classification") or "")
            metadata_detail = str(metadata.get("detail") or "")
            metadata_log_dir = str(metadata.get("log_dir") or "")
            item["classification"] = metadata_classification
            item["detail"] = metadata_detail
            item["log_dir"] = metadata_log_dir
            if not item["passed"]:
                classification = metadata_classification or classification
            if metadata_log_dir:
                log_dir = metadata_log_dir
        result_items.append(item)
    return {
        "protocol": "claim-plane.acceptance-summary.v1",
        "commands": safe_commands,
        "command_count": len(safe_commands),
        "passed": bool(completion.get("acceptance_passed")),
        "classification": classification,
        "log_dir": log_dir,
        "results": result_items,
        "duration_ms": int(completion.get("acceptance_duration_ms") or 0),
        "errors": int(completion.get("errors") or 0),
        "warnings": int(completion.get("warnings") or 0),
    }


_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[/\\]")
_SCOPE_GLOB_CHARS = frozenset("*?[")


def _normalize_initial_scope(root: Path, values: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize operator-provided initial mutation scope.

    Literal files remain exact resources. Existing directories and values ending in
    a slash become recursive repository-relative patterns. User-supplied glob syntax
    is intentionally rejected so the authority boundary is visually unambiguous.
    """

    normalized: list[str] = []
    for raw in values:
        value = raw.strip().replace("\\", "/")
        while value.startswith("./"):
            value = value[2:]
        if not value or value == ".":
            raise ValueError("--scope must name a repository file or directory")
        if value.startswith("/") or _WINDOWS_ABSOLUTE_PATH.match(value):
            raise ValueError("--scope paths must be repository-relative")
        if any(char in value for char in _SCOPE_GLOB_CHARS):
            raise ValueError(
                "--scope accepts literal files or directories, not glob syntax"
            )
        directory_hint = value.endswith("/")
        clean = posixpath.normpath(value).rstrip("/")
        if clean in {"", ".", ".."} or clean.startswith("../"):
            raise ValueError("--scope paths cannot escape the repository")
        if clean in {".git", ".codex", ".claim-plane"} or clean.startswith(
            (".git/", ".codex/", ".claim-plane/")
        ):
            raise ValueError(
                "--scope cannot grant connector, Git, or Claim Plane control state"
            )
        candidate = root / clean
        selector = f"{clean}/**" if directory_hint or candidate.is_dir() else clean
        if selector not in normalized:
            normalized.append(selector)
    return tuple(normalized)


def _scope_identifiers(value: Any) -> list[str]:
    result: list[str] = []
    for item in value or ():
        if not isinstance(item, Mapping):
            continue
        identifier = item.get("identifier") or item.get("path")
        if isinstance(identifier, str) and identifier and identifier not in result:
            result.append(identifier)
    return result


def _scope_amendment_history(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in value or ():
        if not isinstance(item, Mapping):
            continue
        reason = str(item.get("reason") or "")
        result.append(
            {
                "allowed": bool(item.get("allowed")),
                "resources": _scope_identifiers(item.get("operations")),
                "reason_sha256": (
                    hashlib.sha256(reason.encode("utf-8")).hexdigest()
                    if reason
                    else None
                ),
            }
        )
    return result


def _scope_summary(
    initial_scope: tuple[str, ...],
    *,
    locked: bool,
    adapter_status: Mapping[str, Any],
) -> dict[str, Any]:
    amendment = adapter_status.get("scope_amendment")
    amendment_map = amendment if isinstance(amendment, Mapping) else {}
    final_scope = _scope_identifiers(adapter_status.get("committed_scope"))
    history = _scope_amendment_history(amendment_map.get("history"))
    return {
        "protocol": "claim-plane.controlled-scope.v1",
        "mode": "operator" if initial_scope else "planner",
        "initial": list(initial_scope),
        "final": final_scope,
        "locked": locked,
        "amendments": {
            "tickets_issued": int(amendment_map.get("tickets_issued") or 0),
            "requests": int(amendment_map.get("requests") or 0),
            "admitted": int(amendment_map.get("admitted") or 0),
            "denied": int(amendment_map.get("denied") or 0),
            "history": history,
        },
    }


def _inspection_summary(adapter_status: Mapping[str, Any]) -> dict[str, Any]:
    guard = adapter_status.get("guard")
    guard_map = guard if isinstance(guard, Mapping) else {}
    inspection = guard_map.get("inspection")
    source = inspection if isinstance(inspection, Mapping) else {}
    return {
        "protocol": "claim-plane.inspection-friction.v1",
        "shell_calls": int(source.get("shell_calls") or 0),
        "read_only_allowed": int(source.get("read_only_allowed") or 0),
        "compound_allowed": int(source.get("compound_allowed") or 0),
        "pipelines_allowed": int(source.get("pipelines_allowed") or 0),
        "unclassified_denied": int(source.get("unclassified_denied") or 0),
        "recovered_after_denial": int(source.get("recovered_after_denial") or 0),
        "pending_denials": int(source.get("pending_denials") or 0),
        "test_feedback_allowed": int(source.get("test_feedback_allowed") or 0),
        "last_denial": dict(source.get("last_denial") or {}),
    }


def _codex_command(
    *,
    root: Path,
    task: str,
    final_message_path: Path,
    model: str | None,
) -> list[str]:
    executable = shutil.which("codex")
    if executable is None:
        raise ControlledRunPreflightError("Codex executable was not found on PATH")
    command = [
        executable,
        "--ask-for-approval",
        "never",
        "exec",
        "--json",
        "--color",
        "never",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(root),
        "--output-last-message",
        str(final_message_path),
    ]
    if model:
        command.extend(("--model", model))
    command.append(task)
    return command


_CONTROLLED_CODEX_FLAGS = frozenset(
    {
        "--ask-for-approval",
        "-a",
        "--sandbox",
        "-s",
        "--cd",
        "-C",
        "--model",
        "-m",
    }
)
_CONTROLLED_CODEX_SUBCOMMANDS = frozenset(
    {"exec", "resume", "fork", "mcp", "mcp-server", "app-server", "cloud"}
)


def _validate_interactive_codex_args(values: tuple[str, ...]) -> tuple[str, ...]:
    """Allow TUI preferences while retaining launcher-owned authority controls."""

    clean = tuple(item for item in values if item != "--")
    for index, value in enumerate(clean):
        name = value.split("=", 1)[0]
        if name in _CONTROLLED_CODEX_FLAGS:
            raise ValueError(
                f"Codex option {name!r} is owned by Claim Plane; use the matching "
                "claim-plane codex option instead"
            )
        if index == 0 and value in _CONTROLLED_CODEX_SUBCOMMANDS:
            raise ValueError(
                "claim-plane codex launches the interactive TUI; Codex subcommands "
                "must not be passed through"
            )
    return clean


def _codex_interactive_command(
    *,
    root: Path,
    model: str | None,
    initial_prompt: str | None,
    codex_args: tuple[str, ...],
) -> list[str]:
    executable = shutil.which("codex")
    if executable is None:
        raise ControlledRunPreflightError("Codex executable was not found on PATH")
    command = [
        executable,
        "--ask-for-approval",
        "never",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(root),
    ]
    if model:
        command.extend(("--model", model))
    command.extend(_validate_interactive_codex_args(codex_args))
    if initial_prompt:
        command.append(initial_prompt)
    return command


def _spawn_interactive_codex(
    command: list[str], *, root: Path, env: Mapping[str, str]
) -> ProcessLike:
    return subprocess.Popen(
        command,
        cwd=root,
        env=dict(env),
        text=True,
    )


def _terminate_interactive_process(
    process: ProcessLike, *, grace_seconds: float = 5.0
) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except (OSError, ProcessLookupError):
        return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        return
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def _spawn_codex(
    command: list[str], *, root: Path, env: Mapping[str, str]
) -> ProcessLike:
    return subprocess.Popen(
        command,
        cwd=root,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=(os.name == "posix"),
    )


def _reader(
    stream: IO[str] | None,
    name: str,
    output: queue.Queue[tuple[str, str | None]],
) -> None:
    if stream is None:
        output.put((name, None))
        return
    try:
        for line in iter(stream.readline, ""):
            output.put((name, line))
    finally:
        output.put((name, None))


def _stream_runtime(
    process: ProcessLike,
    *,
    timeout_seconds: float,
    summary: RuntimeSummary,
    quiet: bool,
    verbose: bool,
    stdout: TextIO,
    stderr: TextIO,
    console: ConsoleRenderer | None,
) -> int:
    messages: queue.Queue[tuple[str, str | None]] = queue.Queue()
    readers = (
        threading.Thread(
            target=_reader,
            args=(process.stdout, "stdout", messages),
            daemon=True,
        ),
        threading.Thread(
            target=_reader,
            args=(process.stderr, "stderr", messages),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    open_streams = 2
    started = time.monotonic()
    deadline = started + timeout_seconds
    while open_streams:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("controlled run exceeded its wall-time limit")
        try:
            source, line = messages.get(timeout=min(0.1, remaining))
        except queue.Empty:
            continue
        if line is None:
            open_streams -= 1
            continue
        if source == "stderr":
            if console is not None:
                console.runtime_stderr(line)
            elif not quiet and verbose:
                stderr.write(line)
                stderr.flush()
            continue
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            if not quiet and verbose:
                stdout.write(stripped + "\n")
                stdout.flush()
            continue
        if not isinstance(payload, Mapping):
            continue
        summary.observe(payload)
        if console is not None:
            console.runtime_payload(
                payload,
                elapsed_seconds=time.monotonic() - started,
            )
    return process.wait(timeout=max(0.1, deadline - time.monotonic()))


def _terminate_process(process: ProcessLike, *, grace_seconds: float = 5.0) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        return
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def _session_record_from_run(root: Path, run_id: str) -> dict[str, Any] | None:
    sessions = root / ".claim-plane/codex/sessions"
    if not sessions.is_dir():
        return None
    matches: list[tuple[str, dict[str, Any]]] = []
    for path in sessions.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("controlled_run_id") != run_id:
            continue
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            matches.append((str(payload.get("updated_at") or ""), payload))
    return dict(sorted(matches)[-1][1]) if matches else None


def _session_from_run(root: Path, run_id: str) -> str | None:
    payload = _session_record_from_run(root, run_id)
    if payload is None:
        return None
    session_id = payload.get("session_id")
    return str(session_id) if isinstance(session_id, str) and session_id else None


def _inspect_binding(
    adapter: AgentAdapter,
    *,
    root: Path,
    run_id: str,
    session_id: str,
) -> tuple[str | None, int | None, dict[str, Any]]:
    response = adapter.inspect(
        _request(
            AdapterOperation.INSPECT,
            adapter=adapter.name,
            root=root,
            run_id=run_id,
            session_id=session_id,
        )
    )
    return response.intent_id, response.intent_version, dict(response.payload)


def _cancel_authority(
    adapter: AgentAdapter,
    *,
    root: Path,
    run_id: str,
    session_id: str | None,
) -> Mapping[str, Any] | None:
    if not session_id:
        return None
    try:
        intent_id, intent_version, _ = _inspect_binding(
            adapter,
            root=root,
            run_id=run_id,
            session_id=session_id,
        )
        response = adapter.cancel(
            _request(
                AdapterOperation.CANCEL,
                adapter=adapter.name,
                root=root,
                run_id=run_id,
                session_id=session_id,
                intent_id=intent_id,
                intent_version=intent_version,
            )
        )
        return {
            "status": response.status.value,
            "intent_id": response.intent_id,
            "intent_version": response.intent_version,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "failed",
            "error_type": exc.__class__.__name__,
            "message_sha256": hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
        }


def _completion_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for item in payload.get("findings") or ():
        if not isinstance(item, Mapping):
            continue
        message = str(item.get("message") or "")
        findings.append(
            {
                "code": str(item.get("code") or "unknown"),
                "severity": str(item.get("severity") or "unknown"),
                "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
            }
        )
    report = payload.get("report")
    changed_files = None
    if isinstance(report, Mapping):
        metrics = report.get("metrics")
        if isinstance(metrics, Mapping):
            value = metrics.get("changed_files")
            if isinstance(value, int):
                changed_files = value
    if changed_files is None:
        value = payload.get("changed_files")
        changed_files = int(value) if isinstance(value, int) else 0
    return {
        "protocol": payload.get("protocol"),
        "verified": bool(payload.get("verified")),
        "changed_files": changed_files,
        "acceptance_passed": bool(payload.get("acceptance_passed")),
        "acceptance_duration_ms": int(payload.get("acceptance_duration_ms") or 0),
        "errors": int(payload.get("errors") or 0),
        "warnings": int(payload.get("warnings") or 0),
        "executed_violations": int(payload.get("executed_violations") or 0),
        "authorized_mutation_calls": int(payload.get("authorized_mutation_calls") or 0),
        "denied_mutation_calls": int(payload.get("denied_mutation_calls") or 0),
        "scope_promotions": int(payload.get("scope_promotions") or 0),
        "scope_expansions": int(payload.get("scope_expansions") or 0),
        "task_obligations": dict(payload.get("task_obligations") or {}),
        "findings": findings,
    }


def _lifecycle_summary(root: Path, session_id: str | None) -> Mapping[str, Any] | None:
    if not session_id:
        return None
    try:
        with LifecycleEventStore.for_project(root) as store:
            return store.report(adapter="codex", session_id=session_id).to_dict()
    except Exception as exc:  # noqa: BLE001
        return {
            "valid": False,
            "error_type": exc.__class__.__name__,
            "message_sha256": hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
        }


def _classify_outcome(
    *, runtime_returncode: int, completion: Mapping[str, Any]
) -> ControlledRunOutcome:
    if runtime_returncode != 0:
        return ControlledRunOutcome.FAILED
    if completion.get("verified") is True:
        return ControlledRunOutcome.VERIFIED
    if (
        int(completion.get("errors") or 0) > 0
        or int(completion.get("executed_violations") or 0) > 0
    ):
        return ControlledRunOutcome.REJECTED
    return ControlledRunOutcome.REVIEW_REQUIRED


def _task_obligation_error(
    completion: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    obligations = completion.get("task_obligations")
    if not isinstance(obligations, Mapping):
        return None
    unsatisfied = [
        str(item)
        for item in obligations.get("unsatisfied") or ()
        if isinstance(item, str)
    ]
    if not unsatisfied:
        return None
    return {
        "code": "task_obligation_unsatisfied",
        "message": (
            "required task outcome was not present in the final repository change"
        ),
        "obligations": unsatisfied,
    }


def _exit_code(outcome: ControlledRunOutcome) -> int:
    return int(
        {
            ControlledRunOutcome.VERIFIED: ExitCode.OK,
            ControlledRunOutcome.REVIEW_REQUIRED: ExitCode.ACTION_REQUIRED,
            ControlledRunOutcome.REJECTED: ExitCode.INCOMPLETE,
            ControlledRunOutcome.FAILED: ExitCode.BLOCKED,
            ControlledRunOutcome.TIMED_OUT: ExitCode.TIMED_OUT,
            ControlledRunOutcome.CANCELLED: ExitCode.CANCELLED,
        }[outcome]
    )


def _apply_deterministic_gate(
    *,
    task_sha256: str,
    adapter_name: str,
    manifest_digest: str,
    handshake: Mapping[str, Any],
    policy_name: str,
    effective_policy: Mapping[str, Any],
    start_git: GitState,
    result_git: GitState,
    changes: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    completion: Mapping[str, Any],
    scope: Mapping[str, Any],
    lifecycle: Mapping[str, Any] | None,
    risk: Mapping[str, Any],
    outcome: ControlledRunOutcome,
    error: Mapping[str, Any] | None,
) -> tuple[ControlledRunOutcome, Mapping[str, Any] | None, dict[str, Any]]:
    record = build_determinism_record(
        task_sha256=task_sha256,
        adapter_name=adapter_name,
        manifest_digest=manifest_digest,
        handshake=handshake,
        policy_name=policy_name,
        effective_policy=effective_policy,
        start_git=start_git.to_dict(),
        result_git=result_git.to_dict(),
        changes=changes,
        acceptance=acceptance,
        completion=completion,
        scope=scope,
        lifecycle=lifecycle,
        risk=risk,
        outcome=outcome.value,
        error=error,
    )
    completeness = record.get("completeness")
    complete = bool(isinstance(completeness, Mapping) and completeness.get("complete"))
    if outcome is ControlledRunOutcome.VERIFIED and not complete:
        findings = (
            completeness.get("findings") if isinstance(completeness, Mapping) else ()
        )
        outcome = ControlledRunOutcome.REVIEW_REQUIRED
        error = {
            "code": "deterministic_evidence_incomplete",
            "message": "verified delivery lacks complete deterministic evidence",
            "finding_codes": sorted(
                {
                    str(item.get("code"))
                    for item in findings or ()
                    if isinstance(item, Mapping) and item.get("code")
                }
            ),
        }
        record = build_determinism_record(
            task_sha256=task_sha256,
            adapter_name=adapter_name,
            manifest_digest=manifest_digest,
            handshake=handshake,
            policy_name=policy_name,
            effective_policy=effective_policy,
            start_git=start_git.to_dict(),
            result_git=result_git.to_dict(),
            changes=changes,
            acceptance=acceptance,
            completion=completion,
            scope=scope,
            lifecycle=lifecycle,
            risk=risk,
            outcome=outcome.value,
            error=error,
        )
    return outcome, error, record


def run_controlled_task(
    task: str,
    *,
    root: str | Path = ".",
    adapter: AgentAdapter,
    handshake: AdapterHandshake,
    policy: str | None = None,
    timeout_seconds: float = 3600.0,
    acceptance_timeout: float = 300.0,
    model: str | None = None,
    initial_scope: tuple[str, ...] = (),
    lock_scope: bool = False,
    quiet: bool = False,
    verbose: bool = False,
    stdout: TextIO,
    stderr: TextIO,
    process_factory: Callable[..., ProcessLike] = _spawn_codex,
) -> ControlledRunResult:
    """Run one Codex task under adapter authority and final Git verification."""

    preflight_started = time.monotonic()
    cleaned_task = task.strip()
    if not cleaned_task:
        raise ValueError("controlled run task must not be empty")
    if timeout_seconds <= 0 or acceptance_timeout <= 0:
        raise ValueError("controlled run timeouts must be positive")
    if adapter.name != "codex":
        raise ControlledRunPreflightError(
            "one-command execution currently has a complete launcher only for Codex"
        )

    resolved_root = resolve_project_root(root)
    normalized_scope = _normalize_initial_scope(resolved_root, tuple(initial_scope))
    if lock_scope and not normalized_scope:
        raise ValueError("--lock-scope requires at least one --scope path")
    run_id = "cpr_" + os.urandom(12).hex()
    started_at = _utc_now()
    effective_policy = _configured_policy(resolved_root, adapter.name, policy)
    selected_policy = effective_policy.name
    if not handshake.compatible:
        handshake.require_compatible()
    doctor = _doctor_ready(adapter, root=resolved_root, run_id=run_id)
    manifest = adapter.capability_manifest(str(resolved_root))
    compatibility = require_adapter_policy(manifest, selected_policy)
    start_git = capture_git_state(resolved_root)
    start_worktree_baseline = _dirty_worktree_baseline(resolved_root)
    run_directory = resolved_root / CONTROLLED_RUNS_PATH / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    os.chmod(run_directory, 0o700)
    final_message_path = run_directory / "final-message.txt"
    command = _codex_command(
        root=resolved_root,
        task=cleaned_task,
        final_message_path=final_message_path,
        model=model,
    )
    environment = dict(os.environ)
    environment[CONTROLLED_RUN_ENV] = run_id
    environment[CONTROLLED_POLICY_ENV] = selected_policy
    environment[CONTROLLED_POLICY_MANIFEST_ENV] = _canonical_json(
        effective_policy.to_dict()
    )
    if normalized_scope:
        environment[CONTROLLED_INITIAL_SCOPE_ENV] = json.dumps(
            list(normalized_scope), separators=(",", ":")
        )
    if lock_scope:
        environment[CONTROLLED_SCOPE_LOCK_ENV] = "1"
    environment["PYTHONUNBUFFERED"] = "1"

    console = None if quiet else ConsoleRenderer(stdout, stderr, verbose=verbose)
    if console is not None:
        console.header(
            run_id=run_id,
            root=resolved_root,
            policy=selected_policy,
            adapter=handshake.adapter,
            adapter_version=handshake.adapter_version,
            protocol_version=handshake.negotiated_protocol_version,
            runtime_name=handshake.runtime_name,
            runtime_version=handshake.runtime_version,
            model=model,
            initial_scope=normalized_scope,
            scope_locked=lock_scope,
        )
        console.step(
            "Preflight ready",
            detail="adapter, policy, Git, and runtime checks passed",
            elapsed_seconds=time.monotonic() - preflight_started,
        )
        console.step("Task submitted")

    process: ProcessLike | None = None
    runtime = RuntimeSummary()
    runtime_returncode: int | None = None
    completion_payload: dict[str, Any] = {}
    adapter_status: dict[str, Any] = {}
    cancellation: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    session_id: str | None = None
    intent_id: str | None = None
    intent_version: int | None = None
    outcome: ControlledRunOutcome
    try:
        process = process_factory(command, root=resolved_root, env=environment)
        runtime_returncode = _stream_runtime(
            process,
            timeout_seconds=timeout_seconds,
            summary=runtime,
            quiet=quiet,
            verbose=verbose,
            stdout=stdout,
            stderr=stderr,
            console=console,
        )
        runtime.seal()
        session_id = runtime.session_id or _session_from_run(resolved_root, run_id)
        if not session_id:
            raise ControlledRunError(
                "Codex finished without a session identity bound to this run"
            )
        if console is not None:
            console.verification_started()
        intent_id, intent_version, adapter_status = _inspect_binding(
            adapter,
            root=resolved_root,
            run_id=run_id,
            session_id=session_id,
        )
        existing_completion = adapter_status.get("completion")
        completion_is_verified = bool(
            isinstance(existing_completion, Mapping)
            and existing_completion.get("verified") is True
        )
        if intent_id and not completion_is_verified:
            response = adapter.verify_completion(
                _request(
                    AdapterOperation.VERIFY_COMPLETION,
                    adapter=adapter.name,
                    root=resolved_root,
                    run_id=run_id,
                    session_id=session_id,
                    intent_id=intent_id,
                    intent_version=intent_version,
                    timeout_seconds=acceptance_timeout,
                    payload={"source": "controlled_run_final_verifier"},
                )
            )
            completion_payload = dict(response.payload)
        elif isinstance(existing_completion, Mapping) and existing_completion:
            completion_payload = dict(existing_completion)
        else:
            completion_payload = {
                "verified": False,
                "errors": 0,
                "warnings": 1,
                "findings": [],
            }
        outcome = _classify_outcome(
            runtime_returncode=runtime_returncode,
            completion=completion_payload,
        )
    except KeyboardInterrupt:
        if process is not None:
            _terminate_process(process)
        session_id = runtime.session_id or _session_from_run(resolved_root, run_id)
        cancellation = _cancel_authority(
            adapter,
            root=resolved_root,
            run_id=run_id,
            session_id=session_id,
        )
        outcome = ControlledRunOutcome.CANCELLED
        error = {"code": "cancelled", "message": "run interrupted by user"}
    except TimeoutError:
        if process is not None:
            _terminate_process(process)
        session_id = runtime.session_id or _session_from_run(resolved_root, run_id)
        cancellation = _cancel_authority(
            adapter,
            root=resolved_root,
            run_id=run_id,
            session_id=session_id,
        )
        outcome = ControlledRunOutcome.TIMED_OUT
        error = {
            "code": "timeout",
            "message": "run exceeded its configured wall-time limit",
        }
    except Exception as exc:  # noqa: BLE001
        if process is not None:
            _terminate_process(process)
        session_id = runtime.session_id or _session_from_run(resolved_root, run_id)
        cancellation = _cancel_authority(
            adapter,
            root=resolved_root,
            run_id=run_id,
            session_id=session_id,
        )
        outcome = ControlledRunOutcome.FAILED
        error = {
            "code": "controlled_run_failed",
            "type": exc.__class__.__name__,
            "message_sha256": hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
        }
    finally:
        try:
            if final_message_path.exists():
                final_text = final_message_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                if final_text and runtime.final_message is None:
                    runtime.final_message = final_text
                    runtime.seal()
                final_message_path.unlink()
        except OSError:
            pass

    result_git = capture_git_state(resolved_root)
    changed_paths = _changed_paths(
        resolved_root,
        start_git.head_commit,
        result_git,
        preexisting=start_worktree_baseline,
    )
    risk = effective_policy.classify_many(changed_paths)
    final_policy_action = PolicyAction(str(risk["final_action"]))
    if outcome is ControlledRunOutcome.VERIFIED:
        if final_policy_action is PolicyAction.DENY:
            outcome = ControlledRunOutcome.REJECTED
        elif final_policy_action is PolicyAction.REVIEW_REQUIRED:
            outcome = ControlledRunOutcome.REVIEW_REQUIRED
    completion = _completion_summary(completion_payload)
    if error is None and outcome is ControlledRunOutcome.REJECTED:
        error = _task_obligation_error(completion)
    changes = _change_summary(
        resolved_root,
        start_git,
        result_git,
        preexisting=start_worktree_baseline,
    )
    acceptance = _acceptance_summary(resolved_root, completion)
    lifecycle = _lifecycle_summary(resolved_root, session_id)
    scope = _scope_summary(
        normalized_scope, locked=lock_scope, adapter_status=adapter_status
    )
    task_sha256 = hashlib.sha256(cleaned_task.encode("utf-8")).hexdigest()
    outcome, error, determinism = _apply_deterministic_gate(
        task_sha256=task_sha256,
        adapter_name=adapter.name,
        manifest_digest=manifest.digest(),
        handshake=handshake.evidence_summary(),
        policy_name=selected_policy,
        effective_policy=effective_policy.to_dict(),
        start_git=start_git,
        result_git=result_git,
        changes=changes,
        acceptance=acceptance,
        completion=completion,
        scope=scope,
        lifecycle=lifecycle,
        risk=risk,
        outcome=outcome,
        error=error,
    )
    result = ControlledRunResult(
        run_id=run_id,
        adapter=adapter.name,
        policy=selected_policy,
        root=str(resolved_root),
        started_at=started_at,
        finished_at=_utc_now(),
        outcome=outcome,
        exit_code=_exit_code(outcome),
        runtime_returncode=runtime_returncode,
        task_sha256=task_sha256,
        task_length=len(cleaned_task),
        session_id=session_id,
        intent_id=intent_id,
        intent_version=intent_version,
        start_git=start_git,
        result_git=result_git,
        manifest_digest=manifest.digest(),
        handshake=handshake.evidence_summary(),
        policy_compatibility=compatibility.to_dict(),
        effective_policy=effective_policy.to_dict(),
        risk=risk,
        runtime={
            **runtime.to_dict(),
            "doctor_ready": bool(doctor.get("ready")),
            "model_override": model,
            "inspection": _inspection_summary(adapter_status),
        },
        completion=completion,
        changes=changes,
        acceptance=acceptance,
        scope=scope,
        lifecycle=lifecycle,
        determinism=determinism,
        cancellation=cancellation,
        error=error,
    )
    _atomic_write_json(controlled_run_path(resolved_root, run_id), result.to_dict())

    if console is not None:
        console.finish(
            result=result.to_dict(),
            evidence_path=controlled_run_path(resolved_root, run_id),
            root=resolved_root,
            final_message=runtime.final_message,
        )
    return result


def run_interactive_codex(
    task: str | None = None,
    *,
    root: str | Path = ".",
    adapter: AgentAdapter,
    handshake: AdapterHandshake,
    policy: str | None = None,
    timeout_seconds: float | None = None,
    acceptance_timeout: float = 300.0,
    model: str | None = None,
    initial_scope: tuple[str, ...] = (),
    lock_scope: bool = False,
    codex_args: tuple[str, ...] = (),
    stdout: TextIO,
    stderr: TextIO,
    require_tty: bool = True,
    process_factory: Callable[..., ProcessLike] = _spawn_interactive_codex,
) -> ControlledRunResult:
    """Launch the interactive Codex TUI under Claim Plane authority.

    Codex retains its normal conversational interface. Claim Plane owns the adapter
    policy, initial scope, final verification, evidence record, and terminal outcome.
    """

    preflight_started = time.monotonic()
    cleaned_task = task.strip() if isinstance(task, str) and task.strip() else None
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("interactive timeout must be positive when provided")
    if acceptance_timeout <= 0:
        raise ValueError("acceptance timeout must be positive")
    if adapter.name != "codex":
        raise ControlledRunPreflightError(
            "interactive execution currently has a complete launcher only for Codex"
        )
    if require_tty:
        stdin_isatty = getattr(sys.stdin, "isatty", lambda: False)
        stdout_isatty = getattr(stdout, "isatty", lambda: False)
        if not stdin_isatty() or not stdout_isatty():
            raise ControlledRunPreflightError(
                "claim-plane codex requires an interactive terminal; use "
                "'claim-plane run' for non-interactive execution"
            )

    resolved_root = resolve_project_root(root)
    normalized_scope = _normalize_initial_scope(resolved_root, tuple(initial_scope))
    if lock_scope and not normalized_scope:
        raise ValueError("--lock-scope requires at least one --scope path")

    run_id = "cpr_" + os.urandom(12).hex()
    started_at = _utc_now()
    effective_policy = _configured_policy(resolved_root, adapter.name, policy)
    selected_policy = effective_policy.name
    if not handshake.compatible:
        handshake.require_compatible()
    doctor = _doctor_ready(adapter, root=resolved_root, run_id=run_id)
    manifest = adapter.capability_manifest(str(resolved_root))
    compatibility = require_adapter_policy(manifest, selected_policy)
    start_git = capture_git_state(resolved_root)
    start_worktree_baseline = _dirty_worktree_baseline(resolved_root)
    run_directory = resolved_root / CONTROLLED_RUNS_PATH / run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    os.chmod(run_directory, 0o700)

    command = _codex_interactive_command(
        root=resolved_root,
        model=model,
        initial_prompt=cleaned_task,
        codex_args=tuple(codex_args),
    )
    environment = dict(os.environ)
    environment[CONTROLLED_RUN_ENV] = run_id
    environment[CONTROLLED_POLICY_ENV] = selected_policy
    environment[CONTROLLED_POLICY_MANIFEST_ENV] = _canonical_json(
        effective_policy.to_dict()
    )
    if normalized_scope:
        environment[CONTROLLED_INITIAL_SCOPE_ENV] = json.dumps(
            list(normalized_scope), separators=(",", ":")
        )
    if lock_scope:
        environment[CONTROLLED_SCOPE_LOCK_ENV] = "1"
    environment[CONTROLLED_INTERACTIVE_ENV] = "1"
    environment["PYTHONUNBUFFERED"] = "1"

    console = ConsoleRenderer(stdout, stderr)
    console.header(
        run_id=run_id,
        root=resolved_root,
        policy=selected_policy,
        adapter=handshake.adapter,
        adapter_version=handshake.adapter_version,
        protocol_version=handshake.negotiated_protocol_version,
        runtime_name=handshake.runtime_name,
        runtime_version=handshake.runtime_version,
        model=model,
        initial_scope=normalized_scope,
        scope_locked=lock_scope,
        title="Claim Plane · Interactive Codex",
    )
    console.step(
        "Preflight ready",
        detail="adapter, policy, Git, and runtime checks passed",
        elapsed_seconds=time.monotonic() - preflight_started,
    )
    console.step(
        "Opening Codex TUI",
        detail="exit Codex normally to seal final evidence",
        state="active",
    )
    stdout.write("\n")
    stdout.flush()

    process: ProcessLike | None = None
    runtime_returncode: int | None = None
    completion_payload: dict[str, Any] = {}
    adapter_status: dict[str, Any] = {}
    cancellation: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    session_record: dict[str, Any] | None = None
    session_id: str | None = None
    intent_id: str | None = None
    intent_version: int | None = None
    outcome = ControlledRunOutcome.FAILED

    previous_sigint: Any = None
    signal_handler_changed = False
    try:
        if threading.current_thread() is threading.main_thread():
            previous_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, lambda _signum, _frame: None)
            signal_handler_changed = True
        process = process_factory(command, root=resolved_root, env=environment)
        try:
            runtime_returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                "interactive Codex session exceeded its wall-time limit"
            ) from exc

        session_record = _session_record_from_run(resolved_root, run_id)
        session_id = _session_from_run(resolved_root, run_id)
        if not session_id:
            outcome = (
                ControlledRunOutcome.CANCELLED
                if runtime_returncode == 0
                else ControlledRunOutcome.FAILED
            )
            error = {
                "code": "no_task_submitted",
                "message": (
                    "interactive Codex exited without a Claim Plane task session"
                ),
            }
        else:
            console.verification_started()
            intent_id, intent_version, adapter_status = _inspect_binding(
                adapter,
                root=resolved_root,
                run_id=run_id,
                session_id=session_id,
            )
            existing_completion = adapter_status.get("completion")
            completion_is_verified = bool(
                isinstance(existing_completion, Mapping)
                and existing_completion.get("verified") is True
            )
            if intent_id and not completion_is_verified:
                response = adapter.verify_completion(
                    _request(
                        AdapterOperation.VERIFY_COMPLETION,
                        adapter=adapter.name,
                        root=resolved_root,
                        run_id=run_id,
                        session_id=session_id,
                        intent_id=intent_id,
                        intent_version=intent_version,
                        timeout_seconds=acceptance_timeout,
                        payload={"source": "interactive_codex_final_verifier"},
                    )
                )
                completion_payload = dict(response.payload)
            elif isinstance(existing_completion, Mapping) and existing_completion:
                completion_payload = dict(existing_completion)
            elif intent_id:
                completion_payload = {
                    "verified": False,
                    "errors": 0,
                    "warnings": 1,
                    "findings": [],
                }
            else:
                outcome = ControlledRunOutcome.CANCELLED
                error = {
                    "code": "no_admitted_intent",
                    "message": (
                        "interactive session ended without an admitted ChangeIntent"
                    ),
                }
            if intent_id:
                outcome = _classify_outcome(
                    runtime_returncode=runtime_returncode,
                    completion=completion_payload,
                )
                adapter.stop_session(
                    _request(
                        AdapterOperation.STOP_SESSION,
                        adapter=adapter.name,
                        root=resolved_root,
                        run_id=run_id,
                        session_id=session_id,
                        payload={"source": "interactive_codex_launcher"},
                    )
                )
    except KeyboardInterrupt:
        if process is not None:
            _terminate_interactive_process(process)
        session_record = _session_record_from_run(resolved_root, run_id)
        session_id = _session_from_run(resolved_root, run_id)
        cancellation = _cancel_authority(
            adapter,
            root=resolved_root,
            run_id=run_id,
            session_id=session_id,
        )
        outcome = ControlledRunOutcome.CANCELLED
        error = {"code": "cancelled", "message": "interactive session interrupted"}
    except TimeoutError:
        if process is not None:
            _terminate_interactive_process(process)
        session_record = _session_record_from_run(resolved_root, run_id)
        session_id = _session_from_run(resolved_root, run_id)
        cancellation = _cancel_authority(
            adapter,
            root=resolved_root,
            run_id=run_id,
            session_id=session_id,
        )
        outcome = ControlledRunOutcome.TIMED_OUT
        error = {
            "code": "timeout",
            "message": "interactive session exceeded its configured wall-time limit",
        }
    except Exception as exc:  # noqa: BLE001
        if process is not None:
            _terminate_interactive_process(process)
        session_record = _session_record_from_run(resolved_root, run_id)
        session_id = _session_from_run(resolved_root, run_id)
        cancellation = _cancel_authority(
            adapter,
            root=resolved_root,
            run_id=run_id,
            session_id=session_id,
        )
        outcome = ControlledRunOutcome.FAILED
        error = {
            "code": "interactive_codex_failed",
            "type": exc.__class__.__name__,
            "message_sha256": hashlib.sha256(str(exc).encode("utf-8")).hexdigest(),
        }
    finally:
        if signal_handler_changed:
            signal.signal(signal.SIGINT, previous_sigint)

    result_git = capture_git_state(resolved_root)
    changed_paths = _changed_paths(
        resolved_root,
        start_git.head_commit,
        result_git,
        preexisting=start_worktree_baseline,
    )
    if changed_paths and (not session_id or not intent_id):
        outcome = ControlledRunOutcome.REJECTED
        error = {
            "code": "unbound_repository_changes",
            "message": "repository changed without a bound admitted ChangeIntent",
        }
    risk = effective_policy.classify_many(changed_paths)
    final_policy_action = PolicyAction(str(risk["final_action"]))
    if outcome is ControlledRunOutcome.VERIFIED:
        if final_policy_action is PolicyAction.DENY:
            outcome = ControlledRunOutcome.REJECTED
        elif final_policy_action is PolicyAction.REVIEW_REQUIRED:
            outcome = ControlledRunOutcome.REVIEW_REQUIRED

    completion = _completion_summary(completion_payload)
    if error is None and outcome is ControlledRunOutcome.REJECTED:
        error = _task_obligation_error(completion)
    changes = _change_summary(
        resolved_root,
        start_git,
        result_git,
        preexisting=start_worktree_baseline,
    )
    acceptance = _acceptance_summary(resolved_root, completion)
    lifecycle = _lifecycle_summary(resolved_root, session_id)

    prompt_sha = None
    prompt_length = None
    if isinstance(session_record, Mapping):
        raw_sha = session_record.get("prompt_sha256")
        raw_length = session_record.get("prompt_length")
        if isinstance(raw_sha, str) and raw_sha:
            prompt_sha = raw_sha
        if isinstance(raw_length, int):
            prompt_length = raw_length
    fallback_task = cleaned_task or "interactive Codex session"
    task_sha256 = (
        prompt_sha or hashlib.sha256(fallback_task.encode("utf-8")).hexdigest()
    )
    scope = _scope_summary(
        normalized_scope,
        locked=lock_scope,
        adapter_status=adapter_status,
    )
    outcome, error, determinism = _apply_deterministic_gate(
        task_sha256=task_sha256,
        adapter_name=adapter.name,
        manifest_digest=manifest.digest(),
        handshake=handshake.evidence_summary(),
        policy_name=selected_policy,
        effective_policy=effective_policy.to_dict(),
        start_git=start_git,
        result_git=result_git,
        changes=changes,
        acceptance=acceptance,
        completion=completion,
        scope=scope,
        lifecycle=lifecycle,
        risk=risk,
        outcome=outcome,
        error=error,
    )

    result = ControlledRunResult(
        run_id=run_id,
        adapter=adapter.name,
        policy=selected_policy,
        root=str(resolved_root),
        started_at=started_at,
        finished_at=_utc_now(),
        outcome=outcome,
        exit_code=_exit_code(outcome),
        runtime_returncode=runtime_returncode,
        task_sha256=task_sha256,
        task_length=(
            prompt_length if prompt_length is not None else len(fallback_task)
        ),
        session_id=session_id,
        intent_id=intent_id,
        intent_version=intent_version,
        start_git=start_git,
        result_git=result_git,
        manifest_digest=manifest.digest(),
        handshake=handshake.evidence_summary(),
        policy_compatibility=compatibility.to_dict(),
        effective_policy=effective_policy.to_dict(),
        risk=risk,
        runtime={
            "session_id": session_id,
            "event_counts": {},
            "errors": 0 if runtime_returncode in {0, None} else 1,
            "final_message_sha256": None,
            "final_message_length": 0,
            "usage": {},
            "doctor_ready": bool(doctor.get("ready")),
            "model_override": model,
            "interactive": True,
            "launcher": "codex_tui",
            "inspection": _inspection_summary(adapter_status),
        },
        completion=completion,
        changes=changes,
        acceptance=acceptance,
        scope=scope,
        lifecycle=lifecycle,
        determinism=determinism,
        cancellation=cancellation,
        error=error,
    )
    _atomic_write_json(controlled_run_path(resolved_root, run_id), result.to_dict())
    console.finish(
        result=result.to_dict(),
        evidence_path=controlled_run_path(resolved_root, run_id),
        root=resolved_root,
        final_message=None,
    )
    return result
