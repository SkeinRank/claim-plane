from __future__ import annotations

from claim_plane import (
    AccessMode,
    AdmissionKind,
    ChangeIntent,
    ChangeManifest,
    IntentOperation,
    Plane,
    ResourceKind,
    ResourceRef,
    ScopeCommitment,
)


def _intent(
    intent_id: str,
    owner: str,
    path: str,
    *,
    commitment: ScopeCommitment = ScopeCommitment.COMMITTED,
) -> ChangeIntent:
    return ChangeIntent(
        intent_id=intent_id,
        task_id=intent_id,
        owner=owner,
        base_revision="main",
        base_commit="a" * 40,
        operations=(
            IntentOperation(
                AccessMode.WRITE,
                ResourceRef(ResourceKind.FILE, path),
                commitment=commitment,
            ),
        ),
    )


def test_contingent_write_does_not_block_initial_admission() -> None:
    plane = Plane.open(":memory:")
    assert plane.admit(_intent("writer", "a", "src/shared.py")).allowed

    decision = plane.admit(
        _intent(
            "possible-writer",
            "b",
            "src/shared.py",
            commitment=ScopeCommitment.CONTINGENT,
        )
    )

    assert decision.allowed
    assert decision.kind is AdmissionKind.NOTIFY_ON_CHANGE
    assert decision.conflicts and not decision.conflicts[0].blocking
    stored = plane.intent("possible-writer")
    assert stored is not None
    assert stored.contingent_operations[0].resource.identifier == "src/shared.py"
    assert stored.mutating_operations == ()


def test_rejected_scope_promotion_is_non_destructive() -> None:
    plane = Plane.open(":memory:")
    assert plane.admit(_intent("writer", "a", "src/shared.py")).allowed
    assert plane.admit(
        _intent(
            "possible-writer",
            "b",
            "src/shared.py",
            commitment=ScopeCommitment.CONTINGENT,
        )
    ).allowed

    decision = plane.promote_contingent_scope(
        "possible-writer",
        path="src/shared.py",
        modes=(AccessMode.WRITE,),
    )

    assert not decision.allowed
    assert decision.kind is AdmissionKind.SERIALIZE
    stored = plane.intent("possible-writer")
    assert stored is not None
    assert len(stored.contingent_operations) == 1
    assert stored.mutating_operations == ()


def test_scope_promotion_re_admits_and_preserves_active_state() -> None:
    plane = Plane.open(":memory:")
    assert plane.admit(_intent("writer", "a", "src/shared.py")).allowed
    assert plane.admit(
        _intent(
            "possible-writer",
            "b",
            "src/shared.py",
            commitment=ScopeCommitment.CONTINGENT,
        )
    ).allowed
    plane.activate("possible-writer")
    plane.release_intent("writer")

    decision = plane.promote_contingent_scope(
        "possible-writer",
        path="src/shared.py",
        modes=(AccessMode.WRITE,),
    )

    assert decision.allowed
    stored = plane.intent("possible-writer")
    assert stored is not None
    assert len(stored.contingent_operations) == 0
    assert len(stored.mutating_operations) == 1
    record = next(
        item for item in plane.intents() if item["intent_id"] == "possible-writer"
    )
    assert record["state"] == "active"
    events = [item["event_type"] for item in plane.events()]
    assert "intent_scope_expanded" in events


def test_unpromoted_contingent_write_is_not_admitted_for_verification() -> None:
    plane = Plane.open(":memory:")
    assert plane.admit(
        _intent(
            "worker",
            "agent",
            "src/optional.py",
            commitment=ScopeCommitment.CONTINGENT,
        )
    ).allowed
    manifest = ChangeManifest(
        "worker",
        "agent",
        "main",
        ("src/optional.py",),
        base_commit="a" * 40,
    )

    report = plane.verify_manifest(manifest)
    assert not report.clean
    assert "undeclared_change" in {finding.code.value for finding in report.findings}

    assert plane.promote_contingent_scope(
        "worker", path="src/optional.py", modes=(AccessMode.WRITE,)
    ).allowed
    report = plane.verify_manifest(manifest)
    assert report.clean


def test_pattern_contingent_scope_promotes_only_concrete_path() -> None:
    plane = Plane.open(":memory:")
    intent = ChangeIntent(
        intent_id="worker",
        task_id="worker",
        owner="agent",
        base_revision="main",
        base_commit="a" * 40,
        operations=(
            IntentOperation(
                AccessMode.WRITE,
                ResourceRef(ResourceKind.FILE, "src/**"),
                commitment=ScopeCommitment.CONTINGENT,
            ),
        ),
    )
    assert plane.admit(intent).allowed

    assert plane.promote_contingent_scope(
        "worker", path="src/core.py", modes=(AccessMode.WRITE,)
    ).allowed
    stored = plane.intent("worker")
    assert stored is not None

    assert any(
        operation.contingent and operation.resource.identifier == "src/**"
        for operation in stored.operations
    )
    assert any(
        operation.committed and operation.resource.identifier == "src/core.py"
        for operation in stored.operations
    )
    assert not any(
        operation.committed and operation.resource.identifier == "src/**"
        for operation in stored.operations
    )


def test_committed_commitment_is_omitted_from_legacy_serialization() -> None:
    operation = IntentOperation(
        AccessMode.WRITE,
        ResourceRef(ResourceKind.FILE, "src/core.py"),
    )

    payload = operation.to_dict()

    assert "commitment" not in payload
    assert IntentOperation.from_dict(payload).commitment is ScopeCommitment.COMMITTED


def test_scope_promotion_rejects_repository_escape_paths() -> None:
    plane = Plane.open(":memory:")
    assert plane.admit(
        _intent(
            "worker",
            "agent",
            "src/**",
            commitment=ScopeCommitment.CONTINGENT,
        )
    ).allowed

    for path in ("../outside.py", "/tmp/outside.py", "src/../../outside.py"):
        try:
            plane.promote_contingent_scope(
                "worker",
                path=path,
                modes=(AccessMode.WRITE,),
            )
        except ValueError as exc:
            assert "inside the repository" in str(exc)
        else:
            raise AssertionError(f"unsafe path was accepted: {path}")
