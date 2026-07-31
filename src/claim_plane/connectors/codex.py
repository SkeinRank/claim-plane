"""Project-local Codex enrollment and lifecycle dispatch."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from claim_plane.core import ChangeIntent, IntentOperation, Plane, ResourceKind

PROJECT_PROTOCOL = "claim-plane.project.v1"
CODEX_ENROLLMENT_PROTOCOL = "claim-plane.codex-enrollment.v1"
CODEX_SESSION_PROTOCOL = "claim-plane.codex-session.v1"
CODEX_INTENT_PROPOSAL_PROTOCOL = "claim-plane.codex-intent-proposal.v1"
CODEX_INTENT_ADMISSION_PROTOCOL = "claim-plane.codex-intent-admission.v1"
CODEX_HOOK_COMMAND = "claim-plane codex-hook"
CODEX_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SessionEnd",
)

_PROJECT_STATE = Path(".claim-plane/project.json")
_CODEX_STATE = Path(".claim-plane/codex.json")
_CODEX_SESSIONS = Path(".claim-plane/codex/sessions")
_PLANE_DB = Path(".claim-plane/plane.db")
_CODEX_HOOKS = Path(".codex/hooks.json")
_CODEX_CONFIG = Path(".codex/config.toml")


@dataclass(frozen=True)
class CodexDoctorReport:
    """Machine-readable health report for one project-local Codex enrollment."""

    root: str
    ready: bool
    checks: tuple[dict[str, Any], ...]
    codex_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": "claim-plane.codex-doctor.v1",
            "root": self.root,
            "ready": self.ready,
            "codex_version": self.codex_version,
            "checks": [dict(item) for item in self.checks],
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _session_key(session_id: str) -> str:
    return _sha256_text(session_id)[:24]


def _session_state_path(root: Path, session_id: str) -> Path:
    return root / _CODEX_SESSIONS / f"{_session_key(session_id)}.json"


def _git(root_or_child: str | Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=Path(root_or_child).resolve(),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git failed"
        raise ValueError(detail)
    return completed.stdout.strip()


def _head_commit(root: Path) -> str:
    commit = _git(root, "rev-parse", "--verify", "HEAD^{commit}").lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ValueError("Git HEAD did not resolve to a full object id")
    return commit


def _branch_name(root: Path) -> str:
    value = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return value or "HEAD"


def _worktree_status(root: Path) -> tuple[bool, str]:
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    return bool(status), _sha256_text(status)


def resolve_project_root(root_or_child: str | Path = ".") -> Path:
    """Resolve the Git worktree root used by project-local enrollment."""

    root = _git(root_or_child, "rev-parse", "--show-toplevel")
    return Path(root).resolve()


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _ensure_local_state_excluded(root: Path) -> Path:
    git_path = _git(root, "rev-parse", "--git-path", "info/exclude")
    exclude = Path(git_path)
    if not exclude.is_absolute():
        exclude = root / exclude
    exclude = exclude.resolve()
    exclude.parent.mkdir(parents=True, exist_ok=True)

    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    active_lines = {
        line.strip()
        for line in existing.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if ".claim-plane/" in active_lines or ".claim-plane" in active_lines:
        return exclude

    prefix = existing
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    prefix += "# Claim Plane local state\n.claim-plane/\n"
    _atomic_write_text(exclude, prefix)
    return exclude


def init_project(root_or_child: str | Path = ".") -> dict[str, Any]:
    """Initialize local Claim Plane state without adding tracked repository files."""

    root = resolve_project_root(root_or_child)
    state_path = root / _PROJECT_STATE
    now = _utc_now()
    if state_path.exists():
        state = _read_json_object(state_path)
        if state.get("protocol") != PROJECT_PROTOCOL:
            raise ValueError(
                f"{state_path} uses unsupported protocol {state.get('protocol')!r}"
            )
        initialized_at = str(state.get("initialized_at") or now)
    else:
        initialized_at = now

    state = {
        "protocol": PROJECT_PROTOCOL,
        "initialized_at": initialized_at,
        "updated_at": now,
    }
    _atomic_write_json(state_path, state)
    exclude = _ensure_local_state_excluded(root)
    return {
        "root": str(root),
        "state": str(state_path),
        "exclude": str(exclude),
        "initialized": True,
    }


def _require_initialized(root: Path) -> dict[str, Any]:
    state_path = root / _PROJECT_STATE
    if not state_path.exists():
        raise ValueError("project is not initialized; run 'claim-plane init' first")
    state = _read_json_object(state_path)
    if state.get("protocol") != PROJECT_PROTOCOL:
        raise ValueError(f"unsupported project state protocol in {state_path}")
    return state


def _canonical_group(event: str) -> dict[str, Any]:
    matcher: str | None = None
    if event == "SessionStart":
        matcher = "startup|resume|clear|compact"
    elif event in {"PreToolUse", "PostToolUse"}:
        matcher = "*"
    elif event == "SessionEnd":
        matcher = "other"

    group: dict[str, Any] = {}
    if matcher is not None:
        group["matcher"] = matcher
    timeout = 3 if event == "SessionEnd" else 30
    group["hooks"] = [
        {
            "type": "command",
            "command": CODEX_HOOK_COMMAND,
            "timeout": timeout,
        }
    ]
    return group


def _is_claim_plane_handler(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("type") == "command"
        and value.get("command") == CODEX_HOOK_COMMAND
    )


def _validate_hooks_shape(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    hooks = payload.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path}: 'hooks' must be a JSON object")
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise ValueError(f"{path}: hooks.{event} must be a JSON array")
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError(f"{path}: hooks.{event} entries must be JSON objects")
            handlers = group.get("hooks")
            if handlers is not None and not isinstance(handlers, list):
                raise ValueError(
                    f"{path}: hooks.{event} group 'hooks' must be a JSON array"
                )
    return hooks


def _remove_claim_plane_handlers(payload: dict[str, Any], path: Path) -> int:
    hooks = _validate_hooks_shape(payload, path)
    removed = 0
    for event in list(hooks):
        groups = hooks[event]
        kept_groups: list[dict[str, Any]] = []
        for group in groups:
            handlers = group.get("hooks")
            if handlers is None:
                kept_groups.append(group)
                continue
            kept_handlers = []
            for handler in handlers:
                if _is_claim_plane_handler(handler):
                    removed += 1
                else:
                    kept_handlers.append(handler)
            if kept_handlers:
                updated = dict(group)
                updated["hooks"] = kept_handlers
                kept_groups.append(updated)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event, None)
    return removed


def _project_hooks_disabled(config_path: Path) -> bool:
    """Detect an explicit project-local setting that disables Codex hooks."""

    if not config_path.exists():
        return False
    section = ""
    settings: dict[str, bool] = {}
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        table = re.fullmatch(r"\[([^\[\]]+)\]", line)
        if table:
            section = table.group(1).strip()
            continue
        if section != "features":
            continue
        assignment = re.fullmatch(r"(hooks|codex_hooks)\s*=\s*(true|false)", line, re.I)
        if assignment:
            settings[assignment.group(1).lower()] = (
                assignment.group(2).lower() == "true"
            )

    enabled = settings.get("hooks", settings.get("codex_hooks", True))
    return not enabled


def _has_inline_hooks(config_path: Path) -> bool:
    if not config_path.exists():
        return False
    text = config_path.read_text(encoding="utf-8")
    return bool(re.search(r"(?m)^\s*\[\[?hooks(?:\.|\]|\.)", text))


def _load_hooks(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"hooks": {}}
    return _read_json_object(path)


def _installed_events(payload: dict[str, Any], path: Path) -> set[str]:
    hooks = _validate_hooks_shape(payload, path)
    installed: set[str] = set()
    for event, groups in hooks.items():
        for group in groups:
            handlers = group.get("hooks") or []
            if any(_is_claim_plane_handler(item) for item in handlers):
                installed.add(str(event))
    return installed


def connect_codex(root_or_child: str | Path = ".") -> dict[str, Any]:
    """Enroll Codex in a project-local lifecycle bridge."""

    root = resolve_project_root(root_or_child)
    _require_initialized(root)
    config_path = root / _CODEX_CONFIG
    if _project_hooks_disabled(config_path):
        raise ValueError(
            f"{config_path} disables Codex hooks; set [features] hooks = true before enrollment"
        )

    hooks_path = root / _CODEX_HOOKS
    state_path = root / _CODEX_STATE
    now = _utc_now()
    created_at = now
    if state_path.exists():
        existing = _read_json_object(state_path)
        if existing.get("protocol") != CODEX_ENROLLMENT_PROTOCOL:
            raise ValueError(f"unsupported Codex enrollment protocol in {state_path}")
        created_at = str(existing.get("created_at") or now)

    payload = _load_hooks(hooks_path)
    _remove_claim_plane_handlers(payload, hooks_path)
    hooks = _validate_hooks_shape(payload, hooks_path)
    for event in CODEX_HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        groups.append(_canonical_group(event))
    _atomic_write_json(hooks_path, payload)

    state = {
        "protocol": CODEX_ENROLLMENT_PROTOCOL,
        "created_at": created_at,
        "updated_at": now,
        "hooks_path": _CODEX_HOOKS.as_posix(),
        "hook_command": CODEX_HOOK_COMMAND,
        "events": list(CODEX_HOOK_EVENTS),
    }
    _atomic_write_json(state_path, state)
    return {
        "root": str(root),
        "hooks": str(hooks_path),
        "state": str(state_path),
        "events": list(CODEX_HOOK_EVENTS),
        "inline_hooks_present": _has_inline_hooks(config_path),
        "connected": True,
    }


def disconnect_codex(root_or_child: str | Path = ".") -> dict[str, Any]:
    """Remove only Claim Plane-owned Codex hook handlers from a project."""

    root = resolve_project_root(root_or_child)
    hooks_path = root / _CODEX_HOOKS
    state_path = root / _CODEX_STATE
    removed = 0
    if hooks_path.exists():
        payload = _load_hooks(hooks_path)
        removed = _remove_claim_plane_handlers(payload, hooks_path)
        hooks = payload.get("hooks")
        if hooks == {}:
            payload.pop("hooks", None)
        if payload:
            _atomic_write_json(hooks_path, payload)
        else:
            hooks_path.unlink()
            try:
                hooks_path.parent.rmdir()
            except OSError:
                pass
    try:
        state_path.unlink()
    except FileNotFoundError:
        pass
    return {
        "root": str(root),
        "removed_handlers": removed,
        "connected": False,
    }


def _codex_version() -> tuple[str | None, str | None]:
    executable = shutil.which("codex")
    if executable is None:
        return None, None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return executable, None
    output = (completed.stdout.strip() or completed.stderr.strip()).splitlines()
    version = output[0] if output else None
    return executable, version


def doctor_codex(root_or_child: str | Path = ".") -> CodexDoctorReport:
    """Inspect local enrollment without modifying project state."""

    root = resolve_project_root(root_or_child)
    checks: list[dict[str, Any]] = []

    project_state = root / _PROJECT_STATE
    initialized = project_state.exists()
    if initialized:
        try:
            initialized = (
                _read_json_object(project_state).get("protocol") == PROJECT_PROTOCOL
            )
        except (json.JSONDecodeError, ValueError):
            initialized = False
    checks.append(
        {
            "name": "project_initialized",
            "status": "ok" if initialized else "error",
            "detail": str(project_state),
        }
    )

    state_path = root / _CODEX_STATE
    enrolled = False
    if state_path.exists():
        try:
            enrolled = (
                _read_json_object(state_path).get("protocol")
                == CODEX_ENROLLMENT_PROTOCOL
            )
        except (json.JSONDecodeError, ValueError):
            enrolled = False
    checks.append(
        {
            "name": "enrollment_state",
            "status": "ok" if enrolled else "error",
            "detail": str(state_path),
        }
    )

    hooks_path = root / _CODEX_HOOKS
    installed_events: set[str] = set()
    hooks_valid = False
    if hooks_path.exists():
        try:
            installed_events = _installed_events(_load_hooks(hooks_path), hooks_path)
            hooks_valid = set(CODEX_HOOK_EVENTS).issubset(installed_events)
        except (json.JSONDecodeError, ValueError):
            hooks_valid = False
    missing = sorted(set(CODEX_HOOK_EVENTS) - installed_events)
    checks.append(
        {
            "name": "lifecycle_hooks",
            "status": "ok" if hooks_valid else "error",
            "detail": str(hooks_path),
            "missing_events": missing,
        }
    )

    config_path = root / _CODEX_CONFIG
    disabled = _project_hooks_disabled(config_path)
    checks.append(
        {
            "name": "project_hook_feature",
            "status": "error" if disabled else "ok",
            "detail": (
                "project config disables hooks"
                if disabled
                else "project config does not disable hooks"
            ),
        }
    )

    inline = _has_inline_hooks(config_path)
    checks.append(
        {
            "name": "inline_hook_source",
            "status": "warning" if inline else "ok",
            "detail": (
                "inline hooks also exist; Codex merges both project-local hook sources"
                if inline
                else "hooks.json is the only detected project-local hook source"
            ),
        }
    )

    executable, version = _codex_version()
    checks.append(
        {
            "name": "codex_cli",
            "status": "ok" if executable else "error",
            "detail": executable or "codex executable not found on PATH",
        }
    )

    checks.append(
        {
            "name": "hook_trust",
            "status": "info",
            "detail": "review project-local command hooks with /hooks in Codex",
        }
    )

    ready = all(item["status"] != "error" for item in checks)
    return CodexDoctorReport(
        root=str(root),
        ready=ready,
        checks=tuple(checks),
        codex_version=version,
    )


def _enrollment_state(root: Path) -> dict[str, Any] | None:
    state_path = root / _CODEX_STATE
    if not state_path.exists():
        return None
    state = _read_json_object(state_path)
    if state.get("protocol") != CODEX_ENROLLMENT_PROTOCOL:
        return None
    return state


def _record_enrollment_event(root: Path, payload: dict[str, Any]) -> None:
    state_path = root / _CODEX_STATE
    state = _enrollment_state(root)
    if state is None:
        return
    state["last_seen_at"] = _utc_now()
    state["last_session_id"] = payload.get("session_id")
    state["last_event"] = payload.get("hook_event_name")
    _atomic_write_json(state_path, state)


def _load_session(root: Path, session_id: str) -> dict[str, Any]:
    path = _session_state_path(root, session_id)
    if not path.exists():
        raise ValueError(
            "Codex session is not bootstrapped; start Codex in the enrolled project first"
        )
    state = _read_json_object(path)
    if state.get("protocol") != CODEX_SESSION_PROTOCOL:
        raise ValueError(f"unsupported Codex session protocol in {path}")
    if state.get("session_id") != session_id:
        raise ValueError("Codex session identity does not match local session state")
    return state


def _write_session(root: Path, session: dict[str, Any]) -> None:
    session_id = str(session["session_id"])
    session["updated_at"] = _utc_now()
    _atomic_write_json(_session_state_path(root, session_id), session)


def _record_session_handshake(
    root: Path, payload: dict[str, Any]
) -> dict[str, Any] | None:
    if _enrollment_state(root) is None:
        return None
    _record_enrollment_event(root, payload)

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    session_id = session_id.strip()
    path = _session_state_path(root, session_id)
    now = _utc_now()
    if path.exists():
        session = _load_session(root, session_id)
        session["source"] = str(payload.get("source") or session.get("source") or "startup")
        session["last_event"] = "SessionStart"
        session["last_seen_at"] = now
        _write_session(root, session)
        return session

    dirty, status_digest = _worktree_status(root)
    session = {
        "protocol": CODEX_SESSION_PROTOCOL,
        "session_id": session_id,
        "root": str(root),
        "source": str(payload.get("source") or "startup"),
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,
        "last_event": "SessionStart",
        "session_open_commit": _head_commit(root),
        "session_open_branch": _branch_name(root),
        "session_open_worktree_dirty": dirty,
        "session_open_status_sha256": status_digest,
        "task_id": None,
        "reserved_intent_id": None,
        "owner": None,
        "task_base_commit": None,
        "task_state": "awaiting_prompt",
        "active_intent_id": None,
    }
    _write_session(root, session)
    return session


def _ensure_session(root: Path, payload: dict[str, Any]) -> dict[str, Any] | None:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    session_id = session_id.strip()
    path = _session_state_path(root, session_id)
    if path.exists():
        return _load_session(root, session_id)
    synthetic = dict(payload)
    synthetic["hook_event_name"] = "SessionStart"
    synthetic.setdefault("source", "startup")
    return _record_session_handshake(root, synthetic)


def _ensure_task_bootstrap(
    root: Path, payload: dict[str, Any]
) -> dict[str, Any] | None:
    session = _ensure_session(root, payload)
    if session is None:
        return None
    if session.get("task_id"):
        session["last_event"] = "UserPromptSubmit"
        session["last_seen_at"] = _utc_now()
        _write_session(root, session)
        return session

    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        prompt = ""
    prompt_digest = _sha256_text(prompt)
    base_commit = _head_commit(root)
    token = _sha256_text(
        f"{session['session_id']}\0{base_commit}\0{prompt_digest}"
    )[:20]
    dirty, status_digest = _worktree_status(root)
    session.update(
        {
            "task_id": f"codex-task-{token}",
            "reserved_intent_id": f"codex-intent-{token}",
            "owner": f"codex:{_session_key(str(session['session_id']))}",
            "task_base_commit": base_commit,
            "task_base_revision": base_commit,
            "task_branch": _branch_name(root),
            "task_worktree_dirty": dirty,
            "task_status_sha256": status_digest,
            "prompt_sha256": prompt_digest,
            "prompt_length": len(prompt),
            "task_bootstrapped_at": _utc_now(),
            "task_state": "awaiting_intent",
            "last_event": "UserPromptSubmit",
            "last_seen_at": _utc_now(),
        }
    )
    _write_session(root, session)
    return session


def _string_list(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, list):
        raise ValueError(f"Codex intent proposal '{field}' must be a JSON array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"Codex intent proposal '{field}' entries must be non-empty strings"
            )
        result.append(item.strip())
    return tuple(result)


def _validate_operation_path(operation: IntentOperation) -> None:
    if operation.resource.kind not in {ResourceKind.FILE, ResourceKind.DOCUMENT}:
        return
    raw = operation.resource.identifier.replace("\\", "/").strip()
    if raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw):
        raise ValueError("Codex file/document resources must be repository-relative")
    normalized = posixpath.normpath(raw)
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError("Codex file/document resources cannot escape the repository")


def _intent_from_proposal(
    session: dict[str, Any], proposal: dict[str, Any]
) -> tuple[ChangeIntent, str]:
    allowed_keys = {
        "protocol",
        "goal",
        "operations",
        "preserves",
        "acceptance",
        "dependencies",
        "metadata",
    }
    unknown = sorted(set(proposal) - allowed_keys)
    if unknown:
        raise ValueError(
            "unsupported Codex intent proposal field(s): " + ", ".join(unknown)
        )
    if proposal.get("protocol") != CODEX_INTENT_PROPOSAL_PROTOCOL:
        raise ValueError(
            f"Codex intent proposal protocol must be {CODEX_INTENT_PROPOSAL_PROTOCOL!r}"
        )
    goal = proposal.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("Codex intent proposal requires a non-empty 'goal'")
    goal = goal.strip()

    raw_operations = proposal.get("operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        raise ValueError("Codex intent proposal requires a non-empty 'operations' array")
    operations = tuple(IntentOperation.from_dict(item) for item in raw_operations)
    for operation in operations:
        _validate_operation_path(operation)

    proposal_metadata = proposal.get("metadata") or {}
    if not isinstance(proposal_metadata, dict):
        raise ValueError("Codex intent proposal 'metadata' must be a JSON object")
    metadata = {
        **proposal_metadata,
        "goal": goal,
        "connector": "codex",
        "codex_session_id": str(session["session_id"]),
        "prompt_sha256": str(session.get("prompt_sha256") or ""),
        "bootstrap_protocol": CODEX_SESSION_PROTOCOL,
    }

    intent = ChangeIntent(
        intent_id=str(session["reserved_intent_id"]),
        task_id=str(session["task_id"]),
        owner=str(session["owner"]),
        base_revision=str(session["task_base_revision"]),
        base_commit=str(session["task_base_commit"]),
        operations=operations,
        preserves=_string_list(proposal.get("preserves"), field="preserves"),
        acceptance=_string_list(proposal.get("acceptance"), field="acceptance"),
        dependencies=_string_list(proposal.get("dependencies"), field="dependencies"),
        metadata=metadata,
    )
    return intent, goal


def _operation_summary(operation: IntentOperation) -> dict[str, Any]:
    return {
        "access": operation.access.value,
        "kind": operation.resource.kind.value,
        "identifier": operation.resource.identifier,
        "region": operation.resource.region,
        "commitment": operation.commitment.value,
    }


def admit_codex_intent(
    root_or_child: str | Path,
    *,
    session_id: str,
    proposal: dict[str, Any],
) -> dict[str, Any]:
    """Bind and atomically admit one model-proposed intent to a Codex session."""

    root = resolve_project_root(root_or_child)
    _require_initialized(root)
    if _enrollment_state(root) is None:
        raise ValueError("Codex is not enrolled; run 'claim-plane connect codex' first")
    session = _load_session(root, session_id)
    if not session.get("task_id") or not session.get("task_base_commit"):
        raise ValueError("Codex session has no task bootstrap; submit a prompt first")

    expected_base = str(session["task_base_commit"])
    current_base = _head_commit(root)
    if current_base != expected_base:
        raise ValueError(
            "Codex task base revision changed before admission; "
            "start a fresh session or task bootstrap"
        )

    intent, goal = _intent_from_proposal(session, proposal)
    plane = Plane.open(root / _PLANE_DB)
    try:
        decision = plane.admit(intent)
        if decision.allowed:
            plane.activate(intent.intent_id)
    finally:
        plane.close()

    now = _utc_now()
    session["last_event"] = "IntentAdmission"
    session["last_seen_at"] = now
    session["last_admission_at"] = now
    session["last_admission_allowed"] = decision.allowed
    if decision.allowed:
        session["active_intent_id"] = intent.intent_id
        session["intent_fingerprint"] = intent.fingerprint()
        session["intent_goal"] = goal
        session["intent_admitted_at"] = now
        session["task_state"] = "active"
        session["committed_scope"] = [
            _operation_summary(item) for item in intent.committed_operations
        ]
        session["contingent_scope"] = [
            _operation_summary(item) for item in intent.contingent_operations
        ]
        session["preserves"] = list(intent.preserves)
        session["acceptance"] = list(intent.acceptance)
    else:
        session["task_state"] = "blocked"
    _write_session(root, session)

    return {
        "protocol": CODEX_INTENT_ADMISSION_PROTOCOL,
        "allowed": decision.allowed,
        "session_id": session_id,
        "task_id": intent.task_id,
        "intent_id": intent.intent_id,
        "owner": intent.owner,
        "base_commit": intent.base_commit,
        "goal": goal,
        "state": "active" if decision.allowed else "blocked",
        "committed_scope": [
            _operation_summary(item) for item in intent.committed_operations
        ],
        "contingent_scope": [
            _operation_summary(item) for item in intent.contingent_operations
        ],
        "preserves": list(intent.preserves),
        "acceptance": list(intent.acceptance),
        "decision": decision.to_dict(),
    }


def codex_intent_status(
    root_or_child: str | Path, *, session_id: str
) -> dict[str, Any]:
    """Return the local session-to-intent binding without exposing prompt text."""

    root = resolve_project_root(root_or_child)
    session = _load_session(root, session_id)
    return {
        "protocol": "claim-plane.codex-intent-status.v1",
        "session_id": session_id,
        "task_id": session.get("task_id"),
        "intent_id": session.get("active_intent_id"),
        "state": session.get("task_state"),
        "base_commit": session.get("task_base_commit"),
        "goal": session.get("intent_goal"),
        "committed_scope": list(session.get("committed_scope") or ()),
        "contingent_scope": list(session.get("contingent_scope") or ()),
        "preserves": list(session.get("preserves") or ()),
        "acceptance": list(session.get("acceptance") or ()),
        "worktree_dirty_at_bootstrap": bool(session.get("task_worktree_dirty")),
    }


def _scope_lines(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        access = str(item.get("access") or "")
        kind = str(item.get("kind") or "")
        identifier = str(item.get("identifier") or "")
        commitment = str(item.get("commitment") or "committed")
        suffix = " (contingent)" if commitment == "contingent" else ""
        lines.append(f"- {access} {kind}:{identifier}{suffix}")
    return lines


def _task_context(session: dict[str, Any]) -> str:
    if session.get("active_intent_id"):
        committed = _scope_lines(session.get("committed_scope")) or ["- none"]
        contingent = _scope_lines(session.get("contingent_scope")) or ["- none"]
        preserves = [f"- {item}" for item in session.get("preserves") or ()] or [
            "- none"
        ]
        acceptance = [
            f"- {item}" for item in session.get("acceptance") or ()
        ] or ["- none"]
        return "\n".join(
            [
                "Claim Plane execution contract is active for this Codex session.",
                f"Task: {session.get('task_id')}",
                f"Intent: {session.get('active_intent_id')}",
                f"Base commit: {session.get('task_base_commit')}",
                f"Goal: {session.get('intent_goal')}",
                "Committed scope:",
                *committed,
                "Contingent scope:",
                *contingent,
                "Preserve requirements:",
                *preserves,
                "Acceptance:",
                *acceptance,
                "Treat this admitted ChangeIntent as the authority boundary for the task.",
            ]
        )

    dirty_note = (
        "The worktree already had local changes when the task was bootstrapped; "
        "do not treat them as agent-authored changes."
        if session.get("task_worktree_dirty")
        else "The worktree was clean when the task was bootstrapped."
    )
    session_id = str(session["session_id"])
    return "\n".join(
        [
            "Claim Plane is enrolled for this Codex session.",
            f"Session: {session_id}",
            f"Task: {session.get('task_id')}",
            f"Pinned base commit: {session.get('task_base_commit')}",
            dirty_note,
            "Before the first repository mutation, inspect the repository read-only "
            "and admit one ChangeIntent for this task.",
            "Submit the proposal as JSON on stdin with:",
            f"claim-plane codex-intent admit --session-id {json.dumps(session_id)} --repo .",
            "Proposal shape:",
            json.dumps(
                {
                    "protocol": CODEX_INTENT_PROPOSAL_PROTOCOL,
                    "goal": "concise intended outcome",
                    "operations": [
                        {
                            "access": "write",
                            "kind": "file",
                            "identifier": "path/to/file.py",
                            "commitment": "committed",
                        },
                        {
                            "access": "write",
                            "kind": "file",
                            "identifier": "plausible/fallback.py",
                            "commitment": "contingent",
                            "required": False,
                        },
                    ],
                    "preserves": [],
                    "acceptance": [],
                },
                separators=(",", ":"),
            ),
            "Use committed scope for expected changes and contingent scope only for "
            "plausible fallback surfaces. Do not provide intent_id, owner, or base "
            "revision; Claim Plane binds those to this session.",
        ]
    )


def _write_hook_output(output: TextIO | None, payload: dict[str, Any]) -> None:
    if output is None:
        return
    json.dump(payload, output, ensure_ascii=False, separators=(",", ":"))
    output.write("\n")


def _record_session_lifecycle_event(root: Path, payload: dict[str, Any]) -> None:
    session = _ensure_session(root, payload)
    if session is None:
        return
    event = str(payload.get("hook_event_name") or "")
    session["last_event"] = event
    session["last_seen_at"] = _utc_now()
    if event == "SessionEnd":
        session["ended_at"] = _utc_now()
        reason = payload.get("reason")
        if isinstance(reason, str):
            session["end_reason"] = reason
    _write_session(root, session)


def _heartbeat_session_intent(root: Path, session: dict[str, Any]) -> None:
    intent_id = session.get("active_intent_id")
    if not isinstance(intent_id, str) or not intent_id:
        return
    plane = Plane.open(root / _PLANE_DB)
    try:
        plane.heartbeat(intent_id)
    finally:
        plane.close()


def handle_codex_hook(
    payload: dict[str, Any], *, output: TextIO | None = None
) -> int:
    """Dispatch a Codex lifecycle event for an enrolled project.

    The project hook command remains stable across connector versions. Session and
    task binding can therefore evolve without rewriting or re-trusting the project
    hook definition.
    """

    event = payload.get("hook_event_name")
    if event not in CODEX_HOOK_EVENTS:
        return 0
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return 0
    try:
        root = resolve_project_root(cwd)
    except (FileNotFoundError, ValueError):
        return 0
    if _enrollment_state(root) is None:
        return 0

    if event == "SessionStart":
        _record_session_handshake(root, payload)
        return 0

    _record_enrollment_event(root, payload)
    if event == "UserPromptSubmit":
        session = _ensure_task_bootstrap(root, payload)
        if session is None:
            return 0
        _heartbeat_session_intent(root, session)
        _write_hook_output(
            output,
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _task_context(session),
                }
            },
        )
        return 0

    if event in {"PreToolUse", "PostToolUse", "Stop"}:
        session = _ensure_session(root, payload)
        if session is not None:
            _heartbeat_session_intent(root, session)

    if event in {"Stop", "SessionEnd"}:
        _record_session_lifecycle_event(root, payload)
    return 0
