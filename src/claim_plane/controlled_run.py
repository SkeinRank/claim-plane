"""One-command controlled execution for coding-agent adapters.

The controlled runner owns preflight, process lifetime, cancellation, final Git
binding, and durable run state. Runtime-specific lifecycle events still cross the
versioned adapter protocol; the runner never grants mutation authority directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, TextIO

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
    stdout: TextIO | None
    stderr: TextIO | None

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
    runtime: Mapping[str, Any]
    completion: Mapping[str, Any]
    lifecycle: Mapping[str, Any] | None
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
            "runtime": dict(self.runtime),
            "completion": dict(self.completion),
            "lifecycle": dict(self.lifecycle) if self.lifecycle is not None else None,
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
            result[relative] = "error:" + hashlib.sha256(
                str(exc).encode("utf-8")
            ).hexdigest()
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
    return GitState(**unsigned, digest=digest)


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


def _configured_policy(root: Path, adapter: str, explicit: str | None) -> str:
    if explicit is not None:
        return explicit
    config = load_project_config(root)
    adapters = config.get("adapters")
    settings = adapters.get(adapter) if isinstance(adapters, Mapping) else None
    if not isinstance(settings, Mapping) or not settings.get("enabled"):
        raise ControlledRunPreflightError(
            f"adapter {adapter!r} is not enrolled; run 'claim-plane connect {adapter}'"
        )
    return str(settings.get("policy") or "guarded")


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
        "exec",
        "--json",
        "--color",
        "never",
        "--sandbox",
        "workspace-write",
        "--ask-for-approval",
        "never",
        "--cd",
        str(root),
        "--output-last-message",
        str(final_message_path),
    ]
    if model:
        command.extend(("--model", model))
    command.append(task)
    return command


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
    stream: TextIO | None,
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


def _runtime_stage(payload: Mapping[str, Any]) -> str | None:
    event_type = str(payload.get("type") or "")
    if event_type == "thread.started":
        return "Codex session started"
    if event_type == "turn.started":
        return "Agent working"
    if event_type == "turn.completed":
        return "Agent turn completed"
    if event_type == "turn.failed":
        return "Agent turn failed"
    if event_type == "error":
        return "Agent runtime error"
    return None


def _stream_runtime(
    process: ProcessLike,
    *,
    timeout_seconds: float,
    summary: RuntimeSummary,
    quiet: bool,
    stdout: TextIO,
    stderr: TextIO,
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
    deadline = time.monotonic() + timeout_seconds
    announced: set[str] = set()
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
            if not quiet:
                stderr.write(line)
                stderr.flush()
            continue
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            if not quiet:
                stdout.write(stripped + "\n")
                stdout.flush()
            continue
        if not isinstance(payload, Mapping):
            continue
        summary.observe(payload)
        stage = _runtime_stage(payload)
        if stage and stage not in announced and not quiet:
            announced.add(stage)
            stdout.write(stage + "\n")
            stdout.flush()
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


def _session_from_run(root: Path, run_id: str) -> str | None:
    sessions = root / ".claim-plane/codex/sessions"
    if not sessions.is_dir():
        return None
    matches: list[tuple[str, str]] = []
    for path in sessions.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            not isinstance(payload, Mapping)
            or payload.get("controlled_run_id") != run_id
        ):
            continue
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            matches.append((str(payload.get("updated_at") or ""), session_id))
    return sorted(matches)[-1][1] if matches else None


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
        "errors": int(payload.get("errors") or 0),
        "warnings": int(payload.get("warnings") or 0),
        "executed_violations": int(payload.get("executed_violations") or 0),
        "authorized_mutation_calls": int(
            payload.get("authorized_mutation_calls") or 0
        ),
        "denied_mutation_calls": int(payload.get("denied_mutation_calls") or 0),
        "scope_promotions": int(payload.get("scope_promotions") or 0),
        "scope_expansions": int(payload.get("scope_expansions") or 0),
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
    if int(completion.get("errors") or 0) > 0 or int(
        completion.get("executed_violations") or 0
    ) > 0:
        return ControlledRunOutcome.REJECTED
    return ControlledRunOutcome.REVIEW_REQUIRED


def _exit_code(outcome: ControlledRunOutcome) -> int:
    return {
        ControlledRunOutcome.VERIFIED: 0,
        ControlledRunOutcome.REVIEW_REQUIRED: 2,
        ControlledRunOutcome.REJECTED: 3,
        ControlledRunOutcome.FAILED: 4,
        ControlledRunOutcome.TIMED_OUT: 124,
        ControlledRunOutcome.CANCELLED: 130,
    }[outcome]


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
    quiet: bool = False,
    stdout: TextIO,
    stderr: TextIO,
    process_factory: Callable[..., ProcessLike] = _spawn_codex,
) -> ControlledRunResult:
    """Run one Codex task under adapter authority and final Git verification."""

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
    run_id = "cpr_" + os.urandom(12).hex()
    started_at = _utc_now()
    selected_policy = _configured_policy(resolved_root, adapter.name, policy)
    if not handshake.compatible:
        handshake.require_compatible()
    doctor = _doctor_ready(adapter, root=resolved_root, run_id=run_id)
    manifest = adapter.capability_manifest(str(resolved_root))
    compatibility = require_adapter_policy(manifest, selected_policy)
    start_git = capture_git_state(resolved_root)
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
    environment["PYTHONUNBUFFERED"] = "1"

    if not quiet:
        stdout.write(f"RUN {run_id}\n")
        stdout.write(f"Policy: {selected_policy}\n")
        stdout.write("Preflight: READY\n")
        stdout.write("Task submitted\n")
        stdout.flush()

    process: ProcessLike | None = None
    runtime = RuntimeSummary()
    runtime_returncode: int | None = None
    completion_payload: dict[str, Any] = {}
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
            stdout=stdout,
            stderr=stderr,
        )
        runtime.seal()
        session_id = runtime.session_id or _session_from_run(resolved_root, run_id)
        if not session_id:
            raise ControlledRunError(
                "Codex finished without a session identity bound to this run"
            )
        intent_id, intent_version, status = _inspect_binding(
            adapter,
            root=resolved_root,
            run_id=run_id,
            session_id=session_id,
        )
        existing_completion = status.get("completion")
        if isinstance(existing_completion, Mapping) and existing_completion:
            completion_payload = dict(existing_completion)
        elif intent_id:
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
                )
            )
            completion_payload = dict(response.payload)
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
    completion = _completion_summary(completion_payload)
    lifecycle = _lifecycle_summary(resolved_root, session_id)
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
        task_sha256=hashlib.sha256(cleaned_task.encode("utf-8")).hexdigest(),
        task_length=len(cleaned_task),
        session_id=session_id,
        intent_id=intent_id,
        intent_version=intent_version,
        start_git=start_git,
        result_git=result_git,
        manifest_digest=manifest.digest(),
        handshake=handshake.evidence_summary(),
        policy_compatibility=compatibility.to_dict(),
        runtime={
            **runtime.to_dict(),
            "doctor_ready": bool(doctor.get("ready")),
            "model_override": model,
        },
        completion=completion,
        lifecycle=lifecycle,
        cancellation=cancellation,
        error=error,
    )
    _atomic_write_json(controlled_run_path(resolved_root, run_id), result.to_dict())

    if not quiet:
        stdout.write("Verification:\n")
        scope_clean = (
            completion.get("errors", 0) == 0
            and completion.get("executed_violations", 0) == 0
        )
        stdout.write(f"  scope: {'PASS' if scope_clean else 'FAIL'}\n")
        stdout.write(
            "  acceptance: "
            + ("PASS" if completion.get("acceptance_passed") else "NOT VERIFIED")
            + "\n"
        )
        stdout.write(f"DELIVERY {outcome.value.replace('_', ' ')}\n")
        stdout.write(f"Evidence: {controlled_run_path(resolved_root, run_id)}\n")
        if runtime.final_message:
            stdout.write("\nCodex final message:\n")
            stdout.write(runtime.final_message.rstrip() + "\n")
        stdout.flush()
    return result
