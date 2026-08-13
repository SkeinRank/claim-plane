"""Language-neutral semantic dependency graph for deterministic concurrency reasoning.

Dependency Graph v2 is deliberately separate from the runtime intent-dependency graph.
The runtime graph orders admitted work items; this graph describes relationships between
repository resources such as symbols, contracts, files, and shared state.  Language
frontends populate it without giving the graph authority to execute repository code.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from claim_plane.core.resource_ir import SemanticResource

SEMANTIC_DEPENDENCY_GRAPH_PROTOCOL = "claim-plane.semantic-dependency-graph.v2"
DEPENDENCY_EVIDENCE_PROTOCOL = "claim-plane.dependency-evidence.v1"


class DependencyRelation(str, Enum):
    """Normalized dependency relations emitted by language frontends."""

    DEFINES = "defines"
    IMPORTS = "imports"
    CALLS = "calls"
    READS = "reads"
    WRITES = "writes"
    INHERITS = "inherits"
    TYPES = "types"
    TESTS = "tests"
    PUBLIC_API = "public_api"
    REFERENCES = "references"
    IMPLEMENTS = "implements"
    DEFINITION_OF = "definition_of"


class DependencyResolution(str, Enum):
    """How confidently an edge target was resolved to repository state."""

    INTERNAL = "internal"
    EXTERNAL = "external"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class DependencyEvidence:
    """One immutable provenance record supporting a dependency edge.

    Evidence is deliberately provider-neutral.  Source-specific details stay in
    ``metadata`` while revision/artifact identity and source coordinates remain
    queryable by core code.  Source ranges use the provider's native coordinate
    convention; ``metadata`` must identify it when a range is present.
    """

    provider_id: str
    evidence_type: str
    revision: str | None = None
    workspace_fingerprint: str | None = None
    artifact_sha256: str | None = None
    path: str | None = None
    source_range: tuple[int, int, int, int] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = DEPENDENCY_EVIDENCE_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != DEPENDENCY_EVIDENCE_PROTOCOL:
            raise ValueError(f"unsupported dependency evidence {self.protocol!r}")
        provider_id = self.provider_id.strip().casefold()
        evidence_type = self.evidence_type.strip().casefold()
        if not provider_id or not evidence_type:
            raise ValueError("dependency evidence provider/type must not be empty")
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "evidence_type", evidence_type)
        for name in ("revision", "workspace_fingerprint", "artifact_sha256", "path"):
            value = getattr(self, name)
            object.__setattr__(
                self, name, None if value is None else str(value).strip() or None
            )
        if (
            self.workspace_fingerprint is not None
            and len(self.workspace_fingerprint) != 64
        ):
            raise ValueError("workspace_fingerprint must be a SHA-256 hex digest")
        if self.artifact_sha256 is not None and len(self.artifact_sha256) != 64:
            raise ValueError("artifact_sha256 must be a SHA-256 hex digest")
        if self.source_range is not None:
            values = tuple(int(item) for item in self.source_range)
            if len(values) != 4 or any(item < 0 for item in values):
                raise ValueError(
                    "dependency evidence source_range must contain four "
                    "non-negative integers"
                )
            if (values[2], values[3]) < (values[0], values[1]):
                raise ValueError("dependency evidence source_range end precedes start")
            object.__setattr__(self, "source_range", values)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict())).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "protocol": self.protocol,
            "provider_id": self.provider_id,
            "evidence_type": self.evidence_type,
            "revision": self.revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "artifact_sha256": self.artifact_sha256,
            "path": self.path,
            "source_range": (
                None if self.source_range is None else list(self.source_range)
            ),
            "metadata": dict(self.metadata),
        }
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DependencyEvidence":
        source_range = data.get("source_range")
        return cls(
            protocol=str(data.get("protocol") or DEPENDENCY_EVIDENCE_PROTOCOL),
            provider_id=str(data["provider_id"]),
            evidence_type=str(data["evidence_type"]),
            revision=data.get("revision"),
            workspace_fingerprint=data.get("workspace_fingerprint"),
            artifact_sha256=data.get("artifact_sha256"),
            path=data.get("path"),
            source_range=(
                None
                if source_range is None
                else tuple(int(item) for item in source_range)
            ),
            metadata=dict(data.get("metadata") or {}),
        )


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class DependencyNode:
    """One graph node backed by a Semantic Resource IR v2 coordinate."""

    resource: SemanticResource
    public: bool = False
    test: bool = False
    external: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.resource, SemanticResource):
            object.__setattr__(
                self,
                "resource",
                SemanticResource.from_dict(self.resource),
            )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def identity(self) -> str:
        return self.resource.identity

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource": self.resource.to_dict(),
            "public": self.public,
            "test": self.test,
            "external": self.external,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DependencyNode":
        return cls(
            resource=SemanticResource.from_dict(data["resource"]),
            public=bool(data.get("public", False)),
            test=bool(data.get("test", False)),
            external=bool(data.get("external", False)),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    """One typed, deterministic relationship between semantic resources."""

    source_identity: str
    target_identity: str
    relation: DependencyRelation
    resolution: DependencyResolution = DependencyResolution.INTERNAL
    locations: tuple[int, ...] = ()
    evidence: tuple[DependencyEvidence, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        source = self.source_identity.strip()
        target = self.target_identity.strip()
        if not source or not target:
            raise ValueError("dependency edge endpoints must not be empty")
        object.__setattr__(self, "source_identity", source)
        object.__setattr__(self, "target_identity", target)
        object.__setattr__(self, "relation", DependencyRelation(self.relation))
        object.__setattr__(self, "resolution", DependencyResolution(self.resolution))
        locations = tuple(sorted({int(line) for line in self.locations}))
        if any(line < 1 for line in locations):
            raise ValueError("dependency edge locations must be positive line numbers")
        object.__setattr__(self, "locations", locations)
        evidence = tuple(
            item
            if isinstance(item, DependencyEvidence)
            else DependencyEvidence.from_dict(item)
            for item in self.evidence
        )
        by_fingerprint = {item.fingerprint: item for item in evidence}
        object.__setattr__(
            self,
            "evidence",
            tuple(by_fingerprint[key] for key in sorted(by_fingerprint)),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            self.source_identity,
            self.target_identity,
            self.relation.value,
            self.resolution.value,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "source_identity": self.source_identity,
            "target_identity": self.target_identity,
            "relation": self.relation.value,
            "resolution": self.resolution.value,
            "locations": list(self.locations),
            "metadata": dict(self.metadata),
        }
        # Keep evidence-free v2 edge serialization byte-for-byte compatible with
        # existing graph fingerprints and persisted research artifacts.
        if self.evidence:
            payload["evidence"] = [item.to_dict() for item in self.evidence]
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DependencyEdge":
        return cls(
            source_identity=str(data["source_identity"]),
            target_identity=str(data["target_identity"]),
            relation=DependencyRelation(data["relation"]),
            resolution=DependencyResolution(
                data.get("resolution", DependencyResolution.INTERNAL.value)
            ),
            locations=tuple(int(item) for item in data.get("locations") or ()),
            evidence=tuple(
                DependencyEvidence.from_dict(item)
                for item in data.get("evidence") or ()
            ),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class SemanticDependencyGraph:
    """Immutable repository semantic graph consumed by later admission stages."""

    nodes: tuple[DependencyNode, ...]
    edges: tuple[DependencyEdge, ...]
    source_digests: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SEMANTIC_DEPENDENCY_GRAPH_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SEMANTIC_DEPENDENCY_GRAPH_PROTOCOL:
            raise ValueError(f"unsupported semantic dependency graph {self.protocol!r}")

        node_by_identity: dict[str, DependencyNode] = {}
        for raw_node in self.nodes:
            node = (
                raw_node
                if isinstance(raw_node, DependencyNode)
                else DependencyNode.from_dict(raw_node)
            )
            existing = node_by_identity.get(node.identity)
            if (
                existing is not None
                and existing.resource.to_dict() != node.resource.to_dict()
            ):
                raise ValueError(f"conflicting dependency nodes for {node.identity!r}")
            if existing is None:
                node_by_identity[node.identity] = node
            else:
                node_by_identity[node.identity] = DependencyNode(
                    resource=existing.resource,
                    public=existing.public or node.public,
                    test=existing.test or node.test,
                    external=existing.external or node.external,
                    metadata={**existing.metadata, **node.metadata},
                )

        edge_groups: dict[tuple[str, str, str, str], DependencyEdge] = {}
        for raw_edge in self.edges:
            edge = (
                raw_edge
                if isinstance(raw_edge, DependencyEdge)
                else DependencyEdge.from_dict(raw_edge)
            )
            if edge.source_identity not in node_by_identity:
                raise ValueError(
                    "dependency edge source is not a graph node: "
                    f"{edge.source_identity}"
                )
            if edge.target_identity not in node_by_identity:
                raise ValueError(
                    "dependency edge target is not a graph node: "
                    f"{edge.target_identity}"
                )
            previous = edge_groups.get(edge.key)
            if previous is None:
                edge_groups[edge.key] = edge
            else:
                edge_groups[edge.key] = DependencyEdge(
                    source_identity=edge.source_identity,
                    target_identity=edge.target_identity,
                    relation=edge.relation,
                    resolution=edge.resolution,
                    locations=(*previous.locations, *edge.locations),
                    evidence=(*previous.evidence, *edge.evidence),
                    metadata={**previous.metadata, **edge.metadata},
                )

        object.__setattr__(
            self,
            "nodes",
            tuple(sorted(node_by_identity.values(), key=lambda item: item.identity)),
        )
        object.__setattr__(
            self,
            "edges",
            tuple(
                sorted(
                    edge_groups.values(),
                    key=lambda item: (
                        item.source_identity,
                        item.target_identity,
                        item.relation.value,
                        item.resolution.value,
                    ),
                )
            ),
        )
        source_digests = {
            str(path): str(digest) for path, digest in self.source_digests.items()
        }
        if any(len(digest) != 64 for digest in source_digests.values()):
            raise ValueError("source digests must be SHA-256 hex digests")
        object.__setattr__(self, "source_digests", dict(sorted(source_digests.items())))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def fingerprint(self) -> str:
        return _graph_fingerprint_payload(self)

    def node(self, identity: str) -> DependencyNode | None:
        return next((item for item in self.nodes if item.identity == identity), None)

    def outgoing(
        self,
        identity: str,
        *,
        relations: Iterable[DependencyRelation | str] | None = None,
    ) -> tuple[DependencyEdge, ...]:
        allowed = _relation_values(relations)
        return tuple(
            edge
            for edge in self.edges
            if edge.source_identity == identity
            and (allowed is None or edge.relation in allowed)
        )

    def incoming(
        self,
        identity: str,
        *,
        relations: Iterable[DependencyRelation | str] | None = None,
    ) -> tuple[DependencyEdge, ...]:
        allowed = _relation_values(relations)
        return tuple(
            edge
            for edge in self.edges
            if edge.target_identity == identity
            and (allowed is None or edge.relation in allowed)
        )

    def dependencies(
        self,
        identity: str,
        *,
        relations: Iterable[DependencyRelation | str] | None = None,
        transitive: bool = False,
    ) -> tuple[DependencyNode, ...]:
        return self._walk(
            identity, incoming=False, relations=relations, transitive=transitive
        )

    def dependents(
        self,
        identity: str,
        *,
        relations: Iterable[DependencyRelation | str] | None = None,
        transitive: bool = False,
    ) -> tuple[DependencyNode, ...]:
        return self._walk(
            identity, incoming=True, relations=relations, transitive=transitive
        )

    def _walk(
        self,
        identity: str,
        *,
        incoming: bool,
        relations: Iterable[DependencyRelation | str] | None,
        transitive: bool,
    ) -> tuple[DependencyNode, ...]:
        allowed = _relation_values(relations)
        seen = {identity}
        pending = [identity]
        found: dict[str, DependencyNode] = {}
        while pending:
            current = pending.pop(0)
            edges = (
                self.incoming(current, relations=allowed)
                if incoming
                else self.outgoing(current, relations=allowed)
            )
            for edge in edges:
                neighbor_identity = (
                    edge.source_identity if incoming else edge.target_identity
                )
                if neighbor_identity in seen:
                    continue
                seen.add(neighbor_identity)
                node = self.node(neighbor_identity)
                if node is not None:
                    found[node.identity] = node
                if transitive:
                    pending.append(neighbor_identity)
            if not transitive:
                break
        return tuple(found[key] for key in sorted(found))

    def edges_for_path(self, path: str) -> tuple[DependencyEdge, ...]:
        identities = {
            node.identity for node in self.nodes if node.resource.path == path
        }
        return tuple(
            edge
            for edge in self.edges
            if edge.source_identity in identities or edge.target_identity in identities
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "fingerprint": _graph_fingerprint_payload(self),
            "source_digests": dict(self.source_digests),
            "nodes": [item.to_dict() for item in self.nodes],
            "edges": [item.to_dict() for item in self.edges],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SemanticDependencyGraph":
        graph = cls(
            protocol=str(data.get("protocol") or SEMANTIC_DEPENDENCY_GRAPH_PROTOCOL),
            source_digests=dict(data.get("source_digests") or {}),
            nodes=tuple(
                DependencyNode.from_dict(item) for item in data.get("nodes") or ()
            ),
            edges=tuple(
                DependencyEdge.from_dict(item) for item in data.get("edges") or ()
            ),
            metadata=dict(data.get("metadata") or {}),
        )
        expected = data.get("fingerprint")
        if expected is not None and str(expected) != _graph_fingerprint_payload(graph):
            raise ValueError("semantic dependency graph fingerprint mismatch")
        return graph


def _graph_fingerprint_payload(graph: SemanticDependencyGraph) -> str:
    payload = {
        "protocol": graph.protocol,
        "source_digests": dict(graph.source_digests),
        "nodes": [item.to_dict() for item in graph.nodes],
        "edges": [item.to_dict() for item in graph.edges],
        "metadata": dict(graph.metadata),
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _relation_values(
    relations: Iterable[DependencyRelation | str] | None,
) -> set[DependencyRelation] | None:
    if relations is None:
        return None
    return {DependencyRelation(item) for item in relations}
