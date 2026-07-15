from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claim_plane import (
    AccessMode,
    AdmissionKind,
    ChangeIntent,
    ChangeManifest,
    ChangedRegion,
    IntentOperation,
    Plane,
    ResourceKind,
    ResourceRef,
)


def op(
    access: AccessMode, kind: ResourceKind, identifier: str, **kwargs
) -> IntentOperation:
    return IntentOperation(access, ResourceRef(kind, identifier, **kwargs))


def make_intent(
    intent_id: str,
    owner: str,
    *operations: IntentOperation,
    dependencies: tuple[str, ...] = (),
    base_revision: str = "main",
    acceptance: tuple[str, ...] = (),
    preserves: tuple[str, ...] = (),
) -> ChangeIntent:
    return ChangeIntent(
        intent_id=intent_id,
        task_id=intent_id,
        owner=owner,
        base_revision=base_revision,
        base_commit=(base_revision if len(base_revision) >= 40 else "a" * 40),
        operations=tuple(operations),
        dependencies=dependencies,
        acceptance=acceptance,
        preserves=preserves,
    )


def test_glob_scope_and_concrete_file_are_serialized() -> None:
    plane = Plane.open(":memory:")
    assert plane.admit(
        make_intent("broad", "a", op(AccessMode.WRITE, ResourceKind.FILE, "src/**"))
    ).allowed
    decision = plane.admit(
        make_intent(
            "exact", "b", op(AccessMode.WRITE, ResourceKind.FILE, "src/core.py")
        )
    )
    assert decision.allowed is False
    assert decision.kind is AdmissionKind.SERIALIZE


def test_unbound_shared_contract_does_not_unlock_concept_overlap() -> None:
    plane = Plane.open(":memory:")
    plane.admit(
        make_intent(
            "a",
            "a",
            op(AccessMode.EXTEND, ResourceKind.CONCEPT, "RateLimiter"),
            op(
                AccessMode.WRITE,
                ResourceKind.CONTRACT,
                "healthcheck",
                signature="healthcheck()->bool",
            ),
        )
    )
    decision = plane.admit(
        make_intent(
            "b",
            "b",
            op(AccessMode.EXTEND, ResourceKind.CONCEPT, "RateLimiter"),
            op(
                AccessMode.READ,
                ResourceKind.CONTRACT,
                "healthcheck",
                signature="healthcheck()->bool",
            ),
        )
    )
    assert decision.allowed is False
    assert decision.kind is AdmissionKind.REQUIRES_STUB


def test_disjoint_declared_and_observed_hunks_integrate_cleanly() -> None:
    plane = Plane.open(":memory:")
    first = make_intent(
        "a",
        "a",
        op(AccessMode.WRITE, ResourceKind.FILE, "src/core.py", region="lines 1-10"),
    )
    second = make_intent(
        "b",
        "b",
        op(AccessMode.WRITE, ResourceKind.FILE, "src/core.py", region="lines 20-30"),
    )
    assert plane.admit(first).allowed
    assert plane.admit(second).allowed
    reports = plane.verify_batch(
        (
            ChangeManifest(
                "a",
                "a",
                "main",
                ("src/core.py",),
                base_commit="a" * 40,
                changed_regions=(ChangedRegion("src/core.py", 2, 4),),
            ),
            ChangeManifest(
                "b",
                "b",
                "main",
                ("src/core.py",),
                base_commit="a" * 40,
                changed_regions=(ChangedRegion("src/core.py", 22, 24),),
            ),
        )
    )
    assert reports["a"].clean
    assert reports["b"].clean


def test_hunk_outside_admitted_region_is_rejected() -> None:
    plane = Plane.open(":memory:")
    plane.admit(
        make_intent(
            "a",
            "a",
            op(AccessMode.WRITE, ResourceKind.FILE, "src/core.py", region="lines 1-10"),
        )
    )
    report = plane.verify_manifest(
        ChangeManifest(
            "a",
            "a",
            "main",
            ("src/core.py",),
            changed_regions=(ChangedRegion("src/core.py", 9, 15),),
        )
    )
    assert not report.clean
    assert "region_violation" in {finding.code.value for finding in report.findings}


def test_missing_dependency_is_blocked() -> None:
    plane = Plane.open(":memory:")
    decision = plane.admit(
        make_intent(
            "consumer",
            "b",
            op(AccessMode.WRITE, ResourceKind.FILE, "src/b.py"),
            dependencies=("missing-producer",),
        )
    )
    assert not decision.allowed
    assert decision.kind is AdmissionKind.REJECT


def test_contract_amendment_marks_consumer_stale_and_creates_notice() -> None:
    plane = Plane.open(":memory:")
    producer_v1 = make_intent(
        "producer",
        "agent-a",
        op(AccessMode.WRITE, ResourceKind.FILE, "src/rate_limit.py"),
        op(
            AccessMode.WRITE,
            ResourceKind.CONTRACT,
            "allow",
            signature="allow(request)->Decision",
            subject_concept_id="RateLimiter",
        ),
    )
    consumer = make_intent(
        "consumer",
        "agent-b",
        op(AccessMode.WRITE, ResourceKind.FILE, "src/metrics.py"),
        op(
            AccessMode.READ,
            ResourceKind.CONTRACT,
            "allow",
            signature="allow(request)->Decision",
            subject_concept_id="RateLimiter",
        ),
    )
    assert plane.admit(producer_v1).allowed
    assert plane.admit(consumer).allowed

    producer_v2 = make_intent(
        "producer",
        "agent-a",
        op(AccessMode.WRITE, ResourceKind.FILE, "src/rate_limit.py"),
        op(
            AccessMode.WRITE,
            ResourceKind.CONTRACT,
            "allow",
            signature="allow(request, context)->Decision",
            subject_concept_id="RateLimiter",
        ),
    )
    decision = plane.amend(producer_v2, expected_version=1)
    assert decision.allowed
    records = {record["intent_id"]: record for record in plane.intents()}
    assert records["consumer"]["state"] == "stale"
    notices = plane.notices("consumer")
    assert notices and notices[0]["notice_type"] == "premise_invalidated"
    with pytest.raises(ValueError, match="cannot move intent"):
        plane.activate("consumer")


def test_semantic_mode_is_fail_closed_without_lexicon() -> None:
    with pytest.raises(ValueError, match="requires --lexicon"):
        Plane.open(":memory:", semantic=True)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def test_git_collector_extracts_hunks_and_typed_qualified_contract(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")

    source = repo / "src" / "rate_limit.py"
    source.parent.mkdir()
    source.write_text(
        "class RateLimiter:\n"
        "    def allow(self, request: str) -> bool:\n"
        "        return True\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/rate_limit.py")

    plane = Plane.open(":memory:")
    intent = make_intent(
        "worker",
        "agent-a",
        op(
            AccessMode.WRITE,
            ResourceKind.FILE,
            "src/rate_limit.py",
            region="lines 1-10",
        ),
        op(
            AccessMode.WRITE,
            ResourceKind.CONTRACT,
            "allow",
            signature="allow(request: str)->bool",
            subject_concept_id="RateLimiter",
        ),
        base_revision=base,
    )
    assert plane.admit(intent).allowed
    manifest = plane.collect_git_manifest("worker", repo)
    assert manifest.changed_regions
    contract = next(
        artifact
        for artifact in manifest.artifacts
        if artifact.kind is ResourceKind.CONTRACT
    )
    assert contract.signature == "allow(request: str)->bool"
    assert contract.qualified_identifier and contract.qualified_identifier.endswith(
        "RateLimiter.allow"
    )
    assert contract.subject_concept_id == "RateLimiter"
    assert plane.verify_manifest(manifest).clean


def test_acceptance_commands_are_executed_only_when_requested(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "result.txt").write_text("ok\n", encoding="utf-8")
    _git(repo, "add", "result.txt")

    plane = Plane.open(":memory:")
    command = (
        "python -c \"from pathlib import Path; assert Path('result.txt').exists()\""
    )
    plane.admit(
        make_intent(
            "a",
            "agent-a",
            op(AccessMode.WRITE, ResourceKind.FILE, "result.txt"),
            base_revision=base,
            acceptance=(command,),
        )
    )
    report = plane.verify_git("a", repo, run_acceptance=True)
    assert report.clean
    assert report.metrics["acceptance_commands"] == 1
    assert not any(
        finding.code.value == "acceptance_failed" for finding in report.findings
    )
