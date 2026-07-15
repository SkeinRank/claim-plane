from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from claim_plane import (
    AccessMode,
    AdmissionKind,
    ChangeIntent,
    ChangeManifest,
    Claim,
    ClaimType,
    IntentOperation,
    ObservedArtifact,
    Plane,
    ResourceKind,
    ResourceRef,
    VerdictKind,
    WorkerTier,
)


def op(
    access: AccessMode, kind: ResourceKind, identifier: str, **kwargs
) -> IntentOperation:
    return IntentOperation(access, ResourceRef(kind, identifier, **kwargs))


def intent(
    intent_id: str,
    owner: str,
    *operations: IntentOperation,
    dependencies: tuple[str, ...] = (),
    acceptance: tuple[str, ...] = ("pytest",),
    preserves: tuple[str, ...] = ("public API",),
) -> ChangeIntent:
    return ChangeIntent(
        intent_id=intent_id,
        task_id=intent_id,
        owner=owner,
        base_revision="main",
        base_commit="a" * 40,
        operations=tuple(operations),
        dependencies=dependencies,
        acceptance=acceptance,
        preserves=preserves,
    )


def test_independent_intents_are_admitted():
    plane = Plane.open(":memory:")
    assert plane.admit(
        intent("a", "agent-a", op(AccessMode.WRITE, ResourceKind.FILE, "src/a.py"))
    ).allowed
    decision = plane.admit(
        intent("b", "agent-b", op(AccessMode.WRITE, ResourceKind.FILE, "src/b.py"))
    )
    assert decision.allowed
    assert decision.kind is AdmissionKind.INDEPENDENT


def test_same_file_writers_are_serialized():
    plane = Plane.open(":memory:")
    plane.admit(
        intent("a", "agent-a", op(AccessMode.WRITE, ResourceKind.FILE, "src/core.py"))
    )
    decision = plane.admit(
        intent("b", "agent-b", op(AccessMode.WRITE, ResourceKind.FILE, "src/core.py"))
    )
    assert not decision.allowed
    assert decision.kind is AdmissionKind.SERIALIZE


def test_disjoint_regions_in_one_file_can_run_in_parallel():
    plane = Plane.open(":memory:")
    plane.admit(
        intent(
            "a",
            "agent-a",
            op(AccessMode.WRITE, ResourceKind.FILE, "src/core.py", region="lines 1-40"),
        )
    )
    decision = plane.admit(
        intent(
            "b",
            "agent-b",
            op(
                AccessMode.WRITE,
                ResourceKind.FILE,
                "src/core.py",
                region="lines 80-120",
            ),
        )
    )
    assert decision.allowed
    assert decision.kind is AdmissionKind.PARALLEL_WITH_CONSTRAINT


def test_read_write_overlap_is_advisory_not_a_lock():
    plane = Plane.open(":memory:")
    plane.admit(
        intent(
            "producer", "agent-a", op(AccessMode.WRITE, ResourceKind.SCHEMA, "Session")
        )
    )
    decision = plane.admit(
        intent(
            "consumer", "agent-b", op(AccessMode.READ, ResourceKind.SCHEMA, "Session")
        )
    )
    assert decision.allowed
    assert decision.kind is AdmissionKind.NOTIFY_ON_CHANGE
    assert decision.notifications


def test_concept_overlap_requires_contract_stub():
    plane = Plane.open(":memory:")
    plane.admit(
        intent(
            "a", "agent-a", op(AccessMode.EXTEND, ResourceKind.CONCEPT, "RateLimiter")
        )
    )
    decision = plane.admit(
        intent(
            "b", "agent-b", op(AccessMode.EXTEND, ResourceKind.CONCEPT, "RateLimiter")
        )
    )
    assert not decision.allowed
    assert decision.kind is AdmissionKind.REQUIRES_STUB


def test_concept_overlap_with_shared_contract_is_admitted():
    plane = Plane.open(":memory:")
    signature = "allow(request)->Decision"
    plane.admit(
        intent(
            "a",
            "agent-a",
            op(AccessMode.EXTEND, ResourceKind.CONCEPT, "RateLimiter"),
            op(
                AccessMode.WRITE,
                ResourceKind.CONTRACT,
                "allow",
                signature=signature,
                subject_concept_id="RateLimiter",
            ),
        )
    )
    decision = plane.admit(
        intent(
            "b",
            "agent-b",
            op(AccessMode.EXTEND, ResourceKind.CONCEPT, "RateLimiter"),
            op(
                AccessMode.READ,
                ResourceKind.CONTRACT,
                "allow",
                signature=signature,
                subject_concept_id="RateLimiter",
            ),
        )
    )
    assert decision.allowed
    assert decision.kind in {
        AdmissionKind.COMPATIBLE_OVERLAP,
        AdmissionKind.CONTRACT_DEPENDENCY,
    }


def test_context_pack_is_bounded_and_contains_contracts():
    plane = Plane.open(":memory:")
    plane.admit(
        intent(
            "a",
            "agent-a",
            op(AccessMode.WRITE, ResourceKind.FILE, "src/rate_limit.py"),
            op(
                AccessMode.WRITE,
                ResourceKind.CONTRACT,
                "allow",
                signature="allow(request)->Decision",
            ),
        )
    )
    pack = plane.context_pack("a")
    assert pack["protocol"] == "claim-plane.context-pack.v1"
    assert pack["contracts"][0]["signature"] == "allow(request)->Decision"
    assert "Do not mutate undeclared resources." in pack["worker_rules"]


def test_verifier_catches_undeclared_change_and_contract_mismatch():
    plane = Plane.open(":memory:")
    plane.admit(
        intent(
            "a",
            "agent-a",
            op(AccessMode.WRITE, ResourceKind.FILE, "src/rate_limit.py"),
            op(
                AccessMode.WRITE,
                ResourceKind.CONTRACT,
                "allow",
                signature="allow(request)->Decision",
            ),
        )
    )
    manifest = ChangeManifest(
        intent_id="a",
        owner="agent-a",
        base_revision="main",
        changed_files=("src/rate_limit.py", "src/extra.py"),
        artifacts=(
            ObservedArtifact(
                ResourceKind.CONTRACT,
                "allow",
                "src/rate_limit.py",
                signature="allow(request, scope)->Decision",
            ),
        ),
    )
    report = plane.verify_manifest(manifest)
    assert not report.clean
    codes = {finding.code.value for finding in report.findings}
    assert "undeclared_change" in codes
    assert "contract_mismatch" in codes


def test_verifier_accepts_declared_manifest():
    plane = Plane.open(":memory:")
    plane.admit(
        intent(
            "a",
            "agent-a",
            op(AccessMode.WRITE, ResourceKind.FILE, "src/rate_limit.py"),
            op(
                AccessMode.WRITE,
                ResourceKind.CONTRACT,
                "allow",
                signature="allow(request)",
            ),
        )
    )
    report = plane.verify_manifest(
        ChangeManifest(
            intent_id="a",
            owner="agent-a",
            base_revision="main",
            base_commit="a" * 40,
            changed_files=("src/rate_limit.py",),
            artifacts=(
                ObservedArtifact(
                    ResourceKind.CONTRACT,
                    "allow",
                    "src/rate_limit.py",
                    signature="allow(request)",
                ),
            ),
        )
    )
    assert report.clean


def test_router_escalates_public_contract_and_destructive_work():
    plane = Plane.open(":memory:")
    plane.admit(
        intent(
            "risky",
            "agent-a",
            op(AccessMode.RENAME, ResourceKind.SCHEMA, "Customer"),
            op(
                AccessMode.WRITE,
                ResourceKind.CONTRACT,
                "load_customer",
                signature="load_customer(id)",
            ),
        )
    )
    recommendation = plane.recommend_worker("risky")
    assert recommendation.tier is WorkerTier.FRONTIER


def test_semantic_aliases_are_one_concept(tmp_path: Path):
    pytest.importorskip("agent_lexicon")
    lexicon = tmp_path / "lexicon.yaml"
    lexicon.write_text(
        """version: 1
metadata:
  name: Test
scopes:
  - id: project
    label: Project
terms:
  - id: project.rate_limiter
    canonical: RateLimiter
    scopes: [project]
    aliases:
      - surface: RequestThrottler
        scopes: [project]
proposals: []
""",
        encoding="utf-8",
    )
    plane = Plane.open(tmp_path / "plane.db", semantic=True, lexicon_path=str(lexicon))
    first = plane.claim(Claim(ClaimType.NAME, "RateLimiter", owner="a"))
    second = plane.claim(Claim(ClaimType.NAME, "RequestThrottler", owner="b"))
    assert first.granted
    assert second.kind is VerdictKind.DUPLICATE


def _claim_worker(db_path: str, owner: str, start, queue) -> None:
    start.wait()
    plane = Plane.open(db_path)
    try:
        queue.put(
            plane.claim(Claim(ClaimType.NAME, "RateLimiter", owner=owner)).kind.value
        )
    finally:
        plane.close()


def _intent_worker(db_path: str, payload: dict, start, queue) -> None:
    start.wait()
    plane = Plane.open(db_path)
    try:
        queue.put(plane.admit(ChangeIntent.from_dict(payload)).allowed)
    finally:
        plane.close()


def test_atomic_claim_allows_exactly_one_process(tmp_path: Path):
    db = str(tmp_path / "atomic-claim.db")
    Plane.open(db).close()  # initialize schema before the race
    ctx = mp.get_context("spawn")
    start = ctx.Event()
    queue = ctx.Queue()
    processes = [
        ctx.Process(target=_claim_worker, args=(db, owner, start, queue))
        for owner in ("a", "b")
    ]
    for process in processes:
        process.start()
    start.set()
    results = [queue.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    queue.close()
    queue.join_thread()
    assert results.count("granted") == 1
    assert results.count("conflict") == 1


def test_atomic_intent_admission_allows_only_one_same_file_writer(tmp_path: Path):
    db = str(tmp_path / "atomic-intent.db")
    Plane.open(db).close()
    payloads = [
        intent(
            name, owner, op(AccessMode.WRITE, ResourceKind.FILE, "src/core.py")
        ).to_dict()
        for name, owner in (("a", "agent-a"), ("b", "agent-b"))
    ]
    ctx = mp.get_context("spawn")
    start = ctx.Event()
    queue = ctx.Queue()
    processes = [
        ctx.Process(target=_intent_worker, args=(db, payload, start, queue))
        for payload in payloads
    ]
    for process in processes:
        process.start()
    start.set()
    results = [queue.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    queue.close()
    queue.join_thread()
    assert sorted(results) == [False, True]


def test_blocked_intent_cannot_be_activated():
    plane = Plane.open(":memory:")
    plane.admit(
        intent("a", "agent-a", op(AccessMode.WRITE, ResourceKind.FILE, "src/core.py"))
    )
    blocked = plane.admit(
        intent("b", "agent-b", op(AccessMode.WRITE, ResourceKind.FILE, "src/core.py"))
    )
    assert not blocked.allowed
    with pytest.raises(ValueError, match="cannot move intent"):
        plane.activate("b")


def test_targeted_repair_plan_does_not_restart_everything():
    plane = Plane.open(":memory:")
    plane.admit(
        intent(
            "a",
            "agent-a",
            op(AccessMode.WRITE, ResourceKind.FILE, "src/core.py"),
            op(AccessMode.WRITE, ResourceKind.CONTRACT, "run", signature="run(task)"),
        )
    )
    report = plane.verify_manifest(
        ChangeManifest(
            intent_id="a",
            owner="agent-a",
            base_revision="main",
            changed_files=("src/core.py", "src/extra.py"),
            artifacts=(
                ObservedArtifact(
                    ResourceKind.CONTRACT,
                    "run",
                    "src/core.py",
                    signature="run(task, force)",
                ),
            ),
        )
    )
    plan = plane.repair_plan(report)
    kinds = {action.kind.value for action in plan.actions}
    assert "align_contract" in kinds
    assert "redeclare_scope" in kinds
    assert plan.requires_replan
