"""Project-local Codex enrollment and lifecycle dispatch."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_PROTOCOL = "claim-plane.project.v1"
CODEX_ENROLLMENT_PROTOCOL = "claim-plane.codex-enrollment.v1"
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


def _record_session_handshake(root: Path, payload: dict[str, Any]) -> None:
    state_path = root / _CODEX_STATE
    if not state_path.exists():
        return
    state = _read_json_object(state_path)
    if state.get("protocol") != CODEX_ENROLLMENT_PROTOCOL:
        return
    state["last_seen_at"] = _utc_now()
    state["last_session_id"] = payload.get("session_id")
    state["last_event"] = payload.get("hook_event_name")
    _atomic_write_json(state_path, state)


def handle_codex_hook(payload: dict[str, Any]) -> int:
    """Dispatch a Codex lifecycle event for an enrolled project.

    Enrollment deliberately keeps this dispatcher stable. Later runtime policies can
    strengthen individual lifecycle events without changing the project-local hook
    definition or requiring a new enrollment command.
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
    if event == "SessionStart":
        _record_session_handshake(root, payload)
    return 0
