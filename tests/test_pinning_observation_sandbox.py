from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from claim_plane import (
    AccessMode,
    ChangeIntent,
    IntentOperation,
    Plane,
    ResourceKind,
    ResourceRef,
)
from claim_plane.integration import (
    IntegrationRunSpec,
    SandboxPolicy,
    WorkerTarget,
    append_observation,
    verify_evidence_file,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return result.stdout.strip()


def _repo(path: Path) -> tuple[Path, str]:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "producer.txt").write_text("base\n", encoding="utf-8")
    (path / "consumer.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path, _git(path, "rev-parse", "HEAD")


def _intent(
    intent_id: str,
    base: str,
    path: str,
    *,
    acceptance: tuple[str, ...] = (),
    dependencies: tuple[str, ...] = (),
) -> ChangeIntent:
    return ChangeIntent(
        intent_id=intent_id,
        task_id=intent_id,
        owner=f"agent-{intent_id}",
        base_revision="main",
        base_commit=base,
        operations=(
            IntentOperation(
                AccessMode.WRITE,
                ResourceRef(ResourceKind.FILE, path),
            ),
        ),
        acceptance=acceptance,
        dependencies=dependencies,
    )


def test_mutable_base_requires_exact_pin(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    plane = Plane.open(tmp_path / "plane.db")
    intent = ChangeIntent(
        intent_id="worker",
        task_id="worker",
        owner="agent-worker",
        base_revision="main",
        operations=(
            IntentOperation(
                AccessMode.WRITE, ResourceRef(ResourceKind.FILE, "producer.txt")
            ),
        ),
    )
    with pytest.raises(ValueError, match="governed admission requires base_commit"):
        plane.admit(intent)
    plane.close()

    exploratory = Plane.open(
        tmp_path / "plane-exploratory.db", governance="exploratory"
    )
    assert exploratory.admit(intent).allowed
    with pytest.raises(ValueError, match="base_commit pin"):
        exploratory.run_integration(
            IntegrationRunSpec(
                run_id="unpinned",
                base_repo=str(repo),
                base_revision="main",
                workers=(WorkerTarget("worker", str(repo)),),
                artifact_dir=str(tmp_path / "runs"),
            )
        )
    exploratory.close()
    plane = Plane.open(tmp_path / "plane-pinned.db")
    pinned = _intent("pinned", base, "producer.txt")
    assert plane.admit(pinned).allowed
    (repo / "producer.txt").write_text("changed\n", encoding="utf-8")
    # The exact commit is authoritative even when the human-readable ref is absent or moved.
    clean = plane.run_integration(
        IntegrationRunSpec(
            run_id="pinned-label",
            base_repo=str(repo),
            base_revision="main",
            base_commit=base,
            workers=(WorkerTarget("pinned", str(repo)),),
            artifact_dir=str(tmp_path / "runs"),
        )
    )
    assert clean.clean
    with pytest.raises(ValueError, match="not available"):
        plane.run_integration(
            IntegrationRunSpec(
                run_id="wrong-pin",
                base_repo=str(repo),
                base_revision=base,
                base_commit="0" * 40,
                workers=(WorkerTarget("pinned", str(repo)),),
                artifact_dir=str(tmp_path / "runs"),
            )
        )


def test_observed_read_requires_dependency_on_concurrent_writer(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    _git(repo, "branch", "producer-worker", base)
    _git(repo, "branch", "consumer-worker", base)
    producer = tmp_path / "producer"
    consumer = tmp_path / "consumer"
    _git(repo, "worktree", "add", "-q", str(producer), "producer-worker")
    _git(repo, "worktree", "add", "-q", str(consumer), "consumer-worker")
    (producer / "producer.txt").write_text("changed\n", encoding="utf-8")
    (consumer / "consumer.txt").write_text("changed\n", encoding="utf-8")

    trace = tmp_path / "consumer.trace.jsonl"
    append_observation(
        trace,
        mode=AccessMode.READ,
        kind=ResourceKind.FILE,
        identifier="producer.txt",
        tool="read_file",
    )

    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent("producer", base, "producer.txt")).allowed
    assert plane.admit(_intent("consumer", base, "consumer.txt")).allowed

    result = plane.run_integration(
        IntegrationRunSpec(
            run_id="observed-dependency",
            base_repo=str(repo),
            base_revision=base,
            workers=(
                WorkerTarget("producer", str(producer)),
                WorkerTarget("consumer", str(consumer), observation_trace=str(trace)),
            ),
            artifact_dir=str(tmp_path / "runs"),
        )
    )
    assert not result.clean
    findings = result.attempts[0].reports["consumer"].findings
    assert any(item.code.value == "observed_dependency_missing" for item in findings)


def test_signed_evidence_and_file_checksums(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _repo(tmp_path / "repo")
    (repo / "producer.txt").write_text("changed\n", encoding="utf-8")
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent("worker", base, "producer.txt")).allowed
    monkeypatch.setenv("CLAIM_PLANE_TEST_SIGNING_KEY", "secret-key")

    result = plane.run_integration(
        IntegrationRunSpec(
            run_id="signed",
            base_repo=str(repo),
            base_revision=base,
            workers=(WorkerTarget("worker", str(repo)),),
            artifact_dir=str(tmp_path / "runs"),
            evidence_signing_key_env="CLAIM_PLANE_TEST_SIGNING_KEY",
            evidence_key_id="tests",
        )
    )
    assert result.clean
    assert result.evidence_path and result.evidence_signature_path
    evidence_bytes = Path(result.evidence_path).read_bytes()
    assert hashlib.sha256(evidence_bytes).hexdigest() == result.evidence_file_sha256
    assert verify_evidence_file(
        result.evidence_path,
        result.evidence_signature_path,
        key=os.environ["CLAIM_PLANE_TEST_SIGNING_KEY"].encode(),
    )
    signature = json.loads(Path(result.evidence_signature_path).read_text())
    assert signature["key_id"] == "tests"


def test_strict_sandbox_fails_closed_without_backend(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    (repo / "producer.txt").write_text("changed\n", encoding="utf-8")
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(
        _intent("worker", base, "producer.txt", acceptance=("true",))
    ).allowed
    result = plane.run_integration(
        IntegrationRunSpec(
            run_id="strict-sandbox",
            base_repo=str(repo),
            base_revision=base,
            workers=(WorkerTarget("worker", str(repo)),),
            artifact_dir=str(tmp_path / "runs"),
            worker_sandbox=SandboxPolicy(backend="bwrap", strict=True),
        )
    )
    assert not result.clean
    acceptance = (
        result.attempts[0].worker_evidence["worker"].manifest.acceptance_results
    )
    assert acceptance and acceptance[0].returncode == 125
    assert "no supported backend" in acceptance[0].stderr_tail
