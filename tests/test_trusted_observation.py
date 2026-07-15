from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from claim_plane import (
    AccessMode,
    ChangeIntent,
    IntentOperation,
    ObservationPolicy,
    Plane,
    ResourceKind,
    ResourceRef,
)
from claim_plane.integration import (
    IntegrationRunSpec,
    SandboxPolicy,
    WorkerTarget,
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
    (path / "a.txt").write_text("a0\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path, _git(path, "rev-parse", "HEAD")


def _intent(intent_id: str, base: str, path: str) -> ChangeIntent:
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
    )


def test_governed_admission_requires_pin_and_exploratory_is_explicit() -> None:
    unpinned = ChangeIntent(
        intent_id="worker",
        task_id="worker",
        owner="agent-worker",
        base_revision="main",
        operations=(
            IntentOperation(AccessMode.WRITE, ResourceRef(ResourceKind.FILE, "a.txt")),
        ),
    )
    governed = Plane.open(":memory:")
    with pytest.raises(ValueError, match="governed admission requires base_commit"):
        governed.admit(unpinned)
    governed.close()

    exploratory = Plane.open(":memory:", governance="exploratory")
    assert exploratory.admit(unpinned).allowed
    exploratory.close()


def test_trusted_observation_session_detects_database_tampering(tmp_path: Path) -> None:
    db = tmp_path / "plane.db"
    plane = Plane.open(db)
    assert plane.admit(_intent("worker", "a" * 40, "a.txt")).allowed
    key = b"trusted-monitor-key"
    plane.start_observation_session(
        "session-1", "worker", monitor_id="mcp-proxy", required_tools=("read_file",)
    )
    plane.record_observed_access(
        "session-1",
        mode="read",
        kind="file",
        identifier="config.py",
        tool="read_file",
        key=key,
    )
    plane.seal_observation_session("session-1", key=key)
    verified = plane.verify_observation_session("session-1", key=key)
    assert verified["valid"] is True
    assert verified["session"]["complete"] is True
    plane.close()

    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE observation_events SET access_json=? WHERE session_id=? AND seq=1",
        (
            '{"mode":"read","resource":{"kind":"file","identifier":"tampered.py"}}',
            "session-1",
        ),
    )
    connection.commit()
    connection.close()

    reopened = Plane.open(db)
    tampered = reopened.verify_observation_session("session-1", key=key)
    assert tampered["valid"] is False
    assert any("mismatch" in error for error in tampered["errors"])
    reopened.close()


def test_trusted_observation_policy_rejects_editable_trace(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a1\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        '{"mode":"read","resource":{"kind":"file","identifier":"a.txt"}}\n',
        encoding="utf-8",
    )
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent("worker", base, "a.txt")).allowed
    with pytest.raises(ValueError, match="rejects editable file traces"):
        plane.run_integration(
            IntegrationRunSpec(
                run_id="trusted-file-rejected",
                base_repo=str(repo),
                base_revision=base,
                workers=(
                    WorkerTarget("worker", str(repo), observation_trace=str(trace)),
                ),
                observation_policy=ObservationPolicy(mode="trusted"),
                observation_key_env="OBSERVATION_KEY",
                artifact_dir=str(tmp_path / "runs"),
            )
        )
    plane.close()


def test_trusted_observation_session_is_bound_into_worker_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a1\n", encoding="utf-8")
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent("worker", base, "a.txt")).allowed
    monkeypatch.setenv("OBSERVATION_KEY", "monitor-key")
    plane.start_observation_session(
        "session-1", "worker", monitor_id="trusted-proxy", coverage="tool_proxy"
    )
    plane.record_observed_access(
        "session-1",
        mode="write",
        kind="file",
        identifier="a.txt",
        tool="write_file",
        key=b"monitor-key",
    )
    plane.seal_observation_session("session-1", key=b"monitor-key")
    result = plane.run_integration(
        IntegrationRunSpec(
            run_id="trusted-session",
            base_repo=str(repo),
            base_revision=base,
            workers=(
                WorkerTarget("worker", str(repo), observation_session_id="session-1"),
            ),
            observation_policy=ObservationPolicy(mode="trusted"),
            observation_key_env="OBSERVATION_KEY",
            artifact_dir=str(tmp_path / "runs"),
        )
    )
    assert result.clean
    evidence = result.attempts[0].worker_evidence["worker"]
    assert evidence.observation_trusted is True
    assert evidence.observation_session_id == "session-1"
    assert evidence.observation_session_digest
    plane.close()


def test_repair_command_uses_fail_closed_sandbox(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent("worker", base, "missing.txt")).allowed
    result = plane.run_integration(
        IntegrationRunSpec(
            run_id="repair-sandbox",
            base_repo=str(repo),
            base_revision=base,
            workers=(WorkerTarget("worker", str(repo), repair_command="true"),),
            max_attempts=2,
            repair_sandbox=SandboxPolicy(backend="bwrap", strict=True),
            artifact_dir=str(tmp_path / "runs"),
        )
    )
    assert not result.clean
    execution = result.attempts[0].repair_executions[0]
    assert execution.returncode == 125
    assert execution.sandbox_backend == "unavailable"
    plane.close()


def test_ed25519_evidence_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cryptography = pytest.importorskip("cryptography")
    del cryptography
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    repo, base = _repo(tmp_path / "repo")
    (repo / "a.txt").write_text("a1\n", encoding="utf-8")
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    monkeypatch.setenv("ED25519_PRIVATE_KEY", private_pem.decode("utf-8"))

    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent("worker", base, "a.txt")).allowed
    result = plane.run_integration(
        IntegrationRunSpec(
            run_id="ed25519",
            base_repo=str(repo),
            base_revision=base,
            workers=(WorkerTarget("worker", str(repo)),),
            artifact_dir=str(tmp_path / "runs"),
            evidence_signing_key_env="ED25519_PRIVATE_KEY",
            evidence_signing_method="ed25519",
            evidence_key_id="test-ed25519",
        )
    )
    assert result.clean
    assert result.evidence_path and result.evidence_signature_path
    assert verify_evidence_file(
        result.evidence_path,
        result.evidence_signature_path,
        public_key_pem=public_pem,
    )
    plane.close()


def test_trusted_observation_detects_hidden_concurrent_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _repo(tmp_path / "repo")
    (repo / "consumer.txt").write_text("c0\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "consumer base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "producer-worker", base)
    _git(repo, "branch", "consumer-worker", base)
    producer = tmp_path / "producer"
    consumer = tmp_path / "consumer"
    _git(repo, "worktree", "add", "-q", str(producer), "producer-worker")
    _git(repo, "worktree", "add", "-q", str(consumer), "consumer-worker")
    (producer / "a.txt").write_text("a1\n", encoding="utf-8")
    (consumer / "consumer.txt").write_text("c1\n", encoding="utf-8")

    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent("producer", base, "a.txt")).allowed
    assert plane.admit(_intent("consumer", base, "consumer.txt")).allowed
    monkeypatch.setenv("OBSERVATION_KEY", "monitor-key")
    plane.start_observation_session(
        "producer-session", "producer", monitor_id="trusted-proxy"
    )
    plane.seal_observation_session("producer-session", key=b"monitor-key")
    plane.start_observation_session(
        "consumer-session", "consumer", monitor_id="trusted-proxy"
    )
    plane.record_observed_access(
        "consumer-session",
        mode="read",
        kind="file",
        identifier="a.txt",
        tool="read_file",
        key=b"monitor-key",
    )
    plane.seal_observation_session("consumer-session", key=b"monitor-key")

    result = plane.run_integration(
        IntegrationRunSpec(
            run_id="trusted-hidden-dependency",
            base_repo=str(repo),
            base_revision=base,
            workers=(
                WorkerTarget(
                    "producer", str(producer), observation_session_id="producer-session"
                ),
                WorkerTarget(
                    "consumer",
                    str(consumer),
                    observation_session_id="consumer-session",
                ),
            ),
            observation_policy=ObservationPolicy(mode="trusted"),
            observation_key_env="OBSERVATION_KEY",
            artifact_dir=str(tmp_path / "runs"),
        )
    )
    assert not result.clean
    findings = result.attempts[0].reports["consumer"].findings
    assert any(item.code.value == "observed_dependency_missing" for item in findings)
    plane.close()
