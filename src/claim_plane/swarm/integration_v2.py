"""Deterministic integration preflight and post-apply semantic verification.

The integration layer treats a worker snapshot as untrusted input.  Before a Git
replay can become part of the managed integration branch, Claim Plane derives the
actual changed paths/regions, maps Python hunks back to structural owners, checks
that those owners remain inside admitted authority, and re-checks semantic overlap
against already integrated work.  After Git applies the patch, the same authority
surface is reconstructed from the staged result so line movement cannot bypass the
semantic gate.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from claim_plane.coordination.admission import parse_line_region
from claim_plane.core import (
    ResourceKind,
    SemanticChange,
    SemanticChangeKind,
    SemanticConflictKind,
    build_python_repository_dependency_graph,
    classify_semantic_conflict,
    extract_python_structure,
    normalize_resource_ref,
)
from claim_plane.core.models import ChangedRegion, IntentOperation
from claim_plane.integration.collector import _parse_changed_regions
from claim_plane.swarm.admission import SharedAdmissionPlan
from claim_plane.swarm.models import SwarmSession, WorkItem

DETERMINISTIC_INTEGRATION_PROTOCOL = "claim-plane.deterministic-integration.v2"


class IntegrationDisposition(str, Enum):
    APPLY = "apply"
    REJECT = "reject"


class IntegrationReason(str, Enum):
    AUTHORITY_VERIFIED = "authority_verified"
    UNDECLARED_PATH = "undeclared_path"
    REGION_VIOLATION = "region_violation"
    SEMANTIC_SCOPE_VIOLATION = "semantic_scope_violation"
    STRUCTURAL_EXTRACTION_FAILED = "structural_extraction_failed"
    ACTUAL_SEMANTIC_CONFLICT = "actual_semantic_conflict"
    ACTUAL_SEMANTIC_UNKNOWN = "actual_semantic_unknown"
    STAGED_PATH_MISMATCH = "staged_path_mismatch"
    STAGED_SEMANTIC_MISMATCH = "staged_semantic_mismatch"


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=False
    )


def _git_text(root: Path, *args: str) -> str:
    result = _git(root, *args)
    if result.returncode != 0:
        raise ValueError(
            result.stderr.strip() or result.stdout.strip() or "git command failed"
        )
    return result.stdout


def _normal_path(value: str) -> str:
    text = value.replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def _operation_path(operation: IntentOperation) -> str | None:
    resource = operation.resource
    if resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}:
        return _normal_path(resource.identifier)
    value = (
        resource.metadata.get("path")
        or resource.metadata.get("file")
        or resource.metadata.get("source_path")
        or resource.metadata.get("repository_path")
    )
    return None if value is None else _normal_path(str(value))


def _covers_path(operation: IntentOperation, path: str) -> bool:
    resource = operation.resource
    candidate = _operation_path(operation)
    if candidate is None:
        return False
    if resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}:
        return resource.covers_path(path)
    if any(ch in candidate for ch in "*?["):
        import fnmatch

        return fnmatch.fnmatchcase(path, candidate)
    return candidate == path


def _committed_mutations(item: WorkItem) -> tuple[IntentOperation, ...]:
    return tuple(
        operation
        for operation in item.operations
        if operation.committed and operation.mutating
    )


def _semantic_operations(item: WorkItem, path: str) -> tuple[IntentOperation, ...]:
    semantic_kinds = {ResourceKind.SYMBOL, ResourceKind.CONTRACT, ResourceKind.SCHEMA}
    return tuple(
        operation
        for operation in _committed_mutations(item)
        if operation.resource.kind in semantic_kinds
        and _operation_path(operation) == path
    )


def _admitted_semantic_identities(item: WorkItem, path: str) -> set[str]:
    identities: set[str] = set()
    for operation in _semantic_operations(item, path):
        normalized = normalize_resource_ref(operation.resource)
        identities.add(normalized.identity)
        if normalized.parent_identity:
            identities.add(normalized.parent_identity)
        qualified = (
            normalized.qualified_name
            or operation.resource.metadata.get("qualified_identifier")
            or operation.resource.identifier
        )
        if operation.resource.kind is ResourceKind.CONTRACT and qualified:
            identities.add(f"symbol:{path}#{str(qualified).strip()}")
    return identities


def _commit_parent(root: Path, commit: str) -> str:
    return _git_text(root, "rev-parse", f"{commit}^").strip().lower()


def _commit_regions(root: Path, base: str, commit: str) -> tuple[ChangedRegion, ...]:
    patch = _git_text(root, "diff", "--no-color", "--unified=0", base, commit, "--")
    return tuple(_parse_changed_regions(patch))


def _commit_paths(root: Path, base: str, commit: str) -> tuple[str, ...]:
    output = _git_text(root, "diff", "--name-only", base, commit, "--")
    return tuple(
        sorted({_normal_path(line) for line in output.splitlines() if line.strip()})
    )


def _show_file(root: Path, revision: str, path: str) -> str | None:
    result = _git(root, "show", f"{revision}:{path}")
    if result.returncode != 0:
        return None
    return result.stdout


def _worktree_file(root: Path, path: str) -> str | None:
    target = root / path
    if not target.exists() or not target.is_file():
        return None
    return target.read_text(encoding="utf-8")


def _owners_for_regions(
    *,
    path: str,
    regions: Iterable[ChangedRegion],
    after_source: str | None,
    before_source: str | None,
) -> tuple[str, ...]:
    if not path.endswith((".py", ".pyi")):
        return ()
    after_index = (
        None
        if after_source is None
        else extract_python_structure(after_source, path=path)
    )
    before_index = (
        None
        if before_source is None
        else extract_python_structure(before_source, path=path)
    )
    owners: set[str] = set()
    for region in regions:
        if region.path != path:
            continue
        if after_index is not None and region.start_line > 0:
            start = max(1, region.start_line)
            end = max(start, region.end_line)
            owners.update(
                item.identity for item in after_index.owners_for_region(start, end)
            )
        elif before_index is not None and region.old_start_line is not None:
            start = max(1, int(region.old_start_line))
            end = max(start, int(region.old_end_line or region.old_start_line))
            owners.update(
                item.identity for item in before_index.owners_for_region(start, end)
            )
    return tuple(sorted(owners))


def _semantic_kind(identity: str) -> SemanticChangeKind:
    return (
        SemanticChangeKind.CONTRACT
        if identity.startswith("contract:")
        else SemanticChangeKind.IMPLEMENTATION
    )


def _changes(identities: Iterable[str]) -> tuple[SemanticChange, ...]:
    return tuple(
        SemanticChange(identity=identity, kind=_semantic_kind(identity))
        for identity in sorted(set(identities))
    )


def _region_authorized(operation: IntentOperation, region: ChangedRegion) -> bool:
    if not _covers_path(operation, region.path):
        return False
    declared = operation.resource.region
    if not declared:
        return True
    parsed = parse_line_region(declared)
    if parsed is None:
        return False
    if region.start_line <= 0:
        old_start = region.old_start_line
        old_end = region.old_end_line
        if old_start is None or old_end is None:
            return False
        return parsed[0] <= old_start and old_end <= parsed[1]
    return parsed[0] <= region.start_line and region.end_line <= parsed[1]


@dataclass(frozen=True, slots=True)
class ActualMutationSurface:
    base_commit: str
    commit: str
    changed_paths: tuple[str, ...]
    changed_regions: tuple[Mapping[str, Any], ...]
    semantic_roots: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_commit": self.base_commit,
            "commit": self.commit,
            "changed_paths": list(self.changed_paths),
            "changed_regions": [dict(item) for item in self.changed_regions],
            "semantic_roots": list(self.semantic_roots),
        }


@dataclass(frozen=True, slots=True)
class DeterministicIntegrationEvidence:
    work_id: str
    source: ActualMutationSurface
    integration_head_before: str
    disposition: IntegrationDisposition
    reasons: tuple[IntegrationReason, ...]
    authority_violations: tuple[Mapping[str, Any], ...] = ()
    semantic_checks: tuple[Mapping[str, Any], ...] = ()
    graph_fingerprint: str | None = None
    staged_paths: tuple[str, ...] = ()
    staged_semantic_roots: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = DETERMINISTIC_INTEGRATION_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != DETERMINISTIC_INTEGRATION_PROTOCOL:
            raise ValueError(
                f"unsupported deterministic integration protocol {self.protocol!r}"
            )
        object.__setattr__(
            self, "disposition", IntegrationDisposition(self.disposition)
        )
        object.__setattr__(
            self, "reasons", tuple(IntegrationReason(item) for item in self.reasons)
        )
        object.__setattr__(
            self,
            "authority_violations",
            tuple(dict(item) for item in self.authority_violations),
        )
        object.__setattr__(
            self, "semantic_checks", tuple(dict(item) for item in self.semantic_checks)
        )
        object.__setattr__(self, "staged_paths", tuple(sorted(set(self.staged_paths))))
        object.__setattr__(
            self,
            "staged_semantic_roots",
            tuple(sorted(set(self.staged_semantic_roots))),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def allowed(self) -> bool:
        return self.disposition is IntegrationDisposition.APPLY

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "protocol": self.protocol,
            "work_id": self.work_id,
            "source": self.source.to_dict(),
            "integration_head_before": self.integration_head_before,
            "disposition": self.disposition.value,
            "reasons": [item.value for item in self.reasons],
            "authority_violations": [dict(item) for item in self.authority_violations],
            "semantic_checks": [dict(item) for item in self.semantic_checks],
            "graph_fingerprint": self.graph_fingerprint,
            "staged_paths": list(self.staged_paths),
            "staged_semantic_roots": list(self.staged_semantic_roots),
            "metadata": dict(self.metadata),
        }
        if include_fingerprint:
            payload["fingerprint"] = self.fingerprint
        return payload

    @property
    def fingerprint(self) -> str:
        raw = _canonical(self.to_dict(include_fingerprint=False))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def inspect_actual_mutation_surface(
    root: Path,
    *,
    base_commit: str,
    commit: str,
) -> ActualMutationSurface:
    paths = _commit_paths(root, base_commit, commit)
    regions = _commit_regions(root, base_commit, commit)
    roots: set[str] = set()
    for path in paths:
        path_regions = tuple(item for item in regions if item.path == path)
        roots.update(
            _owners_for_regions(
                path=path,
                regions=path_regions,
                after_source=_show_file(root, commit, path),
                before_source=_show_file(root, base_commit, path),
            )
        )
    return ActualMutationSurface(
        base_commit=base_commit,
        commit=commit,
        changed_paths=paths,
        changed_regions=tuple(item.to_dict() for item in regions),
        semantic_roots=tuple(sorted(roots)),
    )


def _authority_violations(
    item: WorkItem, surface: ActualMutationSurface
) -> list[dict[str, Any]]:
    operations = _committed_mutations(item)
    violations: list[dict[str, Any]] = []
    for path in surface.changed_paths:
        if not any(_covers_path(operation, path) for operation in operations):
            violations.append(
                {"reason": IntegrationReason.UNDECLARED_PATH.value, "path": path}
            )
    regions = tuple(ChangedRegion.from_dict(item) for item in surface.changed_regions)
    for region in regions:
        matching = [
            operation
            for operation in operations
            if _covers_path(operation, region.path)
        ]
        bounded = [operation for operation in matching if operation.resource.region]
        if bounded and not any(
            _region_authorized(operation, region) for operation in bounded
        ):
            violations.append(
                {
                    "reason": IntegrationReason.REGION_VIOLATION.value,
                    "path": region.path,
                    "start_line": region.start_line,
                    "end_line": region.end_line,
                }
            )
    # Use the already-derived roots, grouped by their embedded path coordinate.
    for path in surface.changed_paths:
        semantic_ops = _semantic_operations(item, path)
        if not semantic_ops:
            continue
        admitted = _admitted_semantic_identities(item, path)
        actual = {
            identity
            for identity in surface.semantic_roots
            if identity.startswith(f"symbol:{path}#")
            or identity == f"file:{path}"
            or identity.startswith(f"contract:{path}#")
        }
        for identity in sorted(actual - admitted):
            violations.append(
                {
                    "reason": IntegrationReason.SEMANTIC_SCOPE_VIOLATION.value,
                    "path": path,
                    "semantic_identity": identity,
                    "admitted": sorted(admitted),
                }
            )
    return violations


def _semantic_rechecks(
    root: Path,
    *,
    admission: SharedAdmissionPlan,
    work_id: str,
    current_surface: ActualMutationSurface,
    integration_head: str,
    integrated_entries: Iterable[Any],
) -> tuple[list[dict[str, Any]], str | None, list[IntegrationReason]]:
    if not current_surface.semantic_roots:
        return [], None, []
    graph = build_python_repository_dependency_graph(root)
    checks: list[dict[str, Any]] = []
    reasons: list[IntegrationReason] = []
    current_admission = admission.admission_map[work_id]
    for entry in integrated_entries:
        if entry.source_commit is None or entry.integration_commit is None:
            continue
        prior_base = _commit_parent(root, entry.integration_commit)
        prior_surface = inspect_actual_mutation_surface(
            root, base_commit=prior_base, commit=entry.integration_commit
        )
        if not prior_surface.semantic_roots:
            continue
        # Different files are independent at integration time unless the dependency
        # graph proves a semantic relationship between their actual mutation roots.
        decision = classify_semantic_conflict(
            graph,
            _changes(prior_surface.semantic_roots),
            _changes(current_surface.semantic_roots),
            left_id=entry.work_id,
            right_id=work_id,
        )
        declared_dependency = entry.work_id in current_admission.effective_dependencies
        refreshed_on_current_head = current_surface.base_commit == integration_head
        allowed = decision.kind in {
            SemanticConflictKind.INDEPENDENT,
            SemanticConflictKind.COMMUTATIVE,
        }
        if decision.kind in {
            SemanticConflictKind.ORDERED,
            SemanticConflictKind.CONFLICTING,
        }:
            allowed = declared_dependency and refreshed_on_current_head
        checks.append(
            {
                "prior_work_id": entry.work_id,
                "kind": decision.kind.value,
                "order": None if decision.order is None else decision.order.value,
                "decision_fingerprint": decision.fingerprint,
                "declared_dependency": declared_dependency,
                "source_base_matches_integration_head": refreshed_on_current_head,
                "allowed": allowed,
            }
        )
        if not allowed:
            reasons.append(
                IntegrationReason.ACTUAL_SEMANTIC_UNKNOWN
                if decision.kind is SemanticConflictKind.UNKNOWN
                else IntegrationReason.ACTUAL_SEMANTIC_CONFLICT
            )
    return checks, graph.fingerprint, reasons


def build_integration_preflight(
    integration_root: Path,
    *,
    session: SwarmSession,
    admission: SharedAdmissionPlan,
    work_id: str,
    source_commit: str,
    integration_head: str,
    integrated_entries: Iterable[Any],
) -> DeterministicIntegrationEvidence:
    source_base = _commit_parent(integration_root, source_commit)
    surface = inspect_actual_mutation_surface(
        integration_root, base_commit=source_base, commit=source_commit
    )
    item = session.work_graph.item_map[work_id]
    violations = _authority_violations(item, surface)
    semantic_checks: list[dict[str, Any]] = []
    graph_fingerprint: str | None = None
    reasons: list[IntegrationReason] = [
        IntegrationReason(item["reason"]) for item in violations
    ]
    disposition = (
        IntegrationDisposition.REJECT if reasons else IntegrationDisposition.APPLY
    )
    if not reasons:
        reasons = [IntegrationReason.AUTHORITY_VERIFIED]
    return DeterministicIntegrationEvidence(
        work_id=work_id,
        source=surface,
        integration_head_before=integration_head,
        disposition=disposition,
        reasons=tuple(dict.fromkeys(reasons)),
        authority_violations=tuple(violations),
        semantic_checks=tuple(semantic_checks),
        graph_fingerprint=graph_fingerprint,
        metadata={
            "source_base_matches_integration_head": source_base == integration_head,
            "ordering": "effective-dependencies-before-replay",
        },
    )


def verify_staged_integration(
    integration_root: Path,
    *,
    item: WorkItem,
    admission: SharedAdmissionPlan,
    integrated_entries: Iterable[Any],
    evidence: DeterministicIntegrationEvidence,
) -> DeterministicIntegrationEvidence:
    patch = _git_text(
        integration_root, "diff", "--cached", "--no-color", "--unified=0", "--"
    )
    regions = tuple(_parse_changed_regions(patch))
    paths = tuple(
        sorted(
            {
                _normal_path(line)
                for line in _git_text(
                    integration_root, "diff", "--cached", "--name-only", "--"
                ).splitlines()
                if line.strip()
            }
        )
    )
    roots: set[str] = set()
    structural_failed = False
    for path in paths:
        if not path.endswith((".py", ".pyi")):
            continue
        source = _worktree_file(integration_root, path)
        before = _show_file(integration_root, evidence.integration_head_before, path)
        try:
            roots.update(
                _owners_for_regions(
                    path=path,
                    regions=tuple(item for item in regions if item.path == path),
                    after_source=source,
                    before_source=before,
                )
            )
        except Exception:
            structural_failed = True
            break

    reasons: list[IntegrationReason] = [
        reason
        for reason in evidence.reasons
        if reason is not IntegrationReason.AUTHORITY_VERIFIED
    ]
    violations = list(evidence.authority_violations)
    if paths != evidence.source.changed_paths:
        reasons.append(IntegrationReason.STAGED_PATH_MISMATCH)
        violations.append(
            {
                "reason": IntegrationReason.STAGED_PATH_MISMATCH.value,
                "source_paths": list(evidence.source.changed_paths),
                "staged_paths": list(paths),
            }
        )
    if structural_failed:
        reasons.append(IntegrationReason.STRUCTURAL_EXTRACTION_FAILED)
    else:
        for path in paths:
            semantic_ops = _semantic_operations(item, path)
            if not semantic_ops:
                continue
            admitted = _admitted_semantic_identities(item, path)
            actual = {
                identity
                for identity in roots
                if identity.startswith(f"symbol:{path}#") or identity == f"file:{path}"
            }
            for identity in sorted(actual - admitted):
                reasons.append(IntegrationReason.STAGED_SEMANTIC_MISMATCH)
                violations.append(
                    {
                        "reason": IntegrationReason.STAGED_SEMANTIC_MISMATCH.value,
                        "path": path,
                        "semantic_identity": identity,
                        "admitted": sorted(admitted),
                    }
                )
    semantic_checks: list[dict[str, Any]] = [
        dict(item) for item in evidence.semantic_checks
    ]
    graph_fingerprint = evidence.graph_fingerprint
    if not reasons and roots:
        staged_surface = ActualMutationSurface(
            base_commit=evidence.source.base_commit,
            commit=evidence.source.commit,
            changed_paths=paths,
            changed_regions=tuple(item.to_dict() for item in regions),
            semantic_roots=tuple(sorted(roots)),
        )
        semantic_checks, graph_fingerprint, semantic_reasons = _semantic_rechecks(
            integration_root,
            admission=admission,
            work_id=evidence.work_id,
            current_surface=staged_surface,
            integration_head=evidence.integration_head_before,
            integrated_entries=integrated_entries,
        )
        reasons.extend(semantic_reasons)
    reasons = list(dict.fromkeys(reasons))
    if not reasons:
        reasons = [IntegrationReason.AUTHORITY_VERIFIED]
    return DeterministicIntegrationEvidence(
        work_id=evidence.work_id,
        source=evidence.source,
        integration_head_before=evidence.integration_head_before,
        disposition=(
            IntegrationDisposition.APPLY
            if reasons == [IntegrationReason.AUTHORITY_VERIFIED]
            else IntegrationDisposition.REJECT
        ),
        reasons=tuple(reasons),
        authority_violations=tuple(violations),
        semantic_checks=tuple(semantic_checks),
        graph_fingerprint=graph_fingerprint,
        staged_paths=paths,
        staged_semantic_roots=tuple(sorted(roots)),
        metadata={**dict(evidence.metadata), "post_apply_recheck": True},
    )
