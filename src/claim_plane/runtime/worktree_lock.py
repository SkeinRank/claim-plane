"""Authoritative single-host writer lock for governed Git worktrees.

The SQLite lease protects brokers that share one registry. This lock protects the
physical worktree even when independent Claim Plane processes use different local
registries. The lock namespace is derived from Git's canonical common directory;
operators cannot redirect governed brokers into independent lock directories.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import IO


class WorktreeLockError(RuntimeError):
    """Raised when the canonical worktree writer lock cannot be acquired."""


def _git_common_dir(root: Path) -> Path:
    completed = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise WorktreeLockError(
            completed.stderr.strip()
            or f"governed worktree is not a Git repository: {root}"
        )
    raw = completed.stdout.strip()
    if not raw:
        raise WorktreeLockError(f"Git returned an empty common directory for {root}")
    common = Path(raw).expanduser()
    if not common.is_absolute():
        common = root / common
    return common.resolve()


def _secure_directory(path: Path, *, trusted_parent: Path) -> Path:
    """Create private descendants without following a symlink in the namespace."""

    try:
        relative = path.relative_to(trusted_parent)
    except ValueError as exc:
        raise WorktreeLockError(
            f"worktree lock directory escapes Git common directory: {path}"
        ) from exc
    current = trusted_parent
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except OSError as exc:
                raise WorktreeLockError(
                    f"cannot create worktree lock directory: {current}"
                ) from exc
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise WorktreeLockError(
                f"worktree lock namespace must not contain a symlink: {current}"
            )
        if not stat.S_ISDIR(info.st_mode):
            raise WorktreeLockError(
                f"worktree lock namespace component is not a directory: {current}"
            )
    try:
        path.chmod(0o700)
    except PermissionError as exc:
        raise WorktreeLockError(
            f"cannot secure worktree lock directory: {path}"
        ) from exc
    return path


def canonical_worktree_lock_dir(
    root: str | Path,
    configured: str | Path | None = None,
) -> Path:
    """Return the one authoritative lock namespace for a governed worktree.

    Governed brokers store locks under Git's common directory and reject any
    configured or environment-provided alternative, preventing two processes from
    selecting different lock files for the same worktree.
    """

    resolved = Path(root).expanduser().resolve()
    canonical = _git_common_dir(resolved) / "claim-plane" / "worktree-locks"
    supplied = configured or os.environ.get("CLAIM_PLANE_LOCK_DIR")
    if supplied:
        candidate = Path(supplied).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        if candidate.resolve() != canonical.resolve():
            raise WorktreeLockError(
                "custom worktree lock directories are not allowed in governed mode; "
                f"the canonical namespace is {canonical}"
            )
    return _secure_directory(canonical, trusted_parent=_git_common_dir(resolved))


def worktree_lock_path(root: str | Path, lock_dir: str | Path | None = None) -> Path:
    resolved = Path(root).expanduser().resolve()
    common = _git_common_dir(resolved)
    digest = hashlib.sha256(os.fsencode(f"{common}\n{resolved}\n")).hexdigest()
    return canonical_worktree_lock_dir(resolved, lock_dir) / f"{digest}.lock"


def _open_lock_file(path: Path) -> IO[bytes]:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise WorktreeLockError(f"cannot open canonical worktree lock: {path}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorktreeLockError(
                f"canonical worktree lock must be one regular file: {path}"
            )
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, PermissionError):
            pass
        return os.fdopen(fd, "r+b", buffering=0)
    except Exception:
        os.close(fd)
        raise


class WorktreeWriterLock:
    """Non-blocking cross-process lock held for one broker lifetime."""

    def __init__(
        self,
        root: str | Path,
        *,
        instance_id: str,
        lock_dir: str | Path | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.instance_id = instance_id
        self.path = worktree_lock_path(self.root, lock_dir)
        self._handle: IO[bytes] | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        handle = _open_lock_file(self.path)
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows CI
                import msvcrt

                handle.seek(0)
                if self.path.stat().st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                try:
                    getattr(msvcrt, "locking")(
                        handle.fileno(), getattr(msvcrt, "LK_NBLCK"), 1
                    )
                except OSError as exc:
                    raise WorktreeLockError(
                        f"governed worktree already has a local writer: {self.root}"
                    ) from exc
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise WorktreeLockError(
                        f"governed worktree already has a local writer: {self.root}"
                    ) from exc
            payload = {
                "protocol": "claim-plane.worktree-lock.v2",
                "root": str(self.root),
                "lock_path": str(self.path),
                "instance_id": self.instance_id,
                "pid": os.getpid(),
                "acquired_at_unix": time.time(),
            }
            handle.seek(0)
            handle.truncate(0)
            handle.write(json.dumps(payload, sort_keys=True).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
            self._handle = handle
        except Exception:
            handle.close()
            raise

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows CI
                import msvcrt

                handle.seek(0)
                getattr(msvcrt, "locking")(
                    handle.fileno(), getattr(msvcrt, "LK_UNLCK"), 1
                )
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> "WorktreeWriterLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
