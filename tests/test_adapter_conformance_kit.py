from __future__ import annotations

import json
from pathlib import Path

import pytest

from claim_plane.cli import main
from claim_plane.connectors import codex
from claim_plane.protocol import (
    AdapterCapabilityManifest,
    AdapterConformanceDriver,
    ConformanceObservation,
    ConformanceScenario,
    ConformanceStatus,
    EnforcementLevel,
    GuaranteeDeclaration,
    GuaranteeProvider,
    RuntimeIdentity,
    run_adapter_conformance,
)
from claim_plane.testing.codex import CodexConformanceDriver
from claim_plane.testing.conformance import ReferenceConformanceDriver


def _healthy_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        codex,
        "_codex_version",
        lambda: ("/usr/bin/codex", "codex-cli 0.123.0"),
    )


@pytest.mark.parametrize("driver_kind", ["reference", "codex"])
def test_all_built_in_adapters_pass_the_same_conformance_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    driver_kind: str,
) -> None:
    _healthy_runtime(monkeypatch)
    driver: AdapterConformanceDriver
    if driver_kind == "codex":
        driver = CodexConformanceDriver(tmp_path / "codex")
    else:
        driver = ReferenceConformanceDriver(tmp_path / "reference")

    report = run_adapter_conformance(driver)

    assert report.compatible is True
    assert report.passed is True
    assert report.claims_verified is True
    assert len(report.results) == 13
    assert all(item.status is ConformanceStatus.PASSED for item in report.results)
    assert all(item.verified for item in report.guarantees)
    assert report.digest() == report.to_dict()["digest"]


def test_available_guarantee_without_scenario_fails_claim_verification() -> None:
    class UncoveredDriver:
        name = "uncovered"

        def manifest(self) -> AdapterCapabilityManifest:
            return AdapterCapabilityManifest(
                adapter=self.name,
                adapter_version="1",
                adapter_protocol_version="1.0",
                runtime=RuntimeIdentity("uncovered", "1", True),
                capabilities={},
                guarantees={
                    "unmapped_guarantee": GuaranteeDeclaration(
                        EnforcementLevel.HARD_BLOCKED,
                        GuaranteeProvider.CLAIM_PLANE,
                        ("declaration without coverage",),
                    )
                },
            )

        def run(self, scenario: ConformanceScenario) -> ConformanceObservation:
            return ConformanceObservation("synthetic scenario passed")

    report = run_adapter_conformance(UncoveredDriver())

    assert report.passed is True
    assert report.claims_verified is False
    assert report.compatible is False
    assert report.guarantees[0].verified is False
    assert "No conformance scenario" in report.guarantees[0].detail


def test_failed_claim_scenario_makes_report_incompatible() -> None:
    class FailingDriver:
        name = "failing"

        def manifest(self) -> AdapterCapabilityManifest:
            return AdapterCapabilityManifest(
                adapter=self.name,
                adapter_version="1",
                adapter_protocol_version="1.0",
                runtime=RuntimeIdentity("failing", "1", True),
                capabilities={},
                guarantees={
                    "stale_intent_version": GuaranteeDeclaration(
                        EnforcementLevel.HARD_BLOCKED,
                        GuaranteeProvider.CLAIM_PLANE,
                        ("declared stale check",),
                    )
                },
            )

        def run(self, scenario: ConformanceScenario) -> ConformanceObservation:
            if scenario is ConformanceScenario.STALE_INTENT_VERSION_DENIED:
                raise AssertionError("stale version was accepted")
            return ConformanceObservation("synthetic scenario passed")

    report = run_adapter_conformance(FailingDriver())

    failed = next(
        item
        for item in report.results
        if item.scenario is ConformanceScenario.STALE_INTENT_VERSION_DENIED
    )
    assert failed.status is ConformanceStatus.FAILED
    assert report.compatible is False
    claim = next(
        item for item in report.guarantees if item.guarantee == "stale_intent_version"
    )
    assert claim.verified is False


def test_cli_emits_machine_readable_reference_compatibility_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "reference-conformance.json"

    assert (
        main(
            [
                "adapters",
                "conformance",
                "reference",
                "--workdir",
                str(tmp_path / "fixtures"),
                "--out",
                str(output),
            ]
        )
        == 0
    )
    capsys.readouterr()
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["protocol"] == "claim-plane.adapter-conformance.v1"
    assert payload["adapter"] == "reference"
    assert payload["compatible"] is True
    assert payload["summary"] == {
        "passed": 13,
        "failed": 0,
        "skipped": 0,
        "total": 13,
        "claims_verified": True,
    }
