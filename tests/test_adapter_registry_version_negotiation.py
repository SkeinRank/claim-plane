from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from claim_plane.cli import main
from claim_plane.connectors import CodexAdapter, build_adapter_registry
from claim_plane.connectors import codex
from claim_plane.connectors.codex import init_project
from claim_plane.protocol import (
    AdapterPin,
    AdapterRegistry,
    AdapterRegistryError,
    AdapterSource,
    HandshakeCode,
    SemanticVersion,
    VersionRange,
    load_adapter_pin,
    save_adapter_pin,
)
from claim_plane.testing.reference_adapter import ReferenceAdapter


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
    init_project(repo)
    return repo


def _healthy_runtime(
    monkeypatch: pytest.MonkeyPatch, version: str = "codex-cli 0.123.0"
) -> None:
    monkeypatch.setattr(
        codex,
        "_codex_version",
        lambda: ("/usr/bin/codex", version),
    )


def test_semantic_versions_and_ranges_accept_short_versions() -> None:
    assert str(SemanticVersion.parse("3")) == "3.0.0"
    assert str(SemanticVersion.parse("1.2")) == "1.2.0"
    assert VersionRange.parse(">=1.0,<2.0").contains("1.9.4")
    assert not VersionRange.parse(">=1.0,<2.0").contains("2.0")
    assert VersionRange.parse("1.x").contains("1.8.2")
    assert VersionRange.parse("~=1.2.3").contains("1.2.9")
    assert not VersionRange.parse("~=1.2.3").contains("1.3.0")
    assert VersionRange.parse("~=1.2").contains("1.9.0")
    assert not VersionRange.parse("~=1.2").contains("2.0.0")


def test_builtin_codex_handshake_negotiates_current_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _healthy_runtime(monkeypatch)
    registry = build_adapter_registry(discover_external=False)

    handshake = registry.handshake("codex", project_root=repo)

    assert handshake.compatible is True
    assert handshake.negotiated_protocol_version == "1.0"
    assert handshake.adapter_version == "4"
    assert handshake.runtime_version == "codex-cli 0.123.0"
    assert handshake.source is AdapterSource.BUILTIN
    assert handshake.findings[0].code is HandshakeCode.COMPATIBLE
    assert handshake.to_dict()["digest"]


def test_incompatible_protocol_range_fails_before_adapter_use(tmp_path: Path) -> None:
    registry = AdapterRegistry()
    registry.register(
        "reference",
        ReferenceAdapter,
        protocol_range=">=2.0,<3.0",
        source=AdapterSource.PROGRAMMATIC,
    )

    handshake = registry.handshake("reference", project_root=tmp_path)

    assert handshake.compatible is False
    assert handshake.negotiated_protocol_version is None
    assert any(
        item.code is HandshakeCode.PROTOCOL_INCOMPATIBLE for item in handshake.findings
    )
    with pytest.raises(AdapterRegistryError):
        handshake.require_compatible()


def test_project_pin_detects_runtime_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _healthy_runtime(monkeypatch, "codex-cli 0.123.0")
    registry = build_adapter_registry(discover_external=False)
    pin, path = registry.pin("codex", project_root=repo)

    assert path.exists()
    assert load_adapter_pin(repo, "codex") == pin

    _healthy_runtime(monkeypatch, "codex-cli 0.124.0")
    handshake = registry.handshake("codex", project_root=repo)

    assert handshake.compatible is False
    finding = next(
        item
        for item in handshake.findings
        if item.code is HandshakeCode.PIN_RUNTIME_VERSION_MISMATCH
    )
    assert finding.expected == "codex-cli 0.123.0"
    assert finding.actual == "codex-cli 0.124.0"
    assert finding.remediation


def test_codex_session_start_refuses_mismatched_pin_before_state_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _healthy_runtime(monkeypatch)
    adapter = CodexAdapter()
    manifest = adapter.capability_manifest(str(repo))
    save_adapter_pin(
        repo,
        AdapterPin(
            adapter="codex",
            adapter_version="999.0.0",
            protocol_version="1.0",
            protocol_range=">=1.0,<2.0",
            runtime_name="codex",
            runtime_version="codex-cli 0.123.0",
            source=AdapterSource.BUILTIN,
            manifest_digest=manifest.digest(),
        ),
    )

    from claim_plane.protocol import AdapterOperation, AdapterRequest

    with pytest.raises(AdapterRegistryError):
        adapter.start_session(
            AdapterRequest.create(
                AdapterOperation.START_SESSION,
                adapter="codex",
                project_root=str(repo),
                request_id="pin-mismatch-start",
                session_id="pin-mismatch-session",
                payload={"source": "startup"},
            )
        )

    sessions = repo / ".claim-plane" / "codex" / "sessions"
    assert not sessions.exists() or not list(sessions.glob("*.json"))


def test_pin_requires_detected_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(codex, "_codex_version", lambda: (None, None))
    registry = build_adapter_registry(discover_external=False)

    with pytest.raises(AdapterRegistryError, match="before its runtime"):
        registry.pin("codex", project_root=repo)


def test_external_entry_point_registers_without_core_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claim_plane.protocol import registry as registry_module

    class ExternalReferenceAdapter(ReferenceAdapter):
        name = "external-reference"

    entry_point = SimpleNamespace(
        name="external-reference",
        module="third_party.adapter",
        attr="ExternalReferenceAdapter",
        dist=SimpleNamespace(name="third-party-claim-plane-adapter"),
        load=lambda: ExternalReferenceAdapter,
    )
    monkeypatch.setattr(registry_module, "_entry_points", lambda: (entry_point,))
    registry = AdapterRegistry()

    discovered = registry.discover_entry_points()
    handshake = registry.handshake("external-reference")

    assert len(discovered) == 1
    assert discovered[0].source is AdapterSource.ENTRY_POINT
    assert discovered[0].distribution == "third-party-claim-plane-adapter"
    assert handshake.compatible is True
    assert handshake.source is AdapterSource.ENTRY_POINT


def test_cli_lists_pins_and_reports_handshake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo(tmp_path)
    _healthy_runtime(monkeypatch)

    assert main(["adapters", "pin", "codex", "--repo", str(repo), "--json"]) == 0
    pin_payload = json.loads(capsys.readouterr().out)
    assert pin_payload["adapter"] == "codex"
    assert pin_payload["adapter_version_normalized"] == "4.0.0"
    assert pin_payload["protocol_version"] == "1.0"

    assert (
        main(
            [
                "adapters",
                "list",
                "--repo",
                str(repo),
                "--inspect",
                "--json",
            ]
        )
        == 0
    )
    registry_payload = json.loads(capsys.readouterr().out)
    assert registry_payload["protocol"] == "claim-plane.adapter-registry.v1"
    codex_payload = next(
        item for item in registry_payload["adapters"] if item["name"] == "codex"
    )
    assert codex_payload["pinned"] is True
    assert codex_payload["handshake"]["compatible"] is True

    assert (
        main(
            [
                "adapters",
                "doctor",
                "codex",
                "--repo",
                str(repo),
                "--json",
            ]
        )
        == 2
    )
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["registry_handshake"]["compatible"] is True
    assert doctor["ready"] is False


def test_session_evidence_binds_negotiated_protocol_and_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    _healthy_runtime(monkeypatch)
    registry = build_adapter_registry(discover_external=False)
    registry.pin("codex", project_root=repo)
    adapter = registry.create("codex")

    from claim_plane.protocol import (
        AdapterOperation,
        AdapterRequest,
        LifecycleEventStore,
    )

    response = adapter.start_session(
        AdapterRequest.create(
            AdapterOperation.START_SESSION,
            adapter="codex",
            project_root=str(repo),
            request_id="registry-evidence-start",
            session_id="registry-evidence-session",
            payload={"source": "startup"},
        )
    )

    summary = response.payload["adapter_handshake"]
    assert summary["negotiated_protocol_version"] == "1.0"
    assert summary["runtime_version"] == "codex-cli 0.123.0"
    assert summary["pin_digest"]
    assert summary["compatible"] is True

    with LifecycleEventStore.for_project(repo) as store:
        report = store.report(adapter="codex", session_id="registry-evidence-session")
        events = store.list_events(
            adapter="codex", session_id="registry-evidence-session"
        )
    assert report.adapter_handshake == summary
    assert report.to_dict()["adapter_handshake"] == summary
    assert events[0].payload["adapter_handshake"] == summary
