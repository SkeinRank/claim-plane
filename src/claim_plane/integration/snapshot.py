"""Immutable Git snapshots used by the verified integration pipeline."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GitSnapshot:
    source_repo: str
    base_commit: str
    tree_hash: str
    snapshot_commit: str
    patch_bytes: bytes
    patch_sha256: str

    @property
    def patch_size(self) -> int:
        return len(self.patch_bytes)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def freeze_worktree(
    repo_path: str | Path,
    base_revision: str,
    *,
    message: str,
) -> GitSnapshot:
    """Capture tracked and non-ignored untracked files without changing the real index.

    A temporary Git index is seeded from ``base_revision`` and populated from the
    current worktree.  The resulting tree and synthetic commit are immutable Git
    objects.  The returned patch is generated once from those exact objects.
    """

    repo = Path(repo_path).resolve()
    _require_git_worktree(repo)
    base_commit = _git_text(
        repo, "rev-parse", "--verify", f"{base_revision}^{{commit}}"
    ).strip()

    fd, index_name = tempfile.mkstemp(prefix="claim-plane-index-")
    os.close(fd)
    index_path = Path(index_name)
    index_path.unlink(missing_ok=True)
    env = {**os.environ, "GIT_INDEX_FILE": str(index_path)}
    try:
        _git(repo, "read-tree", base_commit, env=env)
        # -A captures additions, modifications, renames and deletions.  Git's
        # normal ignore rules still apply, so caches do not enter evidence.
        _git(repo, "add", "-A", "--", ".", env=env)
        tree_hash = _git_text(repo, "write-tree", env=env).strip()
    finally:
        index_path.unlink(missing_ok=True)
        Path(f"{index_path}.lock").unlink(missing_ok=True)

    commit_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Claim Plane",
        "GIT_AUTHOR_EMAIL": "claim-plane@local",
        "GIT_COMMITTER_NAME": "Claim Plane",
        "GIT_COMMITTER_EMAIL": "claim-plane@local",
        # Deterministic metadata makes the snapshot commit reproducible for the
        # same base, tree and message.
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    }
    snapshot_commit = _git_text(
        repo,
        "commit-tree",
        tree_hash,
        "-p",
        base_commit,
        input_text=message + "\n",
        env=commit_env,
    ).strip()
    patch = _git_bytes(
        repo,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        base_commit,
        snapshot_commit,
        "--",
    )
    return GitSnapshot(
        source_repo=str(repo),
        base_commit=base_commit,
        tree_hash=tree_hash,
        snapshot_commit=snapshot_commit,
        patch_bytes=patch,
        patch_sha256=sha256_bytes(patch),
    )


def materialize_snapshot(
    repo_path: str | Path,
    snapshot_commit: str,
    *,
    parent: str | Path | None = None,
) -> Path:
    repo = Path(repo_path).resolve()
    root = Path(
        tempfile.mkdtemp(
            prefix="claim-plane-frozen-",
            dir=str(Path(parent).resolve()) if parent is not None else None,
        )
    )
    worktree = root / "worktree"
    completed = _git_process(
        repo,
        "worktree",
        "add",
        "--detach",
        str(worktree),
        snapshot_commit,
    )
    if completed.returncode != 0:
        shutil.rmtree(root, ignore_errors=True)
        raise RuntimeError(
            completed.stderr.decode("utf-8", errors="replace").strip()
            or "could not materialize frozen worker snapshot"
        )
    return worktree


def remove_materialized_snapshot(repo_path: str | Path, worktree: str | Path) -> None:
    repo = Path(repo_path).resolve()
    path = Path(worktree).resolve()
    root = path.parent
    completed = _git_process(repo, "worktree", "remove", "--force", str(path))
    if completed.returncode != 0:
        shutil.rmtree(path, ignore_errors=True)
        _git_process(repo, "worktree", "prune")
    shutil.rmtree(root, ignore_errors=True)


def capture_worktree_tree(repo_path: str | Path, *, seed: str = "HEAD") -> str:
    """Return the content tree for a worktree without mutating its real index."""

    repo = Path(repo_path).resolve()
    fd, index_name = tempfile.mkstemp(prefix="claim-plane-integrity-index-")
    os.close(fd)
    index_path = Path(index_name)
    index_path.unlink(missing_ok=True)
    env = {**os.environ, "GIT_INDEX_FILE": str(index_path)}
    try:
        _git(repo, "read-tree", seed, env=env)
        _git(repo, "add", "-A", "--", ".", env=env)
        return _git_text(repo, "write-tree", env=env).strip()
    finally:
        index_path.unlink(missing_ok=True)
        Path(f"{index_path}.lock").unlink(missing_ok=True)


def changed_worktree_paths(repo_path: str | Path) -> tuple[str, ...]:
    repo = Path(repo_path).resolve()
    output = _git_bytes(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    paths: list[str] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        text = record.decode("utf-8", errors="replace")
        value = text[3:] if len(text) >= 4 else text
        # Rename records may contain ``old -> new`` in porcelain v1.
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        paths.append(value)
    return tuple(dict.fromkeys(paths))


def write_patch(path: str | Path, patch: bytes) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(patch)
    return sha256_bytes(patch)


def create_commit_from_tree(
    repo_path: str | Path,
    tree_hash: str,
    parent_commit: str,
    *,
    message: str,
) -> str:
    repo = Path(repo_path).resolve()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Claim Plane",
        "GIT_AUTHOR_EMAIL": "claim-plane@local",
        "GIT_COMMITTER_NAME": "Claim Plane",
        "GIT_COMMITTER_EMAIL": "claim-plane@local",
    }
    return _git_text(
        repo,
        "commit-tree",
        tree_hash,
        "-p",
        parent_commit,
        input_text=message + "\n",
        env=env,
    ).strip()


def diff_objects(repo_path: str | Path, left: str, right: str) -> bytes:
    return _git_bytes(
        Path(repo_path).resolve(),
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        left,
        right,
        "--",
    )


def _require_git_worktree(repo: Path) -> None:
    completed = _git_process(repo, "rev-parse", "--is-inside-work-tree")
    if completed.returncode != 0 or completed.stdout.strip() != b"true":
        raise ValueError(f"not a Git worktree: {repo}")


def _git(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> None:
    completed = _git_process(repo, *args, env=env)
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.decode("utf-8", errors="replace").strip()
            or f"git {' '.join(args)} failed"
        )


def _git_text(
    repo: Path,
    *args: str,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    completed = _git_process(
        repo,
        *args,
        input_bytes=input_text.encode("utf-8") if input_text is not None else None,
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.decode("utf-8", errors="replace").strip()
            or f"git {' '.join(args)} failed"
        )
    return completed.stdout.decode("utf-8", errors="replace")


def _git_bytes(repo: Path, *args: str) -> bytes:
    completed = _git_process(repo, *args)
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.decode("utf-8", errors="replace").strip()
            or f"git {' '.join(args)} failed"
        )
    return completed.stdout


def _git_process(
    repo: Path,
    *args: str,
    input_bytes: bytes | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_bytes,
        capture_output=True,
        check=False,
        env=env,
    )
