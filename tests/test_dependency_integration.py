from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claim_plane import (
    AccessMode,
    AdmissionKind,
    ChangeIntent,
    ChangeManifest,
    IntegrationRunSpec,
    IntentOperation,
    Plane,
    ResourceKind,
    ResourceRef,
    WorkerTarget,
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
    preserves: tuple[str, ...] = (),
    base_revision: str = "main",
) -> ChangeIntent:
    return ChangeIntent(
        intent_id=intent_id,
        task_id=intent_id,
        owner=owner,
        base_revision=base_revision,
        base_commit=(base_revision if len(base_revision) >= 40 else "a" * 40),
        operations=tuple(operations),
        dependencies=dependencies,
        preserves=preserves,
    )


def test_dependency_cycle_is_rejected_atomically() -> None:
    plane = Plane.open(":memory:")
    assert plane.admit(
        intent("a", "agent-a", op(AccessMode.WRITE, ResourceKind.FILE, "a.py"))
    ).allowed
    assert plane.admit(
        intent(
            "b",
            "agent-b",
            op(AccessMode.WRITE, ResourceKind.FILE, "b.py"),
            dependencies=("a",),
        )
    ).allowed

    decision = plane.amend(
        intent(
            "a",
            "agent-a",
            op(AccessMode.WRITE, ResourceKind.FILE, "a.py"),
            dependencies=("b",),
        ),
        expected_version=1,
    )

    assert decision.allowed is False
    assert decision.kind is AdmissionKind.REJECT
    assert "a -> b -> a" in decision.guidance or "b -> a -> b" in decision.guidance
    graph = plane.dependency_graph()
    assert graph["acyclic"] is True
    assert graph["topological_order"].index("a") < graph["topological_order"].index("b")
    record = next(item for item in plane.intents() if item["intent_id"] == "a")
    assert record["version"] == 1
    assert record["state"] == "admitted"


def test_resource_scoped_invalidation_propagates_transitively() -> None:
    plane = Plane.open(":memory:")
    producer_v1 = intent(
        "producer",
        "agent-a",
        op(AccessMode.WRITE, ResourceKind.FILE, "producer.py"),
        op(
            AccessMode.WRITE,
            ResourceKind.CONTRACT,
            "load_x",
            signature="load_x()->X",
            subject_concept_id="X",
        ),
        op(
            AccessMode.WRITE,
            ResourceKind.CONTRACT,
            "load_y",
            signature="load_y()->Y",
            subject_concept_id="Y",
        ),
    )
    consumer_x = intent(
        "consumer-x",
        "agent-b",
        op(AccessMode.WRITE, ResourceKind.FILE, "consumer_x.py"),
        op(
            AccessMode.READ,
            ResourceKind.CONTRACT,
            "load_x",
            signature="load_x()->X",
            subject_concept_id="X",
        ),
    )
    downstream = intent(
        "downstream",
        "agent-c",
        op(AccessMode.WRITE, ResourceKind.FILE, "downstream.py"),
        dependencies=("consumer-x",),
    )
    consumer_y = intent(
        "consumer-y",
        "agent-d",
        op(AccessMode.WRITE, ResourceKind.FILE, "consumer_y.py"),
        op(
            AccessMode.READ,
            ResourceKind.CONTRACT,
            "load_y",
            signature="load_y()->Y",
            subject_concept_id="Y",
        ),
    )
    for item in (producer_v1, consumer_x, downstream, consumer_y):
        assert plane.admit(item).allowed

    producer_v2 = intent(
        "producer",
        "agent-a",
        op(AccessMode.WRITE, ResourceKind.FILE, "producer.py"),
        op(
            AccessMode.WRITE,
            ResourceKind.CONTRACT,
            "load_x",
            signature="load_x(context)->X",
            subject_concept_id="X",
        ),
        op(
            AccessMode.WRITE,
            ResourceKind.CONTRACT,
            "load_y",
            signature="load_y()->Y",
            subject_concept_id="Y",
        ),
    )
    assert plane.amend(producer_v2, expected_version=1).allowed

    states = {item["intent_id"]: item["state"] for item in plane.intents()}
    assert states["consumer-x"] == "stale"
    assert states["downstream"] == "stale"
    assert states["consumer-y"] == "admitted"

    pack = plane.context_pack("downstream")
    dependency = next(
        item for item in pack["dependencies"] if item["intent_id"] == "consumer-x"
    )
    assert dependency["status"] == "stale"
    assert dependency["available"] is False
    notice = plane.notices("downstream")[0]
    assert notice["payload_json"]["depth"] == 2
    assert notice["payload_json"]["dependency_chain"] == [
        "producer",
        "consumer-x",
        "downstream",
    ]


def test_missing_preserved_contract_fails_closed() -> None:
    plane = Plane.open(":memory:")
    assert plane.admit(
        intent(
            "worker",
            "agent-a",
            op(AccessMode.WRITE, ResourceKind.FILE, "src/core.py"),
            preserves=("contract:run=run(task)",),
        )
    ).allowed
    report = plane.verify_manifest(
        ChangeManifest(
            intent_id="worker",
            owner="agent-a",
            base_revision="main",
            changed_files=("src/core.py",),
        )
    )
    assert report.clean is False
    assert any(
        finding.code.value == "preserve_violation" for finding in report.findings
    )


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def _init_repo(path: Path) -> tuple[Path, str]:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    return path, ""


def test_git_collector_builds_preserve_inventory_across_repo(tmp_path: Path) -> None:
    repo, _ = _init_repo(tmp_path / "repo")
    (repo / "api.py").write_text(
        "def run(task: str) -> bool:\n    return True\n", encoding="utf-8"
    )
    (repo / "impl.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "impl.py").write_text("VALUE = 2\n", encoding="utf-8")

    plane = Plane.open(":memory:")
    assert plane.admit(
        intent(
            "worker",
            "agent-a",
            op(AccessMode.WRITE, ResourceKind.FILE, "impl.py"),
            preserves=("contract:run=run(task: str)->bool",),
            base_revision=base,
        )
    ).allowed
    manifest = plane.collect_git_manifest("worker", repo)
    preserved = [
        artifact
        for artifact in manifest.artifacts
        if artifact.identifier == "run" and artifact.metadata.get("inventory_only")
    ]
    assert preserved
    assert plane.verify_manifest(manifest).clean


def _make_parallel_worktrees(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    repo, _ = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a0\n", encoding="utf-8")
    (repo / "b.txt").write_text("b0\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "worker-a", base)
    _git(repo, "branch", "worker-b", base)
    worker_a = tmp_path / "worker-a"
    worker_b = tmp_path / "worker-b"
    _git(repo, "worktree", "add", "-q", str(worker_a), "worker-a")
    _git(repo, "worktree", "add", "-q", str(worker_b), "worker-b")
    return repo, worker_a, worker_b, base


def test_integration_runner_composes_clean_worktrees(tmp_path: Path) -> None:
    repo, worker_a, worker_b, base = _make_parallel_worktrees(tmp_path)
    (worker_a / "a.txt").write_text("a1\n", encoding="utf-8")
    (worker_b / "b.txt").write_text("b1\n", encoding="utf-8")
    _git(worker_a, "add", "a.txt")
    _git(worker_b, "add", "b.txt")

    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(
        intent(
            "worker-a",
            "agent-a",
            op(AccessMode.WRITE, ResourceKind.FILE, "a.txt"),
            base_revision=base,
        )
    ).allowed
    assert plane.admit(
        intent(
            "worker-b",
            "agent-b",
            op(AccessMode.WRITE, ResourceKind.FILE, "b.txt"),
            base_revision=base,
        )
    ).allowed

    command = (
        'python -c "from pathlib import Path; '
        "assert Path('a.txt').read_text() == 'a1\\n'; "
        "assert Path('b.txt').read_text() == 'b1\\n'\""
    )
    result = plane.run_integration(
        IntegrationRunSpec(
            run_id="clean",
            base_repo=str(repo),
            base_revision=base,
            workers=(
                WorkerTarget("worker-a", str(worker_a)),
                WorkerTarget("worker-b", str(worker_b)),
            ),
            integration_commands=(command,),
            artifact_dir=str(tmp_path / "runs"),
        )
    )
    assert result.clean
    assert result.attempts[-1].merge is not None
    assert result.attempts[-1].merge.clean


def test_integration_runner_executes_bounded_repair_loop(tmp_path: Path) -> None:
    repo, _ = _init_repo(tmp_path / "repo")
    (repo / "result.txt").write_text("old\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "result.txt").write_text("new\n", encoding="utf-8")
    (repo / "extra.txt").write_text("remove me\n", encoding="utf-8")
    _git(repo, "add", "result.txt", "extra.txt")

    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(
        intent(
            "worker",
            "agent-a",
            op(AccessMode.WRITE, ResourceKind.FILE, "result.txt"),
            base_revision=base,
        )
    ).allowed
    repair = (
        'python -c "import pathlib, subprocess; '
        "subprocess.run(['git','reset','-q','HEAD','--','extra.txt'], check=True); "
        "pathlib.Path('extra.txt').unlink()\""
    )
    result = plane.run_integration(
        IntegrationRunSpec(
            run_id="repair",
            base_repo=str(repo),
            base_revision=base,
            workers=(WorkerTarget("worker", str(repo), repair),),
            max_attempts=2,
            artifact_dir=str(tmp_path / "runs"),
        )
    )
    assert result.clean
    assert len(result.attempts) == 2
    assert result.attempts[0].repair_executions[0].passed
    assert not (repo / "extra.txt").exists()


def test_integration_runner_rejects_mismatched_base(tmp_path: Path) -> None:
    repo, _ = _init_repo(tmp_path / "repo")
    (repo / "result.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(
        intent(
            "worker",
            "agent-a",
            op(AccessMode.WRITE, ResourceKind.FILE, "result.txt"),
            base_revision=base,
        )
    ).allowed
    with pytest.raises(ValueError, match="integration base"):
        plane.run_integration(
            IntegrationRunSpec(
                run_id="wrong-base",
                base_repo=str(repo),
                base_revision="HEAD~0",
                workers=(WorkerTarget("worker", str(repo)),),
                artifact_dir=str(tmp_path / "runs"),
            )
        )
