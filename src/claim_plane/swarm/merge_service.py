"""Git-backed deterministic swarm merge queue operations."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claim_plane.swarm.admission import SharedAdmissionStatus
from claim_plane.swarm.integration_v2 import (
    build_integration_preflight,
    verify_staged_integration,
)
from claim_plane.swarm.merge_queue import (
    DeterministicMergeQueue,
    MergeEntryState,
    MergeQueueEntry,
    compute_merge_queue,
)
from claim_plane.swarm.runs import CodexRunState
from claim_plane.swarm.service import (
    _branch_exists,
    _git_result,
    _registered_worktrees,
    _repository_identity,
    _require_initialized,
    _resolve_commit,
    _store,
    _validate_session_id,
    ensure_swarm_admission,
    resolve_repository_root,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _queue_is_fresh(
    queue: DeterministicMergeQueue, session: Any, admission: Any
) -> bool:
    return (
        queue.repository_identity == session.repository_identity
        and queue.base_commit == session.base_commit
        and queue.graph_version == session.graph_version
        and queue.graph_fingerprint == session.graph_fingerprint
        and queue.budget_version == session.budget_version
        and queue.budget_fingerprint == session.budget_fingerprint
        and queue.admission_fingerprint == admission.fingerprint()
    )


def _integration_head(
    root: Path, queue: DeterministicMergeQueue | None, base: str
) -> str:
    if queue is None:
        return base
    branch_result = _git_result(
        root, "rev-parse", "--verify", f"{queue.integration_branch}^{{commit}}"
    )
    if branch_result.returncode != 0:
        if queue.integration_head != base:
            raise ValueError("stored integration branch is missing")
        return base
    return branch_result.stdout.strip().lower()


def plan_swarm_merge_queue(repo: str | Path, session_id: str) -> dict[str, Any]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    ensure_swarm_admission(root, session_id)
    with _store(root) as store:
        session = store.require(session_id)
        shared = store.get_shared_admission(session_id)
        runs = store.list_codex_runs(session_id)
        recovery_events = store.list_recovery_events(session_id)
        worktrees = store.list_worktrees(session_id)
        previous_data = store.get_merge_queue(session_id)
    if session.repository_identity != _repository_identity(root):
        raise ValueError("swarm session is bound to a different repository identity")
    if _resolve_commit(root, session.base_commit) != session.base_commit:
        raise ValueError("swarm session base commit is no longer resolvable")
    if shared is None:
        raise ValueError("swarm session has no shared admission")
    admission, _ = shared
    if admission.status is not SharedAdmissionStatus.READY:
        raise ValueError("shared admission requires replanning")
    previous = None if previous_data is None else previous_data[0]
    if previous is not None and not _queue_is_fresh(previous, session, admission):
        previous = None
    head = _integration_head(root, previous, session.base_commit)
    now = _utc_now()
    from claim_plane.swarm.rescue import effective_runs_for_rescue

    queue = compute_merge_queue(
        session,
        admission,
        effective_runs_for_rescue(runs, recovery_events),
        worktrees,
        root=root,
        integration_head=head,
        now=now,
        previous=previous,
    )
    with _store(root) as store:
        stored, version, changed = store.save_merge_queue(
            session_id,
            queue,
            expected_admission_fingerprint=admission.fingerprint(),
        )
    return {
        "session_id": session_id,
        "created": changed,
        "queue_version": version,
        "queue_fingerprint": stored.fingerprint(),
        "merge_queue": stored.to_dict(),
        "summary": stored.summary(),
    }


def get_swarm_merge_queue(
    repo: str | Path, session_id: str, *, refresh: bool = True
) -> dict[str, Any]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    if refresh:
        return plan_swarm_merge_queue(root, session_id)
    with _store(root) as store:
        session = store.require(session_id)
        stored = store.get_merge_queue(session_id)
        shared = store.get_shared_admission(session_id)
    if session.repository_identity != _repository_identity(root):
        raise ValueError("swarm session is bound to a different repository identity")
    if stored is None:
        raise KeyError(
            f"swarm session {session_id!r} has no merge queue; "
            "run 'claim-plane swarm merge-plan' first"
        )
    if shared is None or not _queue_is_fresh(stored[0], session, shared[0]):
        raise ValueError("stored merge queue is stale")
    queue, version = stored
    return {
        "session_id": session_id,
        "created": False,
        "queue_version": version,
        "queue_fingerprint": queue.fingerprint(),
        "merge_queue": queue.to_dict(),
        "summary": queue.summary(),
    }


def _ensure_integration_worktree(root: Path, queue: DeterministicMergeQueue) -> Path:
    path = Path(queue.integration_worktree_path).resolve()
    registered = _registered_worktrees(root)
    git_record = registered.get(path)
    if git_record is not None:
        actual_branch = git_record.get("branch", "").removeprefix("refs/heads/")
        if actual_branch != queue.integration_branch:
            raise ValueError(
                "integration worktree branch does not match queue ownership"
            )
    else:
        if path.exists():
            raise ValueError(
                f"refusing to overwrite unregistered integration path: {path}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        if _branch_exists(root, queue.integration_branch):
            result = _git_result(
                root, "worktree", "add", str(path), queue.integration_branch
            )
        else:
            result = _git_result(
                root,
                "worktree",
                "add",
                "-b",
                queue.integration_branch,
                str(path),
                queue.base_commit,
            )
        if result.returncode != 0:
            raise ValueError(
                result.stderr.strip()
                or result.stdout.strip()
                or "failed to create integration worktree"
            )
    status = _git_result(path, "status", "--porcelain=v1", "--untracked-files=all")
    if status.returncode != 0:
        raise ValueError(status.stderr.strip() or "cannot inspect integration worktree")
    if status.stdout.strip():
        raise ValueError("integration worktree is dirty")
    head = _resolve_commit(path, "HEAD")
    if head != queue.integration_head:
        raise ValueError(
            "integration worktree HEAD differs from the durable merge-queue head"
        )
    return path


def _git_env(timestamp: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Claim Plane",
            "GIT_AUTHOR_EMAIL": "claim-plane@example.invalid",
            "GIT_COMMITTER_NAME": "Claim Plane",
            "GIT_COMMITTER_EMAIL": "claim-plane@example.invalid",
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }
    )
    return env


def _meaningful_status(path: Path) -> list[str]:
    result = _git_result(path, "status", "--porcelain=v1", "--untracked-files=all")
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "cannot inspect worker worktree")
    ignored = {".codex/hooks.json"}
    lines: list[str] = []
    for line in result.stdout.splitlines():
        candidate = line[3:].strip() if len(line) >= 4 else line.strip()
        if candidate in ignored or candidate.startswith(".claim-plane/"):
            continue
        lines.append(line)
    return lines


def _snapshot_worker(
    root: Path,
    queue: DeterministicMergeQueue,
    entry: MergeQueueEntry,
    *,
    timestamp: str,
) -> str | None:
    with _store(root) as store:
        records = store.list_worktrees(queue.session_id)
        runs = store.list_codex_runs(queue.session_id, work_id=entry.work_id)
    worktree = next((item for item in records if item.work_id == entry.work_id), None)
    if worktree is None:
        raise ValueError(f"managed worktree {entry.work_id!r} is missing")
    latest = max(runs, key=lambda item: (item.attempt, item.created_at), default=None)
    if (
        latest is None
        or latest.run_id != entry.run_id
        or latest.state is not CodexRunState.SUCCEEDED
    ):
        raise ValueError("merge entry no longer points at the latest successful run")
    source = Path(worktree.worktree_path).resolve()
    head = _resolve_commit(source, "HEAD")
    if head != latest.base_commit:
        raise ValueError(
            "worker branch HEAD differs from the execution base recorded by the "
            "successful run; deterministic snapshotting refuses unexpected commits"
        )
    status = _meaningful_status(source)
    if not status:
        return None
    add_tracked = subprocess.run(
        ["git", "add", "-u", "--", "."],
        cwd=source,
        text=True,
        capture_output=True,
        check=False,
    )
    if add_tracked.returncode != 0:
        raise ValueError(
            add_tracked.stderr.strip()
            or add_tracked.stdout.strip()
            or "failed to stage tracked worker changes"
        )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=source,
        capture_output=True,
        check=False,
    )
    if untracked.returncode != 0:
        raise ValueError("failed to enumerate untracked worker files")
    paths = [
        value.decode("utf-8", errors="strict")
        for value in untracked.stdout.split(b"\0")
        if value
        and value != b".codex/hooks.json"
        and not value.startswith(b".claim-plane/")
    ]
    if paths:
        add_untracked = subprocess.run(
            ["git", "add", "--", *paths],
            cwd=source,
            text=True,
            capture_output=True,
            check=False,
        )
        if add_untracked.returncode != 0:
            raise ValueError(
                add_untracked.stderr.strip()
                or add_untracked.stdout.strip()
                or "failed to stage untracked worker files"
            )
    staged = _git_result(source, "diff", "--cached", "--quiet")
    if staged.returncode == 0:
        return None
    if staged.returncode != 1:
        raise ValueError(
            staged.stderr.strip() or "cannot inspect staged worker snapshot"
        )
    commit = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--no-gpg-sign",
            "-m",
            f"claim-plane swarm {queue.session_id}: {entry.work_id}",
        ],
        cwd=source,
        text=True,
        capture_output=True,
        check=False,
        env=_git_env(timestamp),
    )
    if commit.returncode != 0:
        _git_result(source, "reset")
        raise ValueError(
            commit.stderr.strip()
            or commit.stdout.strip()
            or "failed to commit worker snapshot"
        )
    return _resolve_commit(source, "HEAD")


def _apply_snapshot(
    integration: Path,
    queue: DeterministicMergeQueue,
    source_commit: str,
) -> tuple[str, ...]:
    cherry = subprocess.run(
        ["git", "cherry-pick", "--no-commit", source_commit],
        cwd=integration,
        text=True,
        capture_output=True,
        check=False,
    )
    if cherry.returncode == 0:
        return ()
    conflicts = _git_result(integration, "diff", "--name-only", "--diff-filter=U")
    paths = tuple(sorted(line for line in conflicts.stdout.splitlines() if line))
    _git_result(integration, "cherry-pick", "--abort")
    _git_result(integration, "reset", "--hard", queue.integration_head)
    return paths or ("<unknown>",)


def _commit_applied_snapshot(
    integration: Path,
    queue: DeterministicMergeQueue,
    entry: MergeQueueEntry,
    *,
    timestamp: str,
) -> str:
    commit = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--no-gpg-sign",
            "-m",
            f"claim-plane integrate {queue.session_id}: {entry.work_id}",
        ],
        cwd=integration,
        text=True,
        capture_output=True,
        check=False,
        env=_git_env(timestamp),
    )
    if commit.returncode != 0:
        _git_result(integration, "reset", "--hard", queue.integration_head)
        raise ValueError(
            commit.stderr.strip()
            or commit.stdout.strip()
            or "failed to commit integration result"
        )
    return _resolve_commit(integration, "HEAD")


def integrate_next_swarm_result(repo: str | Path, session_id: str) -> dict[str, Any]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    planned = plan_swarm_merge_queue(root, session_id)
    queue = DeterministicMergeQueue.from_dict(planned["merge_queue"])
    ready = next(
        (entry for entry in queue.entries if entry.state is MergeEntryState.READY),
        None,
    )
    if ready is None:
        raise ValueError("deterministic merge queue has no ready work item")
    now = _utc_now()
    with _store(root) as store:
        claimed_queue, version, claimed = store.claim_merge_entry(
            session_id,
            ready.work_id,
            expected_queue_fingerprint=queue.fingerprint(),
            updated_at=now,
        )
        session = store.require(session_id)
        shared = store.get_shared_admission(session_id)
    if shared is None:
        raise ValueError("swarm session has no shared admission")
    admission, _ = shared
    integrated_entries = tuple(
        entry
        for entry in claimed_queue.entries
        if entry.state is MergeEntryState.INTEGRATED
    )
    integration = _ensure_integration_worktree(root, claimed_queue)
    evidence = None
    source_commit = claimed.source_commit
    try:
        if source_commit is None:
            source_commit = _snapshot_worker(
                root, claimed_queue, claimed, timestamp=now
            )
        if source_commit is None:
            finished = MergeQueueEntry(
                work_id=claimed.work_id,
                order=claimed.order,
                effective_dependencies=claimed.effective_dependencies,
                source_branch=claimed.source_branch,
                state=MergeEntryState.INTEGRATED,
                run_id=claimed.run_id,
                source_commit=None,
                integration_commit=claimed_queue.integration_head,
                detail="successful no-op execution recorded without a merge commit",
            )
        else:
            evidence = build_integration_preflight(
                integration,
                session=session,
                admission=admission,
                work_id=claimed.work_id,
                source_commit=source_commit,
                integration_head=claimed_queue.integration_head,
                integrated_entries=integrated_entries,
            )
            if not evidence.allowed:
                finished = MergeQueueEntry(
                    work_id=claimed.work_id,
                    order=claimed.order,
                    effective_dependencies=claimed.effective_dependencies,
                    source_branch=claimed.source_branch,
                    state=MergeEntryState.CONFLICT,
                    run_id=claimed.run_id,
                    source_commit=source_commit,
                    conflict_paths=tuple(
                        sorted(
                            {
                                str(item.get("path") or "<semantic-authority>")
                                for item in evidence.authority_violations
                            }
                        )
                    )
                    or ("<semantic-authority>",),
                    integration_evidence=evidence.to_dict(),
                    detail="actual worker diff failed deterministic integration preflight",
                )
            else:
                conflicts = _apply_snapshot(integration, claimed_queue, source_commit)
                if conflicts:
                    finished = MergeQueueEntry(
                        work_id=claimed.work_id,
                        order=claimed.order,
                        effective_dependencies=claimed.effective_dependencies,
                        source_branch=claimed.source_branch,
                        state=MergeEntryState.CONFLICT,
                        run_id=claimed.run_id,
                        source_commit=source_commit,
                        conflict_paths=conflicts,
                        integration_evidence=evidence.to_dict(),
                        detail=(
                            "integration conflict; integration worktree restored to prior head"
                        ),
                    )
                else:
                    evidence = verify_staged_integration(
                        integration,
                        item=session.work_graph.item_map[claimed.work_id],
                        admission=admission,
                        integrated_entries=integrated_entries,
                        evidence=evidence,
                    )
                    if not evidence.allowed:
                        _git_result(integration, "cherry-pick", "--abort")
                        _git_result(
                            integration,
                            "reset",
                            "--hard",
                            claimed_queue.integration_head,
                        )
                        finished = MergeQueueEntry(
                            work_id=claimed.work_id,
                            order=claimed.order,
                            effective_dependencies=claimed.effective_dependencies,
                            source_branch=claimed.source_branch,
                            state=MergeEntryState.CONFLICT,
                            run_id=claimed.run_id,
                            source_commit=source_commit,
                            conflict_paths=tuple(
                                sorted(
                                    {
                                        str(item.get("path") or "<semantic-recheck>")
                                        for item in evidence.authority_violations
                                    }
                                )
                            )
                            or ("<semantic-recheck>",),
                            integration_evidence=evidence.to_dict(),
                            detail=(
                                "post-apply semantic recheck rejected the integration result"
                            ),
                        )
                    else:
                        integration_head = _commit_applied_snapshot(
                            integration, claimed_queue, claimed, timestamp=now
                        )
                        finished = MergeQueueEntry(
                            work_id=claimed.work_id,
                            order=claimed.order,
                            effective_dependencies=claimed.effective_dependencies,
                            source_branch=claimed.source_branch,
                            state=MergeEntryState.INTEGRATED,
                            run_id=claimed.run_id,
                            source_commit=source_commit,
                            integration_commit=integration_head,
                            integration_evidence=evidence.to_dict(),
                            detail="worker snapshot integrated after deterministic semantic recheck",
                        )
    except Exception:
        _git_result(integration, "cherry-pick", "--abort")
        _git_result(integration, "reset", "--hard", claimed_queue.integration_head)
        failed = MergeQueueEntry(
            work_id=claimed.work_id,
            order=claimed.order,
            effective_dependencies=claimed.effective_dependencies,
            source_branch=claimed.source_branch,
            state=MergeEntryState.CONFLICT,
            run_id=claimed.run_id,
            source_commit=source_commit,
            integration_evidence=None if evidence is None else evidence.to_dict(),
            detail="integration failed before a durable result was produced",
            conflict_paths=("<integration-error>",),
        )
        with _store(root) as store:
            store.finish_merge_entry(
                session_id,
                failed,
                integration_head=claimed_queue.integration_head,
                updated_at=_utc_now(),
            )
        raise
    with _store(root) as store:
        stored, final_version = store.finish_merge_entry(
            session_id,
            finished,
            integration_head=(
                claimed_queue.integration_head
                if finished.state is MergeEntryState.CONFLICT
                else finished.integration_commit or claimed_queue.integration_head
            ),
            updated_at=_utc_now(),
        )
    refreshed = plan_swarm_merge_queue(root, session_id)
    return {
        "session_id": session_id,
        "queue_version": max(version, final_version, int(refreshed["queue_version"])),
        "integrated": finished.state is MergeEntryState.INTEGRATED,
        "entry": finished.to_dict(),
        "merge_queue": refreshed["merge_queue"],
        "summary": refreshed["summary"],
    }


def drain_swarm_merge_queue(repo: str | Path, session_id: str) -> dict[str, Any]:
    integrated: list[dict[str, Any]] = []
    while True:
        current = plan_swarm_merge_queue(repo, session_id)
        if current["summary"]["status"] in {"completed", "conflict", "waiting"}:
            return {
                "session_id": session_id,
                "integrated": integrated,
                "merge_queue": current["merge_queue"],
                "summary": current["summary"],
            }
        result = integrate_next_swarm_result(repo, session_id)
        integrated.append(result["entry"])
        if not result["integrated"]:
            return {
                "session_id": session_id,
                "integrated": integrated,
                "merge_queue": result["merge_queue"],
                "summary": result["summary"],
            }
