"""Capability declarations, policy compatibility, and lifecycle evidence."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claim_plane.cli import main
from claim_plane.connectors import CodexAdapter, codex
from claim_plane.protocol import (
    AdapterCapabilityManifest,
    AdapterOperation,
    AdapterPolicyRequirements,
    AdapterRequest,
    CapabilityLevel,
    EnforcementLevel,
    GuaranteeDeclaration,
    GuaranteeProvider,
    LifecycleEventStore,
    RuntimeIdentity,
    evaluate_adapter_policy,
    require_adapter_policy,
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
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
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    codex.init_project(repo)
    codex.connect_codex(repo)
    return repo


def _healthy_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        codex,
        "_codex_version",
        lambda: ("/usr/bin/codex", "codex-cli 0.123.0"),
    )


def test_manifest_round_trip_and_digest_rejects_tampering() -> None:
    manifest = AdapterCapabilityManifest(
        adapter="reference",
        adapter_version="1",
        adapter_protocol_version="1.0",
        runtime=RuntimeIdentity("reference", "1.2.3", True),
        capabilities={"pre_write_blocking": CapabilityLevel.COMPLETE},
        guarantees={
            "undeclared_tool_write": GuaranteeDeclaration(
                EnforcementLevel.HARD_BLOCKED,
                GuaranteeProvider.RUNTIME,
                ("native pre-write hook",),
                required_capability="pre_write_blocking",
            )
        },
    )

    payload = manifest.to_dict()
    assert AdapterCapabilityManifest.from_dict(payload) == manifest
    assert len(payload["digest"]) == 64

    payload["runtime"]["version"] = "9.9.9"
    with pytest.raises(ValueError, match="digest"):
        AdapterCapabilityManifest.from_dict(payload)


def test_partial_capability_cannot_claim_hard_blocking() -> None:
    with pytest.raises(ValueError, match="cannot be HARD_BLOCKED"):
        AdapterCapabilityManifest(
            adapter="unsafe",
            adapter_version="1",
            adapter_protocol_version="1.0",
            runtime=RuntimeIdentity("unsafe"),
            capabilities={"pre_write_blocking": CapabilityLevel.PARTIAL},
            guarantees={
                "undeclared_tool_write": GuaranteeDeclaration(
                    EnforcementLevel.HARD_BLOCKED,
                    GuaranteeProvider.ADAPTER,
                    ("declared hook",),
                    required_capability="pre_write_blocking",
                )
            },
        )


def test_codex_manifest_is_guarded_compatible_but_not_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _healthy_runtime(monkeypatch)

    manifest = CodexAdapter().capability_manifest(str(repo))

    assert manifest.capabilities["pre_write_blocking"] is CapabilityLevel.COMPLETE
    assert (
        manifest.guarantees["undeclared_tool_write"].level
        is EnforcementLevel.HARD_BLOCKED
    )
    assert (
        manifest.guarantees["bypassed_host_write"].level
        is EnforcementLevel.POST_VERIFIED
    )
    assert evaluate_adapter_policy(manifest, "guarded").compatible is True
    strict = evaluate_adapter_policy(manifest, "strict")
    assert strict.compatible is False
    assert strict.findings[0].guarantee == "bypassed_host_write"
    with pytest.raises(ValueError, match="bypassed_host_write"):
        require_adapter_policy(manifest, AdapterPolicyRequirements.preset("strict"))


def test_adapters_inspect_exposes_sources_versions_and_policy_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo(tmp_path)
    _healthy_runtime(monkeypatch)

    assert (
        main(
            [
                "adapters",
                "inspect",
                "codex",
                "--repo",
                str(repo),
                "--policy",
                "strict",
                "--json",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["adapter"] == "codex"
    assert payload["adapter_version"] == "4"
    assert payload["runtime"]["version"] == "codex-cli 0.123.0"
    assert payload["guarantees"]["undeclared_tool_write"] == {
        "level": "HARD_BLOCKED",
        "provided_by": "composite",
        "evidence": [
            "Codex PreToolUse interception",
            "Claim Plane intent-version and mutation admission",
        ],
        "required_capability": "pre_write_blocking",
        "detail": "Supported tool writes are denied before mutation.",
    }
    assert payload["policy_compatibility"]["compatible"] is False


def test_doctor_refuses_unavailable_strict_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo(tmp_path)
    _healthy_runtime(monkeypatch)

    assert (
        main(
            [
                "doctor",
                "codex",
                "--repo",
                str(repo),
                "--policy",
                "strict",
                "--json",
            ]
        )
        == 2
    )
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is False
    assert report["policy_compatibility"]["compatible"] is False
    assert report["adapter_manifest"]["digest"]


def test_session_evidence_binds_effective_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _healthy_runtime(monkeypatch)
    adapter = CodexAdapter()
    session_id = "capability-evidence"

    response = adapter.start_session(
        AdapterRequest.create(
            AdapterOperation.START_SESSION,
            adapter="codex",
            project_root=str(repo),
            request_id="start-capability-evidence",
            session_id=session_id,
            payload={
                "hook_event_name": "SessionStart",
                "cwd": str(repo),
                "session_id": session_id,
            },
        )
    )

    summary = response.payload["adapter_manifest"]
    assert summary["adapter_version"] == "4"
    assert summary["runtime_version"] == "codex-cli 0.123.0"
    assert summary["guarantees"]["undeclared_tool_write"] == {
        "level": "HARD_BLOCKED",
        "provided_by": "composite",
    }

    with LifecycleEventStore.for_project(repo) as store:
        report = store.report(adapter="codex", session_id=session_id)
        events = store.list_events(adapter="codex", session_id=session_id)
    assert report.adapter_manifest == summary
    assert report.to_dict()["adapter_manifest"] == summary
    assert events[0].payload["adapter_manifest"] == summary
