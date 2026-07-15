from __future__ import annotations

import subprocess
import threading
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
from claim_plane.integration import IntegrationRunSpec, WorkerTarget
from claim_plane.integration.snapshot import capture_worktree_tree
from claim_plane.runtime import BrokerClient, BrokerPolicy, BrokerServer
from claim_plane.runtime.broker import BrokerError


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
    (path / "allowed.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path, _git(path, "rev-parse", "HEAD")


def _intent(base: str) -> ChangeIntent:
    return ChangeIntent(
        intent_id="worker",
        task_id="worker",
        owner="agent-worker",
        base_revision="main",
        base_commit=base,
        operations=(
            IntentOperation(
                AccessMode.WRITE,
                ResourceRef(ResourceKind.FILE, "allowed.txt"),
            ),
        ),
    )


def _policy(
    tmp_path: Path,
    repo: Path,
    *,
    instance_id: str,
    session_id: str,
) -> BrokerPolicy:
    return BrokerPolicy(
        root=str(repo),
        intent_id="worker",
        session_id=session_id,
        socket_path=str(tmp_path / f"{instance_id}.sock"),
        token="token",
        observation_key=b"observation-key",
        broker_key=b"broker-key",
        db_path=str(tmp_path / "plane.db"),
        instance_id=instance_id,
        writer_lease_seconds=300,
    )


def _start(server: BrokerServer) -> tuple[threading.Thread, BrokerClient]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = BrokerClient(server.policy.socket_path, "token")
    assert client.call("health")["ok"] is True
    return thread, client


def _stop(server: BrokerServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_broker_rejects_preexisting_unbrokered_changes(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent(base)).allowed
    (repo / "allowed.txt").write_text("unbrokered\n", encoding="utf-8")

    with pytest.raises(BrokerError, match="requires a clean worktree"):
        BrokerServer(
            _policy(
                tmp_path,
                repo,
                instance_id="broker-a",
                session_id="session-a",
            )
        )
    plane.close()


def test_only_one_active_broker_writer_per_worktree(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent(base)).allowed
    first = BrokerServer(
        _policy(tmp_path, repo, instance_id="broker-a", session_id="session-a")
    )
    thread, client = _start(first)
    try:
        with pytest.raises(ValueError, match="active broker writer"):
            BrokerServer(
                _policy(
                    tmp_path,
                    repo,
                    instance_id="broker-b",
                    session_id="session-b",
                )
            )
        assert client.call("health")["ok"] is True
    finally:
        _stop(first, thread)
    plane.close()


def test_writer_lease_is_released_on_shutdown(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent(base)).allowed
    first = BrokerServer(
        _policy(tmp_path, repo, instance_id="broker-a", session_id="session-a")
    )
    first.server_close()

    second = BrokerServer(
        _policy(tmp_path, repo, instance_id="broker-b", session_id="session-b")
    )
    second.server_close()
    plane.close()


def test_external_worktree_mutation_revokes_live_broker(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent(base)).allowed
    server = BrokerServer(
        _policy(tmp_path, repo, instance_id="broker-a", session_id="session-a")
    )
    thread, client = _start(server)
    try:
        assert client.call("write_file", path="allowed.txt", content="brokered\n")["ok"]
        (repo / "allowed.txt").write_text("outside\n", encoding="utf-8")
        denied = client.call("read_file", path="allowed.txt")
        assert denied["ok"] is False
        assert "diverged from the committed operation chain" in denied["error"]
    finally:
        _stop(server, thread)
        plane.seal_observation_session("session-a", key=b"observation-key")
        plane.close()


def test_integration_requires_exact_broker_derived_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _repo(tmp_path / "repo")
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent(base)).allowed
    server = BrokerServer(
        _policy(tmp_path, repo, instance_id="broker-a", session_id="session-a")
    )
    thread, client = _start(server)
    try:
        assert client.call("write_file", path="allowed.txt", content="brokered\n")["ok"]
    finally:
        _stop(server, thread)
    plane.seal_observation_session("session-a", key=b"observation-key")

    verified = plane.verify_broker_session("session-a", broker_key=b"broker-key")
    assert verified["valid"] is True
    instance = verified["instance"]
    assert instance is not None
    assert instance["expected_tree_hash"] == capture_worktree_tree(repo, seed=base)

    # The selected broker evidence describes the brokered tree, not this direct edit.
    (repo / "allowed.txt").write_text("outside\n", encoding="utf-8")
    monkeypatch.setenv("OBSERVATION_KEY", "observation-key")
    monkeypatch.setenv("BROKER_KEY", "broker-key")
    with pytest.raises(ValueError, match="broker-derived tree"):
        plane.run_integration(
            IntegrationRunSpec(
                run_id="tree-provenance",
                base_repo=str(repo),
                base_revision=base,
                base_commit=base,
                workers=(
                    WorkerTarget(
                        "worker", str(repo), observation_session_id="session-a"
                    ),
                ),
                observation_policy=ObservationPolicy(mode="brokered"),
                observation_key_env="OBSERVATION_KEY",
                broker_key_env="BROKER_KEY",
                artifact_dir=str(tmp_path / "runs"),
            )
        )
    plane.close()
