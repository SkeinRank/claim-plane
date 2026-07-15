"""Deterministic intent-to-diff integration verification."""

from __future__ import annotations

import fnmatch
from collections import defaultdict
from typing import Iterable, Mapping

from claim_plane.coordination.admission import parse_line_region
from claim_plane.core.models import (
    ChangeIntent,
    ChangeManifest,
    ChangedRegion,
    FindingCode,
    FindingSeverity,
    IntegrationFinding,
    IntegrationReport,
    ResourceKind,
)


class IntegrationVerifier:
    def verify(
        self,
        intent: ChangeIntent,
        manifest: ChangeManifest,
        *,
        active_intents: Iterable[ChangeIntent] = (),
        dependency_records: Iterable[Mapping[str, object]] = (),
    ) -> IntegrationReport:
        findings: list[IntegrationFinding] = []

        if manifest.intent_id != intent.intent_id:
            findings.append(
                _finding(
                    FindingCode.UNKNOWN_INTENT,
                    f"Manifest targets {manifest.intent_id}, expected {intent.intent_id}.",
                )
            )
        if manifest.owner != intent.owner:
            findings.append(
                _finding(
                    FindingCode.OWNER_MISMATCH,
                    f"Manifest owner {manifest.owner} does not match admitted owner {intent.owner}.",
                )
            )
        pinned_base = intent.base_commit
        manifest_base = manifest.base_commit or manifest.metadata.get(
            "resolved_base_commit"
        )
        if (
            manifest.base_revision != intent.base_revision
            or manifest.metadata.get("base_is_ancestor") is False
            or (pinned_base is not None and manifest_base != pinned_base)
        ):
            expected = pinned_base or intent.base_revision
            findings.append(
                _finding(
                    FindingCode.STALE_BASE,
                    f"Observed work is not based on admitted commit {expected}; rebase and request re-admission.",
                )
            )

        semantic_meta = intent.metadata.get("semantic_resolution") or {}
        if semantic_meta.get("required") and not semantic_meta.get("enabled"):
            findings.append(
                _finding(
                    FindingCode.SEMANTIC_UNAVAILABLE,
                    "Required Agent Lexicon semantic verification was unavailable.",
                )
            )

        for record in dependency_records:
            status = str(record.get("status") or "")
            dependency_id = str(record.get("depends_on_intent_id") or "")
            if status == "missing":
                findings.append(
                    _finding(
                        FindingCode.DEPENDENCY_MISSING,
                        f"Dependency {dependency_id} does not exist.",
                        related=dependency_id,
                    )
                )
            elif status == "stale":
                findings.append(
                    _finding(
                        FindingCode.DEPENDENCY_STALE,
                        f"Premise from {dependency_id} changed; amend and re-admit this intent.",
                        related=dependency_id,
                    )
                )

        path_operations = tuple(
            operation
            for operation in intent.operations
            if operation.mutating
            and operation.resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}
        )
        for path in manifest.changed_files:
            if not any(
                operation.resource.covers_path(path) for operation in path_operations
            ):
                findings.append(
                    _finding(
                        FindingCode.UNDECLARED_CHANGE,
                        f"Changed file {path} is outside the admitted write surface.",
                        path=path,
                    )
                )

        for operation in path_operations:
            resource = operation.resource
            if (
                operation.required
                and not resource.is_pattern
                and not any(
                    resource.covers_path(path) for path in manifest.changed_files
                )
            ):
                findings.append(
                    IntegrationFinding(
                        FindingCode.MISSING_DECLARED_CHANGE,
                        FindingSeverity.ERROR,
                        f"Required resource {resource.identifier} was not changed.",
                        path=resource.identifier,
                    )
                )

        self._verify_regions(intent, manifest, findings)
        self._verify_contracts(intent, manifest, findings)
        self._verify_semantics(intent, manifest, findings)
        self._verify_preserves(intent, manifest, findings)
        self._verify_observed_accesses(intent, manifest, findings)
        self._verify_snapshot_integrity(manifest, findings)
        self._verify_acceptance(intent, manifest, findings)
        self._verify_active_collisions(intent, manifest, active_intents, findings)

        metrics = {
            "changed_files": len(manifest.changed_files),
            "changed_regions": len(manifest.changed_regions),
            "observed_artifacts": len(manifest.artifacts),
            "observed_accesses": len(manifest.observed_accesses),
            "acceptance_commands": len(manifest.acceptance_results),
            "errors": sum(f.severity is FindingSeverity.ERROR for f in findings),
            "warnings": sum(f.severity is FindingSeverity.WARNING for f in findings),
            "declared_operations": len(intent.operations),
        }
        return IntegrationReport(
            intent.intent_id, tuple(_deduplicate(findings)), metrics
        )

    def _verify_regions(
        self,
        intent: ChangeIntent,
        manifest: ChangeManifest,
        findings: list[IntegrationFinding],
    ) -> None:
        bounded = [
            operation
            for operation in intent.operations
            if operation.mutating
            and operation.resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}
            and operation.resource.region
        ]
        if not bounded:
            return
        regions_by_path: dict[str, list[ChangedRegion]] = defaultdict(list)
        for region in manifest.changed_regions:
            regions_by_path[region.path].append(region)
        for operation in bounded:
            allowed = parse_line_region(operation.resource.region or "")
            if allowed is None:
                findings.append(
                    _finding(
                        FindingCode.REGION_VIOLATION,
                        f"Region `{operation.resource.region}` is not a machine-checkable line range.",
                        path=operation.resource.identifier,
                    )
                )
                continue
            matching_paths = [
                path
                for path in manifest.changed_files
                if operation.resource.covers_path(path)
            ]
            for path in matching_paths:
                actual = regions_by_path.get(path, [])
                if not actual:
                    findings.append(
                        _finding(
                            FindingCode.REGION_VIOLATION,
                            f"No Git hunk evidence is available to prove that {path} stayed inside {operation.resource.region}.",
                            path=path,
                        )
                    )
                    continue
                start, end = allowed
                for region in actual:
                    if region.start_line < start or region.end_line > end:
                        findings.append(
                            _finding(
                                FindingCode.REGION_VIOLATION,
                                (
                                    f"Git hunk {region.start_line}-{region.end_line} in {path} exceeds "
                                    f"admitted region {start}-{end}."
                                ),
                                path=path,
                            )
                        )

    def _verify_contracts(
        self,
        intent: ChangeIntent,
        manifest: ChangeManifest,
        findings: list[IntegrationFinding],
    ) -> None:
        artifacts: dict[tuple[str, str], list] = defaultdict(list)
        for artifact in manifest.artifacts:
            if artifact.kind is ResourceKind.CONTRACT:
                artifacts[(artifact.subject_key or "", artifact.key)].append(artifact)

        for operation in intent.operations:
            resource = operation.resource
            if not operation.mutating or resource.kind is not ResourceKind.CONTRACT:
                continue
            key = (resource.subject_key or "", resource.semantic_key)
            candidates = artifacts.get(key, [])
            # Backward-compatible unbound contracts match by identifier only.
            if not candidates and not resource.subject_key:
                candidates = [
                    a
                    for (subject, contract), values in artifacts.items()
                    if contract == resource.semantic_key
                    for a in values
                ]
            if not candidates:
                findings.append(
                    IntegrationFinding(
                        FindingCode.CONTRACT_MISSING,
                        FindingSeverity.ERROR
                        if operation.required
                        else FindingSeverity.WARNING,
                        f"Declared contract {resource.identifier} was not observed in changed code with subject {resource.subject_concept_id or '<unbound>'}.",
                        identifier=resource.identifier,
                    )
                )
                continue
            declared = _norm_signature(resource.signature)
            if declared:
                matching = [
                    candidate
                    for candidate in candidates
                    if _norm_signature(candidate.signature) == declared
                ]
                if not matching:
                    observed = sorted(
                        {
                            _norm_signature(candidate.signature) or "<missing>"
                            for candidate in candidates
                        }
                    )
                    findings.append(
                        IntegrationFinding(
                            FindingCode.CONTRACT_MISMATCH,
                            FindingSeverity.ERROR,
                            f"Observed signatures {observed} do not match declared `{declared}`.",
                            path=candidates[0].path,
                            identifier=resource.identifier,
                        )
                    )

    def _verify_semantics(
        self,
        intent: ChangeIntent,
        manifest: ChangeManifest,
        findings: list[IntegrationFinding],
    ) -> None:
        declared_semantic = {
            operation.resource.semantic_key
            for operation in intent.operations
            if operation.mutating
            and operation.resource.kind
            in {
                ResourceKind.CONCEPT,
                ResourceKind.SYMBOL,
                ResourceKind.CONTRACT,
                ResourceKind.ROUTE,
                ResourceKind.SCHEMA,
                ResourceKind.CONFIG,
            }
        }
        for artifact in manifest.artifacts:
            if artifact.metadata.get("inventory_only"):
                continue
            status = artifact.metadata.get("semantic_status")
            if status == "ambiguous":
                findings.append(
                    _finding(
                        FindingCode.SEMANTIC_DRIFT,
                        f"Observed surface {artifact.identifier} is semantically ambiguous.",
                        path=artifact.path,
                        identifier=artifact.identifier,
                    )
                )
            if artifact.metadata.get("deprecated"):
                findings.append(
                    _finding(
                        FindingCode.SEMANTIC_DRIFT,
                        f"Deprecated terminology `{artifact.identifier}` was introduced or modified.",
                        path=artifact.path,
                        identifier=artifact.identifier,
                    )
                )
            elif (
                artifact.metadata.get("documentation")
                and artifact.metadata.get("surface_kind") == "alias"
            ):
                findings.append(
                    IntegrationFinding(
                        FindingCode.SEMANTIC_DRIFT,
                        FindingSeverity.WARNING,
                        f"Documentation uses alias `{artifact.identifier}` instead of its canonical term.",
                        path=artifact.path,
                        identifier=artifact.identifier,
                    )
                )
            if (
                artifact.concept_id
                and artifact.metadata.get("documentation") is not True
                and artifact.key not in declared_semantic
            ):
                # Only emitted/defined code surfaces are considered undeclared semantic writes.
                if artifact.kind in {ResourceKind.SYMBOL, ResourceKind.CONTRACT}:
                    findings.append(
                        IntegrationFinding(
                            FindingCode.SEMANTIC_DRIFT,
                            FindingSeverity.WARNING,
                            f"Observed semantic artifact {artifact.identifier} ({artifact.concept_id}) was not declared explicitly.",
                            path=artifact.path,
                            identifier=artifact.identifier,
                        )
                    )

    def _verify_preserves(
        self,
        intent: ChangeIntent,
        manifest: ChangeManifest,
        findings: list[IntegrationFinding],
    ) -> None:
        for policy in intent.preserves:
            if policy.startswith("path-unchanged:"):
                pattern = policy.split(":", 1)[1].strip()
                touched = [
                    path
                    for path in manifest.changed_files
                    if fnmatch.fnmatchcase(path, pattern)
                ]
                if touched:
                    findings.append(
                        _finding(
                            FindingCode.PRESERVE_VIOLATION,
                            f"Preserve policy `{policy}` was violated by: {', '.join(touched)}.",
                        )
                    )
            elif policy.startswith("contract:") and "=" in policy:
                body = policy.split(":", 1)[1]
                subject, identifier, expected = _parse_contract_preserve(body)
                matches = [
                    a
                    for a in manifest.artifacts
                    if a.kind is ResourceKind.CONTRACT
                    and (
                        a.identifier == identifier
                        or a.qualified_identifier == identifier
                    )
                    and (subject is None or a.subject_key == _norm_key(subject))
                ]
                if not matches:
                    findings.append(
                        _finding(
                            FindingCode.PRESERVE_VIOLATION,
                            (
                                f"Preserved contract `{identifier}` was not observed; "
                                "deletion or inventory omission is fail-closed."
                            ),
                            identifier=identifier,
                        )
                    )
                elif any(
                    _norm_signature(a.signature) != _norm_signature(expected)
                    for a in matches
                ):
                    findings.append(
                        _finding(
                            FindingCode.PRESERVE_VIOLATION,
                            f"Preserved contract `{identifier}` changed incompatibly.",
                            identifier=identifier,
                        )
                    )

    def _verify_observed_accesses(
        self,
        intent: ChangeIntent,
        manifest: ChangeManifest,
        findings: list[IntegrationFinding],
    ) -> None:
        declared_reads = tuple(
            operation
            for operation in intent.operations
            if operation.access.value == "read"
        )
        declared_writes = tuple(
            operation for operation in intent.operations if operation.mutating
        )
        for access in manifest.observed_accesses:
            resource = access.resource
            if access.mode.value == "read":
                if _access_declared(resource, declared_reads) or _access_declared(
                    resource, declared_writes
                ):
                    continue
                findings.append(
                    IntegrationFinding(
                        FindingCode.UNDECLARED_READ,
                        FindingSeverity.WARNING,
                        f"Observed read of {resource.kind.value}:{resource.identifier} was not declared as a premise.",
                        path=resource.identifier
                        if resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}
                        else None,
                        identifier=resource.identifier,
                    )
                )
            elif not _access_declared(resource, declared_writes):
                findings.append(
                    _finding(
                        FindingCode.UNDECLARED_CHANGE,
                        f"Observed tool write to {resource.kind.value}:{resource.identifier} is outside the admitted write surface.",
                        path=resource.identifier
                        if resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}
                        else None,
                        identifier=resource.identifier,
                    )
                )

    def _verify_snapshot_integrity(
        self,
        manifest: ChangeManifest,
        findings: list[IntegrationFinding],
    ) -> None:
        if manifest.metadata.get("snapshot_integrity_ok") is not False:
            return
        paths = tuple(manifest.metadata.get("acceptance_mutation_paths") or ())
        suffix = f" Mutated paths: {', '.join(paths)}." if paths else ""
        findings.append(
            _finding(
                FindingCode.SNAPSHOT_MUTATION,
                "Worker acceptance mutated the frozen snapshot; the verified patch was not changed, so the worker must repair and re-freeze."
                + suffix,
            )
        )

    def _verify_acceptance(
        self,
        intent: ChangeIntent,
        manifest: ChangeManifest,
        findings: list[IntegrationFinding],
    ) -> None:
        by_command = {result.command: result for result in manifest.acceptance_results}
        for command in intent.acceptance:
            result = by_command.get(command)
            if result is None:
                findings.append(
                    IntegrationFinding(
                        FindingCode.ACCEPTANCE_FAILED,
                        FindingSeverity.WARNING,
                        f"Acceptance command was not executed: {command}",
                        identifier=command,
                    )
                )
            elif not result.passed:
                details = result.stderr_tail.strip() or result.stdout_tail.strip()
                findings.append(
                    _finding(
                        FindingCode.ACCEPTANCE_FAILED,
                        f"Acceptance command failed ({result.returncode}): {command}. {details[-500:]}",
                        identifier=command,
                    )
                )

    def _verify_active_collisions(
        self,
        intent: ChangeIntent,
        manifest: ChangeManifest,
        active_intents: Iterable[ChangeIntent],
        findings: list[IntegrationFinding],
    ) -> None:
        active = tuple(
            item for item in active_intents if item.intent_id != intent.intent_id
        )
        declared_semantic = {
            op.resource.semantic_key for op in intent.operations if op.mutating
        }
        for artifact in manifest.artifacts:
            if not artifact.concept_id:
                continue
            owners = _active_semantic_owners(active, artifact.key)
            if owners and artifact.key not in declared_semantic:
                related_id, related_owner = owners[0]
                findings.append(
                    _finding(
                        FindingCode.CROSS_INTENT_COLLISION,
                        f"Artifact {artifact.identifier} touches concept {artifact.concept_id} owned by {related_id} ({related_owner}) but was not declared.",
                        path=artifact.path,
                        identifier=artifact.identifier,
                        related=related_id,
                    )
                )

        for existing in active:
            for operation in existing.operations:
                if not operation.mutating or operation.resource.kind not in {
                    ResourceKind.FILE,
                    ResourceKind.DOCUMENT,
                }:
                    continue
                for path in manifest.changed_files:
                    if not operation.resource.covers_path(
                        path
                    ) or not _intent_declares_path(intent, path):
                        continue
                    if _actual_regions_disjoint_from_operation(
                        manifest, path, operation.resource.region
                    ):
                        continue
                    # A shared file is valid only when both intents declared disjoint regions.
                    if _declared_regions_disjoint(intent, existing, path):
                        continue
                    findings.append(
                        _finding(
                            FindingCode.CROSS_INTENT_COLLISION,
                            f"{path} enters the write surface of active intent {existing.intent_id} ({existing.owner}).",
                            path=path,
                            related=existing.intent_id,
                        )
                    )

    def verify_batch(
        self,
        intents: Iterable[ChangeIntent],
        manifests: Iterable[ChangeManifest],
        *,
        dependency_records: Mapping[str, Iterable[Mapping[str, object]]] | None = None,
    ) -> dict[str, IntegrationReport]:
        intent_map = {intent.intent_id: intent for intent in intents}
        manifest_map = {manifest.intent_id: manifest for manifest in manifests}
        active = tuple(intent_map.values())
        reports: dict[str, IntegrationReport] = {}
        for intent_id, intent in intent_map.items():
            manifest = manifest_map.get(intent_id)
            if manifest is None:
                reports[intent_id] = IntegrationReport(
                    intent_id,
                    (
                        _finding(
                            FindingCode.UNKNOWN_INTENT,
                            f"No change manifest was supplied for {intent_id}.",
                        ),
                    ),
                    {"errors": 1},
                )
            else:
                reports[intent_id] = self.verify(
                    intent,
                    manifest,
                    active_intents=active,
                    dependency_records=(dependency_records or {}).get(intent_id, ()),
                )

        extras: dict[str, list[IntegrationFinding]] = defaultdict(list)
        manifest_items = list(manifest_map.items())
        for index, (left_id, left) in enumerate(manifest_items):
            for right_id, right in manifest_items[index + 1 :]:
                for path in sorted(set(left.changed_files) & set(right.changed_files)):
                    if _manifest_regions_disjoint(left, right, path):
                        continue
                    message = f"Manifests {left_id} and {right_id} changed overlapping hunks in {path}."
                    extras[left_id].append(
                        _finding(
                            FindingCode.CROSS_INTENT_COLLISION,
                            message,
                            path=path,
                            related=right_id,
                        )
                    )
                    extras[right_id].append(
                        _finding(
                            FindingCode.CROSS_INTENT_COLLISION,
                            message,
                            path=path,
                            related=left_id,
                        )
                    )

        # Dynamic dependency evidence: if one worker actually read a file that
        # another worker changed, the consumer must declare a dependency.
        for consumer_id, consumer_manifest in manifest_map.items():
            consumer_intent = intent_map.get(consumer_id)
            if consumer_intent is None:
                continue
            declared_dependencies = set(consumer_intent.dependencies)
            for access in consumer_manifest.observed_accesses:
                if access.mode.value != "read" or access.resource.kind not in {
                    ResourceKind.FILE,
                    ResourceKind.DOCUMENT,
                }:
                    continue
                path = access.resource.key
                for producer_id, producer_manifest in manifest_map.items():
                    if producer_id == consumer_id or path not in set(
                        producer_manifest.changed_files
                    ):
                        continue
                    if producer_id in declared_dependencies:
                        continue
                    extras[consumer_id].append(
                        _finding(
                            FindingCode.OBSERVED_DEPENDENCY_MISSING,
                            f"Observed read of {path} depends on concurrent writer {producer_id}, but no dependency was declared.",
                            path=path,
                            related=producer_id,
                        )
                    )

        contracts: dict[tuple[str, str], list[tuple[str, str | None, str]]] = (
            defaultdict(list)
        )
        for manifest in manifest_map.values():
            for artifact in manifest.artifacts:
                if artifact.kind is ResourceKind.CONTRACT:
                    contracts[(artifact.subject_key or "", artifact.key)].append(
                        (manifest.intent_id, artifact.signature, artifact.path)
                    )
        for key, entries in contracts.items():
            signatures: set[str] = {
                normalized
                for _, signature, _ in entries
                if (normalized := _norm_signature(signature)) is not None
            }
            if len(signatures) <= 1:
                continue
            for intent_id, signature, path in entries:
                extras[intent_id].append(
                    _finding(
                        FindingCode.CONTRACT_MISMATCH,
                        f"Integrated contract {key} has incompatible signatures: {sorted(signatures)}.",
                        path=path,
                        identifier=signature,
                    )
                )

        for intent_id, additional in extras.items():
            current = reports[intent_id]
            combined = tuple(_deduplicate([*current.findings, *additional]))
            metrics = dict(current.metrics)
            metrics["errors"] = sum(
                f.severity is FindingSeverity.ERROR for f in combined
            )
            metrics["warnings"] = sum(
                f.severity is FindingSeverity.WARNING for f in combined
            )
            reports[intent_id] = IntegrationReport(intent_id, combined, metrics)
        return reports


def _finding(
    code: FindingCode,
    message: str,
    *,
    path: str | None = None,
    identifier: str | None = None,
    related: str | None = None,
) -> IntegrationFinding:
    return IntegrationFinding(
        code, FindingSeverity.ERROR, message, path, identifier, related
    )


def _access_declared(resource, operations) -> bool:
    for operation in operations:
        declared = operation.resource
        if declared.kind != resource.kind:
            continue
        if resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}:
            if declared.covers_path(resource.identifier):
                return True
        elif declared.semantic_key == resource.semantic_key:
            return True
    return False


def _intent_declares_path(intent: ChangeIntent, path: str) -> bool:
    return any(
        operation.mutating
        and operation.resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}
        and operation.resource.covers_path(path)
        for operation in intent.operations
    )


def _declared_regions_disjoint(a: ChangeIntent, b: ChangeIntent, path: str) -> bool:
    a_regions = [
        parse_line_region(op.resource.region or "")
        for op in a.operations
        if op.mutating and op.resource.covers_path(path) and op.resource.region
    ]
    b_regions = [
        parse_line_region(op.resource.region or "")
        for op in b.operations
        if op.mutating and op.resource.covers_path(path) and op.resource.region
    ]
    if (
        not a_regions
        or not b_regions
        or any(region is None for region in [*a_regions, *b_regions])
    ):
        return False
    return all(
        _ranges_disjoint(left, right)
        for left in a_regions
        for right in b_regions
        if left and right
    )


def _actual_regions_disjoint_from_operation(
    manifest: ChangeManifest, path: str, region_text: str | None
) -> bool:
    if not region_text:
        return False
    allowed = parse_line_region(region_text)
    actual = [region for region in manifest.changed_regions if region.path == path]
    if allowed is None or not actual:
        return False
    return all(not region.overlaps(*allowed) for region in actual)


def _manifest_regions_disjoint(a: ChangeManifest, b: ChangeManifest, path: str) -> bool:
    a_regions = [region for region in a.changed_regions if region.path == path]
    b_regions = [region for region in b.changed_regions if region.path == path]
    if not a_regions or not b_regions:
        return False
    return all(
        not left.overlaps(right.start_line, right.end_line)
        for left in a_regions
        for right in b_regions
    )


def _ranges_disjoint(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[1] < b[0] or b[1] < a[0]


def _active_semantic_owners(
    active: Iterable[ChangeIntent], key: str
) -> list[tuple[str, str]]:
    owners = []
    for item in active:
        if any(
            operation.mutating and operation.resource.semantic_key == key
            for operation in item.operations
        ):
            owners.append((item.intent_id, item.owner))
    return owners


def _norm_signature(value: str | None) -> str | None:
    return " ".join(value.split()) if value else None


def _norm_key(value: str) -> str:
    return "".join(ch for ch in value.casefold() if ch.isalnum())


def _parse_contract_preserve(body: str) -> tuple[str | None, str, str]:
    left, expected = body.split("=", 1)
    left = left.strip()
    if "::" in left:
        subject, identifier = left.split("::", 1)
        return subject.strip(), identifier.strip(), expected.strip()
    return None, left, expected.strip()


def _deduplicate(findings: Iterable[IntegrationFinding]) -> list[IntegrationFinding]:
    seen: set[tuple[object, ...]] = set()
    result = []
    for finding in findings:
        marker = (
            finding.code,
            finding.severity,
            finding.message,
            finding.path,
            finding.identifier,
            finding.related_intent_id,
        )
        if marker not in seen:
            seen.add(marker)
            result.append(finding)
    return result
