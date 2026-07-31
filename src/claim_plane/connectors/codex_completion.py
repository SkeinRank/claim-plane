"""Verified completion for project-local Codex sessions."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
from typing import Any, Mapping

from claim_plane.connectors.codex_guard import protected_control_path
from claim_plane.core import Plane
from claim_plane.integration.acceptance import AcceptanceRunner
from claim_plane.integration.snapshot import (
    capture_worktree_tree,
    changed_worktree_paths,
)

CODEX_COMPLETION_PROTOCOL = "claim-plane.codex-completion.v1"


_SCOPE_CODES = {
    "undeclared_change",
    "missing_declared_change",
    "region_violation",
    "undeclared_read",
}
_CONTRACT_CODES = {"contract_missing", "contract_mismatch"}
_PRESERVE_CODES = {"preserve_violation"}
_BASE_CODES = {"stale_base", "owner_mismatch"}

_EXECUTED_VIOLATION_CODES = {
    "undeclared_change",
    "region_violation",
    "contract_mismatch",
    "contract_missing",
    "preserve_violation",
    "snapshot_mutation",
    "undeclared_read",
    "owner_mismatch",
    "stale_base",
}


def _finding_summary(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    findings = report.get("findings") or []
    result: list[dict[str, Any]] = []
    for item in findings:
        if not isinstance(item, Mapping):
            continue
        result.append(
            {
                "code": str(item.get("code") or "unknown"),
                "severity": str(item.get("severity") or "error"),
                "message": str(item.get("message") or ""),
                "path": item.get("path"),
                "identifier": item.get("identifier"),
            }
        )
    return result


def _current_sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _path_fingerprint(path: Path) -> str:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return "missing"
    if path.is_symlink():
        return (
            "symlink:"
            + hashlib.sha256(str(path.readlink()).encode("utf-8")).hexdigest()
        )
    if path.is_file():
        return "file:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if path.is_dir():
        return "dir"
    return f"other:{stat.st_mode}"


def _without_unchanged_preexisting_changes(
    manifest: Any,
    *,
    root: Path,
    baseline: Mapping[str, Any],
) -> Any:
    ignored = tuple(
        path
        for path in manifest.changed_files
        if path in baseline and _path_fingerprint(root / path) == baseline.get(path)
    )
    if not ignored:
        return manifest
    ignored_set = set(ignored)
    return replace(
        manifest,
        changed_files=tuple(
            path for path in manifest.changed_files if path not in ignored_set
        ),
        changed_regions=tuple(
            region
            for region in manifest.changed_regions
            if region.path not in ignored_set
        ),
        artifacts=tuple(
            artifact
            for artifact in manifest.artifacts
            if artifact.path not in ignored_set
        ),
        metadata={
            **manifest.metadata,
            "completion_ignored_preexisting_paths": list(ignored),
        },
    )


def _without_unchanged_connector_control_changes(
    manifest: Any,
    *,
    root: Path,
    baseline: Mapping[str, Any],
) -> Any:
    ignored = tuple(
        path
        for path in manifest.changed_files
        if protected_control_path(path)
        and path in baseline
        and _current_sha256(root / path) == baseline.get(path)
    )
    if not ignored:
        return manifest
    ignored_set = set(ignored)
    return replace(
        manifest,
        changed_files=tuple(
            path for path in manifest.changed_files if path not in ignored_set
        ),
        changed_regions=tuple(
            region
            for region in manifest.changed_regions
            if region.path not in ignored_set
        ),
        artifacts=tuple(
            artifact
            for artifact in manifest.artifacts
            if artifact.path not in ignored_set
        ),
        metadata={
            **manifest.metadata,
            "completion_ignored_connector_paths": list(ignored),
        },
    )


def verify_completion(
    root: Path,
    *,
    intent_id: str,
    run_acceptance: bool = True,
    acceptance_timeout: int = 300,
    connector_control_baseline: Mapping[str, Any] | None = None,
    preexisting_worktree_baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify the current worktree and complete the intent only when evidence is clean."""

    plane = Plane.open(root / ".claim-plane/plane.db")
    try:
        manifest = _without_unchanged_connector_control_changes(
            plane.collect_git_manifest(intent_id, root),
            root=root,
            baseline=connector_control_baseline or {},
        )
        manifest = _without_unchanged_preexisting_changes(
            manifest,
            root=root,
            baseline=preexisting_worktree_baseline or {},
        )
        if run_acceptance:
            intent = plane.intent(intent_id)
            if intent is None:
                raise ValueError(f"unknown intent: {intent_id}")
            tree_before = capture_worktree_tree(root)
            results = AcceptanceRunner(timeout_seconds=acceptance_timeout).run(
                intent.acceptance, root
            )
            tree_after = capture_worktree_tree(root)
            immutable = tree_before == tree_after
            manifest = replace(
                manifest,
                acceptance_results=results,
                metadata={
                    **manifest.metadata,
                    "acceptance_executed": True,
                    "snapshot_integrity_ok": immutable,
                    "snapshot_tree_before_acceptance": tree_before,
                    "snapshot_tree_after_acceptance": tree_after,
                    "acceptance_mutation_paths": (
                        [] if immutable else list(changed_worktree_paths(root))
                    ),
                },
            )
        report_obj = plane.verify_manifest(manifest)
        report = report_obj.to_dict()
        findings = _finding_summary(report)
        if report_obj.clean:
            states = {
                str(item.get("intent_id")): str(item.get("state"))
                for item in plane.intents()
            }
            if states.get(intent_id) != "completed":
                plane.complete(intent_id)
    finally:
        plane.close()

    metrics = dict(report.get("metrics") or {})
    error_findings = [item for item in findings if item["severity"] == "error"]
    acceptance_failures = [
        item for item in findings if item["code"] == "acceptance_failed"
    ]
    executed_violations = [
        item for item in error_findings if item["code"] in _EXECUTED_VIOLATION_CODES
    ]
    error_codes = {item["code"] for item in error_findings}
    return {
        "protocol": CODEX_COMPLETION_PROTOCOL,
        "verified": bool(report.get("clean")),
        "scope_verified": not bool(error_codes & _SCOPE_CODES),
        "contracts_verified": not bool(error_codes & _CONTRACT_CODES),
        "preserves_verified": not bool(error_codes & _PRESERVE_CODES),
        "base_verified": not bool(error_codes & _BASE_CODES),
        "intent_id": intent_id,
        "changed_files": int(metrics.get("changed_files") or 0),
        "changed_paths": list(manifest.changed_files),
        "changed_regions": int(metrics.get("changed_regions") or 0),
        "acceptance_commands": int(metrics.get("acceptance_commands") or 0),
        "acceptance_passed": not acceptance_failures,
        "executed_violations": len(executed_violations),
        "errors": int(metrics.get("errors") or len(error_findings)),
        "warnings": int(metrics.get("warnings") or 0),
        "findings": findings,
        "report": report,
    }


def stop_block_reason(completion: Mapping[str, Any]) -> str:
    """Return a bounded continuation prompt for a failed verified-completion gate."""

    findings = completion.get("findings") or []
    lines = [
        "Claim Plane did not verify completion. Continue working before stopping.",
    ]
    count = 0
    for item in findings:
        if not isinstance(item, Mapping) or item.get("severity") != "error":
            continue
        message = str(item.get("message") or item.get("code") or "verification failed")
        lines.append(f"- {message}")
        count += 1
        if count >= 6:
            break
    if count == 0:
        lines.append(
            "- Verification returned a non-clean result without a detailed error."
        )
    lines.append(
        "Repair the findings within the admitted ChangeIntent, then stop again."
    )
    return "\n".join(lines)
