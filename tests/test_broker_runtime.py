from __future__ import annotations

import json
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
from claim_plane.runtime import (
    BrokerClient,
    BrokerPolicy,
    BrokerServer,
    build_broker_boundary_command,
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
    (path / "allowed.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    (path / "outside.txt").write_text("secret\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path, _git(path, "rev-parse", "HEAD")


def _intent(base: str, *, bounded: bool = False) -> ChangeIntent:
    return ChangeIntent(
        intent_id="worker",
        task_id="worker",
        owner="agent-worker",
        base_revision="main",
        base_commit=base,
        operations=(
            IntentOperation(
                AccessMode.WRITE,
                ResourceRef(
                    ResourceKind.FILE,
                    "allowed.txt",
                    region="lines:2-3" if bounded else None,
                ),
            ),
        ),
    )


def _running_broker(
    tmp_path: Path,
    repo: Path,
    base: str,
    *,
    bounded: bool = False,
) -> tuple[Plane, BrokerServer, threading.Thread, BrokerClient, bytes]:
    db = tmp_path / "plane.db"
    plane = Plane.open(db)
    assert plane.admit(_intent(base, bounded=bounded)).allowed
    key = b"broker-observation-key"
    plane.start_observation_session(
        "broker-session",
        "worker",
        monitor_id="claim-plane-broker",
        coverage="brokered_proxy",
    )
    socket_path = tmp_path / "broker.sock"
    server = BrokerServer(
        BrokerPolicy(
            root=str(repo),
            intent_id="worker",
            session_id="broker-session",
            socket_path=str(socket_path),
            token="broker-token",
            observation_key=key,
            db_path=str(db),
        )
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = BrokerClient(socket_path, "broker-token")
    assert client.call("health")["ok"] is True
    return plane, server, thread, client, key


def test_broker_enforces_intent_and_records_server_side_events(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    plane, server, thread, client, key = _running_broker(tmp_path, repo, base)
    try:
        read = client.call("read_file", path="allowed.txt")
        assert read["ok"] and "two" in read["content"]
        denied = client.call("read_file", path="outside.txt")
        assert denied["ok"] is False
        written = client.call("write_file", path="allowed.txt", content="updated\n")
        assert written["ok"] is True
        assert (repo / "allowed.txt").read_text(encoding="utf-8") == "updated\n"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    plane.seal_observation_session("broker-session", key=key)
    verified = plane.verify_observation_session("broker-session", key=key)
    assert verified["valid"] is True
    assert verified["session"]["coverage"] == "brokered_proxy"
    assert all(
        item["metadata"].get("broker_protocol") == "claim-plane.broker.v2"
        for item in verified["accesses"]
    )
    plane.close()


def test_broker_enforces_declared_line_region(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    plane, server, thread, client, key = _running_broker(
        tmp_path, repo, base, bounded=True
    )
    try:
        denied = client.call("write_file", path="allowed.txt", content="all\n")
        assert denied["ok"] is False
        allowed = client.call(
            "replace_lines",
            path="allowed.txt",
            start_line=2,
            end_line=3,
            content="TWO\nTHREE\n",
        )
        assert allowed["ok"] is True
        outside = client.call(
            "replace_lines",
            path="allowed.txt",
            start_line=1,
            end_line=2,
            content="bad\n",
        )
        assert outside["ok"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    plane.seal_observation_session("broker-session", key=key)
    assert plane.verify_observation_session("broker-session", key=key)["valid"]
    plane.close()


def test_brokered_policy_accepts_only_broker_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _repo(tmp_path / "repo")
    plane, server, thread, client, key = _running_broker(tmp_path, repo, base)
    try:
        assert client.call("read_file", path="allowed.txt")["ok"]
        assert client.call("write_file", path="allowed.txt", content="changed\n")["ok"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    plane.seal_observation_session("broker-session", key=key)
    monkeypatch.setenv("OBSERVATION_KEY", key.decode("utf-8"))
    monkeypatch.setenv("BROKER_KEY", key.decode("utf-8"))
    result = plane.run_integration(
        IntegrationRunSpec(
            run_id="brokered-run",
            base_repo=str(repo),
            base_revision=base,
            base_commit=base,
            workers=(
                WorkerTarget(
                    "worker", str(repo), observation_session_id="broker-session"
                ),
            ),
            observation_policy=ObservationPolicy(mode="brokered"),
            observation_key_env="OBSERVATION_KEY",
            broker_key_env="BROKER_KEY",
            artifact_dir=str(tmp_path / "runs"),
        )
    )
    assert result.clean
    evidence = result.attempts[0].worker_evidence["worker"]
    assert evidence.observation_trusted is True
    payload = json.loads(Path(result.evidence_path or "").read_text(encoding="utf-8"))
    provenance = payload["provenance"]
    assert provenance["claim_plane_source_sha256"]
    assert provenance["policy_bundle_sha256"]
    plane.close()


def test_brokered_policy_rejects_generic_trusted_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _repo(tmp_path / "repo")
    (repo / "allowed.txt").write_text("changed\n", encoding="utf-8")
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent(base)).allowed
    key = b"key"
    monkeypatch.setenv("OBSERVATION_KEY", "key")
    plane.start_observation_session(
        "generic", "worker", monitor_id="generic", coverage="tool_proxy"
    )
    plane.record_observed_access(
        "generic",
        mode="write",
        kind="file",
        identifier="allowed.txt",
        tool="write_file",
        key=key,
    )
    plane.seal_observation_session("generic", key=key)
    with pytest.raises(ValueError, match="brokered_proxy"):
        plane.run_integration(
            IntegrationRunSpec(
                run_id="generic-rejected",
                base_repo=str(repo),
                base_revision=base,
                base_commit=base,
                workers=(
                    WorkerTarget("worker", str(repo), observation_session_id="generic"),
                ),
                observation_policy=ObservationPolicy(mode="brokered"),
                observation_key_env="OBSERVATION_KEY",
                artifact_dir=str(tmp_path / "runs"),
            )
        )
    plane.close()


def test_broker_boundary_preserves_existing_long_socket_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_parent = tmp_path / ("nested-" + "x" * 72)
    long_parent.mkdir()
    socket_path = long_parent / "broker.sock"
    socket_path.write_text("", encoding="utf-8")
    assert len(str(socket_path).encode()) > 100
    monkeypatch.setattr(
        "claim_plane.runtime.broker.shutil.which", lambda name: "/usr/bin/bwrap"
    )
    monkeypatch.setenv("BROKER_TOKEN", "token")

    argv = build_broker_boundary_command(
        "echo ok", socket_path=socket_path, token_env="BROKER_TOKEN"
    )

    assert str(socket_path) in argv


def test_broker_boundary_has_no_repository_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_path = tmp_path / "broker.sock"
    socket_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "claim_plane.runtime.broker.shutil.which", lambda name: "/usr/bin/bwrap"
    )
    monkeypatch.setenv("BROKER_TOKEN", "token")
    argv = build_broker_boundary_command(
        "echo ok", socket_path=socket_path, token_env="BROKER_TOKEN"
    )
    joined = " ".join(argv)
    assert "--tmpfs /" in joined
    assert "/run/claim-plane/broker.sock" in joined
    assert str(tmp_path / "repo") not in joined
