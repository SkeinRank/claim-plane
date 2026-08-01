"""Repository-bound swarm-session planning services."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from claim_plane.swarm.models import (
    SWARM_SESSION_SPEC_PROTOCOL,
    IntegrationTarget,
    RootTask,
    SwarmSession,
    SwarmSessionState,
    WorkGraph,
)
from claim_plane.swarm.store import SwarmSessionStore

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
) -> tuple[RootTask, IntegrationTarget | None, WorkGraph, dict[str, Any]]:
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
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be an object")
    return (
        RootTask.from_dict(root_task_raw),
        target,
        WorkGraph.from_dict(graph_raw),
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
    root_task, target, graph, metadata = parse_session_spec(spec)
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
        graph_version=1,
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
