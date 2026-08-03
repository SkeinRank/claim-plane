from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claim_plane.connectors import codex
from claim_plane.connectors.codex_guard import GuardEvaluation, MutationRequest
from claim_plane.core import AccessMode
from claim_plane.policy import (
    POLICY_PROTOCOL,
    EffectivePolicy,
    PolicyAction,
    PolicyPreset,
    PreWriteMode,
    RiskLevel,
    resolve_policy,
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "claim-plane@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Claim Plane Tests"],
        cwd=repo,
        check=True,
    )
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def test_public_presets_have_stable_distinct_semantics() -> None:
    observe = PolicyPreset.named("observe")
    guarded = PolicyPreset.named("guarded")
    strict = PolicyPreset.named("strict")
    critical = PolicyPreset.named("critical")

    assert observe.pre_write_mode is PreWriteMode.OBSERVE
    assert guarded.pre_write_mode is PreWriteMode.ENFORCE
    assert guarded.scope_expansion_action is PolicyAction.ALLOW
    assert strict.unknown_action is PolicyAction.DENY
    assert strict.risk_actions[RiskLevel.CRITICAL] is PolicyAction.DENY
    assert critical.human_gate is True
    assert critical.auto_merge_allowed is False


def test_project_rules_raise_risk_and_explain_the_policy_action() -> None:
    effective = resolve_policy(
        "guarded",
        risk={
            "default": "low",
            "include_builtin_rules": False,
            "rules": [
                {
                    "match": "src/auth/**",
                    "level": "critical",
                    "reason": "authentication boundary",
                }
            ],
        },
        source="test",
    )

    payload = effective.classify_many(["docs/guide.md", "src/auth/token.py"])

    assert effective.to_dict()["protocol"] == POLICY_PROTOCOL
    assert payload["highest_risk"] == "critical"
    assert payload["final_action"] == "REVIEW_REQUIRED"
    auth = next(item for item in payload["findings"] if item["path"].startswith("src/"))
    assert auth["level"] == "critical"
    assert "authentication boundary" in auth["explanation"]
    assert effective.digest() == resolve_policy(
        "guarded",
        risk={
            "default": "low",
            "include_builtin_rules": False,
            "rules": [
                {
                    "match": "src/auth/**",
                    "level": "critical",
                    "reason": "authentication boundary",
                }
            ],
        },
        source="test",
    ).digest()


def test_observe_records_would_deny_without_weakening_control_invariants(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    codex.init_project(repo)
    session: dict[str, object] = {"controlled_policy": "observe"}
    denied = GuardEvaluation(
        allowed=False,
        mutating=True,
        tool_name="apply_patch",
        classification="repository_mutation",
        reason_code="outside_admitted_scope",
        reason="not admitted",
        mutations=(MutationRequest(AccessMode.WRITE, "app.py"),),
    )

    observed = codex._apply_guard_policy(repo, session, denied)

    assert observed.allowed is True
    assert observed.reason_code == "observe_would_outside_admitted_scope"
    assert session["observe_would_deny_calls"] == 1

    invariant = GuardEvaluation(
        allowed=False,
        mutating=True,
        tool_name="apply_patch",
        classification="control_boundary",
        reason_code="protected_control_path",
        reason="protected",
        mutations=(MutationRequest(AccessMode.WRITE, ".claim-plane/config.yaml"),),
    )
    still_denied = codex._apply_guard_policy(repo, session, invariant)
    assert still_denied.allowed is False


def test_guarded_marks_high_risk_mutation_for_review(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    codex.init_project(repo)
    session: dict[str, object] = {"controlled_policy": "guarded"}
    allowed = GuardEvaluation(
        allowed=True,
        mutating=True,
        tool_name="apply_patch",
        classification="repository_mutation",
        reason_code="authorized",
        reason="admitted",
        mutations=(MutationRequest(AccessMode.WRITE, "pyproject.toml"),),
    )

    adjusted = codex._apply_guard_policy(repo, session, allowed)

    assert adjusted.allowed is True
    assert session["policy_review_required"] is True
    risk = session["guard_last_risk"]
    assert isinstance(risk, dict)
    assert risk["highest_risk"] == "high"
    assert risk["final_action"] == "REVIEW_REQUIRED"


def test_effective_policy_round_trip_rejects_tampered_semantics() -> None:
    effective = resolve_policy("guarded", source="test")
    payload = effective.to_dict()

    assert EffectivePolicy.from_dict(payload).digest() == effective.digest()

    tampered = dict(payload)
    tampered["preset"] = dict(payload["preset"])
    tampered["preset"]["human_gate"] = True
    with pytest.raises(ValueError, match="canonical semantics"):
        EffectivePolicy.from_dict(tampered)


def test_controlled_policy_manifest_pins_risk_rules_across_config_drift(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    codex.init_project(repo)
    pinned = resolve_policy(
        "guarded",
        risk={
            "default": "low",
            "include_builtin_rules": False,
            "rules": [],
        },
        source="controlled_run",
    )
    session: dict[str, object] = {
        "controlled_policy": "guarded",
        "controlled_policy_manifest": pinned.to_dict(),
    }
    config = repo / ".claim-plane/config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "default: medium", "default: critical"
        ),
        encoding="utf-8",
    )

    resolved = codex._session_effective_policy(repo, session)

    assert resolved.digest() == pinned.digest()
    assert resolved.risk.default is RiskLevel.LOW
