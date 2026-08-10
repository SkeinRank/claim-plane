"""Repository-bound swarm-session planning services."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
import tokenize
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from claim_plane.core import (
    PythonStructuralExtractionError,
    build_python_dependency_graph,
)
from claim_plane.swarm.admission import (
    SharedAdmissionStatus,
    compute_shared_admission,
)
from claim_plane.swarm.budget import SwarmBudgetPolicy
from claim_plane.swarm.concurrency import (
    ConcurrencyPlanStatus,
    compute_concurrency_plan,
)
from claim_plane.swarm.merge_queue import MergeEntryState
from claim_plane.swarm.models import (
    SWARM_SESSION_SPEC_PROTOCOL,
    IntegrationTarget,
    RootTask,
    SwarmSession,
    SwarmSessionState,
    WorkGraph,
)
from claim_plane.swarm.scheduler import compute_scheduler_snapshot
from claim_plane.swarm.store import SwarmSessionStore
from claim_plane.swarm.worktrees import (
    ManagedWorktree,
    WorktreeHealth,
    WorktreeInspection,
    managed_branch_name,
    managed_session_component,
    managed_worktree_path,
)

_SWARM_DB = Path(".claim-plane/swarm.db")
_PROJECT_STATE = Path(".claim-plane/project.json")
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")


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
        raise ValueError(
            completed.stderr.strip() or completed.stdout.strip() or "git failed"
        )
    return completed.stdout.strip()


def _git_bytes(root_or_child: str | Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=Path(root_or_child).resolve(),
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        stdout = completed.stdout.decode("utf-8", errors="replace").strip()
        raise ValueError(stderr or stdout or "git failed")
    return completed.stdout


def _python_sources_at_revision(root: Path, revision: str) -> dict[str, str]:
    """Read tracked Python sources from one pinned Git revision without checkout."""

    listing = _git_bytes(root, "ls-tree", "-r", "-z", "--name-only", revision)
    paths = [
        raw.decode("utf-8", errors="surrogateescape")
        for raw in listing.split(b"\0")
        if raw
    ]
    sources: dict[str, str] = {}
    excluded = {".git", ".claim-plane", ".codex", ".venv", "venv", "node_modules"}
    for path in paths:
        parts = tuple(part for part in path.replace("\\", "/").split("/") if part)
        if not parts or any(part in excluded for part in parts):
            continue
        if not path.endswith((".py", ".pyi")):
            continue
        raw = _git_bytes(root, "show", f"{revision}:{path}")
        try:
            encoding, _ = tokenize.detect_encoding(BytesIO(raw).readline)
            sources[path] = raw.decode(encoding)
        except (SyntaxError, UnicodeError) as exc:
            raise PythonStructuralExtractionError(
                f"cannot decode pinned Python source: {exc}", path=path
            ) from exc
    return sources


def _semantic_graph_for_revision(root: Path, revision: str):
    sources = _python_sources_at_revision(root, revision)
    if not sources:
        return None
    try:
        return build_python_dependency_graph(sources)
    except PythonStructuralExtractionError:
        return None


def resolve_repository_root(root_or_child: str | Path = ".") -> Path:
    return Path(_git(root_or_child, "rev-parse", "--show-toplevel")).resolve()


def _require_initialized(root: Path) -> None:
    state_path = root / _PROJECT_STATE
    if not state_path.is_file():
        raise ValueError("project is not initialized; run 'claim-plane init' first")
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("protocol") != "claim-plane.project.v1"
    ):
        raise ValueError(f"unsupported Claim Plane project state in {state_path}")


def _repository_identity(root: Path) -> str:
    common_raw = _git(root, "rev-parse", "--git-common-dir")
    common = Path(common_raw)
    if not common.is_absolute():
        common = (root / common).resolve()
    return hashlib.sha256(f"{common}\n{root}\n".encode("utf-8")).hexdigest()


def _resolve_commit(root: Path, revision: str) -> str:
    commit = _git(root, "rev-parse", "--verify", f"{revision}^{{commit}}").lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ValueError("Git revision did not resolve to a full object id")
    return commit


def _current_branch(root: Path) -> str:
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return branch or "HEAD"


def _validate_session_id(value: str) -> str:
    session_id = value.strip()
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(
            "session_id must start with an alphanumeric character and contain only "
            "letters, digits, '.', '_', or '-' (maximum 96 characters)"
        )
    return session_id


def _store(root: Path) -> SwarmSessionStore:
    return SwarmSessionStore(root / _SWARM_DB)


def parse_session_spec(
    data: Mapping[str, Any],
) -> tuple[
    RootTask,
    IntegrationTarget | None,
    WorkGraph,
    SwarmBudgetPolicy,
    dict[str, Any],
]:
    protocol = str(data.get("protocol") or SWARM_SESSION_SPEC_PROTOCOL)
    if protocol != SWARM_SESSION_SPEC_PROTOCOL:
        raise ValueError(f"unsupported swarm-session spec protocol {protocol!r}")
    root_task_raw = data.get("root_task")
    graph_raw = data.get("work_graph")
    if not isinstance(root_task_raw, Mapping):
        raise ValueError("root_task must be an object")
    if not isinstance(graph_raw, Mapping):
        raise ValueError("work_graph must be an object")
    target_raw = data.get("integration_target")
    target = None
    if target_raw is not None:
        if not isinstance(target_raw, Mapping):
            raise ValueError("integration_target must be an object")
        target = IntegrationTarget.from_dict(target_raw)
    budget_raw = data.get("budget_policy")
    if budget_raw is not None and not isinstance(budget_raw, Mapping):
        raise ValueError("budget_policy must be an object")
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be an object")
    graph = WorkGraph.from_dict(graph_raw)
    budget = SwarmBudgetPolicy.from_dict(budget_raw)
    budget.validate_work_item_count(len(graph.work_items))
    return (
        RootTask.from_dict(root_task_raw),
        target,
        graph,
        budget,
        dict(metadata),
    )


def create_swarm_session(
    repo: str | Path,
    *,
    spec: Mapping[str, Any],
    session_id: str | None = None,
    base_revision: str = "HEAD",
) -> dict[str, Any]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    root_task, target, graph, budget, metadata = parse_session_spec(spec)
    base_commit = _resolve_commit(root, base_revision)
    base_branch = _current_branch(root)
    if target is None:
        target = IntegrationTarget(branch=base_branch)
    session_id = _validate_session_id(session_id or f"swm-{secrets.token_hex(12)}")
    now = _utc_now()
    session = SwarmSession(
        session_id=session_id,
        repository_root=str(root),
        repository_identity=_repository_identity(root),
        base_commit=base_commit,
        base_branch=base_branch,
        root_task=root_task,
        integration_target=target,
        work_graph=graph,
        budget_policy=budget,
        graph_version=1,
        budget_version=1,
        state=SwarmSessionState.PLANNED,
        created_at=now,
        updated_at=now,
        metadata=metadata,
    )
    with _store(root) as store:
        stored, created = store.create(session)
    return {
        "created": created,
        "session": stored.to_dict(),
        "graph": stored.work_graph.summary(),
        "budget": stored.budget_policy.summary(
            work_items=len(stored.work_graph.work_items)
        ),
    }


def get_swarm_session(repo: str | Path, session_id: str) -> SwarmSession:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    with _store(root) as store:
        session = store.require(_validate_session_id(session_id))
    if session.repository_identity != _repository_identity(root):
        raise ValueError("swarm session is bound to a different repository identity")
    return session


def list_swarm_sessions(repo: str | Path) -> list[SwarmSession]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    identity = _repository_identity(root)
    with _store(root) as store:
        sessions = store.list()
    return [session for session in sessions if session.repository_identity == identity]


def replace_swarm_work_graph(
    repo: str | Path,
    session_id: str,
    *,
    graph_data: Mapping[str, Any],
    expected_version: int,
) -> SwarmSession:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    graph = WorkGraph.from_dict(graph_data)
    with _store(root) as store:
        current = store.require(_validate_session_id(session_id))
        if current.repository_identity != _repository_identity(root):
            raise ValueError(
                "swarm session is bound to a different repository identity"
            )
        if _resolve_commit(root, current.base_commit) != current.base_commit:
            raise ValueError("swarm session base commit is no longer resolvable")
        return store.replace_graph(
            current.session_id,
            graph,
            expected_version=expected_version,
            updated_at=_utc_now(),
        )


def validate_work_graph(graph_data: Mapping[str, Any]) -> dict[str, Any]:
    if graph_data.get("protocol") == SWARM_SESSION_SPEC_PROTOCOL:
        nested = graph_data.get("work_graph")
        if not isinstance(nested, Mapping):
            raise ValueError("work_graph must be an object")
        graph_data = nested
    graph = WorkGraph.from_dict(graph_data)
    return graph.summary()


def replace_swarm_budget_policy(
    repo: str | Path,
    session_id: str,
    *,
    policy_data: Mapping[str, Any],
    expected_version: int,
) -> SwarmSession:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    policy = SwarmBudgetPolicy.from_dict(policy_data)
    with _store(root) as store:
        current = store.require(_validate_session_id(session_id))
        if current.repository_identity != _repository_identity(root):
            raise ValueError(
                "swarm session is bound to a different repository identity"
            )
        if _resolve_commit(root, current.base_commit) != current.base_commit:
            raise ValueError("swarm session base commit is no longer resolvable")
        policy.validate_work_item_count(len(current.work_graph.work_items))
        return store.replace_budget_policy(
            current.session_id,
            policy,
            expected_version=expected_version,
            updated_at=_utc_now(),
        )


def validate_budget_policy(
    policy_data: Mapping[str, Any], *, work_items: int | None = None
) -> dict[str, Any]:
    policy = SwarmBudgetPolicy.from_dict(policy_data)
    return {
        "policy": policy.to_dict(),
        "summary": policy.summary(work_items=work_items),
    }


def plan_swarm_concurrency(repo: str | Path, session_id: str) -> dict[str, Any]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    with _store(root) as store:
        current = store.require(_validate_session_id(session_id))
        if current.repository_identity != _repository_identity(root):
            raise ValueError(
                "swarm session is bound to a different repository identity"
            )
        if _resolve_commit(root, current.base_commit) != current.base_commit:
            raise ValueError("swarm session base commit is no longer resolvable")
        semantic_graph = _semantic_graph_for_revision(root, current.base_commit)
        plan = compute_concurrency_plan(
            current.work_graph,
            current.budget_policy,
            graph_version=current.graph_version,
            budget_version=current.budget_version,
            semantic_graph=semantic_graph,
        )
        stored, plan_version, changed = store.save_concurrency_plan(
            current.session_id,
            plan,
            expected_graph_version=current.graph_version,
            expected_budget_version=current.budget_version,
            created_at=_utc_now(),
        )
    return {
        "session_id": current.session_id,
        "created": changed,
        "plan_version": plan_version,
        "plan_fingerprint": stored.fingerprint(),
        "concurrency_plan": stored.to_dict(),
        "summary": stored.summary(),
    }


def get_swarm_concurrency_plan(repo: str | Path, session_id: str) -> dict[str, Any]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    with _store(root) as store:
        current = store.require(_validate_session_id(session_id))
        if current.repository_identity != _repository_identity(root):
            raise ValueError(
                "swarm session is bound to a different repository identity"
            )
        stored = store.get_concurrency_plan(current.session_id)
    if stored is None:
        raise KeyError(
            f"swarm session {current.session_id!r} has no concurrency plan; "
            "run 'claim-plane swarm plan' first"
        )
    plan, plan_version = stored
    if (
        plan.graph_version != current.graph_version
        or plan.graph_fingerprint != current.graph_fingerprint
        or plan.budget_version != current.budget_version
        or plan.budget_fingerprint != current.budget_fingerprint
    ):
        raise ValueError("stored concurrency plan is stale")
    return {
        "session_id": current.session_id,
        "plan_version": plan_version,
        "plan_fingerprint": plan.fingerprint(),
        "concurrency_plan": plan.to_dict(),
        "summary": plan.summary(),
    }


def validate_concurrency_plan(
    graph_data: Mapping[str, Any],
    policy_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    graph = WorkGraph.from_dict(graph_data)
    policy = SwarmBudgetPolicy.from_dict(policy_data)
    plan = compute_concurrency_plan(graph, policy)
    return {
        "valid": True,
        "concurrency_plan": plan.to_dict(),
        "summary": plan.summary(),
    }


def admit_swarm_session(repo: str | Path, session_id: str) -> dict[str, Any]:
    """Compute and persist shared admission for every concurrent candidate."""

    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    identity = _repository_identity(root)
    with _store(root) as store:
        session = store.require(session_id)
        stored_plan = store.get_concurrency_plan(session_id)
        if session.repository_identity != identity:
            raise ValueError(
                "swarm session is bound to a different repository identity"
            )
        if stored_plan is None:
            raise ValueError(
                "swarm session has no concurrency plan; run "
                "'claim-plane swarm plan' first"
            )
        concurrency, _ = stored_plan
        admission = compute_shared_admission(session, concurrency)
        stored, admission_version, changed = store.save_shared_admission(
            session_id,
            admission,
            expected_graph_version=session.graph_version,
            expected_budget_version=session.budget_version,
            expected_concurrency_plan_fingerprint=concurrency.fingerprint(),
            created_at=_utc_now(),
        )
    return {
        "session_id": session_id,
        "created": changed,
        "admission_version": admission_version,
        "admission_fingerprint": stored.fingerprint(),
        "shared_admission": stored.to_dict(),
        "summary": stored.summary(),
    }


def get_swarm_admission(repo: str | Path, session_id: str) -> dict[str, Any]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    with _store(root) as store:
        session = store.require(session_id)
        stored = store.get_shared_admission(session_id)
        concurrency = store.get_concurrency_plan(session_id)
    if session.repository_identity != _repository_identity(root):
        raise ValueError("swarm session is bound to a different repository identity")
    if stored is None:
        raise KeyError(
            f"swarm session {session_id!r} has no shared admission; "
            "run 'claim-plane swarm admit' first"
        )
    if concurrency is None:
        raise ValueError("shared admission exists without a concurrency plan")
    admission, version = stored
    plan, _ = concurrency
    if (
        admission.graph_version != session.graph_version
        or admission.graph_fingerprint != session.graph_fingerprint
        or admission.budget_version != session.budget_version
        or admission.budget_fingerprint != session.budget_fingerprint
        or admission.concurrency_plan_fingerprint != plan.fingerprint()
    ):
        raise ValueError("stored shared admission is stale")
    return {
        "session_id": session_id,
        "admission_version": version,
        "admission_fingerprint": admission.fingerprint(),
        "shared_admission": admission.to_dict(),
        "summary": admission.summary(),
    }


def get_swarm_scheduler(repo: str | Path, session_id: str) -> dict[str, Any]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    with _store(root) as store:
        session = store.require(session_id)
        stored = store.get_shared_admission(session_id)
        records = store.list_codex_runs(session_id)
        recovery_events = store.list_recovery_events(session_id)
        merge_queue = store.get_merge_queue(session_id)
    if session.repository_identity != _repository_identity(root):
        raise ValueError("swarm session is bound to a different repository identity")
    if stored is None:
        raise KeyError(
            f"swarm session {session_id!r} has no shared admission; "
            "run 'claim-plane swarm admit' first"
        )
    admission, admission_version = stored
    integrated = (
        None
        if merge_queue is None
        else {
            entry.work_id
            for entry in merge_queue[0].entries
            if entry.state is MergeEntryState.INTEGRATED
        }
    )
    from claim_plane.swarm.rescue import effective_runs_for_rescue

    snapshot = compute_scheduler_snapshot(
        session,
        admission,
        effective_runs_for_rescue(records, recovery_events),
        integrated_work_ids=integrated,
    )
    return {
        "session_id": session_id,
        "admission_version": admission_version,
        "admission_fingerprint": admission.fingerprint(),
        "scheduler": snapshot.to_dict(),
        "summary": snapshot.summary(),
    }


def ensure_swarm_admission(repo: str | Path, session_id: str) -> dict[str, Any]:
    """Return a fresh admission, creating one for older execution workflows."""

    try:
        return get_swarm_admission(repo, session_id)
    except KeyError:
        result = admit_swarm_session(repo, session_id)
        if result["summary"]["status"] != SharedAdmissionStatus.READY.value:
            raise ValueError("shared admission requires replanning")
        return result


def _git_result(
    root_or_child: str | Path, *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=Path(root_or_child).resolve(),
        text=True,
        capture_output=True,
        check=False,
    )


def _branch_exists(root: Path, branch: str) -> bool:
    return (
        _git_result(
            root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
        ).returncode
        == 0
    )


def _registered_worktrees(root: Path) -> dict[Path, dict[str, str]]:
    output = _git(root, "worktree", "list", "--porcelain")
    records: dict[Path, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in [*output.splitlines(), ""]:
        if not line:
            path_raw = current.get("worktree")
            if path_raw:
                records[Path(path_raw).resolve()] = dict(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return records


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    return (
        _git_result(
            root, "merge-base", "--is-ancestor", ancestor, descendant
        ).returncode
        == 0
    )


def _worktree_dirty(path: Path) -> bool:
    result = _git_result(path, "status", "--porcelain=v1", "--untracked-files=all")
    if result.returncode != 0:
        raise ValueError(
            result.stderr.strip() or result.stdout.strip() or "cannot inspect worktree"
        )
    runner_control_paths = {".codex/hooks.json"}
    meaningful: list[str] = []
    for line in result.stdout.splitlines():
        candidate = line[3:].strip() if len(line) >= 4 else line.strip()
        if candidate in runner_control_paths:
            continue
        meaningful.append(line)
    return bool(meaningful)


def _session_worktree_root(root: Path, session_id: str) -> Path:
    return (
        root / ".claim-plane" / "worktrees" / managed_session_component(session_id)
    ).resolve()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _expected_worktree_record(
    root: Path, session: SwarmSession, work_id: str, *, now: str
) -> ManagedWorktree:
    return ManagedWorktree(
        session_id=session.session_id,
        work_id=work_id,
        repository_identity=session.repository_identity,
        graph_version=session.graph_version,
        graph_fingerprint=session.graph_fingerprint,
        base_commit=session.base_commit,
        branch=managed_branch_name(session.session_id, work_id),
        worktree_path=str(managed_worktree_path(root, session.session_id, work_id)),
        created_at=now,
        updated_at=now,
    )


def _inspect_record(
    root: Path,
    session: SwarmSession,
    record: ManagedWorktree,
    registered: Mapping[Path, Mapping[str, str]],
) -> WorktreeInspection:
    path = Path(record.worktree_path).resolve()
    exists = path.exists()
    git_record = registered.get(path)
    if (
        record.graph_version != session.graph_version
        or record.graph_fingerprint != session.graph_fingerprint
    ):
        return WorktreeInspection(
            record=record,
            health=WorktreeHealth.STALE_GRAPH,
            exists=exists,
            registered=git_record is not None,
            dirty=False,
            head_commit=(None if git_record is None else git_record.get("HEAD")),
            actual_branch=(
                None
                if git_record is None
                else git_record.get("branch", "").removeprefix("refs/heads/") or None
            ),
            detail="worktree was provisioned for an older work-graph version",
        )
    if git_record is None:
        return WorktreeInspection(
            record=record,
            health=(WorktreeHealth.UNREGISTERED if exists else WorktreeHealth.MISSING),
            exists=exists,
            registered=False,
            dirty=False,
            head_commit=None,
            actual_branch=None,
            detail=(
                "directory exists but Git does not register it as a linked worktree"
                if exists
                else "managed worktree directory is missing"
            ),
        )
    actual_branch = git_record.get("branch", "").removeprefix("refs/heads/") or None
    head = git_record.get("HEAD") or None
    if actual_branch != record.branch:
        return WorktreeInspection(
            record=record,
            health=WorktreeHealth.BRANCH_MISMATCH,
            exists=exists,
            registered=True,
            dirty=False,
            head_commit=head,
            actual_branch=actual_branch,
            detail="registered worktree branch differs from the owned branch",
        )
    if head is None or not _is_ancestor(root, record.base_commit, head):
        return WorktreeInspection(
            record=record,
            health=WorktreeHealth.BASE_MISMATCH,
            exists=exists,
            registered=True,
            dirty=False,
            head_commit=head,
            actual_branch=actual_branch,
            detail="worktree HEAD no longer descends from the pinned session base",
        )
    dirty = _worktree_dirty(path)
    return WorktreeInspection(
        record=record,
        health=WorktreeHealth.DIRTY if dirty else WorktreeHealth.READY,
        exists=exists,
        registered=True,
        dirty=dirty,
        head_commit=head,
        actual_branch=actual_branch,
        detail="worktree has uncommitted changes" if dirty else "worktree is ready",
    )


def inspect_swarm_worktrees(repo: str | Path, session_id: str) -> dict[str, Any]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    with _store(root) as store:
        session = store.require(session_id)
        records = store.list_worktrees(session_id)
        merge_queue = store.get_merge_queue(session_id)
    if session.repository_identity != _repository_identity(root):
        raise ValueError("swarm session is bound to a different repository identity")
    registered = _registered_worktrees(root)
    for record in records:
        if record.repository_identity != session.repository_identity:
            raise ValueError(
                f"managed worktree {record.work_id!r} is bound to a different "
                "repository identity"
            )
    inspections = [
        _inspect_record(root, session, record, registered) for record in records
    ]
    owned_paths = {Path(record.worktree_path).resolve() for record in records}
    if merge_queue is not None:
        queue, _ = merge_queue
        owned_paths.add(Path(queue.integration_worktree_path).resolve())
    session_root = _session_worktree_root(root, session_id)
    orphans = []
    for path, item in sorted(registered.items(), key=lambda pair: str(pair[0])):
        if path in owned_paths or not _is_within(path, session_root):
            continue
        orphans.append(
            {
                "worktree_path": str(path),
                "head_commit": item.get("HEAD"),
                "branch": item.get("branch", "").removeprefix("refs/heads/") or None,
                "detail": (
                    "Git worktree is inside the managed session directory but has "
                    "no durable ownership record"
                ),
            }
        )
    counts: dict[str, int] = {}
    for inspection in inspections:
        counts[inspection.health.value] = counts.get(inspection.health.value, 0) + 1
    return {
        "session_id": session_id,
        "graph_version": session.graph_version,
        "graph_fingerprint": session.graph_fingerprint,
        "worktrees": [item.to_dict() for item in inspections],
        "orphans": orphans,
        "summary": {
            "managed": len(inspections),
            "orphans": len(orphans),
            "health": counts,
        },
    }


def provision_swarm_worktrees(repo: str | Path, session_id: str) -> dict[str, Any]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    identity = _repository_identity(root)
    now = _utc_now()
    with _store(root) as store:
        session = store.require(session_id)
        existing = store.list_worktrees(session_id)
        stored_plan = store.get_concurrency_plan(session_id)
    if session.repository_identity != identity:
        raise ValueError("swarm session is bound to a different repository identity")
    if _resolve_commit(root, session.base_commit) != session.base_commit:
        raise ValueError("swarm session base commit is no longer resolvable")
    if session.state is not SwarmSessionState.PLANNED:
        raise ValueError(
            f"cannot provision worktrees while session is {session.state.value}"
        )
    if stored_plan is None:
        raise ValueError(
            "swarm session has no concurrency plan; run 'claim-plane swarm plan' first"
        )
    plan, _ = stored_plan
    if plan.status is not ConcurrencyPlanStatus.READY:
        raise ValueError("cannot provision worktrees for a replan-required plan")
    if (
        plan.graph_version != session.graph_version
        or plan.graph_fingerprint != session.graph_fingerprint
        or plan.budget_version != session.budget_version
        or plan.budget_fingerprint != session.budget_fingerprint
    ):
        raise ValueError(
            "stored concurrency plan is stale; re-plan before provisioning"
        )

    expected = {
        work_id: _expected_worktree_record(root, session, work_id, now=now)
        for work_id in session.work_graph.topological_order()
    }
    existing_by_id = {record.work_id: record for record in existing}
    extra = sorted(set(existing_by_id) - set(expected))
    if extra:
        raise ValueError(
            "stale managed worktrees exist for removed work items: "
            + ", ".join(extra)
            + "; clean them before provisioning the new graph"
        )
    registered = _registered_worktrees(root)
    for work_id, current in existing_by_id.items():
        target = expected[work_id]
        if (
            current.repository_identity != target.repository_identity
            or current.graph_version != target.graph_version
            or current.graph_fingerprint != target.graph_fingerprint
            or current.base_commit != target.base_commit
            or current.branch != target.branch
            or Path(current.worktree_path).resolve()
            != Path(target.worktree_path).resolve()
        ):
            raise ValueError(
                f"managed worktree {work_id!r} is stale or has different "
                "ownership metadata; clean it first"
            )
        inspection = _inspect_record(root, session, current, registered)
        if inspection.health not in {WorktreeHealth.READY, WorktreeHealth.DIRTY}:
            raise ValueError(
                f"managed worktree {work_id!r} is {inspection.health.value}: "
                f"{inspection.detail}"
            )

    created_git: list[ManagedWorktree] = []
    try:
        for work_id in session.work_graph.topological_order():
            if work_id in existing_by_id:
                continue
            record = expected[work_id]
            path = Path(record.worktree_path)
            if path.exists():
                raise ValueError(
                    f"refusing to overwrite existing path for work item "
                    f"{work_id!r}: {path}"
                )
            if path.resolve() in registered:
                raise ValueError(
                    f"worktree path is already registered for work item "
                    f"{work_id!r}: {path}"
                )
            if _branch_exists(root, record.branch):
                raise ValueError(
                    f"refusing to reuse unowned branch for work item "
                    f"{work_id!r}: {record.branch}"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            result = _git_result(
                root,
                "worktree",
                "add",
                "-b",
                record.branch,
                str(path),
                session.base_commit,
            )
            if result.returncode != 0:
                raise ValueError(
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"failed to provision worktree for {work_id!r}"
                )
            created_git.append(record)
        all_records = [
            existing_by_id.get(work_id, expected[work_id])
            for work_id in session.work_graph.topological_order()
        ]
        with _store(root) as store:
            stored, created_count = store.save_worktrees(
                session_id,
                all_records,
                expected_graph_version=session.graph_version,
                expected_graph_fingerprint=session.graph_fingerprint,
            )
    except Exception:
        for record in reversed(created_git):
            path = Path(record.worktree_path)
            _git_result(root, "worktree", "remove", "--force", str(path))
            if _branch_exists(root, record.branch):
                _git_result(root, "branch", "-D", record.branch)
        _git_result(root, "worktree", "prune")
        raise

    status = inspect_swarm_worktrees(root, session_id)
    return {
        "session_id": session_id,
        "created": created_count,
        "reused": len(stored) - created_count,
        "records": [record.to_dict() for record in stored],
        "summary": status["summary"],
    }


def cleanup_swarm_worktrees(
    repo: str | Path,
    session_id: str,
    *,
    work_ids: tuple[str, ...] = (),
    force: bool = False,
) -> dict[str, Any]:
    root = resolve_repository_root(repo)
    _require_initialized(root)
    session_id = _validate_session_id(session_id)
    identity = _repository_identity(root)
    with _store(root) as store:
        session = store.require(session_id)
        records = store.list_worktrees(session_id)
        codex_runs = store.list_codex_runs(session_id)
    if session.repository_identity != identity:
        raise ValueError("swarm session is bound to a different repository identity")
    by_id = {record.work_id: record for record in records}
    selected_ids = tuple(sorted(set(work_ids))) if work_ids else tuple(sorted(by_id))
    missing_ids = sorted(set(selected_ids) - set(by_id))
    if missing_ids:
        raise KeyError("unknown managed work items: " + ", ".join(missing_ids))
    active_run_ids = [
        run.run_id
        for run in codex_runs
        if run.work_id in selected_ids and run.state.active
    ]
    if active_run_ids:
        raise ValueError(
            "refusing to remove worktrees with active Codex runs: "
            + ", ".join(active_run_ids)
        )
    registered = _registered_worktrees(root)
    session_root = _session_worktree_root(root, session_id)

    inspections = [
        _inspect_record(root, session, by_id[work_id], registered)
        for work_id in selected_ids
    ]
    dirty: list[str] = []
    for inspection in inspections:
        record = inspection.record
        path = Path(record.worktree_path).resolve()
        expected_path = managed_worktree_path(root, session_id, record.work_id)
        expected_branch = managed_branch_name(session_id, record.work_id)
        if record.repository_identity != session.repository_identity:
            raise ValueError(
                f"refusing cleanup of worktree {record.work_id!r} bound to "
                "another repository"
            )
        if path != expected_path or not _is_within(path, session_root):
            raise ValueError(
                f"refusing cleanup outside the owned managed-worktree path: {path}"
            )
        if record.branch != expected_branch:
            raise ValueError(
                f"refusing cleanup of unrecognized branch {record.branch!r}"
            )
        if inspection.registered and path.exists() and _worktree_dirty(path):
            dirty.append(record.work_id)
        if not inspection.registered and path.exists():
            raise ValueError(
                f"refusing to delete unregistered directory automatically: {path}"
            )
    if dirty and not force:
        raise ValueError(
            "refusing to remove dirty managed worktrees without --force: "
            + ", ".join(dirty)
        )
    for inspection in inspections:
        record = inspection.record
        path = Path(record.worktree_path).resolve()
        if inspection.registered:
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(str(path))
            result = _git_result(root, *args)
            if result.returncode != 0:
                raise ValueError(
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"failed to remove managed worktree {record.work_id!r}"
                )
        if _branch_exists(root, record.branch):
            result = _git_result(root, "branch", "-D", record.branch)
            if result.returncode != 0:
                raise ValueError(
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"failed to delete managed branch {record.branch!r}"
                )
    with _store(root) as store:
        deleted = store.delete_worktrees(session_id, list(selected_ids))
    _git_result(root, "worktree", "prune")
    current = session_root
    while current != root / ".claim-plane" and _is_within(
        current, root / ".claim-plane"
    ):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent
    return {
        "session_id": session_id,
        "removed": deleted,
        "work_ids": list(selected_ids),
        "forced": force,
    }
