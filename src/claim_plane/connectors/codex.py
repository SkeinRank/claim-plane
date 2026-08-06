"""Project-local Codex enrollment and lifecycle dispatch."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, TextIO

from claim_plane.connectors.codex_amendment import (
    CODEX_SCOPE_AMENDMENT_PROTOCOL,
    CODEX_SCOPE_AMENDMENT_TTL_SECONDS,
    build_scope_amendment,
    mutation_from_dict,
    mutation_to_dict,
)
from claim_plane.connectors.codex_completion import (
    CODEX_COMPLETION_PROTOCOL,
    stop_block_reason,
    verify_completion,
)
from claim_plane.connectors.codex_guard import (
    CODEX_GUARD_PROTOCOL,
    GuardEvaluation,
    amendment_mutations,
    denied_hook_output,
    evaluate_pre_tool_use,
    promotion_modes,
    protected_control_path,
)
from claim_plane.core import (
    AccessMode,
    ChangeIntent,
    IntentOperation,
    Plane,
    ResourceKind,
    ResourceRef,
    ScopeCommitment,
)
from claim_plane.policy import (
    EffectivePolicy,
    PolicyAction,
    PreWriteMode,
    resolve_policy,
)
from claim_plane.project import (
    PROJECT_STATE_PROTOCOL,
    doctor_project,
    init_project as initialize_project,
    load_project_config,
    resolve_project_root as resolve_enrolled_project_root,
    set_adapter_enabled,
)
from claim_plane.test_feedback import TEST_FEEDBACK_PROTOCOL

PROJECT_PROTOCOL = PROJECT_STATE_PROTOCOL
CODEX_ENROLLMENT_PROTOCOL = "claim-plane.codex-enrollment.v1"
CODEX_SESSION_PROTOCOL = "claim-plane.codex-session.v1"
CODEX_INTENT_PROPOSAL_PROTOCOL = "claim-plane.codex-intent-proposal.v1"
CODEX_INTENT_ADMISSION_PROTOCOL = "claim-plane.codex-intent-admission.v1"
CODEX_HOOK_COMMAND = "claim-plane codex-hook"
CODEX_MIN_GUARD_VERSION = (0, 123, 0)
CODEX_COMPLETION_ACCEPTANCE_TIMEOUT = 300
CODEX_CONNECTOR_REVISION = 13
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


_TASK_OBLIGATION_PROTOCOL = "claim-plane.task-obligations.v1"
_TEST_CHANGE_PATTERNS = (
    "tests/**",
    "test/**",
    "**/tests/**",
    "**/test/**",
    "test_*.py",
    "*_test.py",
    "*.test.*",
    "*.spec.*",
    "**/test_*.py",
    "**/*_test.py",
    "**/*.test.*",
    "**/*.spec.*",
)
_TEST_REQUEST_PATTERNS = (
    re.compile(
        r"\b(?:add|create|write|update|extend|include|cover)\b"
        r"[^.\n]{0,120}\b(?:unit\s+|regression\s+|integration\s+)?tests?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:tests?|test\s+coverage|regression\s+coverage)\b"
        r"[^.\n]{0,120}\b(?:add|create|write|update|extend|include|cover)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:test|regression)\s+coverage\b", re.IGNORECASE),
)


def _infer_task_obligations(prompt: str) -> list[dict[str, Any]]:
    """Extract bounded, non-secret completion obligations from an operator prompt.

    Prompt text is never persisted. Only a small structured obligation and the
    already-recorded prompt digest survive in session state. The initial rule is
    intentionally narrow: an explicit request to add or update tests requires at
    least one test artifact to change before delivery can be verified.
    """

    if not prompt or not any(
        pattern.search(prompt) for pattern in _TEST_REQUEST_PATTERNS
    ):
        return []
    return [
        {
            "protocol": _TASK_OBLIGATION_PROTOCOL,
            "id": "test_change",
            "kind": "changed_path_any",
            "description": "requested test coverage must be updated",
            "patterns": list(_TEST_CHANGE_PATTERNS),
            "source_prompt_sha256": _sha256_text(prompt),
        }
    ]


def _merge_task_obligations(
    existing: Any, inferred: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in existing or ():
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            merged[str(item["id"])] = dict(item)
    for item in inferred:
        merged[str(item["id"])] = dict(item)
    return [merged[key] for key in sorted(merged)]


def _file_sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _connector_control_fingerprints(root: Path) -> dict[str, str | None]:
    return {
        str(_CODEX_HOOKS): _file_sha256(root / _CODEX_HOOKS),
        str(_CODEX_CONFIG): _file_sha256(root / _CODEX_CONFIG),
    }


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

    return resolve_enrolled_project_root(root_or_child)


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
    """Initialize project identity, versioned configuration, and local state."""

    return initialize_project(root_or_child)


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
    if event == "SessionEnd":
        timeout = 3
    elif event == "Stop":
        timeout = CODEX_COMPLETION_ACCEPTANCE_TIMEOUT + 60
    else:
        timeout = 30
    group["hooks"] = [
        {
            "type": "command",
            "command": CODEX_HOOK_COMMAND,
            "timeout": timeout,
        }
    ]
    return group


def _is_claim_plane_handler(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("type") != "command":
        return False
    command = value.get("command")
    if not isinstance(command, str):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return command.strip() == CODEX_HOOK_COMMAND
    return argv[:2] == ["claim-plane", "codex-hook"]


def _claim_plane_hook_drift(payload: dict[str, Any], path: Path) -> list[str]:
    """Return connector-owned hook-definition drift without judging foreign hooks."""

    hooks = _validate_hooks_shape(payload, path)
    problems: list[str] = []
    for event in CODEX_HOOK_EVENTS:
        matches: list[dict[str, Any]] = []
        for group in hooks.get(event, []):
            handlers = group.get("hooks") or []
            if any(_is_claim_plane_handler(handler) for handler in handlers):
                matches.append(group)
        if len(matches) != 1:
            problems.append(
                f"{event}: expected one Claim Plane handler, found {len(matches)}"
            )
            continue
        if matches[0] != _canonical_group(event):
            problems.append(f"{event}: Claim Plane hook definition drifted")
    return problems


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

    executable, runtime_version = _codex_version()
    sandbox_status, sandbox_detail = _codex_sandbox_status()
    state = {
        "protocol": CODEX_ENROLLMENT_PROTOCOL,
        "created_at": created_at,
        "updated_at": now,
        "hooks_path": _CODEX_HOOKS.as_posix(),
        "hook_command": CODEX_HOOK_COMMAND,
        "events": list(CODEX_HOOK_EVENTS),
        "connector_revision": CODEX_CONNECTOR_REVISION,
        "runtime": {
            "executable": executable,
            "version": runtime_version,
        },
        "sandbox": {
            "status": sandbox_status,
            "detail": sandbox_detail,
        },
    }
    _atomic_write_json(state_path, state)
    set_adapter_enabled(root, "codex", enabled=True)
    return {
        "root": str(root),
        "hooks": str(hooks_path),
        "state": str(state_path),
        "events": list(CODEX_HOOK_EVENTS),
        "inline_hooks_present": _has_inline_hooks(config_path),
        "runtime_executable": executable,
        "runtime_version": runtime_version,
        "sandbox_status": sandbox_status,
        "sandbox_detail": sandbox_detail,
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
    try:
        set_adapter_enabled(root, "codex", enabled=False)
    except ValueError:
        pass
    return {
        "root": str(root),
        "removed_handlers": removed,
        "connected": False,
    }


def _parse_codex_version(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", value)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


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


def _codex_auth_status(executable: str | None) -> tuple[str, str]:
    if os.environ.get("OPENAI_API_KEY"):
        return "ok", "authentication is available through the process environment"
    if executable is None:
        return (
            "error",
            "Codex runtime is unavailable, so authentication cannot be checked",
        )
    try:
        completed = subprocess.run(
            [executable, "login", "status"],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "warning", "Codex authentication status could not be queried"
    summary = (completed.stdout.strip() or completed.stderr.strip()).lower()
    if completed.returncode == 0:
        return "ok", "Codex reports an available authentication session"
    if any(
        item in summary for item in ("not logged", "unauthenticated", "login required")
    ):
        return "error", "Codex is installed but no authentication session is available"
    return (
        "warning",
        "Codex authentication status is not exposed by this runtime version",
    )


def _codex_sandbox_status() -> tuple[str, str]:
    if os.name == "posix" and shutil.which("bwrap"):
        return (
            "ok",
            "Linux Bubblewrap is available for a brokered non-bypassable boundary",
        )
    if shutil.which("sandbox-exec"):
        return (
            "warning",
            "macOS sandbox-exec is available; project-local hooks remain "
            "bypassable by host writes",
        )
    return (
        "warning",
        "project-local hook enforcement is available; out-of-band host writes "
        "remain post-verified",
    )


def doctor_codex(root_or_child: str | Path = ".") -> CodexDoctorReport:
    """Inspect local enrollment without modifying project state."""

    root = resolve_project_root(root_or_child)
    project_report = doctor_project(root)
    checks: list[dict[str, Any]] = [dict(item) for item in project_report.checks]

    status_lines = tuple(
        line
        for line in _git(
            root, "status", "--porcelain=v1", "--untracked-files=all"
        ).splitlines()
        if line.strip()
    )
    managed_paths = {".codex/hooks.json"}
    managed_lines = tuple(
        line
        for line in status_lines
        if line[3:].strip().replace("\\", "/") in managed_paths
    )
    non_managed_lines = tuple(
        line for line in status_lines if line not in managed_lines
    )
    if status_lines and not non_managed_lines:
        for item in checks:
            if item.get("name") == "working_tree":
                item.update(
                    {
                        "status": "info",
                        "detail": (
                            "working tree changes are limited to managed Codex "
                            "connector state"
                        ),
                    }
                )
                break
    checks.append(
        {
            "name": "managed_connector_state",
            "status": "info" if managed_lines else "ok",
            "detail": (
                ", ".join(line[3:].strip() for line in managed_lines)
                if managed_lines
                else "no untracked managed connector files"
            ),
        }
    )
    project_checks = {str(item.get("name")): item for item in checks}
    project_initialized = all(
        str(project_checks.get(name, {}).get("status")) == "ok"
        for name in ("project_config", "project_state", "state_directory")
    )
    checks.append(
        {
            "name": "project_initialized",
            "status": "ok" if project_initialized else "error",
            "detail": str(root / _PROJECT_STATE),
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

    hook_drift: list[str] = []
    if hooks_path.exists():
        try:
            hook_drift = _claim_plane_hook_drift(_load_hooks(hooks_path), hooks_path)
        except (json.JSONDecodeError, ValueError) as exc:
            hook_drift = [str(exc)]
    else:
        hook_drift = ["project-local hooks file is missing"]
    checks.append(
        {
            "name": "connector_hook_definition",
            "status": "error" if hook_drift else "ok",
            "detail": (
                "; ".join(hook_drift)
                if hook_drift
                else f"connector revision {CODEX_CONNECTOR_REVISION} hook definitions are canonical"
            ),
        }
    )

    connector_revision = 0
    if state_path.exists():
        try:
            connector_revision = int(
                _read_json_object(state_path).get("connector_revision") or 0
            )
        except (json.JSONDecodeError, ValueError, TypeError):
            connector_revision = 0
    checks.append(
        {
            "name": "connector_revision",
            "status": "ok"
            if connector_revision == CODEX_CONNECTOR_REVISION
            else "error",
            "detail": (
                f"revision {connector_revision}"
                if connector_revision == CODEX_CONNECTOR_REVISION
                else f"enrollment revision {connector_revision}; run 'claim-plane connect codex' to repair to {CODEX_CONNECTOR_REVISION}"
            ),
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
    auth_status, auth_detail = _codex_auth_status(executable)
    checks.append(
        {
            "name": "codex_authentication",
            "status": auth_status,
            "detail": auth_detail,
        }
    )
    sandbox_status, sandbox_detail = _codex_sandbox_status()
    checks.append(
        {
            "name": "sandbox_boundary",
            "status": sandbox_status,
            "detail": sandbox_detail,
        }
    )

    parsed_version = _parse_codex_version(version)
    minimum = ".".join(str(item) for item in CODEX_MIN_GUARD_VERSION)
    if executable is None:
        guard_status = "error"
        guard_detail = "Codex executable is required for pre-mutation guard checks"
    elif parsed_version is None:
        guard_status = "warning"
        guard_detail = f"could not parse Codex version; guard requires {minimum}+"
    elif parsed_version < CODEX_MIN_GUARD_VERSION:
        guard_status = "error"
        guard_detail = (
            f"Codex {'.'.join(str(item) for item in parsed_version)} is too old; "
            f"pre-mutation guard requires {minimum}+"
        )
    else:
        guard_status = "ok"
        guard_detail = (
            f"Codex version supports the required apply_patch hook surface ({minimum}+)"
        )
    checks.append(
        {
            "name": "pre_mutation_guard_compatibility",
            "status": guard_status,
            "detail": guard_detail,
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
    if Path(str(state.get("root") or "")).resolve() != root.resolve():
        raise ValueError("Codex session state is bound to a different repository root")
    return state


def _write_session(root: Path, session: dict[str, Any]) -> None:
    session_id = str(session["session_id"])
    session["updated_at"] = _utc_now()
    _atomic_write_json(_session_state_path(root, session_id), session)


def _path_fingerprint(path: Path) -> str:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return "missing"
    if path.is_symlink():
        return "symlink:" + _sha256_text(os.readlink(path))
    if path.is_file():
        return "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if path.is_dir():
        return "dir"
    return f"other:{stat.st_mode}"


def _preexisting_worktree_baseline(root: Path) -> dict[str, str]:
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "-z", "HEAD", "--"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if tracked.returncode != 0:
        raise ValueError(
            tracked.stderr.decode("utf-8", errors="replace").strip()
            or "could not inspect tracked worktree changes"
        )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if untracked.returncode != 0:
        raise ValueError(
            untracked.stderr.decode("utf-8", errors="replace").strip()
            or "could not inspect untracked worktree changes"
        )
    raw_paths = tracked.stdout.split(b"\0") + untracked.stdout.split(b"\0")
    result: dict[str, str] = {}
    for raw in raw_paths:
        if not raw:
            continue
        path = raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        if protected_control_path(path):
            continue
        result[path] = _path_fingerprint(root / path)
    return dict(sorted(result.items()))


def _other_active_codex_sessions(root: Path, session_id: str) -> list[str]:
    if not (root / _PLANE_DB).exists():
        return []
    plane = Plane.open(root / _PLANE_DB)
    try:
        active = {
            str(item.get("intent_id")) for item in plane.intents(active_only=True)
        }
    finally:
        plane.close()
    if not active:
        return []
    owners: list[str] = []
    sessions_dir = root / _CODEX_SESSIONS
    if not sessions_dir.exists():
        return owners
    for path in sessions_dir.glob("*.json"):
        try:
            other = _read_json_object(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        other_session = other.get("session_id")
        other_intent = other.get("active_intent_id")
        if (
            isinstance(other_session, str)
            and other_session != session_id
            and isinstance(other_intent, str)
            and other_intent in active
        ):
            owners.append(other_session)
    return sorted(set(owners))


def _recover_resumed_session(root: Path, session: dict[str, Any]) -> None:
    """Renew or safely re-admit an expired intent when Codex resumes a session."""

    intent_id = session.get("active_intent_id")
    if not isinstance(intent_id, str) or not intent_id:
        return
    plane = Plane.open(root / _PLANE_DB)
    try:
        records = {str(item.get("intent_id")): item for item in plane.intents()}
        record = records.get(intent_id)
        if record is None:
            session["task_state"] = "recovery_required"
            session["recovery_reason"] = "bound intent is missing from the registry"
            return
        state = str(record.get("state") or "")
        if state in {"active", "admitted"}:
            if state == "admitted":
                plane.activate(intent_id)
            plane.heartbeat(intent_id)
            session["task_state"] = "active"
            session["last_resume_at"] = _utc_now()
            return
        if state == "completed":
            if (session.get("completion") or {}).get("verified"):
                session["task_state"] = "verified"
            return
        if state != "expired":
            session["task_state"] = "recovery_required"
            session["recovery_reason"] = f"bound intent is {state}, not resumable"
            return
        expected_base = str(session.get("task_base_commit") or "")
        if not expected_base or _head_commit(root) != expected_base:
            session["task_state"] = "recovery_required"
            session["recovery_reason"] = (
                "Git HEAD changed while the Codex session was inactive"
            )
            return
        expected_branch = str(session.get("task_branch") or "")
        if expected_branch and _branch_name(root) != expected_branch:
            session["task_state"] = "recovery_required"
            session["recovery_reason"] = (
                "Git branch changed while the Codex session was inactive"
            )
            return
        old = plane.intent(intent_id)
        if old is None:
            session["task_state"] = "recovery_required"
            session["recovery_reason"] = "expired intent payload is unavailable"
            return
        resume_count = int(session.get("resume_recoveries") or 0) + 1
        successor_id = f"{intent_id}-resume-{resume_count}"
        successor = replace(
            old,
            intent_id=successor_id,
            metadata={
                **old.metadata,
                "resumed_from_intent_id": intent_id,
                "resume_recovery": resume_count,
            },
        )
        decision = plane.admit(successor)
        if not decision.allowed:
            session["task_state"] = "recovery_blocked"
            session["recovery_reason"] = (
                "expired intent could not be re-admitted under current coordination state"
            )
            session["recovery_decision"] = decision.to_dict()
            return
        plane.activate(successor_id)
        session["active_intent_id"] = successor_id
        session["reserved_intent_id"] = successor_id
        session["resume_recoveries"] = resume_count
        session["recovered_from_intent_id"] = intent_id
        session["last_resume_at"] = _utc_now()
        session["task_state"] = "active"
        session.pop("recovery_reason", None)
        session.pop("recovery_decision", None)
        _sync_session_scope(session, successor)
    finally:
        plane.close()


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
        source = str(payload.get("source") or session.get("source") or "startup")
        session["source"] = source
        controlled_run_id = payload.get("_claim_plane_run_id")
        if isinstance(controlled_run_id, str) and controlled_run_id:
            session["controlled_run_id"] = controlled_run_id
            controlled_policy = payload.get("_claim_plane_policy")
            if isinstance(controlled_policy, str) and controlled_policy:
                session["controlled_policy"] = controlled_policy
            policy_manifest = payload.get("_claim_plane_policy_manifest")
            if isinstance(policy_manifest, Mapping):
                effective = EffectivePolicy.from_dict(policy_manifest)
                session["controlled_policy_manifest"] = effective.to_dict()
        controlled_scope = payload.get("_claim_plane_initial_scope")
        if isinstance(controlled_scope, list) and all(
            isinstance(item, str) and item for item in controlled_scope
        ):
            session["operator_initial_scope"] = list(dict.fromkeys(controlled_scope))
        if payload.get("_claim_plane_scope_locked") is True:
            session["operator_scope_locked"] = True
        if payload.get("_claim_plane_interactive") is True:
            session["controlled_interactive"] = True
        session["last_event"] = "SessionStart"
        session["last_seen_at"] = now
        session.pop("ended_at", None)
        session.pop("end_reason", None)
        if source == "resume":
            _recover_resumed_session(root, session)
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
        "controlled_run_id": (
            str(payload.get("_claim_plane_run_id"))
            if isinstance(payload.get("_claim_plane_run_id"), str)
            and payload.get("_claim_plane_run_id")
            else None
        ),
        "controlled_policy": (
            str(payload.get("_claim_plane_policy"))
            if isinstance(payload.get("_claim_plane_policy"), str)
            and payload.get("_claim_plane_policy")
            else None
        ),
        "controlled_policy_manifest": (
            EffectivePolicy.from_dict(payload["_claim_plane_policy_manifest"]).to_dict()
            if isinstance(payload.get("_claim_plane_policy_manifest"), Mapping)
            else None
        ),
        "operator_initial_scope": (
            list(dict.fromkeys(payload["_claim_plane_initial_scope"]))
            if isinstance(payload.get("_claim_plane_initial_scope"), list)
            and all(
                isinstance(item, str) and item
                for item in payload["_claim_plane_initial_scope"]
            )
            else []
        ),
        "operator_scope_locked": payload.get("_claim_plane_scope_locked") is True,
        "controlled_interactive": payload.get("_claim_plane_interactive") is True,
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
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            prompt = ""
        session["last_event"] = "UserPromptSubmit"
        session["last_seen_at"] = _utc_now()
        session["last_prompt_sha256"] = _sha256_text(prompt)
        session["last_prompt_length"] = len(prompt)
        session["task_obligations"] = _merge_task_obligations(
            session.get("task_obligations"), _infer_task_obligations(prompt)
        )
        session["prompt_turn_count"] = int(session.get("prompt_turn_count") or 1) + 1
        if (
            session.get("controlled_interactive") is True
            and session.get("task_state") == "awaiting_final_verification"
            and session.get("active_intent_id")
        ):
            session["task_state"] = "active"
            session["resumed_after_turn_at"] = session["last_seen_at"]
        _write_session(root, session)
        return session

    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        prompt = ""
    prompt_digest = _sha256_text(prompt)
    base_commit = _head_commit(root)
    token = _sha256_text(f"{session['session_id']}\0{base_commit}\0{prompt_digest}")[
        :20
    ]
    dirty, status_digest = _worktree_status(root)
    preexisting = _preexisting_worktree_baseline(root)
    session.update(
        {
            "task_id": f"codex-task-{token}",
            "reserved_intent_id": f"codex-intent-{token}",
            "owner": f"codex:{_session_key(str(session['session_id']))}",
            "task_base_commit": base_commit,
            "task_base_revision": base_commit,
            "task_branch": _branch_name(root),
            "task_worktree_dirty": bool(preexisting),
            "task_status_sha256": status_digest,
            "preexisting_worktree": preexisting,
            "connector_control_fingerprints": _connector_control_fingerprints(root),
            "prompt_sha256": prompt_digest,
            "prompt_length": len(prompt),
            "prompt_turn_count": 1,
            "last_prompt_sha256": prompt_digest,
            "last_prompt_length": len(prompt),
            "task_obligations": _infer_task_obligations(prompt),
            "required_acceptance": list(_configured_acceptance_commands(root)),
            "task_bootstrapped_at": _utc_now(),
            "task_state": "awaiting_intent",
            "last_event": "UserPromptSubmit",
            "last_seen_at": _utc_now(),
        }
    )
    _write_session(root, session)
    return session


def _configured_acceptance_commands(root: Path) -> tuple[str, ...]:
    """Return project-required acceptance commands in stable order."""

    config = load_project_config(root)
    acceptance = config.get("acceptance")
    commands = acceptance.get("commands") if isinstance(acceptance, Mapping) else ()
    result: list[str] = []
    for item in commands or ():
        if not isinstance(item, str) or not item.strip():
            continue
        command = item.strip()
        if command not in result:
            result.append(command)
    return tuple(result)


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
    if protected_control_path(normalized):
        raise ValueError(
            "Codex file/document resources cannot grant Claim Plane, Git, or Codex "
            "connector control state"
        )


def _operator_scope(session: Mapping[str, Any]) -> tuple[str, ...]:
    raw = session.get("operator_initial_scope")
    if not isinstance(raw, list):
        return ()
    return tuple(
        dict.fromkeys(
            item.strip() for item in raw if isinstance(item, str) and item.strip()
        )
    )


def _scope_selector_covers(selector: str, identifier: str) -> bool:
    return ResourceRef(kind=ResourceKind.FILE, identifier=selector).covers_path(
        identifier
    )


def _apply_operator_initial_scope(
    session: Mapping[str, Any], operations: tuple[IntentOperation, ...]
) -> tuple[tuple[IntentOperation, ...], tuple[dict[str, Any], ...]]:
    """Apply an operator-provided initial authority ceiling to a model proposal.

    The planner still proposes the task intent. Explicit scope only constrains the
    first admitted mutation surface. Out-of-scope file operations are omitted so a
    later concrete write must cross the normal one-time amendment path.
    """

    selectors = _operator_scope(session)
    if not selectors:
        return operations, ()

    retained: list[IntentOperation] = []
    omitted: list[dict[str, Any]] = []
    for operation in operations:
        if (
            operation.access.mutating
            and operation.resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}
            and not any(
                _scope_selector_covers(selector, operation.resource.identifier)
                for selector in selectors
            )
        ):
            omitted.append(_operation_summary(operation))
            continue
        retained.append(operation)

    existing_identifiers = {
        operation.resource.identifier
        for operation in retained
        if operation.access.mutating
        and operation.resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}
    }
    for selector in selectors:
        if selector in existing_identifiers:
            continue
        retained.append(
            IntentOperation(
                access=AccessMode.WRITE,
                resource=ResourceRef(
                    kind=ResourceKind.FILE,
                    identifier=selector,
                    metadata={"operator_initial_scope": True},
                ),
                required=True,
                commitment=ScopeCommitment.COMMITTED,
                metadata={"operator_initial_scope": True},
            )
        )

    return tuple(retained), tuple(omitted)


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
        raise ValueError(
            "Codex intent proposal requires a non-empty 'operations' array"
        )
    operations = tuple(IntentOperation.from_dict(item) for item in raw_operations)
    for operation in operations:
        _validate_operation_path(operation)
    operations, scope_omissions = _apply_operator_initial_scope(session, operations)

    proposal_metadata = proposal.get("metadata") or {}
    if not isinstance(proposal_metadata, dict):
        raise ValueError("Codex intent proposal 'metadata' must be a JSON object")
    proposed_acceptance = _string_list(proposal.get("acceptance"), field="acceptance")
    required_acceptance = tuple(
        str(item).strip()
        for item in session.get("required_acceptance") or ()
        if isinstance(item, str) and item.strip()
    )
    # Project-configured acceptance is operator-owned authority. A model proposal may
    # describe additional checks, but it cannot replace or extend the executable final
    # verification contract when the project already defines one. Keep the proposal in
    # metadata for auditability without executing arbitrary model-authored text.
    effective_acceptance = (
        required_acceptance if required_acceptance else proposed_acceptance
    )
    metadata = {
        **proposal_metadata,
        "goal": goal,
        "connector": "codex",
        "codex_session_id": str(session["session_id"]),
        "prompt_sha256": str(session.get("prompt_sha256") or ""),
        "bootstrap_protocol": CODEX_SESSION_PROTOCOL,
        "operator_initial_scope": list(_operator_scope(session)),
        "operator_scope_locked": bool(session.get("operator_scope_locked")),
        "operator_scope_omissions": list(scope_omissions),
        "configured_acceptance": list(required_acceptance),
        "agent_proposed_acceptance": list(proposed_acceptance),
        "acceptance_authority": (
            "project_config" if required_acceptance else "agent_fallback"
        ),
    }

    intent = ChangeIntent(
        intent_id=str(session["reserved_intent_id"]),
        task_id=str(session["task_id"]),
        owner=str(session["owner"]),
        base_revision=str(session["task_base_revision"]),
        base_commit=str(session["task_base_commit"]),
        operations=operations,
        preserves=_string_list(proposal.get("preserves"), field="preserves"),
        acceptance=effective_acceptance,
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

    other_sessions = _other_active_codex_sessions(root, session_id)
    if other_sessions:
        session["task_state"] = "blocked_concurrent_session"
        session["concurrent_sessions"] = other_sessions
        _write_session(root, session)
        raise ValueError(
            "another active Codex session is already authorized to mutate this physical "
            "worktree; finish it, abandon it with 'claim-plane codex-intent abandon', "
            "or use a separate Git worktree before admitting this task"
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
        session.pop("concurrent_sessions", None)
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
        session["configured_acceptance"] = list(
            intent.metadata.get("configured_acceptance") or ()
        )
        session["agent_proposed_acceptance"] = list(
            intent.metadata.get("agent_proposed_acceptance") or ()
        )
        session["acceptance_authority"] = str(
            intent.metadata.get("acceptance_authority") or "agent_fallback"
        )
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
        "configured_acceptance": list(
            intent.metadata.get("configured_acceptance") or ()
        ),
        "agent_proposed_acceptance": list(
            intent.metadata.get("agent_proposed_acceptance") or ()
        ),
        "acceptance_authority": str(
            intent.metadata.get("acceptance_authority") or "agent_fallback"
        ),
        "decision": decision.to_dict(),
    }


def _intent_record_version(plane: Plane, intent_id: str) -> int:
    record = next(
        (item for item in plane.intents() if item.get("intent_id") == intent_id),
        None,
    )
    if record is None:
        raise KeyError(f"unknown intent: {intent_id}")
    return int(record["version"])


def _sync_session_scope(session: dict[str, Any], intent: ChangeIntent) -> None:
    session["intent_fingerprint"] = intent.fingerprint()
    session["committed_scope"] = [
        _operation_summary(item) for item in intent.committed_operations
    ]
    session["contingent_scope"] = [
        _operation_summary(item) for item in intent.contingent_operations
    ]
    session["preserves"] = list(intent.preserves)
    session["acceptance"] = list(intent.acceptance)


_AMENDMENT_CAUSAL_TERMS = frozenset(
    {
        "because",
        "required",
        "requires",
        "needed",
        "depends",
        "dependency",
        "supporting",
        "coverage",
        "acceptance",
        "compile",
        "import",
        "test",
        "tests",
        "fixture",
        "schema",
        "contract",
        "configuration",
        "generated",
        "documentation",
        "migration",
        "must",
        "implemented",
        "owns",
        "aligned",
        "atomically",
        "invalidation",
    }
)
_AMENDMENT_VAGUE_PHRASES = (
    "while here",
    "just in case",
    "nice to have",
    "for cleanliness",
    "for consistency",
    "cleanup only",
    "general cleanup",
)


def _grounded_amendment_reason(reason: str) -> tuple[bool, str]:
    cleaned = " ".join(reason.strip().split())
    words = tuple(re.findall(r"[A-Za-z0-9_]+", cleaned.lower()))
    if len(cleaned) < 24 or len(words) < 5:
        return False, "reason_too_short"
    lowered = cleaned.lower()
    if any(phrase in lowered for phrase in _AMENDMENT_VAGUE_PHRASES):
        return False, "reason_is_vague"
    if not (_AMENDMENT_CAUSAL_TERMS & set(words)):
        return False, "reason_has_no_task_dependency"
    return True, "grounded"


def _record_denied_scope_amendment(
    root: Path,
    session: dict[str, Any],
    *,
    ticket_id: str,
    reason: str,
    reason_code: str,
    operations: list[Any],
) -> None:
    now = _utc_now()
    session.pop("pending_scope_amendment", None)
    session["scope_amendment_requests"] = (
        int(session.get("scope_amendment_requests") or 0) + 1
    )
    session["scope_amendment_denied"] = (
        int(session.get("scope_amendment_denied") or 0) + 1
    )
    record = {
        "protocol": CODEX_SCOPE_AMENDMENT_PROTOCOL,
        "ticket_id": ticket_id,
        "allowed": False,
        "reason": reason.strip(),
        "reason_code": reason_code,
        "operations": list(operations),
        "at": now,
    }
    session["last_scope_amendment"] = record
    history = [
        dict(item)
        for item in session.get("scope_amendment_history") or ()
        if isinstance(item, dict)
    ]
    history.append(record)
    session["scope_amendment_history"] = history[-50:]
    _write_session(root, session)


def amend_codex_scope(
    root_or_child: str | Path,
    *,
    session_id: str,
    ticket_id: str,
    reason: str,
) -> dict[str, Any]:
    """Atomically amend an active Codex intent from a one-time guard ticket.

    The ticket is created only after Claim Plane has observed an exact denied
    mutation. The caller supplies rationale, not new authority coordinates: the
    candidate operations come from the brokered ticket and are re-admitted through
    the normal ChangeIntent admission path.
    """

    root = resolve_project_root(root_or_child)
    _require_initialized(root)
    if _enrollment_state(root) is None:
        raise ValueError("Codex is not enrolled; run 'claim-plane connect codex' first")
    session = _load_session(root, session_id)
    intent_id = session.get("active_intent_id")
    if not isinstance(intent_id, str) or not intent_id:
        raise ValueError("Codex session has no active ChangeIntent to amend")
    if session.get("operator_scope_locked") is True:
        raise ValueError(
            "operator locked the initial scope; brokered amendments are disabled"
        )

    pending = session.get("pending_scope_amendment")
    if not isinstance(pending, dict):
        raise ValueError("Codex session has no pending scope-amendment ticket")
    if pending.get("protocol") != CODEX_SCOPE_AMENDMENT_PROTOCOL:
        raise ValueError("Codex session has an unsupported scope-amendment ticket")
    if str(pending.get("ticket_id") or "") != ticket_id:
        raise ValueError("scope-amendment ticket does not match the pending request")
    if str(pending.get("intent_id") or "") != intent_id:
        raise ValueError("scope-amendment ticket is bound to a different intent")

    expires_at = pending.get("expires_at")
    if not isinstance(expires_at, str):
        raise ValueError("scope-amendment ticket has no expiry")
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("scope-amendment ticket has invalid expiry") from exc
    if datetime.now(timezone.utc) >= expiry:
        session.pop("pending_scope_amendment", None)
        session["last_scope_amendment"] = {
            "ticket_id": ticket_id,
            "allowed": False,
            "reason_code": "ticket_expired",
            "at": _utc_now(),
        }
        session["scope_amendment_denied"] = (
            int(session.get("scope_amendment_denied") or 0) + 1
        )
        _write_session(root, session)
        raise ValueError(
            "scope-amendment ticket has expired; retry the denied mutation"
        )

    expected_base = str(session.get("task_base_commit") or "")
    if not expected_base or _head_commit(root) != expected_base:
        raise ValueError(
            "repository HEAD no longer matches the task base; start a fresh task bootstrap"
        )

    raw_mutations = pending.get("mutations")
    if not isinstance(raw_mutations, list) or not raw_mutations:
        raise ValueError("scope-amendment ticket contains no requested mutations")
    mutations = tuple(
        mutation_from_dict(item) for item in raw_mutations if isinstance(item, dict)
    )
    if len(mutations) != len(raw_mutations):
        raise ValueError("scope-amendment ticket contains invalid mutation entries")
    ticket_fingerprint = str(pending.get("intent_fingerprint") or "")
    expected_signature = _scope_amendment_signature(mutations, ticket_fingerprint)
    if str(pending.get("request_signature") or "") != expected_signature:
        raise ValueError("scope-amendment ticket integrity check failed")
    if str(pending.get("base_commit") or "") != expected_base:
        raise ValueError("scope-amendment ticket is bound to a different task base")

    plane = Plane.open(root / _PLANE_DB)
    decision = None
    applied: tuple[dict[str, Any], ...] = ()
    try:
        current = plane.intent(intent_id)
        if current is None:
            raise ValueError("active Codex intent disappeared before amendment")
        if current.fingerprint() != str(pending.get("intent_fingerprint") or ""):
            raise ValueError(
                "scope-amendment ticket is stale because the active intent changed; "
                "retry the denied mutation"
            )
        if (
            current.base_commit != expected_base
            or current.base_revision != expected_base
        ):
            raise ValueError("active Codex intent no longer matches the task base")

        grounded, grounding_code = _grounded_amendment_reason(reason)
        if not grounded:
            _record_denied_scope_amendment(
                root,
                session,
                ticket_id=ticket_id,
                reason=reason,
                reason_code=grounding_code,
                operations=raw_mutations,
            )
            raise ValueError(
                "scope-amendment reason is not grounded in a concrete task dependency; "
                "describe why the exact denied resource is required"
            )

        amended_at = _utc_now()
        candidate, applied = build_scope_amendment(
            current,
            mutations,
            ticket_id=ticket_id,
            reason=reason,
            amended_at=amended_at,
        )
        expected_version = _intent_record_version(plane, intent_id)
        decision = plane.amend(candidate, expected_version=expected_version)
        if decision.allowed:
            plane.activate(intent_id)
            reloaded = plane.intent(intent_id)
            if reloaded is None:
                raise ValueError("amended Codex intent could not be reloaded")
            _sync_session_scope(session, reloaded)
    finally:
        plane.close()

    now = _utc_now()
    session.pop("pending_scope_amendment", None)
    session["scope_amendment_requests"] = (
        int(session.get("scope_amendment_requests") or 0) + 1
    )
    allowed = bool(decision and decision.allowed)
    if allowed:
        session["scope_amendment_admitted"] = (
            int(session.get("scope_amendment_admitted") or 0) + 1
        )
        session["task_state"] = "active"
    else:
        session["scope_amendment_denied"] = (
            int(session.get("scope_amendment_denied") or 0) + 1
        )
    amendment_record = {
        "protocol": CODEX_SCOPE_AMENDMENT_PROTOCOL,
        "ticket_id": ticket_id,
        "allowed": allowed,
        "reason": reason.strip(),
        "reason_code": "grounded",
        "operations": list(applied or raw_mutations),
        "at": now,
    }
    session["last_scope_amendment"] = amendment_record
    history = [
        dict(item)
        for item in session.get("scope_amendment_history") or ()
        if isinstance(item, dict)
    ]
    history.append(amendment_record)
    session["scope_amendment_history"] = history[-50:]
    _write_session(root, session)

    if decision is None:
        raise RuntimeError("scope amendment finished without an admission decision")
    return {
        "protocol": CODEX_SCOPE_AMENDMENT_PROTOCOL,
        "allowed": decision.allowed,
        "session_id": session_id,
        "task_id": session.get("task_id"),
        "intent_id": intent_id,
        "base_commit": expected_base,
        "reason": reason.strip(),
        "reason_code": "grounded",
        "operations": list(applied or raw_mutations),
        "state": session.get("task_state") or "active",
        "amendment_state": "admitted" if decision.allowed else "rejected",
        "committed_scope": list(session.get("committed_scope") or ()),
        "contingent_scope": list(session.get("contingent_scope") or ()),
        "decision": decision.to_dict(),
    }


def _completion_system_message(result: dict[str, Any]) -> str:
    changed = int(result.get("changed_files") or 0)
    authorized = int(result.get("authorized_mutation_calls") or 0)
    denied = int(result.get("denied_mutation_calls") or 0)
    expansions = int(result.get("scope_expansions") or 0)
    violations = int(result.get("executed_violations") or 0)
    file_word = "file" if changed == 1 else "files"
    mutation_word = "mutation call" if authorized == 1 else "mutation calls"
    if result.get("verified"):
        return "\n".join(
            [
                "Claim Plane — VERIFIED",
                f"✓ {authorized} {mutation_word} authorized",
                f"✓ {changed} {file_word} changed",
                "✓ admitted scope verified",
                "✓ preserve and contract checks passed",
                "✓ acceptance criteria satisfied",
                f"Scope expansions: {expansions}",
                f"Denied mutations: {denied}",
                f"Executed authority violations: {violations}",
            ]
        )
    return "\n".join(
        [
            "Claim Plane — UNVERIFIED",
            f"Errors: {result.get('errors', 0)}",
            f"Executed authority violations: {violations}",
            (
                "Acceptance: passed"
                if result.get("acceptance_passed")
                else "Acceptance: failed"
            ),
        ]
    )


def verify_codex_completion(
    root_or_child: str | Path,
    *,
    session_id: str,
    acceptance_timeout: int = CODEX_COMPLETION_ACCEPTANCE_TIMEOUT,
    run_acceptance: bool = True,
) -> dict[str, Any]:
    """Verify one active Codex task and complete its intent only on clean evidence."""

    root = resolve_project_root(root_or_child)
    _require_initialized(root)
    session = _load_session(root, session_id)
    intent_id = session.get("active_intent_id")
    if not isinstance(intent_id, str) or not intent_id:
        raise ValueError("Codex session has no active ChangeIntent to verify")

    existing = session.get("completion")
    if session.get("task_state") == "verified" and isinstance(existing, dict):
        return dict(existing)

    result = verify_completion(
        root,
        intent_id=intent_id,
        run_acceptance=run_acceptance,
        acceptance_timeout=acceptance_timeout,
        connector_control_baseline=(
            dict(session.get("connector_control_fingerprints") or {})
        ),
        preexisting_worktree_baseline=(dict(session.get("preexisting_worktree") or {})),
        task_obligations=tuple(
            dict(item)
            for item in session.get("task_obligations") or ()
            if isinstance(item, Mapping)
        ),
    )
    now = _utc_now()
    history = [
        dict(item)
        for item in session.get("scope_amendment_history") or ()
        if isinstance(item, dict)
    ]
    admitted_history = [item for item in history if item.get("allowed") is True]
    result.update(
        {
            "session_id": session_id,
            "task_id": session.get("task_id"),
            "goal": session.get("intent_goal"),
            "base_commit": session.get("task_base_commit"),
            "verified_at": now if result.get("verified") else None,
            "checked_at": now,
            "authorized_mutation_calls": int(
                session.get("guard_authorized_mutation_calls") or 0
            ),
            "denied_mutation_calls": int(
                session.get("guard_denied_mutation_calls") or 0
            ),
            "scope_promotions": int(session.get("guard_promotions") or 0),
            "scope_expansions": len(admitted_history),
            "scope_amendments": admitted_history,
        }
    )
    session["completion_attempts"] = int(session.get("completion_attempts") or 0) + 1
    session["completion"] = result
    session["last_completion_at"] = now
    if result.get("verified"):
        session["task_state"] = "verified"
    elif result.get("acceptance_deferred") and result.get("authority_verified"):
        session["task_state"] = "acceptance_deferred"
    else:
        session["task_state"] = "verification_failed"
    if result.get("verified"):
        session.pop("pending_scope_amendment", None)
    _write_session(root, session)
    return result


def _handle_stop_completion(
    root: Path,
    payload: dict[str, Any],
    session: dict[str, Any],
    *,
    output: TextIO | None,
) -> None:
    intent_id = session.get("active_intent_id")
    if not isinstance(intent_id, str) or not intent_id:
        return
    if (
        session.get("controlled_interactive") is True
        or payload.get("_claim_plane_interactive") is True
    ):
        # In the TUI, Stop is a conversational turn boundary.  Acceptance and
        # terminal evidence belong to the outer ``claim-plane codex`` launcher
        # after the user exits the session, never to an in-turn hook.
        session["task_state"] = "awaiting_final_verification"
        session["last_turn_completed_at"] = _utc_now()
        session.pop("completion", None)
        _write_session(root, session)
        _write_hook_output(
            output,
            {
                "systemMessage": "\n".join(
                    [
                        "Claim Plane — AGENT TURN COMPLETED",
                        "✓ mutation activity recorded",
                        "✓ current authority state preserved",
                        "● final verification pending",
                        (
                            "Exit Codex to run configured acceptance checks and "
                            "seal evidence."
                        ),
                    ]
                )
            },
        )
        return
    try:
        result = verify_codex_completion(root, session_id=str(session["session_id"]))
    except Exception as exc:
        result = {
            "protocol": CODEX_COMPLETION_PROTOCOL,
            "verified": False,
            "errors": 1,
            "executed_violations": 0,
            "acceptance_passed": False,
            "findings": [
                {
                    "code": "completion_error",
                    "severity": "error",
                    "message": str(exc) or exc.__class__.__name__,
                }
            ],
        }
        session["completion_attempts"] = (
            int(session.get("completion_attempts") or 0) + 1
        )
        session["completion"] = result
        session["task_state"] = "verification_failed"
        _write_session(root, session)

    message = _completion_system_message(result)
    if result.get("verified"):
        _write_hook_output(output, {"systemMessage": message})
        return

    reason = stop_block_reason(result)
    if payload.get("stop_hook_active") is True:
        # Avoid an unbounded continuation loop. The task remains explicitly
        # unverified and the user can inspect or resume it.
        _write_hook_output(
            output,
            {
                "systemMessage": message
                + "\nClaim Plane did not block Stop again because this is already a Stop-hook continuation. The task remains UNVERIFIED."
            },
        )
        return
    _write_hook_output(
        output,
        {
            "decision": "block",
            "reason": reason,
            "systemMessage": message,
        },
    )


def abandon_codex_intent(
    root_or_child: str | Path, *, session_id: str
) -> dict[str, Any]:
    """Release unfinished Codex authority so another session can own the worktree."""

    root = resolve_project_root(root_or_child)
    session = _load_session(root, session_id)
    if session.get("task_state") == "verified":
        raise ValueError(
            "verified Codex work is already complete and cannot be abandoned"
        )
    intent_id = session.get("active_intent_id")
    if isinstance(intent_id, str) and intent_id and (root / _PLANE_DB).exists():
        plane = Plane.open(root / _PLANE_DB)
        try:
            plane.release_intent(intent_id)
        finally:
            plane.close()
    session["task_state"] = "abandoned"
    session["abandoned_at"] = _utc_now()
    session.pop("pending_scope_amendment", None)
    session.pop("concurrent_sessions", None)
    _write_session(root, session)
    return {
        "protocol": "claim-plane.codex-abandon.v1",
        "session_id": session_id,
        "intent_id": intent_id,
        "state": "abandoned",
        "released": bool(intent_id),
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
        "task_obligations": [
            dict(item)
            for item in session.get("task_obligations") or ()
            if isinstance(item, Mapping)
        ],
        "worktree_dirty_at_bootstrap": bool(session.get("preexisting_worktree")),
        "hardening": {
            "connector_revision": CODEX_CONNECTOR_REVISION,
            "preexisting_paths": sorted(
                (session.get("preexisting_worktree") or {}).keys()
            ),
            "resume_recoveries": int(session.get("resume_recoveries") or 0),
            "recovered_from_intent_id": session.get("recovered_from_intent_id"),
            "recovery_reason": session.get("recovery_reason"),
            "concurrent_sessions": list(session.get("concurrent_sessions") or ()),
        },
        "operator_scope": {
            "mode": "operator" if _operator_scope(session) else "planner",
            "initial": list(_operator_scope(session)),
            "locked": bool(session.get("operator_scope_locked")),
        },
        "guard": {
            "protocol": session.get("guard_protocol") or CODEX_GUARD_PROTOCOL,
            "pretool_calls": int(session.get("guard_pretool_calls") or 0),
            "authorized_calls": int(session.get("guard_authorized_calls") or 0),
            "denied_calls": int(session.get("guard_denied_calls") or 0),
            "promotions": int(session.get("guard_promotions") or 0),
            "last_decision": session.get("guard_last_decision"),
            "last_reason_code": session.get("guard_last_reason_code"),
            "last_tool": session.get("guard_last_tool"),
            "last_paths": list(session.get("guard_last_paths") or ()),
            "inspection": {
                "shell_calls": int(session.get("guard_shell_calls") or 0),
                "read_only_allowed": int(
                    session.get("guard_read_only_shell_allowed") or 0
                ),
                "compound_allowed": int(
                    session.get("guard_compound_shell_allowed") or 0
                ),
                "pipelines_allowed": int(
                    session.get("guard_pipeline_shell_allowed") or 0
                ),
                "unclassified_denied": int(
                    session.get("guard_unclassified_shell_denied") or 0
                ),
                "recovered_after_denial": int(
                    session.get("guard_shell_recovered_after_denial") or 0
                ),
                "pending_denials": len(
                    session.get("guard_pending_shell_denials") or ()
                ),
                "test_feedback_allowed": int(
                    session.get("guard_test_feedback_allowed") or 0
                ),
                "last_denial": dict(session.get("guard_last_shell_denial") or {}),
            },
        },
        "scope_amendment": {
            "protocol": CODEX_SCOPE_AMENDMENT_PROTOCOL,
            "tickets_issued": int(session.get("scope_amendment_tickets_issued") or 0),
            "requests": int(session.get("scope_amendment_requests") or 0),
            "admitted": int(session.get("scope_amendment_admitted") or 0),
            "denied": int(session.get("scope_amendment_denied") or 0),
            "pending": dict(session.get("pending_scope_amendment") or {}),
            "last": dict(session.get("last_scope_amendment") or {}),
            "history": [
                dict(item)
                for item in session.get("scope_amendment_history") or ()
                if isinstance(item, dict)
            ],
        },
        "completion": dict(session.get("completion") or {}),
        "completion_attempts": int(session.get("completion_attempts") or 0),
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
    if session.get("task_state") in {"recovery_required", "recovery_blocked"}:
        reason = str(
            session.get("recovery_reason")
            or "the previous execution authority is no longer resumable"
        )
        return (
            "Claim Plane resumed this Codex session fail-closed. The previous task cannot "
            f"mutate the repository: {reason}. Inspect `claim-plane codex-intent status "
            f"--session-id {session.get('session_id')} --repo .`, then start a fresh Codex "
            "task or restore the pinned repository state before continuing. Read-only "
            "discovery remains available."
        )
    if session.get("active_intent_id"):
        committed = _scope_lines(session.get("committed_scope")) or ["- none"]
        contingent = _scope_lines(session.get("contingent_scope")) or ["- none"]
        preserves = [f"- {item}" for item in session.get("preserves") or ()] or [
            "- none"
        ]
        acceptance = [f"- {item}" for item in session.get("acceptance") or ()] or [
            "- none"
        ]
        return "\n".join(
            [
                "Claim Plane execution contract is active for this Codex session.",
                f"Task: {session.get('task_id')}",
                f"Intent: {session.get('active_intent_id')}",
                f"Base commit: {session.get('task_base_commit')}",
                f"Goal: {session.get('intent_goal')}",
                (
                    "Operator initial scope: " + ", ".join(_operator_scope(session))
                    if _operator_scope(session)
                    else "Operator initial scope: automatic planner"
                ),
                (
                    "Scope expansion: locked by operator"
                    if session.get("operator_scope_locked")
                    else "Scope expansion: brokered amendments allowed"
                ),
                "Committed scope:",
                *committed,
                "Contingent scope:",
                *contingent,
                "Preserve requirements:",
                *preserves,
                "Acceptance (executed by Claim Plane's trusted final verifier):",
                *acceptance,
                "Do not run configured acceptance commands through the agent shell. "
                "Finish the admitted edits and stop; Claim Plane will execute acceptance "
                "after the Codex process exits and bind the result to the final Git state.",
                "Read-only shell commands may be chained with ; or && and may use pipes "
                "when every stage is independently read-only. Redirection, background "
                "execution, ||, command substitution, and unknown stages remain denied.",
                "Treat this admitted ChangeIntent as the authority boundary for the task.",
                "If a required repository mutation is denied as outside scope, use only "
                "the one-time Claim Plane scope-amendment ticket returned by the guard. "
                "Provide a concrete reason, let Claim Plane re-admit the exact denied "
                "resource(s), then retry the original mutation.",
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
            (
                "Operator-provided initial mutation scope: "
                + ", ".join(_operator_scope(session))
                if _operator_scope(session)
                else "Initial mutation scope is selected automatically by the planner."
            ),
            (
                "The operator locked this scope. Do not request or attempt changes "
                "outside it."
                if session.get("operator_scope_locked")
                else (
                    "If a genuinely required write falls outside explicit initial scope, "
                    "let Claim Plane issue a brokered amendment ticket after the "
                    "denied mutation."
                )
            ),
            "Before the first repository mutation, inspect the repository read-only "
            "and admit one ChangeIntent for this task.",
            "Submit the proposal through the connector-owned control command. Use "
            "--proposal-json so no shell pipe or temporary repository file is required:",
            f"claim-plane codex-intent admit --session-id {json.dumps(session_id)} "
            "--repo . --proposal-json '<proposal JSON>'",
            "Project-required acceptance is executed by Claim Plane's trusted final "
            "verifier after the Codex process exits. Do not run it through the agent "
            "shell; Claim Plane adds it to the admitted intent automatically.",
            "Read-only shell inspection may chain commands with ; or && and may use pipes "
            "when every stage is independently read-only. Redirection, background "
            "execution, ||, command substitution, and unknown stages remain denied.",
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
                    "acceptance": list(session.get("required_acceptance") or ()),
                },
                separators=(",", ":"),
            ),
            (
                "When operator initial scope is present, keep the initial proposal's "
                "mutating file operations inside it. Claim Plane enforces this "
                "server-side."
                if _operator_scope(session)
                else "Use committed scope for expected changes and contingent scope only "
                "for plausible fallback surfaces."
            ),
            "Do not provide intent_id, owner, or base revision; Claim Plane binds those "
            "to this session.",
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


def _heartbeat_session_intent(root: Path, session: dict[str, Any]) -> bool:
    intent_id = session.get("active_intent_id")
    if not isinstance(intent_id, str) or not intent_id:
        return False
    plane = Plane.open(root / _PLANE_DB)
    try:
        active_ids = {
            str(item.get("intent_id")) for item in plane.intents(active_only=True)
        }
        if intent_id not in active_ids:
            return False
        plane.heartbeat(intent_id)
        return True
    except (KeyError, ValueError):
        return False
    finally:
        plane.close()


def _scope_amendment_signature(
    mutations: tuple[Any, ...], intent_fingerprint: str
) -> str:
    payload = {
        "intent_fingerprint": intent_fingerprint,
        "mutations": [mutation_to_dict(item) for item in mutations],
    }
    return _sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _pending_ticket_is_live(ticket: dict[str, Any]) -> bool:
    expires_at = ticket.get("expires_at")
    if not isinstance(expires_at, str):
        return False
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) < expiry


def _attach_scope_amendment_ticket(
    root: Path,
    session: dict[str, Any],
    evaluation: GuardEvaluation,
) -> GuardEvaluation:
    """Attach a one-time exact-scope amendment path to eligible guard denials."""

    if evaluation.allowed or evaluation.reason_code not in {
        "outside_admitted_scope",
        "multiple_scope_promotions",
    }:
        return evaluation
    if session.get("operator_scope_locked") is True:
        return replace(
            evaluation,
            reason_code="operator_scope_locked",
            reason=(
                evaluation.reason
                + " The operator locked the initial scope, so no amendment ticket "
                "is available."
            ),
        )
    intent_id = session.get("active_intent_id")
    if not isinstance(intent_id, str) or not intent_id:
        return evaluation

    plane = Plane.open(root / _PLANE_DB)
    try:
        intent = plane.intent(intent_id)
    finally:
        plane.close()
    if intent is None:
        return evaluation

    missing = amendment_mutations(intent, evaluation.mutations)
    if not missing:
        return evaluation
    fingerprint = intent.fingerprint()
    signature = _scope_amendment_signature(missing, fingerprint)
    pending = session.get("pending_scope_amendment")
    if (
        isinstance(pending, dict)
        and pending.get("request_signature") == signature
        and pending.get("intent_id") == intent_id
        and _pending_ticket_is_live(pending)
    ):
        ticket = pending
    else:
        now = datetime.now(timezone.utc)
        ticket = {
            "protocol": CODEX_SCOPE_AMENDMENT_PROTOCOL,
            "ticket_id": f"csa_{secrets.token_hex(12)}",
            "intent_id": intent_id,
            "intent_fingerprint": fingerprint,
            "base_commit": str(session.get("task_base_commit") or ""),
            "request_signature": signature,
            "reason_code": evaluation.reason_code,
            "tool_name": evaluation.tool_name,
            "mutations": [mutation_to_dict(item) for item in missing],
            "issued_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(seconds=CODEX_SCOPE_AMENDMENT_TTL_SECONDS))
            .isoformat()
            .replace("+00:00", "Z"),
        }
        session["pending_scope_amendment"] = ticket
        session["scope_amendment_tickets_issued"] = (
            int(session.get("scope_amendment_tickets_issued") or 0) + 1
        )
        _write_session(root, session)

    session_arg = shlex.quote(str(session["session_id"]))
    ticket_arg = shlex.quote(str(ticket["ticket_id"]))
    guidance = (
        " Claim Plane opened a one-time scope-amendment ticket for only the denied "
        "mutation(s). If the additional scope is genuinely required, run "
        f"`claim-plane codex-intent amend --session-id {session_arg} --ticket "
        f'{ticket_arg} --reason "<why this scope is required>" --repo .`, then retry '
        "the original tool call. The amendment is re-admitted atomically and may still "
        "be denied by coordination policy."
    )
    return replace(evaluation, reason=evaluation.reason + guidance)


_OBSERVE_INVARIANT_DENIALS = frozenset(
    {
        "guard_error",
        "protected_control_path",
        "branch_changed",
        "preexisting_dirty_path",
        "operator_scope_locked",
    }
)


def _session_effective_policy(
    root: Path, session: Mapping[str, Any]
) -> EffectivePolicy:
    pinned = session.get("controlled_policy_manifest")
    if isinstance(pinned, Mapping):
        return EffectivePolicy.from_dict(pinned)
    config = load_project_config(root)
    adapters = config.get("adapters")
    settings = adapters.get("codex") if isinstance(adapters, Mapping) else None
    configured = (
        str(settings.get("policy") or "guarded")
        if isinstance(settings, Mapping)
        else "guarded"
    )
    selected = str(session.get("controlled_policy") or configured)
    risk = config.get("risk")
    return resolve_policy(
        selected,
        risk=risk if isinstance(risk, Mapping) else None,
        source=(
            "controlled_run" if session.get("controlled_policy") else "project_config"
        ),
        metadata={"adapter": "codex"},
    )


def _apply_guard_policy(
    root: Path,
    session: dict[str, Any],
    evaluation: GuardEvaluation,
) -> GuardEvaluation:
    effective = _session_effective_policy(root, session)
    risk = effective.classify_many(evaluation.paths)
    session["effective_policy"] = effective.name
    session["effective_policy_digest"] = effective.digest()
    session["guard_last_risk"] = risk
    if risk["final_action"] == PolicyAction.REVIEW_REQUIRED.value:
        session["policy_review_required"] = True
        session["policy_review_reason_codes"] = list(risk["reason_codes"])
    if evaluation.allowed and risk["final_action"] == PolicyAction.DENY.value:
        return replace(
            evaluation,
            allowed=False,
            reason_code="risk_policy_denied",
            reason=(
                f"Policy {effective.name} denies this {risk['highest_risk']} risk "
                "mutation before execution. "
                + " ".join(
                    str(item.get("explanation") or "")
                    for item in risk.get("findings") or ()
                    if isinstance(item, Mapping)
                )
            ).strip(),
            promotion=None,
        )
    if (
        not evaluation.allowed
        and effective.preset.pre_write_mode is PreWriteMode.OBSERVE
        and evaluation.reason_code not in _OBSERVE_INVARIANT_DENIALS
    ):
        original_code = evaluation.reason_code
        session["observe_would_deny_calls"] = (
            int(session.get("observe_would_deny_calls") or 0) + 1
        )
        session["observe_last_reason_code"] = original_code
        return replace(
            evaluation,
            allowed=True,
            classification=f"observe:{evaluation.classification}",
            reason_code=f"observe_would_{original_code}",
            reason=(
                f"Observe policy recorded a would-deny decision ({original_code}) "
                "but did not block the runtime call. Final Git verification remains "
                "required and may reject the delivery."
            ),
            promotion=None,
        )
    return evaluation


def _record_guard_evaluation(
    root: Path,
    session: dict[str, Any],
    evaluation: GuardEvaluation,
    *,
    promoted: bool = False,
) -> None:
    session["guard_protocol"] = CODEX_GUARD_PROTOCOL
    session["guard_pretool_calls"] = int(session.get("guard_pretool_calls") or 0) + 1
    if evaluation.allowed:
        session["guard_authorized_calls"] = (
            int(session.get("guard_authorized_calls") or 0) + 1
        )
    else:
        session["guard_denied_calls"] = int(session.get("guard_denied_calls") or 0) + 1
    if evaluation.mutating:
        session["guard_mutation_calls"] = (
            int(session.get("guard_mutation_calls") or 0) + 1
        )
        if evaluation.allowed:
            session["guard_authorized_mutation_calls"] = (
                int(session.get("guard_authorized_mutation_calls") or 0) + 1
            )
        else:
            session["guard_denied_mutation_calls"] = (
                int(session.get("guard_denied_mutation_calls") or 0) + 1
            )
    if promoted:
        session["guard_promotions"] = int(session.get("guard_promotions") or 0) + 1
    if evaluation.allowed and evaluation.reason_code == "test_feedback":
        session["guard_test_feedback_allowed"] = (
            int(session.get("guard_test_feedback_allowed") or 0) + 1
        )
        session["guard_test_feedback_protocol"] = TEST_FEEDBACK_PROTOCOL

    is_read_only_shell = bool(
        evaluation.allowed
        and evaluation.reason_code == "read_only"
        and evaluation.shell_command_count > 0
    )
    is_unclassified_shell = evaluation.reason_code == "opaque_shell"
    if is_read_only_shell or is_unclassified_shell:
        session["guard_shell_calls"] = int(session.get("guard_shell_calls") or 0) + 1
    if is_read_only_shell:
        session["guard_read_only_shell_allowed"] = (
            int(session.get("guard_read_only_shell_allowed") or 0) + 1
        )
        if evaluation.shell_compound:
            session["guard_compound_shell_allowed"] = (
                int(session.get("guard_compound_shell_allowed") or 0) + 1
            )
        if evaluation.shell_pipeline:
            session["guard_pipeline_shell_allowed"] = (
                int(session.get("guard_pipeline_shell_allowed") or 0) + 1
            )
        pending = session.get("guard_pending_shell_denials") or []
        if isinstance(pending, list) and pending:
            session["guard_shell_recovered_after_denial"] = int(
                session.get("guard_shell_recovered_after_denial") or 0
            ) + len(pending)
            session["guard_last_shell_recovery"] = {
                "recovered_at": _utc_now(),
                "denials": [
                    dict(item) for item in pending if isinstance(item, Mapping)
                ],
            }
            session["guard_pending_shell_denials"] = []
    elif is_unclassified_shell:
        session["guard_unclassified_shell_denied"] = (
            int(session.get("guard_unclassified_shell_denied") or 0) + 1
        )
        raw_segment = evaluation.diagnostic_segment or ""
        try:
            segment_argv = shlex.split(raw_segment, posix=True) if raw_segment else []
        except ValueError:
            segment_argv = []
        segment_executable = (
            posixpath.basename(segment_argv[0]).casefold() if segment_argv else None
        )
        denial = {
            "at": _utc_now(),
            "reason_code": evaluation.diagnostic_code or evaluation.reason_code,
            "segment_executable": segment_executable,
            "segment_sha256": _sha256_text(raw_segment) if raw_segment else None,
            "segment_index": evaluation.diagnostic_segment_index,
            "command_count": evaluation.shell_command_count,
            "pipeline_count": evaluation.shell_pipeline_count,
        }
        pending = session.get("guard_pending_shell_denials") or []
        if not isinstance(pending, list):
            pending = []
        pending.append(denial)
        session["guard_pending_shell_denials"] = pending[-20:]
        session["guard_last_shell_denial"] = denial

    session["guard_last_decision"] = "allow" if evaluation.allowed else "deny"
    session["guard_last_reason_code"] = evaluation.reason_code
    session["guard_last_classification"] = evaluation.classification
    session["guard_last_tool"] = evaluation.tool_name
    session["guard_last_paths"] = list(evaluation.paths)
    session["guard_last_at"] = _utc_now()
    _write_session(root, session)


def _guard_error(tool_name: str, reason: str) -> GuardEvaluation:
    return GuardEvaluation(
        allowed=False,
        mutating=True,
        tool_name=tool_name or "unknown",
        classification="guard_error",
        reason_code="guard_error",
        reason=(
            "Claim Plane could not establish mutation authority for this tool call: "
            f"{reason}. The call is denied before execution."
        ),
    )


def _pre_tool_use_guard(
    root: Path, payload: dict[str, Any], session: dict[str, Any]
) -> GuardEvaluation:
    intent_id = session.get("active_intent_id")
    intent: ChangeIntent | None = None
    intent_is_active = False
    plane = Plane.open(root / _PLANE_DB)
    try:
        if isinstance(intent_id, str) and intent_id:
            intent = plane.intent(intent_id)
            active_ids = {
                str(item.get("intent_id")) for item in plane.intents(active_only=True)
            }
            intent_is_active = intent_id in active_ids

        expected_base = session.get("task_base_commit")
        base_commit_matches = bool(
            isinstance(expected_base, str)
            and expected_base
            and _head_commit(root) == expected_base
        )
        evaluation = evaluate_pre_tool_use(
            root=root,
            payload=payload,
            intent=intent,
            intent_is_active=intent_is_active,
            base_commit_matches=base_commit_matches,
        )
        if evaluation.mutating:
            expected_branch = str(session.get("task_branch") or "")
            if expected_branch and _branch_name(root) != expected_branch:
                return GuardEvaluation(
                    allowed=False,
                    mutating=True,
                    tool_name=evaluation.tool_name,
                    classification=evaluation.classification,
                    reason_code="branch_changed",
                    reason=(
                        "The Git branch changed after this Codex task was bootstrapped. "
                        "Claim Plane denies repository mutation until the task is resumed "
                        "on its pinned branch or restarted."
                    ),
                    mutations=evaluation.mutations,
                )
            baseline = session.get("preexisting_worktree") or {}
            if isinstance(baseline, dict):
                touched = sorted(
                    {
                        mutation.path
                        for mutation in evaluation.mutations
                        if mutation.path in baseline
                    }
                    | {
                        mutation.target_path
                        for mutation in evaluation.mutations
                        if mutation.target_path and mutation.target_path in baseline
                    }
                )
                if touched:
                    return GuardEvaluation(
                        allowed=False,
                        mutating=True,
                        tool_name=evaluation.tool_name,
                        classification=evaluation.classification,
                        reason_code="preexisting_dirty_path",
                        reason=(
                            "Claim Plane will not mutate a path that already had user changes "
                            "when this task was bootstrapped: "
                            + ", ".join(touched)
                            + ". "
                            "Commit or stash those changes, or start the task in a clean worktree."
                        ),
                        mutations=evaluation.mutations,
                    )
        if not evaluation.allowed or evaluation.promotion is None:
            return evaluation

        if intent is None or not isinstance(intent_id, str):
            return _guard_error(evaluation.tool_name, "active intent disappeared")
        mutation = evaluation.promotion
        decision = plane.promote_contingent_scope(
            intent_id,
            path=mutation.path,
            modes=promotion_modes(mutation),
        )
        if not decision.allowed:
            blockers = "; ".join(
                str(item.get("message") or item.get("reason") or item)
                for item in decision.to_dict().get("findings", ())
                if isinstance(item, dict)
            )
            suffix = f" ({blockers})" if blockers else ""
            return GuardEvaluation(
                allowed=False,
                mutating=True,
                tool_name=evaluation.tool_name,
                classification=evaluation.classification,
                reason_code="scope_promotion_denied",
                reason=(
                    f"Contingent scope promotion for {mutation.path} was not admitted"
                    f"{suffix}. Re-plan or amend the ChangeIntent before retrying."
                ),
                mutations=evaluation.mutations,
            )

        promoted_intent = plane.intent(intent_id)
        if promoted_intent is None:
            return _guard_error(
                evaluation.tool_name, "promoted intent could not be reloaded"
            )
        session["intent_fingerprint"] = promoted_intent.fingerprint()
        session["committed_scope"] = [
            _operation_summary(item) for item in promoted_intent.committed_operations
        ]
        session["contingent_scope"] = [
            _operation_summary(item) for item in promoted_intent.contingent_operations
        ]
        session["last_scope_promotion"] = {
            "path": mutation.path,
            "access": mutation.access.value,
            "target_path": mutation.target_path,
            "admitted_at": _utc_now(),
        }
        return evaluation
    finally:
        plane.close()


def handle_codex_hook(payload: dict[str, Any], *, output: TextIO | None = None) -> int:
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
    except (FileNotFoundError, ValueError) as exc:
        if event == "PreToolUse":
            _write_hook_output(
                output,
                denied_hook_output(
                    _guard_error(
                        str(
                            payload.get("tool_name")
                            or payload.get("toolName")
                            or "unknown"
                        ),
                        f"project root cannot be resolved: {exc}",
                    )
                ),
            )
        return 0
    if _enrollment_state(root) is None:
        if event == "PreToolUse":
            _write_hook_output(
                output,
                denied_hook_output(
                    _guard_error(
                        str(
                            payload.get("tool_name")
                            or payload.get("toolName")
                            or "unknown"
                        ),
                        "project-local Claim Plane enrollment state is missing",
                    )
                ),
            )
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

    if event == "PreToolUse":
        try:
            session = _ensure_session(root, payload)
        except Exception as exc:
            evaluation = _guard_error(
                str(payload.get("tool_name") or payload.get("toolName") or "unknown"),
                f"Codex session state could not be loaded: {exc}",
            )
            _write_hook_output(output, denied_hook_output(evaluation))
            return 0
        if session is None:
            evaluation = _guard_error(
                str(payload.get("tool_name") or payload.get("toolName") or "unknown"),
                "Codex session state is unavailable",
            )
            _write_hook_output(output, denied_hook_output(evaluation))
            return 0
        _heartbeat_session_intent(root, session)
        try:
            evaluation = _pre_tool_use_guard(root, payload, session)
            evaluation = _apply_guard_policy(root, session, evaluation)
            evaluation = _attach_scope_amendment_ticket(root, session, evaluation)
        except Exception as exc:  # fail closed at the runtime integration boundary
            evaluation = _guard_error(
                str(payload.get("tool_name") or payload.get("toolName") or "unknown"),
                str(exc) or exc.__class__.__name__,
            )
        _record_guard_evaluation(
            root,
            session,
            evaluation,
            promoted=bool(evaluation.allowed and evaluation.promotion is not None),
        )
        if not evaluation.allowed:
            _write_hook_output(
                output,
                denied_hook_output(
                    evaluation,
                    initial_scope=_operator_scope(session),
                ),
            )
        return 0

    if event in {"PostToolUse", "Stop"}:
        session = _ensure_session(root, payload)
        if session is not None:
            _heartbeat_session_intent(root, session)
            if event == "Stop":
                _handle_stop_completion(root, payload, session, output=output)

    if event in {"Stop", "SessionEnd"}:
        _record_session_lifecycle_event(root, payload)
    return 0
