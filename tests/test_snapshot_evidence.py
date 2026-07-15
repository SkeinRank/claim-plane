from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from claim_plane import (
    AccessMode,
    ChangeIntent,
    ChangeManifest,
    IntentOperation,
    Plane,
    ResourceKind,
    ResourceRef,
)
from claim_plane.integration import IntegrationRunSpec, WorkerTarget


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def _repo(path: Path) -> tuple[Path, str]:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "a.txt").write_text("a0\n", encoding="utf-8")
    (path / "b.txt").write_text("b0\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path, _git(path, "rev-parse", "HEAD")


def _intent(
    intent_id: str,
    base: str,
    *paths: str,
    acceptance: tuple[str, ...] = (),
    dependencies: tuple[str, ...] = (),
) -> ChangeIntent:
    return ChangeIntent(
        intent_id=intent_id,
        task_id=intent_id,
        owner=f"agent-{intent_id}",
        base_revision=base,
        base_commit=(base if len(base) >= 40 else "a" * 40),
        operations=tuple(
            IntentOperation(
                AccessMode.WRITE,
                ResourceRef(ResourceKind.FILE, path),
            )
            for path in paths
        ),
        acceptance=acceptance,
        dependencies=dependencies,
    )


def test_required_exact_write_is_blocking() -> None:
    plane = Plane.open(":memory:")
    intent = _intent("worker", "main", "required.txt")
    assert plane.admit(intent).allowed
    report = plane.verify_manifest(
        ChangeManifest(
            intent_id="worker",
            owner="agent-worker",
            base_revision="main",
            changed_files=(),
        )
    )
    assert report.clean is False
    finding = next(
        item for item in report.findings if item.code.value == "missing_declared_change"
    )
    assert finding.severity.value == "error"


def test_worker_acceptance_cannot_change_verified_patch(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a1\n", encoding="utf-8")
    acceptance = (
        "python -c \"from pathlib import Path; Path('b.txt').write_text('hidden\\n')\"",
    )
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent("worker", base, "a.txt", acceptance=acceptance)).allowed

    result = plane.run_integration(
        IntegrationRunSpec(
            run_id="worker-mutation",
            base_repo=str(repo),
            base_revision=base,
            workers=(WorkerTarget("worker", str(repo)),),
            artifact_dir=str(tmp_path / "runs"),
        )
    )

    assert result.clean is False
    report = result.attempts[0].reports["worker"]
    assert any(item.code.value == "snapshot_mutation" for item in report.findings)
    evidence = result.attempts[0].worker_evidence["worker"]
    assert evidence.acceptance_immutable is False
    assert evidence.mutation_paths == ("b.txt",)
    # Acceptance ran only on the detached frozen snapshot.
    assert (repo / "b.txt").read_text(encoding="utf-8") == "b0\n"
    patch = Path(evidence.patch_path).read_bytes()
    assert b"a.txt" in patch
    assert b"b.txt" not in patch


def test_integration_command_mutation_is_blocking(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a1\n", encoding="utf-8")
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent("worker", base, "a.txt")).allowed

    result = plane.run_integration(
        IntegrationRunSpec(
            run_id="integration-mutation",
            base_repo=str(repo),
            base_revision=base,
            workers=(WorkerTarget("worker", str(repo)),),
            integration_commands=(
                "python -c \"from pathlib import Path; Path('b.txt').write_text('mutated\\n')\"",
            ),
            artifact_dir=str(tmp_path / "runs"),
        )
    )

    assert result.clean is False
    merge = result.attempts[0].merge
    assert merge is not None
    assert merge.applied is True
    assert merge.commands_immutable is False
    assert merge.mutation_paths == ("b.txt",)
    assert merge.result_commit is None
    assert "mutated the verified result" in (merge.error or "")


def test_verified_evidence_binds_manifest_patch_and_result_commit(
    tmp_path: Path,
) -> None:
    repo, base = _repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a1\n", encoding="utf-8")
    # Frozen patches must include non-ignored untracked files.
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent("worker", base, "a.txt", "new.txt")).allowed

    result = plane.run_integration(
        IntegrationRunSpec(
            run_id="verified",
            base_repo=str(repo),
            base_revision=base,
            workers=(WorkerTarget("worker", str(repo)),),
            integration_commands=(
                "python -c \"from pathlib import Path; assert Path('new.txt').read_text() == 'new\\n'\"",
            ),
            artifact_dir=str(tmp_path / "runs"),
            result_ref="refs/claim-plane/tests/verified",
        )
    )

    assert result.clean
    assert result.result_commit
    assert result.result_tree
    assert result.result_patch_path
    assert result.evidence_path
    assert _git(repo, "cat-file", "-t", result.result_commit) == "commit"
    assert (
        _git(repo, "rev-parse", "refs/claim-plane/tests/verified")
        == result.result_commit
    )
    assert (
        _git(repo, "rev-parse", f"{result.result_commit}^{{tree}}")
        == result.result_tree
    )

    result_patch = Path(result.result_patch_path).read_bytes()
    assert hashlib.sha256(result_patch).hexdigest() == result.result_patch_sha256
    assert b"new.txt" in result_patch

    worker = result.attempts[-1].worker_evidence["worker"]
    worker_patch = Path(worker.patch_path).read_bytes()
    assert hashlib.sha256(worker_patch).hexdigest() == worker.patch_sha256
    manifest = json.loads(Path(worker.manifest_path).read_text(encoding="utf-8"))
    canonical_manifest = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert hashlib.sha256(canonical_manifest).hexdigest() == worker.manifest_sha256

    evidence_payload = json.loads(
        Path(result.evidence_path).read_text(encoding="utf-8")
    )
    canonical_evidence = json.dumps(
        evidence_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert hashlib.sha256(canonical_evidence).hexdigest() == result.evidence_sha256
    assert evidence_payload["workers"]["worker"]["patch_sha256"] == worker.patch_sha256
    assert evidence_payload["merge"]["result_commit"] == result.result_commit


def test_worker_patches_apply_in_dependency_order(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    _git(repo, "branch", "producer", base)
    _git(repo, "branch", "consumer", base)
    producer = tmp_path / "producer"
    consumer = tmp_path / "consumer"
    _git(repo, "worktree", "add", "-q", str(producer), "producer")
    _git(repo, "worktree", "add", "-q", str(consumer), "consumer")
    (producer / "a.txt").write_text("producer\n", encoding="utf-8")
    (consumer / "b.txt").write_text("consumer\n", encoding="utf-8")

    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent("producer", base, "a.txt")).allowed
    assert plane.admit(
        _intent("consumer", base, "b.txt", dependencies=("producer",))
    ).allowed

    result = plane.run_integration(
        IntegrationRunSpec(
            run_id="order",
            base_repo=str(repo),
            base_revision=base,
            # Deliberately reversed input order.
            workers=(
                WorkerTarget("consumer", str(consumer)),
                WorkerTarget("producer", str(producer)),
            ),
            artifact_dir=str(tmp_path / "runs"),
        )
    )
    assert result.clean
    merge = result.attempts[-1].merge
    assert merge is not None
    assert merge.applied_order == ("producer", "consumer")
