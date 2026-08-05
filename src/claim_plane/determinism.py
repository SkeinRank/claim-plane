"""Deterministic single-agent candidate and verdict binding.

This module deliberately excludes wall-clock timestamps, repository absolute paths,
and raw agent prose from decision identity. The same task, base state, candidate,
policy, adapter contract, acceptance definition, and lifecycle head therefore produce
the same decision digest.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import posixpath
import re
import unicodedata
from typing import Any, Mapping

DETERMINISM_RECORD_PROTOCOL = "claim-plane.single-agent-determinism.v1"
CANDIDATE_IDENTITY_PROTOCOL = "claim-plane.candidate-identity.v1"
POLICY_SNAPSHOT_PROTOCOL = "claim-plane.policy-snapshot.v1"
ADAPTER_SNAPSHOT_PROTOCOL = "claim-plane.adapter-snapshot.v1"
ACCEPTANCE_SNAPSHOT_PROTOCOL = "claim-plane.acceptance-snapshot.v1"
LIFECYCLE_SNAPSHOT_PROTOCOL = "claim-plane.lifecycle-snapshot.v1"
VERDICT_PROTOCOL = "claim-plane.deterministic-verdict.v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_ID_RE = re.compile(r"^[0-9a-f]{40,64}$")


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest_mapping(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def canonical_repository_path(value: str) -> str:
    """Return one stable repository-relative POSIX path or raise ValueError."""

    normalized = unicodedata.normalize("NFC", value.strip()).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    clean = posixpath.normpath(normalized)
    if (
        not clean
        or clean in {".", ".."}
        or clean.startswith("../")
        or clean.startswith("/")
        or re.match(r"^[A-Za-z]:/", clean)
    ):
        raise ValueError("repository path must remain relative to the project root")
    return clean


def _digest_snapshot(protocol: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    unsigned = {"protocol": protocol, **dict(payload)}
    return {**unsigned, "digest": digest_mapping(unsigned)}


def build_policy_snapshot(
    name: str, effective_policy: Mapping[str, Any]
) -> dict[str, Any]:
    return _digest_snapshot(
        POLICY_SNAPSHOT_PROTOCOL,
        {"name": name, "effective": dict(effective_policy)},
    )


def build_adapter_snapshot(
    adapter: str,
    manifest_digest: str,
    handshake: Mapping[str, Any],
) -> dict[str, Any]:
    safe_handshake = {
        key: handshake.get(key)
        for key in (
            "adapter",
            "adapter_version",
            "negotiated_protocol_version",
            "runtime_name",
            "runtime_version",
        )
        if key in handshake
    }
    return _digest_snapshot(
        ADAPTER_SNAPSHOT_PROTOCOL,
        {
            "adapter": adapter,
            "manifest_digest": manifest_digest,
            "handshake": safe_handshake,
        },
    )


def build_acceptance_snapshot(acceptance: Mapping[str, Any]) -> dict[str, Any]:
    commands = [
        str(item)
        for item in acceptance.get("commands") or ()
        if isinstance(item, str) and item.strip()
    ]
    return _digest_snapshot(
        ACCEPTANCE_SNAPSHOT_PROTOCOL,
        {
            "commands": commands,
            "command_count": len(commands),
        },
    )


def build_lifecycle_snapshot(lifecycle: Mapping[str, Any] | None) -> dict[str, Any]:
    source = lifecycle if isinstance(lifecycle, Mapping) else {}
    return _digest_snapshot(
        LIFECYCLE_SNAPSHOT_PROTOCOL,
        {
            "available": bool(source),
            "valid": bool(source.get("valid")),
            "head_digest": source.get("head_digest"),
            "event_count": int(source.get("event_count") or 0),
        },
    )


def build_candidate_identity(
    *,
    task_sha256: str,
    start_git: Mapping[str, Any],
    result_git: Mapping[str, Any],
    changes: Mapping[str, Any],
) -> dict[str, Any]:
    paths: list[str] = []
    for item in changes.get("files") or ():
        if not isinstance(item, Mapping):
            continue
        path = item.get("path")
        if isinstance(path, str):
            paths.append(canonical_repository_path(path))
    start_state = {
        "head_commit": start_git.get("head_commit"),
        "head_tree": start_git.get("head_tree"),
        "status_sha256": start_git.get("status_sha256"),
        "diff_sha256": start_git.get("diff_sha256"),
        "untracked": dict(start_git.get("untracked") or {}),
    }
    result_state = {
        "head_commit": result_git.get("head_commit"),
        "head_tree": result_git.get("head_tree"),
        "status_sha256": result_git.get("status_sha256"),
        "diff_sha256": result_git.get("diff_sha256"),
        "untracked": dict(result_git.get("untracked") or {}),
    }
    unsigned = {
        "protocol": CANDIDATE_IDENTITY_PROTOCOL,
        "task_sha256": task_sha256,
        "base_commit": start_git.get("head_commit"),
        "base_tree": start_git.get("head_tree"),
        "start_state_digest": digest_mapping(start_state),
        "result_state_digest": digest_mapping(result_state),
        "change_digest": changes.get("digest"),
        "changed_paths": sorted(dict.fromkeys(paths)),
    }
    return {**unsigned, "digest": digest_mapping(unsigned)}


def _scope_matches(path: str, selector: str) -> bool:
    raw = selector[:-3] if selector.endswith("/**") else selector
    clean = canonical_repository_path(raw)
    if selector.endswith("/**"):
        return path == clean or path.startswith(clean + "/")
    return fnmatch.fnmatchcase(path, canonical_repository_path(selector))


def _completion_findings(
    *,
    outcome: str,
    candidate: Mapping[str, Any],
    policy: Mapping[str, Any],
    adapter: Mapping[str, Any],
    acceptance_snapshot: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    completion: Mapping[str, Any],
    changes: Mapping[str, Any],
    scope: Mapping[str, Any],
    lifecycle: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []

    def require(condition: bool, code: str, message: str) -> None:
        if not condition:
            findings.append({"code": code, "message": message})

    require(
        bool(_SHA256_RE.fullmatch(str(candidate.get("task_sha256") or ""))),
        "task_digest_missing",
        "task digest is unavailable or malformed",
    )
    require(
        bool(_GIT_ID_RE.fullmatch(str(candidate.get("base_commit") or ""))),
        "base_commit_missing",
        "base commit is unavailable or malformed",
    )
    require(
        bool(_SHA256_RE.fullmatch(str(candidate.get("change_digest") or ""))),
        "change_digest_missing",
        "change summary digest is unavailable or malformed",
    )
    require(
        bool(_SHA256_RE.fullmatch(str(candidate.get("result_state_digest") or ""))),
        "result_state_missing",
        "result repository state is unavailable",
    )
    require(
        bool(_SHA256_RE.fullmatch(str(policy.get("digest") or ""))),
        "policy_snapshot_missing",
        "effective policy snapshot is unavailable",
    )
    require(
        bool(_SHA256_RE.fullmatch(str(adapter.get("digest") or ""))),
        "adapter_snapshot_missing",
        "adapter snapshot is unavailable",
    )
    require(
        bool(_SHA256_RE.fullmatch(str(acceptance_snapshot.get("digest") or ""))),
        "acceptance_snapshot_missing",
        "acceptance definition snapshot is unavailable",
    )
    require(
        changes.get("base_commit") == candidate.get("base_commit"),
        "base_binding_mismatch",
        "change summary is bound to a different base commit",
    )

    changed_paths = [
        str(item)
        for item in candidate.get("changed_paths") or ()
        if isinstance(item, str)
    ]
    final_scope = [
        str(item) for item in scope.get("final") or () if isinstance(item, str)
    ]
    if changed_paths:
        require(
            bool(final_scope),
            "scope_evidence_missing",
            "changed files have no final scope evidence",
        )
        for path in changed_paths:
            try:
                covered = any(
                    _scope_matches(path, selector) for selector in final_scope
                )
            except ValueError:
                covered = False
            require(
                covered,
                "scope_binding_incomplete",
                f"changed path {path!r} is not covered by final scope",
            )

    if outcome == "VERIFIED" or completion.get("verified") is True:
        require(
            bool(completion.get("verified")),
            "completion_not_verified",
            "completion payload is not verified",
        )
        require(
            bool(acceptance.get("passed")),
            "acceptance_not_passed",
            "authoritative acceptance did not pass",
        )
        require(
            isinstance(lifecycle, Mapping) and bool(lifecycle.get("valid")),
            "lifecycle_not_valid",
            "verified delivery lacks a valid lifecycle stream",
        )
    return findings


def deterministic_reason_code(
    *,
    outcome: str,
    error: Mapping[str, Any] | None,
    completion: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    risk: Mapping[str, Any],
) -> str:
    error_code = str((error or {}).get("code") or "")
    if error_code:
        return error_code
    if outcome == "VERIFIED":
        return "verified"
    if outcome == "CANCELLED":
        return "cancelled"
    if outcome == "TIMED_OUT":
        return "timeout"
    if outcome == "FAILED":
        return "runtime_failed"
    if str(risk.get("final_action") or "") == "DENY":
        return "risk_policy_denied"
    if str(risk.get("final_action") or "") == "REVIEW_REQUIRED":
        return "risk_review_required"
    if int(completion.get("executed_violations") or 0) > 0:
        return "executed_violation"
    classification = str(acceptance.get("classification") or "").strip().lower()
    if classification and classification not in {"pass", "command_failed"}:
        return f"acceptance_{classification}"
    if int(completion.get("errors") or 0) > 0:
        return "verification_error"
    if not completion.get("acceptance_passed"):
        return "acceptance_not_passed"
    return "manual_review_required"


def build_determinism_record(
    *,
    task_sha256: str,
    adapter_name: str,
    manifest_digest: str,
    handshake: Mapping[str, Any],
    policy_name: str,
    effective_policy: Mapping[str, Any],
    start_git: Mapping[str, Any],
    result_git: Mapping[str, Any],
    changes: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    completion: Mapping[str, Any],
    scope: Mapping[str, Any],
    lifecycle: Mapping[str, Any] | None,
    risk: Mapping[str, Any],
    outcome: str,
    error: Mapping[str, Any] | None,
) -> dict[str, Any]:
    candidate = build_candidate_identity(
        task_sha256=task_sha256,
        start_git=start_git,
        result_git=result_git,
        changes=changes,
    )
    snapshots = {
        "policy": build_policy_snapshot(policy_name, effective_policy),
        "adapter": build_adapter_snapshot(adapter_name, manifest_digest, handshake),
        "acceptance": build_acceptance_snapshot(acceptance),
        "lifecycle": build_lifecycle_snapshot(lifecycle),
    }
    findings = _completion_findings(
        outcome=outcome,
        candidate=candidate,
        policy=snapshots["policy"],
        adapter=snapshots["adapter"],
        acceptance_snapshot=snapshots["acceptance"],
        acceptance=acceptance,
        completion=completion,
        changes=changes,
        scope=scope,
        lifecycle=lifecycle,
    )
    input_unsigned = {
        "task_sha256": task_sha256,
        "candidate_digest": candidate["digest"],
        "policy_digest": snapshots["policy"]["digest"],
        "adapter_digest": snapshots["adapter"]["digest"],
        "acceptance_digest": snapshots["acceptance"]["digest"],
        "lifecycle_digest": snapshots["lifecycle"]["digest"],
    }
    input_digest = digest_mapping(input_unsigned)
    verdict_unsigned = {
        "protocol": VERDICT_PROTOCOL,
        "outcome": outcome,
        "reason_code": deterministic_reason_code(
            outcome=outcome,
            error=error,
            completion=completion,
            acceptance=acceptance,
            risk=risk,
        ),
        "candidate_digest": candidate["digest"],
        "input_digest": input_digest,
        "complete": not findings,
        "finding_codes": sorted({item["code"] for item in findings}),
    }
    verdict = {**verdict_unsigned, "digest": digest_mapping(verdict_unsigned)}
    unsigned = {
        "protocol": DETERMINISM_RECORD_PROTOCOL,
        "candidate": candidate,
        "snapshots": snapshots,
        "input_digest": input_digest,
        "completeness": {"complete": not findings, "findings": findings},
        "verdict": verdict,
    }
    return {**unsigned, "digest": digest_mapping(unsigned)}


def verify_determinism_record(run: Mapping[str, Any]) -> dict[str, Any]:
    stored = run.get("determinism")
    if not isinstance(stored, Mapping):
        return {
            "available": False,
            "valid": False,
            "findings": [
                {
                    "code": "determinism_unavailable",
                    "message": "run predates deterministic verdict binding",
                }
            ],
        }
    recomputed = build_determinism_record(
        task_sha256=str(run.get("task_sha256") or ""),
        adapter_name=str(run.get("adapter") or ""),
        manifest_digest=str(run.get("manifest_digest") or ""),
        handshake=dict(run.get("handshake") or {}),
        policy_name=str(run.get("policy") or ""),
        effective_policy=dict(run.get("effective_policy") or {}),
        start_git=dict(run.get("start_git") or {}),
        result_git=dict(run.get("result_git") or {}),
        changes=dict(run.get("changes") or {}),
        acceptance=dict(run.get("acceptance") or {}),
        completion=dict(run.get("completion") or {}),
        scope=dict(run.get("scope") or {}),
        lifecycle=(
            dict(run.get("lifecycle") or {})
            if isinstance(run.get("lifecycle"), Mapping)
            else None
        ),
        risk=dict(run.get("risk") or {}),
        outcome=str(run.get("outcome") or ""),
        error=(
            dict(run.get("error") or {})
            if isinstance(run.get("error"), Mapping)
            else None
        ),
    )
    findings: list[dict[str, str]] = []
    for key, expected in (
        ("record", recomputed.get("digest")),
        ("input", recomputed.get("input_digest")),
        ("candidate", (recomputed.get("candidate") or {}).get("digest")),
        ("verdict", (recomputed.get("verdict") or {}).get("digest")),
    ):
        if key == "record":
            actual = stored.get("digest")
        elif key == "input":
            actual = stored.get("input_digest")
        else:
            actual = (
                (stored.get(key) or {}).get("digest")
                if isinstance(stored.get(key), Mapping)
                else None
            )
        if actual != expected:
            findings.append(
                {
                    "code": f"{key}_digest_mismatch",
                    "message": (
                        f"stored {key} digest does not match canonical run inputs"
                    ),
                }
            )
    return {
        "available": True,
        "valid": not findings,
        "findings": findings,
        "stored_digest": stored.get("digest"),
        "recomputed_digest": recomputed.get("digest"),
        "input_digest": recomputed.get("input_digest"),
        "candidate_digest": (recomputed.get("candidate") or {}).get("digest"),
        "verdict_digest": (recomputed.get("verdict") or {}).get("digest"),
    }
