"""Revision-bound semantic graph caching, incremental invalidation, and freshness fencing.

The graph used for admission is evidence, not ambient workspace state.  This module keeps
that evidence bound to one source revision, reuses unchanged graph components across
revisions, and fails closed when provider evidence no longer matches the pinned source.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from claim_plane.core.dependency_graph import SemanticDependencyGraph
from claim_plane.core.python_dependency import build_python_dependency_graph

SEMANTIC_GRAPH_SNAPSHOT_PROTOCOL = "claim-plane.semantic-graph-snapshot.v1"
SEMANTIC_GRAPH_INVALIDATION_PROTOCOL = "claim-plane.semantic-graph-invalidation.v1"
SEMANTIC_GRAPH_CACHE_PROTOCOL = "claim-plane.semantic-graph-cache.v1"
SEMANTIC_GRAPH_CACHE_SCHEMA = "1"
SEMANTIC_GRAPH_BUILDER_SCHEMA = "1"


class SemanticGraphStateError(RuntimeError):
    """Base error for semantic graph cache/freshness handling."""


class StaleSemanticGraphError(SemanticGraphStateError):
    """Raised when graph evidence does not match the pinned source state."""


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _source_digests(sources: Mapping[str, str]) -> dict[str, str]:
    return {
        str(path): hashlib.sha256(str(source).encode("utf-8")).hexdigest()
        for path, source in sorted(sources.items())
    }


def _default_cache_root() -> Path:
    override = os.environ.get("CLAIM_PLANE_CACHE_DIR")
    if override:
        return Path(override).expanduser().resolve() / "code-intelligence/semantic-graphs"
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return (Path(xdg).expanduser() / "claim-plane/code-intelligence/semantic-graphs").resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return (
            Path(os.environ["LOCALAPPDATA"])
            / "claim-plane/Cache/code-intelligence/semantic-graphs"
        ).resolve()
    if os.sys.platform == "darwin":
        return (
            Path.home()
            / "Library/Caches/claim-plane/code-intelligence/semantic-graphs"
        ).resolve()
    return (Path.home() / ".cache/claim-plane/code-intelligence/semantic-graphs").resolve()


@dataclass(frozen=True, slots=True)
class SemanticGraphInvalidationPlan:
    """Deterministic source/graph surface that must be replaced after source changes."""

    previous_graph_fingerprint: str
    changed_paths: tuple[str, ...]
    added_paths: tuple[str, ...]
    removed_paths: tuple[str, ...]
    affected_paths: tuple[str, ...]
    affected_identities: tuple[str, ...]
    full_rebuild: bool
    reasons: tuple[str, ...] = ()
    protocol: str = SEMANTIC_GRAPH_INVALIDATION_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SEMANTIC_GRAPH_INVALIDATION_PROTOCOL:
            raise ValueError(f"unsupported semantic graph invalidation {self.protocol!r}")
        if len(self.previous_graph_fingerprint) != 64:
            raise ValueError("previous graph fingerprint must be SHA-256")
        for name in (
            "changed_paths",
            "added_paths",
            "removed_paths",
            "affected_paths",
            "affected_identities",
            "reasons",
        ):
            values = tuple(sorted({str(item) for item in getattr(self, name) if str(item)}))
            object.__setattr__(self, name, values)
        if not set(self.added_paths).issubset(self.changed_paths):
            raise ValueError("added paths must be changed paths")
        if not set(self.removed_paths).issubset(self.changed_paths):
            raise ValueError("removed paths must be changed paths")
        if not set(self.changed_paths).issubset(self.affected_paths):
            raise ValueError("changed paths must be affected paths")

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_dict(include_fingerprint=False))

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload = {
            "protocol": self.protocol,
            "previous_graph_fingerprint": self.previous_graph_fingerprint,
            "changed_paths": list(self.changed_paths),
            "added_paths": list(self.added_paths),
            "removed_paths": list(self.removed_paths),
            "affected_paths": list(self.affected_paths),
            "affected_identities": list(self.affected_identities),
            "full_rebuild": self.full_rebuild,
            "reasons": list(self.reasons),
        }
        if include_fingerprint:
            return {"fingerprint": self.fingerprint, **payload}
        return payload


@dataclass(frozen=True, slots=True)
class SemanticGraphSnapshot:
    """One cached builtin graph tied to a repository identity and source revision."""

    repository_identity: str
    revision: str
    graph: SemanticDependencyGraph
    builder_schema: str = SEMANTIC_GRAPH_BUILDER_SCHEMA
    protocol: str = SEMANTIC_GRAPH_SNAPSHOT_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SEMANTIC_GRAPH_SNAPSHOT_PROTOCOL:
            raise ValueError(f"unsupported semantic graph snapshot {self.protocol!r}")
        if self.builder_schema != SEMANTIC_GRAPH_BUILDER_SCHEMA:
            raise ValueError(
                f"unsupported semantic graph builder schema {self.builder_schema!r}"
            )
        repository_identity = self.repository_identity.strip().lower()
        if len(repository_identity) != 64 or any(
            char not in "0123456789abcdef" for char in repository_identity
        ):
            raise ValueError("repository identity must be a SHA-256 digest")
        object.__setattr__(self, "repository_identity", repository_identity)
        revision = self.revision.strip().lower()
        if not revision:
            raise ValueError("semantic graph snapshot revision must not be empty")
        object.__setattr__(self, "revision", revision)
        if not isinstance(self.graph, SemanticDependencyGraph):
            object.__setattr__(self, "graph", SemanticDependencyGraph.from_dict(self.graph))
        graph_revision = str(self.graph.metadata.get("source_revision") or "").lower()
        if graph_revision and graph_revision != revision:
            raise StaleSemanticGraphError(
                f"snapshot graph revision {graph_revision} != snapshot revision {revision}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "repository_identity": self.repository_identity,
            "revision": self.revision,
            "builder_schema": self.builder_schema,
            "graph": self.graph.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SemanticGraphSnapshot":
        return cls(
            protocol=str(data.get("protocol") or SEMANTIC_GRAPH_SNAPSHOT_PROTOCOL),
            repository_identity=str(data["repository_identity"]),
            revision=str(data["revision"]),
            builder_schema=str(data.get("builder_schema") or ""),
            graph=SemanticDependencyGraph.from_dict(data["graph"]),
        )


class SemanticGraphRevisionCache:
    """Atomic persistent cache for revision-bound builtin semantic graphs."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = (
            _default_cache_root() if root is None else Path(root).expanduser().resolve()
        )

    def _repo_dir(self, repository_identity: str) -> Path:
        identity = repository_identity.strip().lower()
        if len(identity) != 64 or any(char not in "0123456789abcdef" for char in identity):
            raise ValueError("repository identity must be a SHA-256 digest")
        return self.root / identity[:2] / identity

    def _snapshot_path(self, repository_identity: str, revision: str) -> Path:
        revision_key = hashlib.sha256(revision.strip().lower().encode("utf-8")).hexdigest()
        return self._repo_dir(repository_identity) / "revisions" / f"{revision_key}.json"

    def load_exact(self, repository_identity: str, revision: str) -> SemanticGraphSnapshot | None:
        path = self._snapshot_path(repository_identity, revision)
        snapshot = self._load_path(path)
        if snapshot is None or snapshot.revision != revision.strip().lower():
            return None
        return snapshot

    def load_latest(self, repository_identity: str) -> SemanticGraphSnapshot | None:
        pointer = self._repo_dir(repository_identity) / "latest.json"
        try:
            payload = json.loads(pointer.read_text(encoding="utf-8"))
            if payload.get("protocol") != SEMANTIC_GRAPH_CACHE_PROTOCOL:
                return None
            if payload.get("cache_schema") != SEMANTIC_GRAPH_CACHE_SCHEMA:
                return None
            revision = str(payload["revision"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None
        return self.load_exact(repository_identity, revision)

    def store(self, snapshot: SemanticGraphSnapshot) -> SemanticGraphSnapshot:
        target = self._snapshot_path(snapshot.repository_identity, snapshot.revision)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_json(target, snapshot.to_dict())
        pointer = self._repo_dir(snapshot.repository_identity) / "latest.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_json(
            pointer,
            {
                "protocol": SEMANTIC_GRAPH_CACHE_PROTOCOL,
                "cache_schema": SEMANTIC_GRAPH_CACHE_SCHEMA,
                "repository_identity": snapshot.repository_identity,
                "revision": snapshot.revision,
                "graph_fingerprint": snapshot.graph.fingerprint,
            },
        )
        return snapshot

    def _load_path(self, path: Path) -> SemanticGraphSnapshot | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return SemanticGraphSnapshot.from_dict(payload)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
            StaleSemanticGraphError,
        ):
            return None

    @staticmethod
    def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=f".{path.name}.", dir=path.parent, delete=False
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def plan_semantic_graph_invalidation(
    previous: SemanticDependencyGraph,
    current_source_digests: Mapping[str, str],
) -> SemanticGraphInvalidationPlan:
    """Find the connected repository surface whose old dependency facts may be stale."""

    current = {str(path): str(value) for path, value in current_source_digests.items()}
    old = dict(previous.source_digests)
    changed = {
        path for path in set(old) | set(current) if old.get(path) != current.get(path)
    }
    added = {path for path in changed if path not in old}
    removed = {path for path in changed if path not in current}
    if not changed:
        return SemanticGraphInvalidationPlan(
            previous_graph_fingerprint=previous.fingerprint,
            changed_paths=(),
            added_paths=(),
            removed_paths=(),
            affected_paths=(),
            affected_identities=(),
            full_rebuild=False,
            reasons=(),
        )

    # A newly created module can resolve an edge that was previously external/unresolved.
    # Likewise, editing an existing module can introduce a symbol that satisfies an old
    # unresolved reference which has no safe dependency path back to that module.
    # Rebuild rather than treating absence of an old edge as proof of independence.
    unresolved_present = any(edge.resolution.value == "unresolved" for edge in previous.edges)
    if added or unresolved_present:
        return SemanticGraphInvalidationPlan(
            previous_graph_fingerprint=previous.fingerprint,
            changed_paths=tuple(changed),
            added_paths=tuple(added),
            removed_paths=tuple(removed),
            affected_paths=tuple(sorted(set(old) | set(current))),
            affected_identities=tuple(node.identity for node in previous.nodes),
            full_rebuild=True,
            reasons=tuple(
                reason
                for reason, enabled in (
                    ("new_source_can_resolve_previous_unknown_edge", bool(added)),
                    ("unresolved_edge_can_change_resolution", unresolved_present),
                )
                if enabled
            ),
        )

    by_identity = {node.identity: node for node in previous.nodes}
    roots = {
        node.identity
        for node in previous.nodes
        if node.resource.path is not None and node.resource.path in changed
    }
    if not roots:
        return SemanticGraphInvalidationPlan(
            previous_graph_fingerprint=previous.fingerprint,
            changed_paths=tuple(changed),
            added_paths=tuple(added),
            removed_paths=tuple(removed),
            affected_paths=tuple(sorted(set(old) | set(current))),
            affected_identities=tuple(node.identity for node in previous.nodes),
            full_rebuild=True,
            reasons=("changed_source_has_no_old_graph_root",),
        )

    adjacency: dict[str, set[str]] = {identity: set() for identity in by_identity}
    for edge in previous.edges:
        # External package nodes are shared by many otherwise unrelated files and do not
        # become stale when one repository source changes. Unresolved edges are handled
        # above by a full rebuild because their resolution itself may change.
        if edge.resolution.value != "internal":
            continue
        adjacency.setdefault(edge.source_identity, set()).add(edge.target_identity)
        adjacency.setdefault(edge.target_identity, set()).add(edge.source_identity)

    affected = set(roots)
    pending = list(sorted(roots))
    while pending:
        current_identity = pending.pop()
        for neighbor in adjacency.get(current_identity, ()):
            if neighbor in affected:
                continue
            affected.add(neighbor)
            pending.append(neighbor)

    affected_paths = set(changed)
    for identity in affected:
        node = by_identity.get(identity)
        if node is not None and node.resource.path is not None:
            affected_paths.add(node.resource.path)

    return SemanticGraphInvalidationPlan(
        previous_graph_fingerprint=previous.fingerprint,
        changed_paths=tuple(changed),
        added_paths=tuple(added),
        removed_paths=tuple(removed),
        affected_paths=tuple(affected_paths),
        affected_identities=tuple(affected),
        full_rebuild=False,
        reasons=("dependency_component_invalidated",),
    )


def _with_graph_state_metadata(
    graph: SemanticDependencyGraph,
    *,
    revision: str,
    refresh_mode: str,
    invalidation: SemanticGraphInvalidationPlan | None,
) -> SemanticDependencyGraph:
    return SemanticDependencyGraph(
        nodes=graph.nodes,
        edges=graph.edges,
        source_digests=graph.source_digests,
        metadata={
            **dict(graph.metadata),
            "source_revision": revision.lower(),
            "source_mode": "pinned_git",
            "refresh_mode": refresh_mode,
            "invalidation_fingerprint": (
                None if invalidation is None else invalidation.fingerprint
            ),
            "invalidation_changed_paths": (
                [] if invalidation is None else list(invalidation.changed_paths)
            ),
            "invalidation_affected_paths": (
                [] if invalidation is None else list(invalidation.affected_paths)
            ),
        },
    )


def refresh_python_dependency_graph_incrementally(
    previous: SemanticDependencyGraph | None,
    sources: Mapping[str, str],
    *,
    revision: str,
) -> tuple[SemanticDependencyGraph, SemanticGraphInvalidationPlan | None]:
    """Build or refresh the builtin graph while preserving unaffected components."""

    normalized_sources = {str(path): str(source) for path, source in sources.items()}
    current_digests = _source_digests(normalized_sources)
    if previous is None:
        graph = build_python_dependency_graph(normalized_sources)
        return _with_graph_state_metadata(
            graph, revision=revision, refresh_mode="full", invalidation=None
        ), None

    plan = plan_semantic_graph_invalidation(previous, current_digests)
    if not plan.changed_paths:
        reused = SemanticDependencyGraph(
            nodes=previous.nodes,
            edges=previous.edges,
            source_digests=current_digests,
            metadata=dict(previous.metadata),
        )
        return _with_graph_state_metadata(
            reused, revision=revision, refresh_mode="reused", invalidation=plan
        ), plan

    if plan.full_rebuild:
        graph = build_python_dependency_graph(normalized_sources)
        return _with_graph_state_metadata(
            graph, revision=revision, refresh_mode="full", invalidation=plan
        ), plan

    rebuild_paths = tuple(
        path for path in plan.affected_paths if path in normalized_sources
    )
    partial = build_python_dependency_graph(
        normalized_sources,
        emit_paths=rebuild_paths,
    )
    affected = set(plan.affected_identities)
    affected_paths = set(plan.affected_paths)
    retained_nodes = tuple(
        node
        for node in previous.nodes
        if node.identity not in affected
        and (node.resource.path is None or node.resource.path not in affected_paths)
    )
    retained_identities = {node.identity for node in retained_nodes}
    retained_edges = tuple(
        edge
        for edge in previous.edges
        if edge.source_identity in retained_identities
        and edge.target_identity in retained_identities
    )
    graph = SemanticDependencyGraph(
        nodes=(*retained_nodes, *partial.nodes),
        edges=(*retained_edges, *partial.edges),
        source_digests=current_digests,
        metadata={
            **dict(partial.metadata),
            "incremental_previous_graph_fingerprint": previous.fingerprint,
            "incremental_retained_node_count": len(retained_nodes),
            "incremental_rebuilt_path_count": len(rebuild_paths),
        },
    )
    return _with_graph_state_metadata(
        graph, revision=revision, refresh_mode="incremental", invalidation=plan
    ), plan


def assert_semantic_graph_fresh(
    graph: SemanticDependencyGraph,
    *,
    expected_revision: str,
    expected_workspace_fingerprint: str | None = None,
) -> None:
    """Fail closed when graph/provider evidence is bound to another source state."""

    expected = expected_revision.strip().lower()
    if not expected:
        raise ValueError("expected semantic graph revision must not be empty")
    actual = str(
        graph.metadata.get("source_revision") or graph.metadata.get("revision") or ""
    ).lower()
    if not actual:
        raise StaleSemanticGraphError("semantic graph has no source revision binding")
    if actual != expected:
        raise StaleSemanticGraphError(
            f"semantic graph revision {actual} does not match expected {expected}"
        )
    expected_workspace = (
        None
        if expected_workspace_fingerprint is None
        else expected_workspace_fingerprint.strip().lower()
    )
    if expected_workspace is not None and len(expected_workspace) != 64:
        raise ValueError("expected workspace fingerprint must be SHA-256")

    stale: list[str] = []
    for edge in graph.edges:
        for evidence in edge.evidence:
            if evidence.revision is not None and evidence.revision.lower() != expected:
                stale.append(f"{evidence.provider_id}:{evidence.evidence_type}:revision")
            if (
                expected_workspace is not None
                and evidence.workspace_fingerprint is not None
                and evidence.workspace_fingerprint.lower() != expected_workspace
            ):
                stale.append(f"{evidence.provider_id}:{evidence.evidence_type}:workspace")
    if stale:
        detail = ", ".join(sorted(set(stale))[:8])
        raise StaleSemanticGraphError(
            f"semantic graph contains stale dependency evidence ({detail})"
        )
