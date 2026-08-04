"""Project enrollment, configuration, diagnostics, and safe local reset."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from claim_plane.policy import POLICY_NAMES, RiskPolicy

PROJECT_STATE_PROTOCOL = "claim-plane.project.v1"
PROJECT_CONFIG_PROTOCOL = "claim-plane.project-config.v1"
LEGACY_PROJECT_CONFIG_PROTOCOL = "claim-plane.project-config.v0"
PROJECT_CONFIG_MIGRATION_PROTOCOL = "claim-plane.project-config-migration.v1"
PROJECT_CONFIG_STATUS_PROTOCOL = "claim-plane.project-config-status.v1"
PROJECT_DOCTOR_PROTOCOL = "claim-plane.project-doctor.v1"

PROJECT_STATE_PATH = Path(".claim-plane/project.json")
PROJECT_CONFIG_PATH = Path(".claim-plane/config.yaml")

_LOCAL_STATE_PATHS = (
    Path(".claim-plane/project.json"),
    Path(".claim-plane/codex.json"),
    Path(".claim-plane/codex"),
    Path(".claim-plane/adapters"),
    Path(".claim-plane/lifecycle"),
    Path(".claim-plane/reference"),
    Path(".claim-plane/runs"),
    Path(".claim-plane/plane.db"),
    Path(".claim-plane/plane.db-shm"),
    Path(".claim-plane/plane.db-wal"),
    Path(".claim-plane/swarm.db"),
    Path(".claim-plane/swarm.db-shm"),
    Path(".claim-plane/swarm.db-wal"),
)

_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
}
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*\b", re.IGNORECASE),
)


@dataclass(frozen=True, slots=True)
class ProjectDoctorReport:
    """Machine-readable project enrollment health report."""

    root: str
    ready: bool
    checks: tuple[dict[str, Any], ...]
    config: Mapping[str, Any] | None = None
    protocol: str = PROJECT_DOCTOR_PROTOCOL

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "root": self.root,
            "ready": self.ready,
            "checks": [dict(item) for item in self.checks],
            "config": dict(self.config) if self.config is not None else None,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git(root_or_child: str | Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=Path(root_or_child).expanduser().resolve(),
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git failed"
        raise ValueError(detail)
    return completed.stdout.strip()


def resolve_project_root(root_or_child: str | Path = ".") -> Path:
    """Resolve a Git worktree root without mutating the repository."""

    value = _git(root_or_child, "rev-parse", "--show-toplevel")
    return Path(value).resolve()


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


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False, separators=(", ", ": "))
    return json.dumps(str(value), ensure_ascii=False)


def _dump_yaml_mapping(payload: Mapping[str, Any], *, indent: int = 0) -> list[str]:
    lines: list[str] = []
    prefix = " " * indent
    for key, value in payload.items():
        if isinstance(value, Mapping):
            lines.append(f"{prefix}{key}:")
            lines.extend(_dump_yaml_mapping(value, indent=indent + 2))
        else:
            lines.append(f"{prefix}{key}: {_yaml_scalar(value)}")
    return lines


def dump_project_config(config: Mapping[str, Any]) -> str:
    """Render the dependency-free project configuration as deterministic YAML."""

    return "\n".join(_dump_yaml_mapping(config)) + "\n"


def _parse_yaml_scalar(value: str) -> Any:
    text = value.strip()
    if text == "":
        return {}
    if text in {"true", "false"}:
        return text == "true"
    if text == "null":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        return text


def _read_project_config_payload(path: Path) -> dict[str, Any]:
    root_payload: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root_payload)]
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indentation = len(raw) - len(raw.lstrip(" "))
        if indentation % 2:
            raise ValueError(f"{path}:{line_number} uses odd indentation")
        content = raw.strip()
        if ":" not in content:
            raise ValueError(f"{path}:{line_number} must contain a mapping entry")
        key, value_text = content.split(":", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key):
            raise ValueError(f"{path}:{line_number} has an invalid key")
        while stack and indentation <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise ValueError(f"{path}:{line_number} has invalid indentation")
        parent = stack[-1][1]
        value = _parse_yaml_scalar(value_text)
        if value_text.strip() == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indentation, child))
        else:
            parent[key] = value
    return root_payload


def load_project_config(root_or_child: str | Path = ".") -> dict[str, Any]:
    root = resolve_project_root(root_or_child)
    path = root / PROJECT_CONFIG_PATH
    if not path.exists():
        raise ValueError(f"project config is missing: {path}")
    root_payload = _read_project_config_payload(path)
    if root_payload.get("protocol") != PROJECT_CONFIG_PROTOCOL:
        protocol = root_payload.get("protocol")
        remediation = (
            " Run `claim-plane config migrate` first."
            if protocol == LEGACY_PROJECT_CONFIG_PROTOCOL
            else ""
        )
        raise ValueError(f"{path} uses unsupported protocol {protocol!r}.{remediation}")
    return root_payload


def project_config_status(root_or_child: str | Path = ".") -> dict[str, Any]:
    """Inspect config compatibility without mutating repository state."""

    root = resolve_project_root(root_or_child)
    path = root / PROJECT_CONFIG_PATH
    if not path.exists():
        return {
            "protocol": PROJECT_CONFIG_STATUS_PROTOCOL,
            "root": str(root),
            "path": str(path),
            "present": False,
            "source_protocol": None,
            "target_protocol": PROJECT_CONFIG_PROTOCOL,
            "status": "missing",
            "migration_available": False,
        }
    payload = _read_project_config_payload(path)
    source = payload.get("protocol")
    if source == PROJECT_CONFIG_PROTOCOL:
        status = "current"
        migration_available = False
    elif source == LEGACY_PROJECT_CONFIG_PROTOCOL:
        status = "migration_required"
        migration_available = True
    else:
        status = "unsupported"
        migration_available = False
    return {
        "protocol": PROJECT_CONFIG_STATUS_PROTOCOL,
        "root": str(root),
        "path": str(path),
        "present": True,
        "source_protocol": source,
        "target_protocol": PROJECT_CONFIG_PROTOCOL,
        "status": status,
        "migration_available": migration_available,
    }


def _legacy_config_to_current(root: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    project = dict(payload.get("project") or {})
    repository = dict(payload.get("repository") or {})
    acceptance = dict(payload.get("acceptance") or {})
    codex = dict(payload.get("codex") or {})
    risk = dict(payload.get("risk") or {})
    return _project_config(
        root,
        {
            "project": project,
            "repository": repository,
            "acceptance": acceptance,
            "adapters": {"codex": codex},
            "risk": risk,
        },
    )


def migrate_project_config(
    root_or_child: str | Path = ".",
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Atomically migrate a supported legacy config and preserve one backup."""

    root = resolve_project_root(root_or_child)
    path = root / PROJECT_CONFIG_PATH
    if not path.exists():
        raise ValueError(f"project config is missing: {path}")
    payload = _read_project_config_payload(path)
    source = payload.get("protocol")
    if source == PROJECT_CONFIG_PROTOCOL:
        current = load_project_config(root)
        return {
            "protocol": PROJECT_CONFIG_MIGRATION_PROTOCOL,
            "root": str(root),
            "path": str(path),
            "source_protocol": source,
            "target_protocol": PROJECT_CONFIG_PROTOCOL,
            "changed": False,
            "dry_run": dry_run,
            "backup": None,
            "config": current,
        }
    if source != LEGACY_PROJECT_CONFIG_PROTOCOL:
        raise ValueError(f"no migration is available for config protocol {source!r}")

    migrated = _legacy_config_to_current(root, payload)
    rendered = dump_project_config(migrated)
    backup = path.with_name(f"{path.name}.v0.bak")
    if not dry_run:
        original = path.read_bytes()
        if backup.exists() and backup.read_bytes() != original:
            raise ValueError(
                f"migration backup already exists with different content: {backup}"
            )
        if not backup.exists():
            _atomic_write_text(backup, original.decode("utf-8"))
        _atomic_write_text(path, rendered)
        state_path = root / PROJECT_STATE_PATH
        if state_path.exists():
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(state_payload, dict):
                state_payload["updated_at"] = _utc_now()
                state_payload["config_protocol"] = PROJECT_CONFIG_PROTOCOL
                state_payload["config_digest"] = hashlib.sha256(
                    rendered.encode("utf-8")
                ).hexdigest()
                _atomic_write_json(state_path, state_payload)
    return {
        "protocol": PROJECT_CONFIG_MIGRATION_PROTOCOL,
        "root": str(root),
        "path": str(path),
        "source_protocol": source,
        "target_protocol": PROJECT_CONFIG_PROTOCOL,
        "changed": True,
        "dry_run": dry_run,
        "backup": str(backup),
        "config": migrated,
    }


def _default_branch(root: Path) -> tuple[str, str]:
    remote_head = _git(
        root,
        "symbolic-ref",
        "--quiet",
        "--short",
        "refs/remotes/origin/HEAD",
        check=False,
    )
    if remote_head.startswith("origin/"):
        return remote_head.split("/", 1)[1], "origin/HEAD"

    current = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if current:
        return current, "current-branch"

    configured = _git(root, "config", "--get", "init.defaultBranch", check=False)
    if configured:
        return configured, "git-config"

    branches = set(
        _git(
            root,
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads",
            check=False,
        ).splitlines()
    )
    for candidate in ("main", "master"):
        if candidate in branches:
            return candidate, "local-branch"
    return "main", "fallback"


def _repository_identity(root: Path) -> str:
    remote = _git(root, "config", "--get", "remote.origin.url", check=False)
    if remote:
        material = re.sub(r"//[^/@]+@", "//", remote.strip())
        source = f"remote:{material}"
    else:
        roots = _git(
            root, "rev-list", "--max-parents=0", "HEAD", check=False
        ).splitlines()
        source = f"root:{roots[0] if roots else 'unborn'}"
    return "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()


def _detect_acceptance_commands(root: Path) -> list[str]:
    commands: list[str] = []

    check_script = root / "scripts/check.sh"
    if check_script.is_file():
        commands.append("./scripts/check.sh")

    makefile = root / "Makefile"
    if makefile.is_file():
        text = makefile.read_text(encoding="utf-8", errors="replace")
        if re.search(r"(?m)^check\s*:", text):
            commands.append("make check")
        elif re.search(r"(?m)^test\s*:", text):
            commands.append("make test")

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="replace").lower()
        if "pytest" in text:
            commands.append("python -m pytest")

    package_json = root / "package.json"
    if package_json.is_file():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            package = {}
        scripts = package.get("scripts") if isinstance(package, Mapping) else None
        if isinstance(scripts, Mapping) and isinstance(scripts.get("test"), str):
            lock_command = "npm"
            if (root / "pnpm-lock.yaml").exists():
                lock_command = "pnpm"
            elif (root / "yarn.lock").exists():
                lock_command = "yarn"
            commands.append(f"{lock_command} test")

    for marker, command in (
        ("Cargo.toml", "cargo test"),
        ("go.mod", "go test ./..."),
        ("pom.xml", "mvn test"),
        ("gradlew", "./gradlew test"),
    ):
        if (root / marker).exists():
            commands.append(command)

    seen: set[str] = set()
    unique: list[str] = []
    for command in commands:
        if command in seen:
            continue
        seen.add(command)
        unique.append(command)
    return unique


def _ensure_local_state_excluded(root: Path) -> Path:
    git_path = _git(root, "rev-parse", "--git-path", "info/exclude")
    exclude = Path(git_path)
    if not exclude.is_absolute():
        exclude = root / exclude
    exclude = exclude.resolve()
    exclude.parent.mkdir(parents=True, exist_ok=True)

    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    marker = "# Claim Plane local state"
    canonical = f"{marker}\n.claim-plane/\n"
    block_pattern = re.compile(
        (
            r"(?m)^# Claim Plane local state\n"
            r"(?:\.claim-plane/?\n|\.claim-plane/\*\n"
            r"!\.claim-plane/\n!\.claim-plane/config\.yaml\n)"
        )
    )
    updated, count = block_pattern.subn(canonical, existing, count=1)
    if count == 0:
        prefix = existing
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        updated = prefix + canonical
    if updated != existing:
        _atomic_write_text(exclude, updated)
    return exclude


def _project_config(
    root: Path, existing: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    existing = dict(existing or {})
    project = dict(existing.get("project") or {})
    repository = dict(existing.get("repository") or {})
    acceptance = dict(existing.get("acceptance") or {})
    adapters = dict(existing.get("adapters") or {})
    codex = dict(adapters.get("codex") or {})
    risk = dict(existing.get("risk") or {})

    default_branch, source = _default_branch(root)
    commands = acceptance.get("commands")
    if not isinstance(commands, list) or not all(
        isinstance(item, str) for item in commands
    ):
        commands = _detect_acceptance_commands(root)

    project_id = project.get("id")
    if not isinstance(project_id, str) or not re.fullmatch(
        r"cp_[0-9a-f]{24}", project_id
    ):
        project_id = "cp_" + os.urandom(12).hex()

    selected_policy = str(codex.get("policy") or "guarded").casefold()
    if selected_policy not in POLICY_NAMES:
        raise ValueError(
            "adapters.codex.policy must be observe, guarded, strict, or critical"
        )
    risk_policy = RiskPolicy.from_config(risk)
    normalized_rules = [
        {
            "match": rule.match,
            "level": rule.level.value,
            "reason": rule.reason,
        }
        for rule in risk_policy.rules
    ]

    return {
        "protocol": PROJECT_CONFIG_PROTOCOL,
        "project": {"id": project_id},
        "repository": {
            "identity": _repository_identity(root),
            "default_branch": str(repository.get("default_branch") or default_branch),
            "default_branch_source": str(
                repository.get("default_branch_source") or source
            ),
        },
        "acceptance": {
            "commands": commands,
            "detected": bool(commands),
        },
        "adapters": {
            **{key: value for key, value in adapters.items() if key != "codex"},
            "codex": {
                "enabled": bool(codex.get("enabled", False)),
                "policy": selected_policy,
            },
        },
        "risk": {
            "default": risk_policy.default.value,
            "include_builtin_rules": risk_policy.include_builtin_rules,
            "rules": normalized_rules,
        },
    }


def init_project(root_or_child: str | Path = ".") -> dict[str, Any]:
    """Create stable project identity, versioned config, and ignored local state."""

    root = resolve_project_root(root_or_child)
    state_path = root / PROJECT_STATE_PATH
    config_path = root / PROJECT_CONFIG_PATH
    now = _utc_now()

    existing_state: dict[str, Any] = {}
    if state_path.exists():
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("protocol") != PROJECT_STATE_PROTOCOL
        ):
            raise ValueError(f"unsupported project state protocol in {state_path}")
        existing_state = payload

    existing_config: dict[str, Any] | None = None
    migration: dict[str, Any] | None = None
    if config_path.exists():
        status = project_config_status(root)
        if status["status"] == "migration_required":
            migration = migrate_project_config(root)
        existing_config = load_project_config(root)
    config = _project_config(root, existing_config)
    rendered = dump_project_config(config)
    config_created = not config_path.exists()
    config_changed = (
        config_created or config_path.read_text(encoding="utf-8") != rendered
    )
    if config_changed:
        _atomic_write_text(config_path, rendered)

    state = {
        "protocol": PROJECT_STATE_PROTOCOL,
        "initialized_at": str(existing_state.get("initialized_at") or now),
        "updated_at": (
            now
            if (not existing_state or config_changed)
            else str(existing_state.get("updated_at") or now)
        ),
        "project_id": config["project"]["id"],
        "config_protocol": PROJECT_CONFIG_PROTOCOL,
        "config_digest": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
    }
    state_changed = existing_state != state
    if state_changed:
        _atomic_write_json(state_path, state)

    exclude = _ensure_local_state_excluded(root)
    return {
        "protocol": PROJECT_CONFIG_PROTOCOL,
        "root": str(root),
        "state": str(state_path),
        "config": str(config_path),
        "exclude": str(exclude),
        "project_id": config["project"]["id"],
        "default_branch": config["repository"]["default_branch"],
        "acceptance_commands": list(config["acceptance"]["commands"]),
        "initialized": True,
        "created": config_created,
        "changed": config_changed or state_changed or migration is not None,
        "migration": migration,
    }


def set_adapter_enabled(
    root_or_child: str | Path,
    adapter: str,
    *,
    enabled: bool,
    policy: str | None = None,
) -> dict[str, Any]:
    root = resolve_project_root(root_or_child)
    config = load_project_config(root)
    adapters = dict(config.get("adapters") or {})
    settings = dict(adapters.get(adapter) or {})
    settings["enabled"] = enabled
    if policy is not None:
        settings["policy"] = policy
    adapters[adapter] = settings
    config["adapters"] = adapters
    rendered = dump_project_config(config)
    _atomic_write_text(root / PROJECT_CONFIG_PATH, rendered)

    state_path = root / PROJECT_STATE_PATH
    if state_path.exists():
        state_payload = json.loads(state_path.read_text(encoding="utf-8"))
        if (
            isinstance(state_payload, dict)
            and state_payload.get("protocol") == PROJECT_STATE_PROTOCOL
        ):
            state_payload["updated_at"] = _utc_now()
            state_payload["config_digest"] = hashlib.sha256(
                rendered.encode("utf-8")
            ).hexdigest()
            _atomic_write_json(state_path, state_payload)
    return config


def _command_available(root: Path, command: str) -> tuple[bool, str]:
    try:
        parts = shlex.split(command)
    except ValueError:
        return False, "command cannot be parsed"
    if not parts:
        return False, "command is empty"
    executable = parts[0]
    if executable.startswith("./"):
        path = root / executable[2:]
        return path.is_file(), str(path)
    located = shutil.which(executable)
    return located is not None, located or f"{executable} not found on PATH"


def _contains_secret_material(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SECRET_KEYS:
                return True
            if _contains_secret_material(child):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_material(item) for item in value)
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _SECRET_PATTERNS)
    return False


def doctor_project(root_or_child: str | Path = ".") -> ProjectDoctorReport:
    """Inspect Git, configuration, state, acceptance, and secret hygiene."""

    root = resolve_project_root(root_or_child)
    checks: list[dict[str, Any]] = []
    checks.append({"name": "git_repository", "status": "ok", "detail": str(root)})

    dirty = bool(
        _git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            check=False,
        )
    )
    checks.append(
        {
            "name": "working_tree",
            "status": "warning" if dirty else "ok",
            "detail": (
                "working tree contains changes" if dirty else "working tree is clean"
            ),
        }
    )

    config: dict[str, Any] | None = None
    try:
        config = load_project_config(root)
        checks.append(
            {
                "name": "project_config",
                "status": "ok",
                "detail": str(root / PROJECT_CONFIG_PATH),
            }
        )
        try:
            from claim_plane.policy import resolve_policy

            adapters = config.get("adapters")
            codex_settings = (
                adapters.get("codex") if isinstance(adapters, Mapping) else None
            )
            selected_policy = (
                str(codex_settings.get("policy") or "guarded")
                if isinstance(codex_settings, Mapping)
                else "guarded"
            )
            effective = resolve_policy(
                selected_policy,
                risk=(
                    config.get("risk")
                    if isinstance(config.get("risk"), Mapping)
                    else None
                ),
                source="project_config",
            )
            checks.append(
                {
                    "name": "policy_config",
                    "status": "ok",
                    "detail": (
                        f"{effective.name} · risk default "
                        f"{effective.risk.default.value} · {effective.digest()[:12]}"
                    ),
                }
            )
        except (TypeError, ValueError) as exc:
            checks.append(
                {
                    "name": "policy_config",
                    "status": "error",
                    "detail": str(exc),
                    "remediation": (
                        "Use a documented policy preset and valid risk rules in "
                        ".claim-plane/config.yaml."
                    ),
                }
            )
    except ValueError as exc:
        checks.append(
            {
                "name": "project_config",
                "status": "error",
                "detail": str(exc),
                "remediation": (
                    "Run 'claim-plane init' to create or repair the project config."
                ),
            }
        )

    state_path = root / PROJECT_STATE_PATH
    state_valid = False
    if state_path.exists():
        try:
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state_payload = None
        state_valid = (
            isinstance(state_payload, Mapping)
            and state_payload.get("protocol") == PROJECT_STATE_PROTOCOL
        )
    checks.append(
        {
            "name": "project_state",
            "status": "ok" if state_valid else "error",
            "detail": str(state_path),
            "remediation": (
                None
                if state_valid
                else "Run 'claim-plane init' to create or repair local state."
            ),
        }
    )

    state_dir = root / ".claim-plane"
    writable = state_dir.is_dir() and os.access(state_dir, os.R_OK | os.W_OK | os.X_OK)
    checks.append(
        {
            "name": "state_directory",
            "status": "ok" if writable else "error",
            "detail": str(state_dir),
            "remediation": (
                None
                if writable
                else "Run 'claim-plane init' and repair directory permissions."
            ),
        }
    )

    if config is not None:
        repository = config.get("repository")
        default_branch = (
            repository.get("default_branch")
            if isinstance(repository, Mapping)
            else None
        )
        branch_exists = bool(
            default_branch
            and _git(
                root,
                "rev-parse",
                "--verify",
                f"refs/heads/{default_branch}",
                check=False,
            )
        )
        checks.append(
            {
                "name": "default_branch",
                "status": "ok" if branch_exists else "warning",
                "detail": str(default_branch or "not configured"),
            }
        )

        acceptance = config.get("acceptance")
        commands = (
            acceptance.get("commands") if isinstance(acceptance, Mapping) else None
        )
        if not isinstance(commands, list) or not commands:
            checks.append(
                {
                    "name": "acceptance_commands",
                    "status": "warning",
                    "detail": "no acceptance commands were detected",
                    "remediation": f"Add commands to {PROJECT_CONFIG_PATH}.",
                }
            )
        else:
            unavailable: list[dict[str, str]] = []
            for command in commands:
                available, detail = _command_available(root, str(command))
                if not available:
                    unavailable.append({"command": str(command), "detail": detail})
            checks.append(
                {
                    "name": "acceptance_commands",
                    "status": "warning" if unavailable else "ok",
                    "detail": f"{len(commands)} command(s) configured",
                    "commands": list(commands),
                    "unavailable": unavailable,
                }
            )

        secret_found = _contains_secret_material(config)
        checks.append(
            {
                "name": "secret_redaction",
                "status": "error" if secret_found else "ok",
                "detail": (
                    "sensitive material appears in project configuration"
                    if secret_found
                    else (
                        "project configuration contains no credential fields "
                        "or recognized token material"
                    )
                ),
                "remediation": (
                    (
                        "Remove credentials from the project config and use the "
                        "runtime's credential store."
                    )
                    if secret_found
                    else None
                ),
            }
        )

    ready = all(item["status"] != "error" for item in checks)
    return ProjectDoctorReport(
        root=str(root), ready=ready, checks=tuple(checks), config=config
    )


def reset_project(
    root_or_child: str | Path = ".",
    *,
    remove_config: bool = False,
) -> dict[str, Any]:
    """Remove only Claim Plane-owned local state, preserving repository content."""

    root = resolve_project_root(root_or_child)
    removed: list[str] = []
    for relative in _LOCAL_STATE_PATHS:
        path = root / relative
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(relative.as_posix())
        elif path.exists():
            path.unlink()
            removed.append(relative.as_posix())

    if remove_config:
        config_path = root / PROJECT_CONFIG_PATH
        if config_path.exists():
            config_path.unlink()
            removed.append(PROJECT_CONFIG_PATH.as_posix())
        for backup in sorted(config_path.parent.glob(f"{config_path.name}.*.bak")):
            backup.unlink()
            removed.append(backup.relative_to(root).as_posix())

    state_dir = root / ".claim-plane"
    try:
        state_dir.rmdir()
    except OSError:
        pass

    return {
        "protocol": "claim-plane.project-reset.v1",
        "root": str(root),
        "removed": removed,
        "config_preserved": not remove_config and (root / PROJECT_CONFIG_PATH).exists(),
    }
