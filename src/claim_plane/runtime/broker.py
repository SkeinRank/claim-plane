"""Sound repository broker for governed coding-agent execution.

Claim Plane treats repository access as capabilities, not a generic
``mutating`` bit.  Every request is checked against the live admitted intent,
written to a durable broker operation journal before the filesystem action,
and committed to a broker-bound observation session afterwards.

The broker is a reference monitor only when the worker has no alternative path
to the repository.  ``build_broker_boundary_command`` provides that boundary on
Linux with Bubblewrap; other deployments must provide an equivalent sandbox.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import socketserver
import stat
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from claim_plane.core import (
    AccessMode,
    ChangeIntent,
    IntentOperation,
    ObservedAccess,
    Plane,
    ResourceKind,
    ResourceRef,
)
from claim_plane.integration.sandbox import (
    SandboxPolicy,
    resolve_sandbox_command,
    sanitized_environment,
)
from claim_plane.integration.snapshot import (
    capture_worktree_tree,
    freeze_worktree,
    materialize_snapshot,
    remove_materialized_snapshot,
)
from claim_plane.runtime.worktree_lock import (
    WorktreeLockError,
    WorktreeWriterLock,
    canonical_worktree_lock_dir,
)

BROKER_PROTOCOL = "claim-plane.broker.v2"

# ``sockaddr_un.sun_path`` is 104 bytes on macOS and 108 bytes on Linux.
# Keep a small portable margin for the terminating NUL and less common Unix
# variants.  Long project paths are common under CloudStorage and pytest.
_UNIX_SOCKET_PATH_MAX_BYTES = 100


def _ensure_private_socket_directory(path: Path) -> None:
    """Create a per-user socket directory without accepting symlinks.

    The fallback lives below ``/tmp`` because macOS pytest and cloud-storage
    paths can exceed the kernel's AF_UNIX path limit.  Rejecting a symlink here
    avoids redirecting a privileged broker socket into an attacker-controlled
    location.
    """

    if path.is_symlink():
        raise BrokerError(f"broker socket directory must not be a symlink: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise BrokerError(f"broker socket directory is not a directory: {path}")
    if hasattr(os, "getuid"):
        owner = path.stat().st_uid
        if owner != os.getuid():
            raise BrokerError(
                f"broker socket directory is not owned by the current user: {path}"
            )
    try:
        path.chmod(0o700)
    except PermissionError as exc:
        raise BrokerError(
            f"cannot secure broker socket directory permissions: {path}"
        ) from exc


def _portable_unix_socket_path(socket_path: str | Path, *, create_parent: bool) -> Path:
    """Return a deterministic AF_UNIX path that is portable across macOS/Linux.

    ``Path.resolve()`` is intentionally avoided for the socket itself: on macOS
    it expands ``/tmp`` to ``/private/tmp`` and can make an otherwise valid path
    longer.  Server and client both apply this function, so callers may keep
    using the original long path while connecting to the same shortened socket.
    """

    requested = os.path.abspath(os.path.expanduser(os.fspath(socket_path)))
    if len(os.fsencode(requested)) <= _UNIX_SOCKET_PATH_MAX_BYTES:
        path = Path(requested)
        if create_parent:
            path.parent.mkdir(parents=True, exist_ok=True)
        return path

    digest = hashlib.sha256(os.fsencode(requested)).hexdigest()[:24]
    uid = os.getuid() if hasattr(os, "getuid") else 0
    configured_dir = os.environ.get("CLAIM_PLANE_SOCKET_DIR", f"/tmp/claim-plane-{uid}")
    socket_dir = Path(os.path.abspath(os.path.expanduser(configured_dir)))
    candidate = socket_dir / f"broker-{digest}.sock"

    # An operator-provided fallback directory can itself be too long.  Retain a
    # final fixed-width path so the failure cannot recur during normalization.
    if len(os.fsencode(str(candidate))) > _UNIX_SOCKET_PATH_MAX_BYTES:
        socket_dir = Path(f"/tmp/cp-{uid}")
        candidate = socket_dir / f"b-{digest}.sock"
    if len(os.fsencode(str(candidate))) > _UNIX_SOCKET_PATH_MAX_BYTES:
        raise BrokerError("unable to construct a portable AF_UNIX broker socket path")
    if create_parent:
        _ensure_private_socket_directory(socket_dir)
    return candidate


class BrokerError(RuntimeError):
    """A deterministic broker policy, capability, or protocol failure."""


@dataclass(frozen=True, slots=True)
class BrokerCommand:
    """One allowlisted build/test command exposed by the broker."""

    name: str
    argv: tuple[str, ...]
    timeout_seconds: int = 300

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name:
            raise ValueError("broker command name must not be empty")
        argv = tuple(str(item) for item in self.argv if str(item))
        if not argv:
            raise ValueError("broker command argv must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("broker command timeout must be positive")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "argv", argv)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, name: str, data: object) -> "BrokerCommand":
        if isinstance(data, str):
            return cls(name=name, argv=tuple(shlex.split(data)))
        if isinstance(data, list):
            return cls(name=name, argv=tuple(str(item) for item in data))
        if not isinstance(data, Mapping):
            raise TypeError(
                f"broker command {name!r} must be a string, array, or object"
            )
        raw = data.get("argv") or data.get("command")
        if isinstance(raw, str):
            argv = tuple(shlex.split(raw))
        elif isinstance(raw, list):
            argv = tuple(str(item) for item in raw)
        else:
            raise ValueError(f"broker command {name!r} requires argv")
        return cls(
            name=name,
            argv=argv,
            timeout_seconds=int(data.get("timeout_seconds", 300)),
        )


@dataclass(frozen=True, slots=True)
class BrokerPolicy:
    root: str
    intent_id: str
    session_id: str
    socket_path: str
    token: str = field(repr=False)
    observation_key: bytes = field(repr=False)
    broker_key: bytes | None = field(default=None, repr=False)
    db_path: str = ".claim-plane/plane.db"
    monitor_id: str = "claim-plane-broker"
    key_id: str = "default"
    instance_id: str = field(default_factory=lambda: f"broker-{secrets.token_hex(12)}")
    required_tools: tuple[str, ...] = ()
    max_read_bytes: int = 2_000_000
    max_write_bytes: int = 2_000_000
    allow_delete: bool = True
    commands: Mapping[str, Any] = field(default_factory=dict)
    command_sandbox: SandboxPolicy = field(
        default_factory=lambda: SandboxPolicy(
            backend="tree", strict=True, allow_network=False
        )
    )
    journal_dir: str | None = None
    worktree_lock_dir: str | None = None
    writer_lease_seconds: int = 300

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"broker root is not a directory: {root}")
        if not self.intent_id.strip() or not self.session_id.strip():
            raise ValueError("intent_id and session_id must not be empty")
        if not self.instance_id.strip():
            raise ValueError("broker instance_id must not be empty")
        if not self.token:
            raise ValueError("broker token must not be empty")
        if not self.observation_key:
            raise ValueError("observation_key must not be empty")
        broker_key = self.broker_key or self.observation_key
        if not broker_key:
            raise ValueError("broker_key must not be empty")
        if self.max_read_bytes <= 0 or self.max_write_bytes <= 0:
            raise ValueError("broker byte limits must be positive")
        if self.writer_lease_seconds <= 0:
            raise ValueError("broker writer_lease_seconds must be positive")
        commands: dict[str, BrokerCommand] = {}
        for name, value in dict(self.commands).items():
            command = (
                value
                if isinstance(value, BrokerCommand)
                else BrokerCommand.from_dict(name, value)
            )
            commands[command.name] = command
        sandbox = (
            self.command_sandbox
            if isinstance(self.command_sandbox, SandboxPolicy)
            else SandboxPolicy.from_dict(self.command_sandbox)
        )
        socket_path = _portable_unix_socket_path(self.socket_path, create_parent=True)
        if self.journal_dir:
            journal = Path(self.journal_dir).expanduser().resolve()
        elif self.db_path != ":memory:":
            journal = (
                Path(self.db_path).expanduser().resolve().parent / "broker-journal"
            )
        else:
            journal = socket_path.parent / "broker-journal"
        journal.mkdir(parents=True, exist_ok=True)
        try:
            lock_dir = canonical_worktree_lock_dir(root, self.worktree_lock_dir)
        except WorktreeLockError as exc:
            raise ValueError(str(exc)) from exc
        object.__setattr__(self, "root", str(root))
        object.__setattr__(self, "socket_path", str(socket_path))
        object.__setattr__(self, "broker_key", broker_key)
        object.__setattr__(self, "commands", commands)
        object.__setattr__(self, "command_sandbox", sandbox)
        object.__setattr__(self, "journal_dir", str(journal))
        object.__setattr__(self, "worktree_lock_dir", str(lock_dir))
        object.__setattr__(
            self,
            "required_tools",
            tuple(
                dict.fromkeys(
                    item.strip() for item in self.required_tools if item.strip()
                )
            ),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "protocol": BROKER_PROTOCOL,
            "root": self.root,
            "intent_id": self.intent_id,
            "session_id": self.session_id,
            "socket_path": self.socket_path,
            "instance_id": self.instance_id,
            "monitor_id": self.monitor_id,
            "key_id": self.key_id,
            "required_tools": list(self.required_tools),
            "max_read_bytes": self.max_read_bytes,
            "max_write_bytes": self.max_write_bytes,
            "allow_delete": self.allow_delete,
            "writer_lease_seconds": self.writer_lease_seconds,
            "worktree_lock_dir": self.worktree_lock_dir,
            "commands": {
                name: command.to_dict()
                for name, command in sorted(self.commands.items())
            },
            "command_sandbox": self.command_sandbox.to_dict(),
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)


def _apply_file_mode(path: Path, mode: int) -> None:
    # Windows does not model the POSIX executable bits represented by Git.
    if os.name != "nt":
        os.chmod(path, mode, follow_symlinks=False)


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise BrokerError(completed.stderr.strip() or f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def _repo_identity(repo: Path) -> tuple[str, str]:
    head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}").lower()
    common_raw = _git(repo, "rev-parse", "--git-common-dir")
    common = Path(common_raw)
    if not common.is_absolute():
        common = (repo / common).resolve()
    identity = _sha256(f"{common}\n{repo}\n".encode("utf-8"))
    return head, identity


def _tree_changed_paths(repo: Path, left_tree: str, right_tree: str) -> tuple[str, ...]:
    completed = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            left_tree,
            right_tree,
            "--",
        ],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise BrokerError(
            completed.stderr.decode("utf-8", errors="replace").strip()
            or "could not compare broker tree transition"
        )
    return tuple(
        sorted(
            item.decode("utf-8", errors="replace")
            for item in completed.stdout.split(b"\0")
            if item
        )
    )


def _parse_region(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    match = re.fullmatch(r"(?:lines?:)?\s*(\d+)\s*[-:]\s*(\d+)", value)
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    if start <= 0 or end < start:
        return None
    return start, end


def _matching_operations(
    intent: ChangeIntent,
    path: str,
    *,
    modes: Iterable[AccessMode] | None = None,
    committed_only: bool = False,
) -> tuple[IntentOperation, ...]:
    allowed = set(modes) if modes is not None else None
    return tuple(
        operation
        for operation in intent.operations
        if operation.resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}
        and operation.resource.covers_path(path)
        and (allowed is None or operation.access in allowed)
        and (not committed_only or operation.committed)
    )


def _read_allowed(intent: ChangeIntent, path: str) -> bool:
    # A write capability implies read-before-write access to the same surface.
    return bool(_matching_operations(intent, path))


def _line_regions_for(
    intent: ChangeIntent, path: str, *, modes: Iterable[AccessMode]
) -> tuple[tuple[int, int], ...] | None:
    """Return the union of bounded committed regions for a path.

    ``None`` means an unbounded committed capability exists. An empty tuple means
    no committed capability exists. Multiple bounded regions remain distinct instead
    of collapsing into implicit whole-file authority.
    """

    operations = _matching_operations(intent, path, modes=modes, committed_only=True)
    if not operations:
        return ()

    regions: list[tuple[int, int]] = []
    for operation in operations:
        if operation.resource.region is None:
            return None
        region = _parse_region(operation.resource.region)
        if region is None:
            # Unknown region syntax must not widen a broker capability.
            continue
        regions.append(region)
    return tuple(sorted(set(regions)))


def _region_authorized(
    intent: ChangeIntent,
    path: str,
    *,
    modes: Iterable[AccessMode],
    requested: tuple[int, int] | None,
) -> bool:
    operations = _matching_operations(intent, path, modes=modes, committed_only=True)
    if requested is None:
        return any(operation.resource.region is None for operation in operations)

    for operation in operations:
        if operation.resource.region is None:
            return True
        admitted = _parse_region(operation.resource.region)
        if admitted is None:
            continue
        if admitted[0] <= requested[0] <= requested[1] <= admitted[1]:
            return True
    return False


def _directory_visible(intent: ChangeIntent, directory: str) -> bool:
    prefix = directory.rstrip("/")
    if prefix:
        prefix += "/"
    for operation in intent.operations:
        if operation.resource.kind not in {ResourceKind.FILE, ResourceKind.DOCUMENT}:
            continue
        identifier = operation.resource.identifier.replace("\\", "/").lstrip("./")
        literal = re.split(r"[*?[\\]", identifier, maxsplit=1)[0]
        if literal.startswith(prefix) or prefix.startswith(literal.rstrip("/") + "/"):
            return True
    return False


def _rename_target(operation: IntentOperation) -> str | None:
    for source in (operation.metadata, operation.resource.metadata):
        for key in ("rename_to", "target", "to"):
            value = source.get(key)
            if value:
                return str(value).replace("\\", "/").lstrip("./")
    return None


def _binary_digest() -> str:
    try:
        return _sha256(Path(__file__).read_bytes())
    except OSError:
        return _sha256(b"claim-plane.runtime.broker.v10")


class _BrokerCore:
    def __init__(self, policy: BrokerPolicy) -> None:
        self.policy = policy
        self.root = Path(policy.root)
        self._lock = threading.RLock()
        self._journal = Path(policy.journal_dir or "") / policy.instance_id
        self._journal.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._worktree_lock = WorktreeWriterLock(
            self.root,
            instance_id=policy.instance_id,
            lock_dir=policy.worktree_lock_dir,
        )
        try:
            self._worktree_lock.acquire()
        except WorktreeLockError as exc:
            raise ValueError(
                f"governed worktree already has an active broker writer: {self.root}"
            ) from exc
        try:
            plane = Plane.open(policy.db_path, governance="governed")
            try:
                intent = plane.intent(policy.intent_id)
                if intent is None:
                    raise KeyError(f"unknown intent: {policy.intent_id}")
                if not intent.base_commit:
                    raise BrokerError("broker requires an intent pinned to base_commit")
                head, repo_identity = _repo_identity(self.root)
                if head != intent.base_commit:
                    raise BrokerError(
                        f"broker root HEAD {head} does not match intent base {intent.base_commit}"
                    )
                self.base_commit = intent.base_commit
                self.repo_identity = repo_identity
                base_tree = _git(
                    self.root, "rev-parse", f"{intent.base_commit}^{{tree}}"
                )
                initial_tree = capture_worktree_tree(self.root, seed=intent.base_commit)
                if initial_tree != base_tree:
                    raise BrokerError(
                        "broker requires a clean worktree at startup; "
                        f"base tree is {base_tree}, observed {initial_tree}"
                    )
                self._registration = plane.register_broker_instance(
                    instance_id=policy.instance_id,
                    intent_id=policy.intent_id,
                    session_id=policy.session_id,
                    monitor_id=policy.monitor_id,
                    key_id=policy.key_id,
                    root_path=str(self.root),
                    repo_identity=repo_identity,
                    base_commit=intent.base_commit,
                    initial_tree_hash=initial_tree,
                    writer_lease_seconds=policy.writer_lease_seconds,
                    policy=policy.public_dict(),
                    binary_digest=_binary_digest(),
                    broker_key=policy.broker_key or b"",
                    required_tools=policy.required_tools,
                )
                self._fencing_token = int(self._registration["fencing_token"])
            finally:
                plane.close()
            self._recover_pending()
        except Exception:
            self._worktree_lock.release()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            plane = Plane.open(self.policy.db_path, governance="governed")
            try:
                plane.stop_broker_instance(
                    self.policy.instance_id, broker_key=self.policy.broker_key or b""
                )
            except (KeyError, ValueError):
                pass
            finally:
                plane.close()
        finally:
            self._worktree_lock.release()

    def _resolve(self, raw: str, *, allow_missing: bool = False) -> tuple[Path, str]:
        if not isinstance(raw, str) or not raw.strip():
            raise BrokerError("path must be a non-empty string")
        candidate = Path(raw.replace("\\", "/"))
        if candidate.is_absolute() or ".." in candidate.parts:
            raise BrokerError("path must be relative and stay inside the broker root")
        target = self.root / candidate
        parent = target.parent.resolve()
        try:
            parent.relative_to(self.root)
        except ValueError as exc:
            raise BrokerError("path escapes broker root") from exc
        if target.exists() or target.is_symlink():
            resolved = target.resolve()
            try:
                resolved.relative_to(self.root)
            except ValueError as exc:
                raise BrokerError("symlink escapes broker root") from exc
        elif not allow_missing:
            raise BrokerError(f"path does not exist: {candidate.as_posix()}")
        normalized = candidate.as_posix().lstrip("./")
        return target, normalized

    def _validate_live(self) -> ChangeIntent:
        head, identity = _repo_identity(self.root)
        if head != self.base_commit or identity != self.repo_identity:
            raise BrokerError("broker repository identity or base HEAD changed")
        current_tree = capture_worktree_tree(self.root, seed=self.base_commit)
        plane = Plane.open(self.policy.db_path, governance="governed")
        try:
            result = plane.validate_broker_instance(
                self.policy.instance_id,
                broker_key=self.policy.broker_key or b"",
                current_tree_hash=current_tree,
            )
            intent = result["intent"]
            if not isinstance(intent, ChangeIntent):
                raise BrokerError("broker capability returned an invalid intent")
            instance = result["instance"]
            observed_token = int(instance.get("fencing_token") or 0)
            if observed_token != self._fencing_token:
                raise BrokerError(
                    f"broker fencing token was superseded: expected {self._fencing_token}, observed {observed_token}"
                )
            self._expected_tree_hash = str(
                instance.get("expected_tree_hash") or current_tree
            )
            return intent
        finally:
            plane.close()

    def _existing_response(self, request_id: str) -> dict[str, Any] | None:
        plane = Plane.open(self.policy.db_path, governance="governed")
        try:
            existing = plane.broker_operation_for_request(
                self.policy.instance_id, request_id
            )
        finally:
            plane.close()
        if not existing:
            return None
        if existing["state"] == "committed" and isinstance(
            existing.get("response_json"), dict
        ):
            return dict(existing["response_json"])
        if existing["state"] == "pending":
            raise BrokerError("duplicate broker request is still pending")
        raise BrokerError(
            f"duplicate broker request previously ended as {existing['state']}"
        )

    def _prepare(
        self,
        *,
        request_id: str,
        operation: str,
        mode: AccessMode,
        path: str,
        target_path: str | None = None,
        payload: Mapping[str, object] | None = None,
        pre_tree_hash: str | None = None,
    ) -> str:
        operation_id = f"op-{secrets.token_hex(16)}"
        plane = Plane.open(self.policy.db_path, governance="governed")
        try:
            result = plane.prepare_broker_operation(
                operation_id=operation_id,
                instance_id=self.policy.instance_id,
                request_id=request_id,
                operation=operation,
                mode=mode,
                path=path,
                target_path=target_path,
                payload=dict(payload or {}),
                broker_key=self.policy.broker_key or b"",
                fencing_token=self._fencing_token,
                pre_tree_hash=pre_tree_hash,
            )
        finally:
            plane.close()
        return str(result.get("operation_id") or operation_id)

    def _commit(
        self,
        operation_id: str,
        *,
        accesses: Iterable[ObservedAccess],
        response: Mapping[str, object],
        post_tree_hash: str | None = None,
    ) -> dict[str, Any]:
        plane = Plane.open(self.policy.db_path, governance="governed")
        try:
            result = plane.commit_broker_operation(
                operation_id,
                accesses=tuple(accesses),
                response=dict(response),
                observation_key=self.policy.observation_key,
                broker_key=self.policy.broker_key or b"",
                fencing_token=self._fencing_token,
                post_tree_hash=post_tree_hash,
            )
        finally:
            plane.close()
        payload = result.get("response_json")
        return dict(payload) if isinstance(payload, dict) else dict(response)

    def _fail(self, operation_id: str, *, state: str, error: str) -> None:
        plane = Plane.open(self.policy.db_path, governance="governed")
        try:
            plane.fail_broker_operation(
                operation_id,
                state=state,
                error=error,
                broker_key=self.policy.broker_key or b"",
            )
        finally:
            plane.close()

    def _access(
        self,
        mode: AccessMode,
        path: str,
        tool: str,
        **metadata: object,
    ) -> ObservedAccess:
        return ObservedAccess(
            mode=mode,
            resource=ResourceRef(ResourceKind.FILE, path),
            tool=tool,
            metadata=dict(metadata),
        )

    def _verify_tree_transition(
        self,
        pre_tree_hash: str,
        *,
        expected_paths: Iterable[str],
    ) -> str:
        post_tree_hash = capture_worktree_tree(self.root, seed=self.base_commit)
        changed = set(_tree_changed_paths(self.root, pre_tree_hash, post_tree_hash))
        expected = {item.replace("\\", "/").lstrip("./") for item in expected_paths}
        if changed != expected:
            raise BrokerError(
                "broker tree transition changed unexpected paths: "
                f"expected {sorted(expected)}, observed {sorted(changed)}"
            )
        return post_tree_hash

    def _journal_paths(self, operation_id: str) -> tuple[Path, Path]:
        return (
            self._journal / f"{operation_id}.backup",
            self._journal / f"{operation_id}.stage",
        )

    def _capture_backup(
        self, target: Path, backup: Path
    ) -> tuple[bool, str | None, int | None]:
        if target.exists():
            if not target.is_file():
                raise BrokerError("broker mutations only support files")
            data = target.read_bytes()
            backup.write_bytes(data)
            return True, _sha256(data), _file_mode(target)
        backup.unlink(missing_ok=True)
        return False, None, None

    def _rollback_payload(self, payload: Mapping[str, Any]) -> None:
        operation = str(payload.get("operation") or "")
        path = str(payload.get("path") or "")
        target_path = str(payload.get("target_path") or "")
        journal = payload.get("journal") or {}
        if not isinstance(journal, Mapping):
            raise BrokerError("invalid broker recovery journal")
        backup = Path(str(journal.get("backup_path") or ""))
        old_exists = bool(journal.get("old_exists"))
        target = self.root / path
        if operation == "rename_file":
            destination = self.root / target_path
            if destination.exists() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, target)
            return
        if old_exists:
            if not backup.is_file():
                raise BrokerError(f"broker recovery backup is missing: {backup}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f".{target.name}.claim-plane-recovery")
            shutil.copyfile(backup, temp)
            old_mode = journal.get("old_mode")
            if old_mode is not None:
                _apply_file_mode(temp, int(old_mode))
            os.replace(temp, target)
            if old_mode is not None and _file_mode(target) != int(old_mode):
                raise BrokerError(f"broker recovery mode mismatch: {path}")
        elif target.exists():
            expected = journal.get("new_sha256")
            current = _sha256(target.read_bytes()) if target.is_file() else None
            if expected and current != expected:
                raise BrokerError(
                    f"refusing recovery delete because {path} no longer matches pending operation"
                )
            target.unlink()

    def _recover_pending(self) -> None:
        plane = Plane.open(self.policy.db_path, governance="governed")
        try:
            pending = plane.pending_broker_operations(self.policy.instance_id)
        finally:
            plane.close()
        for item in pending:
            payload = dict(item.get("payload_json") or {})
            try:
                self._rollback_payload(payload)
            except Exception as exc:  # noqa: BLE001
                self._fail(
                    str(item["operation_id"]),
                    state="failed",
                    error=f"recovery failed: {exc}",
                )
                raise BrokerError(
                    f"could not recover pending broker operation {item['operation_id']}: {exc}"
                ) from exc
            self._fail(
                str(item["operation_id"]),
                state="rolled_back",
                error="rolled back during broker startup recovery",
            )

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        token = str(request.get("token") or "")
        if not hmac.compare_digest(token, self.policy.token):
            raise BrokerError("unauthorized broker request")
        operation = str(request.get("op") or "")
        request_id = str(request.get("request_id") or secrets.token_hex(8))
        with self._lock:
            existing = self._existing_response(request_id)
            if existing is not None:
                return existing
            intent = self._validate_live()
            if operation == "health":
                return {
                    "protocol": BROKER_PROTOCOL,
                    "ok": True,
                    "intent_id": self.policy.intent_id,
                    "session_id": self.policy.session_id,
                    "broker_instance_id": self.policy.instance_id,
                    "fencing_token": self._fencing_token,
                    "request_id": request_id,
                }
            handlers = {
                "read_file": self._read_file,
                "list_dir": self._list_dir,
                "search_text": self._search_text,
                "stat": self._stat,
                "write_file": self._write_file,
                "append_file": self._append_file,
                "replace_lines": self._replace_lines,
                "delete_file": self._delete_file,
                "rename_file": self._rename_file,
                "run_command": self._run_command,
            }
            handler = handlers.get(operation)
            if handler is None:
                raise BrokerError(f"unsupported broker operation: {operation}")
            return handler(intent, request, request_id)

    def _read_file(
        self, intent: ChangeIntent, request: Mapping[str, Any], request_id: str
    ) -> dict[str, Any]:
        target, path = self._resolve(str(request.get("path") or ""))
        if not _read_allowed(intent, path):
            raise BrokerError(f"read is outside admitted intent: {path}")
        operation_id = self._prepare(
            request_id=request_id,
            operation="read_file",
            mode=AccessMode.READ,
            path=path,
        )
        try:
            data = target.read_bytes()
            if len(data) > self.policy.max_read_bytes:
                raise BrokerError(f"read exceeds max_read_bytes: {path}")
            start = max(1, int(request.get("start_line") or 1))
            end_raw = request.get("end_line")
            text = data.decode("utf-8")
            lines = text.splitlines(keepends=True)
            end = len(lines) if end_raw is None else int(end_raw)
            if end < start:
                raise BrokerError(
                    "end_line must be greater than or equal to start_line"
                )
            response = {
                "ok": True,
                "request_id": request_id,
                "path": path,
                "content": "".join(lines[start - 1 : end]),
                "sha256": _sha256(data),
                "line_count": len(lines),
            }
            return self._commit(
                operation_id,
                accesses=(
                    self._access(
                        AccessMode.READ,
                        path,
                        "broker.read_file",
                        start_line=start,
                        end_line=end,
                        sha256=_sha256(data),
                    ),
                ),
                response=response,
            )
        except Exception as exc:
            self._fail(operation_id, state="failed", error=str(exc))
            raise

    def _list_dir(
        self, intent: ChangeIntent, request: Mapping[str, Any], request_id: str
    ) -> dict[str, Any]:
        raw = str(request.get("path") or ".")
        target, path = (self.root, "") if raw == "." else self._resolve(raw)
        if not target.is_dir():
            raise BrokerError(f"not a directory: {path or '.'}")
        if not _directory_visible(intent, path):
            raise BrokerError(f"directory is outside admitted intent: {path or '.'}")
        operation_id = self._prepare(
            request_id=request_id,
            operation="list_dir",
            mode=AccessMode.READ,
            path=path or ".",
        )
        try:
            entries: list[dict[str, Any]] = []
            for child in sorted(target.iterdir(), key=lambda item: item.name):
                rel = child.relative_to(self.root).as_posix()
                if child.is_symlink():
                    continue
                if child.is_dir() and not _directory_visible(intent, rel):
                    continue
                if child.is_file() and not _read_allowed(intent, rel):
                    continue
                entries.append(
                    {
                        "name": child.name,
                        "path": rel,
                        "type": "dir" if child.is_dir() else "file",
                    }
                )
            response = {
                "ok": True,
                "request_id": request_id,
                "path": path,
                "entries": entries,
            }
            return self._commit(
                operation_id,
                accesses=(
                    self._access(
                        AccessMode.READ,
                        path or ".",
                        "broker.list_dir",
                        entry_count=len(entries),
                    ),
                ),
                response=response,
            )
        except Exception as exc:
            self._fail(operation_id, state="failed", error=str(exc))
            raise

    def _search_text(
        self, intent: ChangeIntent, request: Mapping[str, Any], request_id: str
    ) -> dict[str, Any]:
        query = str(request.get("query") or "")
        if not query:
            raise BrokerError("query must not be empty")
        max_results = min(500, max(1, int(request.get("max_results") or 100)))
        operation_id = self._prepare(
            request_id=request_id,
            operation="search_text",
            mode=AccessMode.READ,
            path=".",
            payload={"query_sha256": _sha256(query.encode("utf-8"))},
        )
        try:
            matches: list[dict[str, Any]] = []
            accesses: list[ObservedAccess] = []
            for item in sorted(self.root.rglob("*")):
                if not item.is_file() or item.is_symlink():
                    continue
                rel = item.relative_to(self.root).as_posix()
                if not _read_allowed(intent, rel):
                    continue
                try:
                    data = item.read_bytes()
                    if len(data) > self.policy.max_read_bytes:
                        continue
                    text = data.decode("utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                accesses.append(
                    self._access(
                        AccessMode.READ,
                        rel,
                        "broker.search_text",
                        query_sha256=_sha256(query.encode("utf-8")),
                        file_sha256=_sha256(data),
                    )
                )
                for number, line in enumerate(text.splitlines(), 1):
                    if query in line:
                        matches.append(
                            {"path": rel, "line": number, "text": line[:500]}
                        )
                        if len(matches) >= max_results:
                            break
                if len(matches) >= max_results:
                    break
            response = {"ok": True, "request_id": request_id, "matches": matches}
            return self._commit(operation_id, accesses=accesses, response=response)
        except Exception as exc:
            self._fail(operation_id, state="failed", error=str(exc))
            raise

    def _stat(
        self, intent: ChangeIntent, request: Mapping[str, Any], request_id: str
    ) -> dict[str, Any]:
        target, path = self._resolve(str(request.get("path") or ""))
        if not _read_allowed(intent, path) and not (
            target.is_dir() and _directory_visible(intent, path)
        ):
            raise BrokerError(f"stat is outside admitted intent: {path}")
        operation_id = self._prepare(
            request_id=request_id,
            operation="stat",
            mode=AccessMode.READ,
            path=path,
        )
        try:
            info = target.stat()
            response = {
                "ok": True,
                "request_id": request_id,
                "path": path,
                "type": "dir" if target.is_dir() else "file",
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
            }
            return self._commit(
                operation_id,
                accesses=(
                    self._access(
                        AccessMode.READ, path, "broker.stat", size=info.st_size
                    ),
                ),
                response=response,
            )
        except Exception as exc:
            self._fail(operation_id, state="failed", error=str(exc))
            raise

    def _mutate_bytes(
        self,
        *,
        intent: ChangeIntent,
        request_id: str,
        operation: str,
        mode: AccessMode,
        target: Path,
        path: str,
        content: bytes,
        tool: str,
        metadata: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        pre_tree_hash = capture_worktree_tree(self.root, seed=self.base_commit)
        operation_id = f"op-{secrets.token_hex(16)}"
        backup, stage = self._journal_paths(operation_id)
        old_exists, old_sha, old_mode = self._capture_backup(target, backup)
        new_mode = old_mode if old_mode is not None else 0o644
        stage.write_bytes(content)
        _apply_file_mode(stage, new_mode)
        journal = {
            "backup_path": str(backup),
            "stage_path": str(stage),
            "old_exists": old_exists,
            "old_sha256": old_sha,
            "old_mode": old_mode,
            "new_sha256": _sha256(content),
            "new_mode": new_mode,
        }
        plane = Plane.open(self.policy.db_path, governance="governed")
        try:
            prepared = plane.prepare_broker_operation(
                operation_id=operation_id,
                instance_id=self.policy.instance_id,
                request_id=request_id,
                operation=operation,
                mode=mode,
                path=path,
                target_path=None,
                payload={
                    "operation": operation,
                    "path": path,
                    "journal": journal,
                    **dict(metadata or {}),
                },
                broker_key=self.policy.broker_key or b"",
                fencing_token=self._fencing_token,
                pre_tree_hash=pre_tree_hash,
            )
            operation_id = str(prepared.get("operation_id") or operation_id)
        finally:
            plane.close()
        mutated = False
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, target)
            mutated = True
            if (
                not target.is_file()
                or _sha256(target.read_bytes()) != journal["new_sha256"]
            ):
                raise BrokerError(f"broker mutation result hash mismatch: {path}")
            if _file_mode(target) != new_mode:
                raise BrokerError(f"broker mutation result mode mismatch: {path}")
            post_tree_hash = self._verify_tree_transition(
                pre_tree_hash, expected_paths=(path,)
            )
            response = {
                "ok": True,
                "request_id": request_id,
                "path": path,
                "sha256": _sha256(content),
                "file_mode": f"{new_mode:04o}",
            }
            response = self._commit(
                operation_id,
                accesses=(
                    self._access(
                        mode,
                        path,
                        tool,
                        sha256=_sha256(content),
                        old_mode=(None if old_mode is None else f"{old_mode:04o}"),
                        new_mode=f"{new_mode:04o}",
                        **dict(metadata or {}),
                    ),
                ),
                response=response,
                post_tree_hash=post_tree_hash,
            )
            backup.unlink(missing_ok=True)
            stage.unlink(missing_ok=True)
            return response
        except Exception as exc:
            rollback_error: Exception | None = None
            if mutated:
                try:
                    self._rollback_payload(
                        {
                            "operation": operation,
                            "path": path,
                            "journal": journal,
                        }
                    )
                except Exception as rollback_exc:  # noqa: BLE001
                    rollback_error = rollback_exc
            state = "rolled_back" if mutated and rollback_error is None else "failed"
            error = (
                str(exc)
                if rollback_error is None
                else f"{exc}; rollback failed: {rollback_error}"
            )
            self._fail(operation_id, state=state, error=error)
            raise BrokerError(error) from exc

    def _ensure_mutation_scope(
        self,
        intent: ChangeIntent,
        path: str,
        *,
        modes: Iterable[AccessMode],
        requested_region: tuple[int, int] | None = None,
    ) -> ChangeIntent:
        """Promote only the contingent capability covering the concrete mutation."""

        modes = tuple(modes)
        if _region_authorized(
            intent,
            path,
            modes=modes,
            requested=requested_region,
        ):
            return intent

        contingent = tuple(
            operation
            for operation in _matching_operations(intent, path, modes=modes)
            if operation.contingent
        )
        if not contingent:
            return intent

        region = (
            None
            if requested_region is None
            else f"lines:{requested_region[0]}-{requested_region[1]}"
        )

        plane = Plane.open(self.policy.db_path, governance="governed")
        try:
            try:
                decision = plane.promote_contingent_scope(
                    intent.intent_id,
                    path=path,
                    modes=modes,
                    region=region,
                    broker_instance_id=self.policy.instance_id,
                    broker_key=self.policy.broker_key or b"",
                )
            except ValueError:
                # A contingent path may exist while none of its bounded regions covers
                # this concrete mutation. Leave the intent unchanged; the caller will
                # reject the missing capability below.
                return intent

            if not decision.allowed:
                blockers = sorted(
                    {
                        conflict.existing_intent_id
                        for conflict in decision.conflicts
                        if conflict.blocking
                    }
                )
                suffix = f"; blockers={','.join(blockers)}" if blockers else ""
                region_suffix = f" region={region}" if region else ""
                raise BrokerError(
                    f"contingent scope promotion rejected for {path}{region_suffix}{suffix}: "
                    f"{decision.guidance}"
                )
            promoted = plane.intent(intent.intent_id)
            if promoted is None:
                raise BrokerError("promoted intent disappeared from the registry")
            return promoted
        finally:
            plane.close()

    def _write_file(
        self, intent: ChangeIntent, request: Mapping[str, Any], request_id: str
    ) -> dict[str, Any]:
        target, path = self._resolve(str(request.get("path") or ""), allow_missing=True)
        allowed = {AccessMode.WRITE, AccessMode.DOCUMENT, AccessMode.TEST}
        intent = self._ensure_mutation_scope(intent, path, modes=allowed)
        if not _region_authorized(intent, path, modes=allowed, requested=None):
            regions = _line_regions_for(intent, path, modes=allowed)
            if regions:
                raise BrokerError("bounded file writes must use replace_lines")
            raise BrokerError(
                f"write_file requires unbounded write/document/test capability: {path}"
            )
        content = str(request.get("content") or "").encode("utf-8")
        if len(content) > self.policy.max_write_bytes:
            raise BrokerError(f"write exceeds max_write_bytes: {path}")
        return self._mutate_bytes(
            intent=intent,
            request_id=request_id,
            operation="write_file",
            mode=AccessMode.WRITE,
            target=target,
            path=path,
            content=content,
            tool="broker.write_file",
            metadata={"size": len(content)},
        )

    def _append_file(
        self, intent: ChangeIntent, request: Mapping[str, Any], request_id: str
    ) -> dict[str, Any]:
        target, path = self._resolve(str(request.get("path") or ""), allow_missing=True)
        intent = self._ensure_mutation_scope(intent, path, modes={AccessMode.EXTEND})
        if not _region_authorized(
            intent, path, modes={AccessMode.EXTEND}, requested=None
        ):
            raise BrokerError(
                f"append_file requires unbounded extend capability: {path}"
            )
        old = target.read_bytes() if target.exists() else b""
        addition = str(request.get("content") or "").encode("utf-8")
        content = old + addition
        if len(content) > self.policy.max_write_bytes:
            raise BrokerError(f"write exceeds max_write_bytes: {path}")
        return self._mutate_bytes(
            intent=intent,
            request_id=request_id,
            operation="append_file",
            mode=AccessMode.EXTEND,
            target=target,
            path=path,
            content=content,
            tool="broker.append_file",
            metadata={"appended_bytes": len(addition)},
        )

    def _replace_lines(
        self, intent: ChangeIntent, request: Mapping[str, Any], request_id: str
    ) -> dict[str, Any]:
        target, path = self._resolve(str(request.get("path") or ""))
        allowed = {AccessMode.WRITE, AccessMode.DOCUMENT, AccessMode.TEST}
        start = int(request.get("start_line") or 0)
        end = int(request.get("end_line") or 0)
        if start <= 0 or end < start:
            raise BrokerError("invalid replace_lines bounds")
        requested_region = (start, end)
        intent = self._ensure_mutation_scope(
            intent,
            path,
            modes=allowed,
            requested_region=requested_region,
        )
        if not _region_authorized(
            intent, path, modes=allowed, requested=requested_region
        ):
            admitted = _line_regions_for(intent, path, modes=allowed)
            rendered = (
                "unbounded"
                if admitted is None
                else ", ".join(f"{a}-{b}" for a, b in admitted) or "none"
            )
            raise BrokerError(
                f"replace_lines {start}-{end} exceeds admitted regions: {rendered}"
            )
        old = target.read_text(encoding="utf-8")
        lines = old.splitlines(keepends=True)
        if end > len(lines):
            raise BrokerError("replace_lines exceeds current file length")
        replacement = str(request.get("content") or "")
        content = (
            "".join(lines[: start - 1]) + replacement + "".join(lines[end:])
        ).encode("utf-8")
        if len(content) > self.policy.max_write_bytes:
            raise BrokerError(f"write exceeds max_write_bytes: {path}")
        return self._mutate_bytes(
            intent=intent,
            request_id=request_id,
            operation="replace_lines",
            mode=AccessMode.WRITE,
            target=target,
            path=path,
            content=content,
            tool="broker.replace_lines",
            metadata={"start_line": start, "end_line": end},
        )

    def _delete_file(
        self, intent: ChangeIntent, request: Mapping[str, Any], request_id: str
    ) -> dict[str, Any]:
        if not self.policy.allow_delete:
            raise BrokerError("delete operations are disabled")
        target, path = self._resolve(str(request.get("path") or ""))
        intent = self._ensure_mutation_scope(intent, path, modes={AccessMode.DELETE})
        if not _region_authorized(
            intent, path, modes={AccessMode.DELETE}, requested=None
        ):
            raise BrokerError(
                f"delete_file requires unbounded delete capability: {path}"
            )
        if target.is_dir():
            raise BrokerError("delete_file only accepts files")
        pre_tree_hash = capture_worktree_tree(self.root, seed=self.base_commit)
        operation_id = f"op-{secrets.token_hex(16)}"
        backup, _ = self._journal_paths(operation_id)
        old_exists, old_sha, old_mode = self._capture_backup(target, backup)
        if not old_exists:
            raise BrokerError(f"path does not exist: {path}")
        journal = {
            "backup_path": str(backup),
            "old_exists": True,
            "old_sha256": old_sha,
            "old_mode": old_mode,
        }
        plane = Plane.open(self.policy.db_path, governance="governed")
        try:
            plane.prepare_broker_operation(
                operation_id=operation_id,
                instance_id=self.policy.instance_id,
                request_id=request_id,
                operation="delete_file",
                mode=AccessMode.DELETE,
                path=path,
                target_path=None,
                payload={"operation": "delete_file", "path": path, "journal": journal},
                broker_key=self.policy.broker_key or b"",
                fencing_token=self._fencing_token,
                pre_tree_hash=pre_tree_hash,
            )
        finally:
            plane.close()
        mutated = False
        try:
            target.unlink()
            mutated = True
            if target.exists() or target.is_symlink():
                raise BrokerError(f"broker delete did not remove path: {path}")
            post_tree_hash = self._verify_tree_transition(
                pre_tree_hash, expected_paths=(path,)
            )
            response = {"ok": True, "request_id": request_id, "path": path}
            response = self._commit(
                operation_id,
                accesses=(
                    self._access(
                        AccessMode.DELETE,
                        path,
                        "broker.delete_file",
                        old_mode=(None if old_mode is None else f"{old_mode:04o}"),
                    ),
                ),
                response=response,
                post_tree_hash=post_tree_hash,
            )
            backup.unlink(missing_ok=True)
            return response
        except Exception as exc:
            rollback_error: Exception | None = None
            if mutated:
                try:
                    self._rollback_payload(
                        {"operation": "delete_file", "path": path, "journal": journal}
                    )
                except Exception as rollback_exc:  # noqa: BLE001
                    rollback_error = rollback_exc
            state = "rolled_back" if mutated and rollback_error is None else "failed"
            error = (
                str(exc)
                if rollback_error is None
                else f"{exc}; rollback failed: {rollback_error}"
            )
            self._fail(operation_id, state=state, error=error)
            raise BrokerError(error) from exc

    def _rename_file(
        self, intent: ChangeIntent, request: Mapping[str, Any], request_id: str
    ) -> dict[str, Any]:
        source, path = self._resolve(str(request.get("path") or ""))
        destination, target_path = self._resolve(
            str(request.get("target_path") or ""), allow_missing=True
        )
        intent = self._ensure_mutation_scope(intent, path, modes={AccessMode.RENAME})
        operations = _matching_operations(
            intent, path, modes={AccessMode.RENAME}, committed_only=True
        )
        if not _region_authorized(
            intent, path, modes={AccessMode.RENAME}, requested=None
        ):
            raise BrokerError(
                f"rename_file requires unbounded rename capability: {path}"
            )
        expected = {_rename_target(operation) for operation in operations}
        if None in expected or target_path not in expected:
            raise BrokerError(
                "rename destination must match rename_to/target/to metadata on the admitted operation"
            )
        if destination.exists():
            raise BrokerError("rename destination already exists")
        pre_tree_hash = capture_worktree_tree(self.root, seed=self.base_commit)
        operation_id = f"op-{secrets.token_hex(16)}"
        source_mode = _file_mode(source)
        journal = {
            "old_exists": True,
            "source_sha256": _sha256(source.read_bytes()),
            "source_mode": source_mode,
        }
        plane = Plane.open(self.policy.db_path, governance="governed")
        try:
            plane.prepare_broker_operation(
                operation_id=operation_id,
                instance_id=self.policy.instance_id,
                request_id=request_id,
                operation="rename_file",
                mode=AccessMode.RENAME,
                path=path,
                target_path=target_path,
                payload={
                    "operation": "rename_file",
                    "path": path,
                    "target_path": target_path,
                    "journal": journal,
                },
                broker_key=self.policy.broker_key or b"",
                fencing_token=self._fencing_token,
                pre_tree_hash=pre_tree_hash,
            )
        finally:
            plane.close()
        mutated = False
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            mutated = True
            if source.exists() or not destination.is_file():
                raise BrokerError("broker rename result does not match requested move")
            if _sha256(destination.read_bytes()) != journal["source_sha256"]:
                raise BrokerError("broker rename changed file content")
            if _file_mode(destination) != source_mode:
                raise BrokerError("broker rename changed file mode")
            post_tree_hash = self._verify_tree_transition(
                pre_tree_hash, expected_paths=(path, target_path)
            )
            response = {
                "ok": True,
                "request_id": request_id,
                "path": path,
                "target_path": target_path,
            }
            return self._commit(
                operation_id,
                accesses=(
                    self._access(
                        AccessMode.RENAME,
                        path,
                        "broker.rename_file",
                        target_path=target_path,
                        old_mode=f"{source_mode:04o}",
                        new_mode=f"{source_mode:04o}",
                    ),
                ),
                response=response,
                post_tree_hash=post_tree_hash,
            )
        except Exception as exc:
            rollback_error: Exception | None = None
            if mutated:
                try:
                    self._rollback_payload(
                        {
                            "operation": "rename_file",
                            "path": path,
                            "target_path": target_path,
                            "journal": journal,
                        }
                    )
                except Exception as rollback_exc:  # noqa: BLE001
                    rollback_error = rollback_exc
            state = "rolled_back" if mutated and rollback_error is None else "failed"
            error = (
                str(exc)
                if rollback_error is None
                else f"{exc}; rollback failed: {rollback_error}"
            )
            self._fail(operation_id, state=state, error=error)
            raise BrokerError(error) from exc

    def _run_command(
        self, intent: ChangeIntent, request: Mapping[str, Any], request_id: str
    ) -> dict[str, Any]:
        name = str(request.get("name") or request.get("command") or "")
        command = self.policy.commands.get(name)
        if command is None:
            raise BrokerError(f"command is not allowlisted: {name}")
        test_operations = tuple(
            operation
            for operation in intent.committed_operations
            if operation.access is AccessMode.TEST
        )
        if not test_operations:
            raise BrokerError(
                "run_command requires at least one declared test operation"
            )
        operation_id = self._prepare(
            request_id=request_id,
            operation="run_command",
            mode=AccessMode.TEST,
            path=test_operations[0].resource.identifier,
            payload={"command": command.to_dict()},
        )
        snapshot = freeze_worktree(
            self.root,
            self.base_commit,
            message=f"Claim Plane broker command {self.policy.instance_id}:{request_id}",
        )
        frozen = materialize_snapshot(self.root, snapshot.snapshot_commit)
        try:
            tree_before = capture_worktree_tree(frozen)
            command_text = shlex.join(command.argv)
            sandbox = resolve_sandbox_command(
                command_text, frozen, self.policy.command_sandbox
            )
            completed = subprocess.run(
                sandbox.argv,
                cwd=frozen,
                shell=False,
                text=True,
                capture_output=True,
                timeout=command.timeout_seconds,
                check=False,
                env=sanitized_environment(
                    allowlist=self.policy.command_sandbox.environment_allowlist
                ),
            )
            tree_after = capture_worktree_tree(frozen)
            immutable = tree_before == tree_after
            returncode = completed.returncode if immutable else 126
            response = {
                "ok": returncode == 0,
                "request_id": request_id,
                "name": name,
                "returncode": returncode,
                "stdout_tail": completed.stdout[-4000:],
                "stderr_tail": completed.stderr[-4000:],
                "sandbox_backend": sandbox.backend,
                "sandbox_enforced": sandbox.enforced,
                "snapshot_immutable": immutable,
                "snapshot_tree": snapshot.tree_hash,
            }
            return self._commit(
                operation_id,
                accesses=tuple(
                    self._access(
                        AccessMode.TEST,
                        operation.resource.identifier,
                        "broker.run_command",
                        command=name,
                        returncode=returncode,
                        sandbox_backend=sandbox.backend,
                        sandbox_enforced=sandbox.enforced,
                    )
                    for operation in test_operations
                ),
                response=response,
            )
        except subprocess.TimeoutExpired as exc:
            self._fail(operation_id, state="failed", error=f"command timed out: {exc}")
            raise BrokerError(
                f"command timed out after {command.timeout_seconds}s"
            ) from exc
        except Exception as exc:
            self._fail(operation_id, state="failed", error=str(exc))
            raise
        finally:
            remove_materialized_snapshot(self.root, frozen)


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, BrokerServer)
        raw = self.rfile.readline(server.max_request_bytes + 1)
        if len(raw) > server.max_request_bytes:
            response = {"ok": False, "error": "request exceeds broker limit"}
        else:
            try:
                request = json.loads(raw.decode("utf-8"))
                if not isinstance(request, Mapping):
                    raise BrokerError("broker request must be a JSON object")
                response = server.core.handle(request)
            except Exception as exc:  # noqa: BLE001
                response = {
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
        self.wfile.write(
            json.dumps(response, ensure_ascii=False, sort_keys=True).encode("utf-8")
            + b"\n"
        )


class BrokerServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, policy: BrokerPolicy, *, max_request_bytes: int = 4_000_000
    ) -> None:
        socket_path = Path(policy.socket_path)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.policy = policy
        self.max_request_bytes = max_request_bytes
        self.core = _BrokerCore(policy)
        try:
            if socket_path.exists():
                socket_path.unlink()
            super().__init__(policy.socket_path, _RequestHandler)
            os.chmod(policy.socket_path, 0o600)
        except Exception:
            self.core.close()
            raise

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self.core.close()
            Path(self.policy.socket_path).unlink(missing_ok=True)


class BrokerClient:
    def __init__(
        self, socket_path: str | Path, token: str, *, timeout: float = 30.0
    ) -> None:
        self.socket_path = str(
            _portable_unix_socket_path(socket_path, create_parent=False)
        )
        self.token = token
        self.timeout = timeout

    def call(self, op: str, **payload: Any) -> dict[str, Any]:
        request = {
            "op": op,
            "token": self.token,
            "request_id": payload.pop("request_id", secrets.token_hex(8)),
            **payload,
        }
        encoded = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout)
            client.connect(self.socket_path)
            client.sendall(encoded)
            chunks = bytearray()
            while not chunks.endswith(b"\n"):
                part = client.recv(65536)
                if not part:
                    break
                chunks.extend(part)
        response = json.loads(bytes(chunks).decode("utf-8"))
        if not isinstance(response, dict):
            raise BrokerError("invalid broker response")
        return response


def serve_broker(policy: BrokerPolicy) -> None:
    with BrokerServer(policy) as server:
        server.serve_forever(poll_interval=0.2)


def build_broker_boundary_command(
    command: str,
    *,
    socket_path: str | Path,
    token_env: str = "CLAIM_PLANE_BROKER_TOKEN",
    allow_network: bool = False,
    runtime_paths: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Build a Linux Bubblewrap command with no repository or home mount."""

    if not shutil.which("bwrap"):
        raise RuntimeError("broker boundary requires Bubblewrap (bwrap)")
    requested_socket_path = Path(
        os.path.abspath(os.path.expanduser(os.fspath(socket_path)))
    )
    # ``broker-run`` must mount the socket that actually exists.  A caller may
    # provide an already-created path that is longer than Claim Plane's
    # conservative portable threshold but still valid on the current host (or
    # a test double representing that socket).  Only fall back to the
    # deterministic short path when the requested path is absent, which is the
    # normal case when BrokerServer shortened an overlong path before binding.
    socket_path = (
        requested_socket_path
        if requested_socket_path.exists()
        else _portable_unix_socket_path(socket_path, create_parent=False)
    )
    if not socket_path.exists():
        raise FileNotFoundError(f"broker socket does not exist: {socket_path}")
    roots = [
        Path(item) for item in ("/usr", "/bin", "/lib", "/lib64") if Path(item).exists()
    ]
    roots.extend(Path(item).resolve() for item in runtime_paths if Path(item).exists())
    argv: list[str] = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--share-net" if allow_network else "--unshare-net",
        "--tmpfs",
        "/",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/run",
        "--dir",
        "/run/claim-plane",
        "--ro-bind",
        str(socket_path),
        "/run/claim-plane/broker.sock",
        "--setenv",
        "CLAIM_PLANE_BROKER_SOCKET",
        "/run/claim-plane/broker.sock",
        "--setenv",
        "CLAIM_PLANE_BROKER_TOKEN_ENV",
        token_env,
    ]
    for root in dict.fromkeys(roots):
        argv.extend(("--ro-bind", str(root), str(root)))
    for name in ("PATH", token_env, "LANG", "LC_ALL"):
        value = os.environ.get(name)
        if value is not None:
            argv.extend(("--setenv", name, value))
    argv.extend(("/bin/sh", "-lc", command))
    return tuple(argv)
