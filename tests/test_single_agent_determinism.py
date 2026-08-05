from __future__ import annotations

import copy

import pytest

from claim_plane.determinism import (
    build_determinism_record,
    canonical_repository_path,
    verify_determinism_record,
)


def _run_inputs() -> dict:
    start_git = {
        "head_commit": "a" * 40,
        "head_tree": "b" * 40,
        "branch": "main",
        "status_sha256": "1" * 64,
        "diff_sha256": "2" * 64,
        "untracked": {},
        "digest": "3" * 64,
    }
    result_git = {
        **start_git,
        "status_sha256": "4" * 64,
        "diff_sha256": "5" * 64,
        "digest": "6" * 64,
    }
    changes = {
        "protocol": "claim-plane.change-summary.v1",
        "available": True,
        "base_commit": "a" * 40,
        "result_commit": "a" * 40,
        "file_count": 1,
        "total_additions": 1,
        "total_deletions": 1,
        "total_hunks": 1,
        "files": [
            {
                "path": "src/app.py",
                "status": "M",
                "additions": 1,
                "deletions": 1,
                "binary": False,
                "patch_sha256": "7" * 64,
                "hunks": [
                    {"old_start": 1, "old_lines": 1, "new_start": 1, "new_lines": 1}
                ],
            }
        ],
        "digest": "8" * 64,
    }
    return {
        "task_sha256": "9" * 64,
        "adapter_name": "codex",
        "manifest_digest": "a" * 64,
        "handshake": {
            "adapter": "codex",
            "adapter_version": "4",
            "negotiated_protocol_version": "1",
            "runtime_name": "codex",
            "runtime_version": "0.146.0",
        },
        "policy_name": "guarded",
        "effective_policy": {"protocol": "claim-plane.policy.v1", "digest": "b" * 64},
        "start_git": start_git,
        "result_git": result_git,
        "changes": changes,
        "acceptance": {
            "protocol": "claim-plane.acceptance-summary.v1",
            "commands": ["python -m pytest"],
            "command_count": 1,
            "passed": True,
        },
        "completion": {
            "verified": True,
            "acceptance_passed": True,
            "errors": 0,
            "executed_violations": 0,
        },
        "scope": {"final": ["src/app.py"]},
        "lifecycle": {"valid": True, "head_digest": "c" * 64, "event_count": 9},
        "risk": {"final_action": "ALLOW"},
        "outcome": "VERIFIED",
        "error": None,
    }


def test_canonical_repository_path_is_stable_and_fail_closed() -> None:
    assert canonical_repository_path("./src\\app.py") == "src/app.py"
    with pytest.raises(ValueError):
        canonical_repository_path("../outside.py")
    with pytest.raises(ValueError):
        canonical_repository_path("/absolute.py")


def test_same_inputs_produce_same_candidate_and_verdict_digests() -> None:
    inputs = _run_inputs()
    first = build_determinism_record(**inputs)
    reordered = copy.deepcopy(inputs)
    reordered["effective_policy"] = {
        "digest": "b" * 64,
        "protocol": "claim-plane.policy.v1",
    }
    second = build_determinism_record(**reordered)

    assert first["digest"] == second["digest"]
    assert first["candidate"]["digest"] == second["candidate"]["digest"]
    assert first["verdict"]["digest"] == second["verdict"]["digest"]
    assert first["completeness"]["complete"] is True
    assert first["verdict"]["reason_code"] == "verified"


def test_policy_or_candidate_change_changes_decision_identity() -> None:
    inputs = _run_inputs()
    baseline = build_determinism_record(**inputs)

    policy_changed = copy.deepcopy(inputs)
    policy_changed["effective_policy"]["digest"] = "d" * 64
    policy_record = build_determinism_record(**policy_changed)
    assert policy_record["input_digest"] != baseline["input_digest"]
    assert policy_record["verdict"]["digest"] != baseline["verdict"]["digest"]

    candidate_changed = copy.deepcopy(inputs)
    candidate_changed["changes"]["digest"] = "e" * 64
    candidate_record = build_determinism_record(**candidate_changed)
    assert candidate_record["candidate"]["digest"] != baseline["candidate"]["digest"]


def test_completeness_fails_when_changed_path_is_not_in_final_scope() -> None:
    inputs = _run_inputs()
    inputs["scope"] = {"final": ["tests/test_app.py"]}
    record = build_determinism_record(**inputs)

    assert record["completeness"]["complete"] is False
    assert "scope_binding_incomplete" in record["verdict"]["finding_codes"]


def test_tampered_run_record_is_detected() -> None:
    inputs = _run_inputs()
    record = build_determinism_record(**inputs)
    run = {
        "task_sha256": inputs["task_sha256"],
        "adapter": inputs["adapter_name"],
        "manifest_digest": inputs["manifest_digest"],
        "handshake": inputs["handshake"],
        "policy": inputs["policy_name"],
        "effective_policy": inputs["effective_policy"],
        "start_git": inputs["start_git"],
        "result_git": inputs["result_git"],
        "changes": inputs["changes"],
        "acceptance": inputs["acceptance"],
        "completion": inputs["completion"],
        "scope": inputs["scope"],
        "lifecycle": inputs["lifecycle"],
        "risk": inputs["risk"],
        "outcome": inputs["outcome"],
        "error": inputs["error"],
        "determinism": record,
    }
    assert verify_determinism_record(run)["valid"] is True

    run["changes"] = {**inputs["changes"], "digest": "f" * 64}
    verification = verify_determinism_record(run)
    assert verification["valid"] is False
    assert {item["code"] for item in verification["findings"]} >= {
        "record_digest_mismatch",
        "candidate_digest_mismatch",
        "verdict_digest_mismatch",
    }


def test_verified_outcome_is_downgraded_when_deterministic_evidence_is_incomplete() -> (
    None
):
    from claim_plane.controlled_run import (
        ControlledRunOutcome,
        GitState,
        _apply_deterministic_gate,
    )

    inputs = _run_inputs()
    start = GitState(**inputs["start_git"])
    result = GitState(**inputs["result_git"])
    outcome, error, record = _apply_deterministic_gate(
        task_sha256=inputs["task_sha256"],
        adapter_name=inputs["adapter_name"],
        manifest_digest=inputs["manifest_digest"],
        handshake=inputs["handshake"],
        policy_name=inputs["policy_name"],
        effective_policy=inputs["effective_policy"],
        start_git=start,
        result_git=result,
        changes=inputs["changes"],
        acceptance=inputs["acceptance"],
        completion=inputs["completion"],
        scope={"final": []},
        lifecycle=inputs["lifecycle"],
        risk=inputs["risk"],
        outcome=ControlledRunOutcome.VERIFIED,
        error=None,
    )

    assert outcome is ControlledRunOutcome.REVIEW_REQUIRED
    assert error is not None
    assert error["code"] == "deterministic_evidence_incomplete"
    assert "scope_evidence_missing" in error["finding_codes"]
    assert record["verdict"]["reason_code"] == "deterministic_evidence_incomplete"
