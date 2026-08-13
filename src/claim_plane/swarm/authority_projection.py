"""Symbol-scoped authority projection for swarm admission analysis.

The work graph remains the source of mutation authority.  This module derives a
narrower *admission projection* only when exact symbol evidence is already present
in the declaration or when a bounded file region is fully enclosed by one
repository symbol in the pinned semantic graph.  The original operations remain
unchanged for worker execution and final verification.

Dependency-aware expansion or narrowing is intentionally out of scope here.  A
symbol projection may only use the declared path/region and graph-backed symbol
coordinates; it cannot add transitive dependencies or infer undeclared mutation
surfaces.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping

from claim_plane.coordination.admission import parse_line_region
from claim_plane.core import (
    AccessMode,
    IntentOperation,
    ResourceKind,
    ResourceRef,
    SemanticDependencyGraph,
    normalize_resource_ref,
)
from claim_plane.swarm.models import WorkGraph, WorkItem

SYMBOL_SCOPED_AUTHORITY_PROJECTION_PROTOCOL = (
    "claim-plane.symbol-scoped-authority-projection.v2"
)


class SymbolProjectionSource(str, Enum):
    """Evidence source proving one projected symbol authority."""

    DECLARED_SYMBOL = "declared_symbol"
    REGION_ENCLOSED_SYMBOL = "region_enclosed_symbol"


class SymbolProjectionReason(str, Enum):
    """Stable reason describing why a broad surface was narrowed or preserved."""

    EXACT_DECLARED_SYMBOL = "exact_declared_symbol"
    UNIQUE_REGION_SYMBOL = "unique_region_symbol"
    NO_SEMANTIC_GRAPH = "no_semantic_graph"
    NO_EXACT_SYMBOL_EVIDENCE = "no_exact_symbol_evidence"
    AMBIGUOUS_REGION = "ambiguous_region"
    DESTRUCTIVE_ACCESS = "destructive_access"
    PATTERN_PATH = "pattern_path"
    NON_MUTATING_SURFACE = "non_mutating_surface"


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _normal_path(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if not text:
        return None
    normalized = posixpath.normpath(text)
    return None if normalized in {"", "."} else normalized


def _operation_path(operation: IntentOperation) -> str | None:
    resource = operation.resource
    if resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}:
        return _normal_path(resource.identifier)
    return _normal_path(
        resource.metadata.get("path")
        or resource.metadata.get("file")
        or resource.metadata.get("source_path")
        or resource.metadata.get("repository_path")
    )


def _operation_key(operation: IntentOperation) -> str:
    return hashlib.sha256(_canonical_json(operation.to_dict())).hexdigest()


def _provider_for_node(node: Any, graph: SemanticDependencyGraph) -> str:
    value = (
        node.metadata.get("evidence_provider")
        or node.resource.metadata.get("evidence_provider")
        or graph.metadata.get("code_intelligence_provider")
        or graph.metadata.get("provider_id")
        or graph.metadata.get("language")
        or "semantic-graph"
    )
    return str(value)


def _exact_declared_symbol_node(
    operation: IntentOperation,
    *,
    path: str,
    graph: SemanticDependencyGraph,
) -> Any | None:
    if operation.resource.kind is not ResourceKind.SYMBOL:
        return None
    normalized = normalize_resource_ref(operation.resource)
    candidates = [normalized.identity]
    qualified = normalized.qualified_name or operation.resource.metadata.get(
        "qualified_identifier"
    )
    if qualified:
        candidates.append(f"symbol:{path}#{str(qualified).strip()}")
    for identity in dict.fromkeys(candidates):
        node = graph.node(identity)
        if (
            node is not None
            and not node.external
            and node.resource.kind is ResourceKind.SYMBOL
            and _normal_path(node.resource.path) == path
        ):
            return node
    return None


def _region_symbol_node(
    operation: IntentOperation,
    *,
    path: str,
    graph: SemanticDependencyGraph,
) -> tuple[Any | None, bool]:
    region = parse_line_region(operation.resource.region or "")
    if region is None:
        return None, False
    start, end = region
    candidates: list[tuple[int, int, str, Any]] = []
    for node in graph.nodes:
        resource = node.resource
        if (
            node.external
            or resource.kind is not ResourceKind.SYMBOL
            or _normal_path(resource.path) != path
        ):
            continue
        symbol_region = parse_line_region(resource.region or "")
        if symbol_region is None:
            continue
        symbol_start, symbol_end = symbol_region
        if symbol_start <= start and end <= symbol_end:
            candidates.append(
                (symbol_end - symbol_start, symbol_start, node.identity, node)
            )
    if not candidates:
        return None, False
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    best_span = candidates[0][0]
    best = [item for item in candidates if item[0] == best_span]
    if len(best) != 1:
        return None, True
    return best[0][3], False


def _symbol_operation_from_node(
    source: IntentOperation,
    *,
    path: str,
    node: Any,
) -> IntentOperation:
    resource = node.resource
    qualified = resource.qualified_name or resource.identifier
    metadata = {
        **dict(resource.metadata),
        "path": path,
        "qualified_identifier": qualified,
        "authority_projection_source": (
            SymbolProjectionSource.REGION_ENCLOSED_SYMBOL.value
        ),
        "authority_projection_identity": resource.identity,
    }
    return IntentOperation(
        access=source.access,
        resource=ResourceRef(
            kind=ResourceKind.SYMBOL,
            identifier=qualified,
            signature=resource.signature,
            region=resource.region,
            concept_id=resource.concept_id,
            subject_concept_id=resource.subject_concept_id,
            metadata=metadata,
        ),
        required=source.required,
        commitment=source.commitment,
        metadata={
            **dict(source.metadata),
            "authority_projection": "symbol-scoped-v2",
            "source_file_region": source.resource.region,
        },
    )


@dataclass(frozen=True, slots=True)
class SymbolProjectionEvidence:
    """One exact graph-backed reason for projecting a file surface to a symbol."""

    path: str
    source: SymbolProjectionSource
    reason: SymbolProjectionReason
    symbol_identity: str
    symbol_qualified_name: str
    symbol_region: str | None
    provider: str
    source_region: str | None = None

    def __post_init__(self) -> None:
        path = _normal_path(self.path)
        if path is None:
            raise ValueError("symbol projection evidence path must not be empty")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "source", SymbolProjectionSource(self.source))
        object.__setattr__(self, "reason", SymbolProjectionReason(self.reason))
        for name in ("symbol_identity", "symbol_qualified_name", "provider"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"{name} must not be empty")
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "source": self.source.value,
            "reason": self.reason.value,
            "symbol_identity": self.symbol_identity,
            "symbol_qualified_name": self.symbol_qualified_name,
            "symbol_region": self.symbol_region,
            "provider": self.provider,
            "source_region": self.source_region,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SymbolProjectionEvidence":
        return cls(
            path=str(data["path"]),
            source=SymbolProjectionSource(data["source"]),
            reason=SymbolProjectionReason(data["reason"]),
            symbol_identity=str(data["symbol_identity"]),
            symbol_qualified_name=str(data["symbol_qualified_name"]),
            symbol_region=(
                str(data["symbol_region"])
                if data.get("symbol_region") is not None
                else None
            ),
            provider=str(data["provider"]),
            source_region=(
                str(data["source_region"])
                if data.get("source_region") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkItemAuthorityProjection:
    """Projected admission authority for one work item.

    ``projected_operations`` are used only for pairwise shared-admission conflict
    analysis. ``analysis_operations`` keep original file carriers and add any
    graph-proven symbol synthesized from a bounded region, allowing existing
    same-file policy and semantic conflict classification to remain authoritative.
    """

    work_id: str
    projected_operations: tuple[IntentOperation, ...]
    analysis_operations: tuple[IntentOperation, ...]
    evidence: tuple[SymbolProjectionEvidence, ...] = ()
    preserved_reasons: tuple[SymbolProjectionReason, ...] = ()
    removed_file_operation_count: int = 0
    synthesized_symbol_operation_count: int = 0

    def __post_init__(self) -> None:
        work_id = self.work_id.strip()
        if not work_id:
            raise ValueError("work_id must not be empty")
        object.__setattr__(self, "work_id", work_id)
        object.__setattr__(
            self,
            "projected_operations",
            tuple(
                item
                if isinstance(item, IntentOperation)
                else IntentOperation.from_dict(item)
                for item in self.projected_operations
            ),
        )
        object.__setattr__(
            self,
            "analysis_operations",
            tuple(
                item
                if isinstance(item, IntentOperation)
                else IntentOperation.from_dict(item)
                for item in self.analysis_operations
            ),
        )
        object.__setattr__(
            self,
            "evidence",
            tuple(
                item
                if isinstance(item, SymbolProjectionEvidence)
                else SymbolProjectionEvidence.from_dict(item)
                for item in self.evidence
            ),
        )
        object.__setattr__(
            self,
            "preserved_reasons",
            tuple(
                sorted(
                    set(
                        SymbolProjectionReason(item)
                        for item in self.preserved_reasons
                    ),
                    key=lambda item: item.value,
                )
            ),
        )
        if (
            self.removed_file_operation_count < 0
            or self.synthesized_symbol_operation_count < 0
        ):
            raise ValueError("projection operation counts must be non-negative")

    @property
    def narrowed(self) -> bool:
        return self.removed_file_operation_count > 0

    @property
    def projected_symbol_identities(self) -> tuple[str, ...]:
        return tuple(sorted({item.symbol_identity for item in self.evidence}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "narrowed": self.narrowed,
            "removed_file_operation_count": self.removed_file_operation_count,
            "synthesized_symbol_operation_count": (
                self.synthesized_symbol_operation_count
            ),
            "projected_symbol_identities": list(self.projected_symbol_identities),
            "projected_operations": [
                item.to_dict() for item in self.projected_operations
            ],
            "analysis_operations": [
                item.to_dict() for item in self.analysis_operations
            ],
            "evidence": [item.to_dict() for item in self.evidence],
            "preserved_reasons": [item.value for item in self.preserved_reasons],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkItemAuthorityProjection":
        result = cls(
            work_id=str(data["work_id"]),
            projected_operations=tuple(
                IntentOperation.from_dict(item)
                for item in data.get("projected_operations") or ()
            ),
            analysis_operations=tuple(
                IntentOperation.from_dict(item)
                for item in data.get("analysis_operations") or ()
            ),
            evidence=tuple(
                SymbolProjectionEvidence.from_dict(item)
                for item in data.get("evidence") or ()
            ),
            preserved_reasons=tuple(
                SymbolProjectionReason(item)
                for item in data.get("preserved_reasons") or ()
            ),
            removed_file_operation_count=int(
                data.get("removed_file_operation_count") or 0
            ),
            synthesized_symbol_operation_count=int(
                data.get("synthesized_symbol_operation_count") or 0
            ),
        )
        if "narrowed" in data and bool(data["narrowed"]) != result.narrowed:
            raise ValueError("symbol authority projection narrowed flag mismatch")
        supplied = tuple(
            str(item) for item in data.get("projected_symbol_identities") or ()
        )
        if supplied and supplied != result.projected_symbol_identities:
            raise ValueError("projected symbol identities do not match evidence")
        return result


@dataclass(frozen=True, slots=True)
class SymbolScopedAuthorityProjectionReport:
    """Source-bound symbol authority projection for one work graph."""

    work_graph_fingerprint: str
    items: tuple[WorkItemAuthorityProjection, ...]
    semantic_graph_fingerprint: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SYMBOL_SCOPED_AUTHORITY_PROJECTION_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SYMBOL_SCOPED_AUTHORITY_PROJECTION_PROTOCOL:
            raise ValueError(
                f"unsupported symbol authority projection {self.protocol!r}"
            )
        for name in ("work_graph_fingerprint", "semantic_graph_fingerprint"):
            value = getattr(self, name)
            if value is None and name == "semantic_graph_fingerprint":
                continue
            text = str(value).lower()
            if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
            object.__setattr__(self, name, text)
        items = tuple(
            item
            if isinstance(item, WorkItemAuthorityProjection)
            else WorkItemAuthorityProjection.from_dict(item)
            for item in self.items
        )
        if len({item.work_id for item in items}) != len(items):
            raise ValueError("symbol authority projection work ids must be unique")
        object.__setattr__(
            self, "items", tuple(sorted(items, key=lambda item: item.work_id))
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def item_map(self) -> dict[str, WorkItemAuthorityProjection]:
        return {item.work_id: item for item in self.items}

    @property
    def fingerprint(self) -> str:
        return _sha256(self.to_dict(include_fingerprint=False))

    def _summary_core(self) -> dict[str, Any]:
        return {
            "work_item_count": len(self.items),
            "narrowed_work_items": sum(1 for item in self.items if item.narrowed),
            "preserved_work_items": sum(1 for item in self.items if not item.narrowed),
            "removed_file_operations": sum(
                item.removed_file_operation_count for item in self.items
            ),
            "synthesized_symbol_operations": sum(
                item.synthesized_symbol_operation_count for item in self.items
            ),
            "projected_symbol_count": len(
                {
                    identity
                    for item in self.items
                    for identity in item.projected_symbol_identities
                }
            ),
        }

    def summary(self) -> dict[str, Any]:
        return {**self._summary_core(), "fingerprint": self.fingerprint}

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload = {
            "protocol": self.protocol,
            "work_graph_fingerprint": self.work_graph_fingerprint,
            "semantic_graph_fingerprint": self.semantic_graph_fingerprint,
            "items": [item.to_dict() for item in self.items],
            "summary": self.summary() if include_fingerprint else self._summary_core(),
            "metadata": dict(self.metadata),
        }
        if include_fingerprint:
            return {"fingerprint": self.fingerprint, **payload}
        return payload

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any]
    ) -> "SymbolScopedAuthorityProjectionReport":
        report = cls(
            protocol=str(
                data.get("protocol") or SYMBOL_SCOPED_AUTHORITY_PROJECTION_PROTOCOL
            ),
            work_graph_fingerprint=str(data["work_graph_fingerprint"]),
            semantic_graph_fingerprint=(
                str(data["semantic_graph_fingerprint"])
                if data.get("semantic_graph_fingerprint") is not None
                else None
            ),
            items=tuple(
                WorkItemAuthorityProjection.from_dict(item)
                for item in data.get("items") or ()
            ),
            metadata=dict(data.get("metadata") or {}),
        )
        supplied = data.get("fingerprint")
        if supplied is not None and str(supplied) != report.fingerprint:
            raise ValueError("symbol authority projection fingerprint mismatch")
        summary = data.get("summary")
        if isinstance(summary, Mapping):
            expected = report.summary()
            for key, value in expected.items():
                if key in summary and summary[key] != value:
                    raise ValueError("symbol authority projection summary mismatch")
        return report


def _dedupe_operations(
    operations: list[IntentOperation],
) -> tuple[IntentOperation, ...]:
    by_key = {_operation_key(item): item for item in operations}
    return tuple(by_key[key] for key in sorted(by_key))


def _project_item(
    item: WorkItem,
    semantic_graph: SemanticDependencyGraph | None,
) -> WorkItemAuthorityProjection:
    if semantic_graph is None:
        return WorkItemAuthorityProjection(
            work_id=item.work_id,
            projected_operations=item.operations,
            analysis_operations=item.operations,
            preserved_reasons=(SymbolProjectionReason.NO_SEMANTIC_GRAPH,),
        )

    exact_symbols_by_path: dict[str, list[tuple[IntentOperation, Any]]] = {}
    unresolved_symbol_paths: set[str] = set()
    for operation in item.operations:
        if (
            not operation.committed
            or not operation.mutating
            or operation.resource.kind is not ResourceKind.SYMBOL
        ):
            continue
        path = _operation_path(operation)
        if path is None:
            continue
        node = _exact_declared_symbol_node(operation, path=path, graph=semantic_graph)
        if node is None:
            unresolved_symbol_paths.add(path)
        else:
            exact_symbols_by_path.setdefault(path, []).append((operation, node))

    projected: list[IntentOperation] = []
    analysis: list[IntentOperation] = list(item.operations)
    evidence: list[SymbolProjectionEvidence] = []
    preserved: set[SymbolProjectionReason] = set()
    removed = 0
    synthesized = 0

    for operation in item.operations:
        resource = operation.resource
        if (
            not operation.committed
            or resource.kind is not ResourceKind.FILE
            or not operation.mutating
        ):
            projected.append(operation)
            if not operation.mutating:
                preserved.add(SymbolProjectionReason.NON_MUTATING_SURFACE)
            continue
        path = _operation_path(operation)
        if path is None:
            projected.append(operation)
            preserved.add(SymbolProjectionReason.NO_EXACT_SYMBOL_EVIDENCE)
            continue
        if resource.is_pattern:
            projected.append(operation)
            preserved.add(SymbolProjectionReason.PATTERN_PATH)
            continue
        if operation.access.destructive:
            projected.append(operation)
            preserved.add(SymbolProjectionReason.DESTRUCTIVE_ACCESS)
            continue

        if resource.region:
            node, ambiguous = _region_symbol_node(
                operation, path=path, graph=semantic_graph
            )
            if node is None:
                projected.append(operation)
                preserved.add(
                    SymbolProjectionReason.AMBIGUOUS_REGION
                    if ambiguous
                    else SymbolProjectionReason.NO_EXACT_SYMBOL_EVIDENCE
                )
                continue
            symbol_operation = _symbol_operation_from_node(
                operation, path=path, node=node
            )
            projected.append(symbol_operation)
            analysis.append(symbol_operation)
            removed += 1
            synthesized += 1
            evidence.append(
                SymbolProjectionEvidence(
                    path=path,
                    source=SymbolProjectionSource.REGION_ENCLOSED_SYMBOL,
                    reason=SymbolProjectionReason.UNIQUE_REGION_SYMBOL,
                    symbol_identity=node.identity,
                    symbol_qualified_name=(
                        node.resource.qualified_name or node.resource.identifier
                    ),
                    symbol_region=node.resource.region,
                    provider=_provider_for_node(node, semantic_graph),
                    source_region=resource.region,
                )
            )
            continue

        exact = exact_symbols_by_path.get(path, ())
        if exact and path not in unresolved_symbol_paths:
            removed += 1
            for _, node in exact:
                evidence.append(
                    SymbolProjectionEvidence(
                        path=path,
                        source=SymbolProjectionSource.DECLARED_SYMBOL,
                        reason=SymbolProjectionReason.EXACT_DECLARED_SYMBOL,
                        symbol_identity=node.identity,
                        symbol_qualified_name=(
                            node.resource.qualified_name or node.resource.identifier
                        ),
                        symbol_region=node.resource.region,
                        provider=_provider_for_node(node, semantic_graph),
                    )
                )
            continue

        projected.append(operation)
        preserved.add(SymbolProjectionReason.NO_EXACT_SYMBOL_EVIDENCE)

    # Explicit symbol operations are already present in ``projected`` through the
    # non-file branch above. Region-derived symbols are synthesized exactly once.
    return WorkItemAuthorityProjection(
        work_id=item.work_id,
        projected_operations=_dedupe_operations(projected),
        analysis_operations=_dedupe_operations(analysis),
        evidence=tuple(
            sorted(
                {
                    json.dumps(item.to_dict(), sort_keys=True): item
                    for item in evidence
                }.values(),
                key=lambda item: (
                    item.path,
                    item.symbol_identity,
                    item.source.value,
                    item.source_region or "",
                ),
            )
        ),
        preserved_reasons=tuple(preserved),
        removed_file_operation_count=removed,
        synthesized_symbol_operation_count=synthesized,
    )


def build_symbol_scoped_authority_projection(
    graph: WorkGraph,
    semantic_graph: SemanticDependencyGraph | None,
) -> SymbolScopedAuthorityProjectionReport:
    """Project declared broad file carriers onto exact graph-backed symbols.

    The returned report is bound to the original work graph and semantic graph.
    No dependency closure is consulted, so this stage cannot infer transitive
    authority; that belongs to the later dependency-aware narrowing stage.
    """

    return SymbolScopedAuthorityProjectionReport(
        work_graph_fingerprint=graph.fingerprint(),
        semantic_graph_fingerprint=(
            semantic_graph.fingerprint if semantic_graph is not None else None
        ),
        items=tuple(_project_item(item, semantic_graph) for item in graph.work_items),
        metadata={
            "projection_scope": "admission-analysis-only",
            "mutation_authority_preserved": True,
            "dependency_aware_narrowing": False,
            "fail_closed": True,
        },
    )


def projected_analysis_graph(
    graph: WorkGraph,
    report: SymbolScopedAuthorityProjectionReport,
) -> WorkGraph:
    """Return an internal work graph augmented with graph-proven symbol evidence."""

    if report.work_graph_fingerprint != graph.fingerprint():
        raise ValueError("symbol authority projection is stale for the work graph")
    item_map = report.item_map
    items = tuple(
        replace(item, operations=item_map[item.work_id].analysis_operations)
        for item in graph.work_items
    )
    return WorkGraph(
        work_items=items, metadata=dict(graph.metadata), protocol=graph.protocol
    )


def projected_operations_for_work(
    report: SymbolScopedAuthorityProjectionReport,
    work_id: str,
) -> tuple[IntentOperation, ...]:
    """Return the narrowed pairwise admission surface for one work item."""

    try:
        return report.item_map[work_id].projected_operations
    except KeyError as exc:
        raise KeyError(f"projection has no work item {work_id!r}") from exc


__all__ = [
    "SYMBOL_SCOPED_AUTHORITY_PROJECTION_PROTOCOL",
    "SymbolProjectionSource",
    "SymbolProjectionReason",
    "SymbolProjectionEvidence",
    "WorkItemAuthorityProjection",
    "SymbolScopedAuthorityProjectionReport",
    "build_symbol_scoped_authority_projection",
    "projected_analysis_graph",
    "projected_operations_for_work",
]
