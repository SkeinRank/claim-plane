from __future__ import annotations

import subprocess
import threading
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
    (path / "allowed.txt").write_text("one\ntwo\n", encoding="utf-8")
    (path / "outside.txt").write_text("outside\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path, _git(path, "rev-parse", "HEAD")


def _intent(
    base: str,
    access: AccessMode = AccessMode.WRITE,
    *,
    path: str = "allowed.txt",
    metadata: dict[str, str] | None = None,
    extra: tuple[IntentOperation, ...] = (),
) -> ChangeIntent:
    return ChangeIntent(
        intent_id="worker",
        task_id="worker",
        owner="agent-worker",
        base_revision="main",
        base_commit=base,
        operations=(
            IntentOperation(
                access,
                ResourceRef(ResourceKind.FILE, path),
                metadata=metadata or {},
            ),
            *extra,
        ),
    )


def _broker(
    tmp_path: Path,
    repo: Path,
    intent: ChangeIntent,
    *,
    commands: dict[str, object] | None = None,
) -> tuple[Plane, BrokerServer, threading.Thread, BrokerClient, bytes, bytes]:
    db = tmp_path / "plane.db"
    plane = Plane.open(db)
    assert plane.admit(intent).allowed
    observation_key = b"observation-key"
    broker_key = b"broker-key"
    server = BrokerServer(
        BrokerPolicy(
            root=str(repo),
            intent_id=intent.intent_id,
            session_id="session",
            socket_path=str(tmp_path / "broker.sock"),
            token="token",
            observation_key=observation_key,
            broker_key=broker_key,
            db_path=str(db),
            instance_id="broker-instance",
            commands=commands or {},
        )
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = BrokerClient(tmp_path / "broker.sock", "token")
    assert client.call("health")["ok"]
    return plane, server, thread, client, observation_key, broker_key


def _stop(
    plane: Plane,
    server: BrokerServer,
    thread: threading.Thread,
    observation_key: bytes,
) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    session = plane.observation_session("session")
    if session["state"] == "open":
        plane.seal_observation_session("session", key=observation_key)


def test_broker_enforces_exact_mutation_capabilities(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    plane, server, thread, client, observation_key, _ = _broker(
        tmp_path, repo, _intent(base, AccessMode.WRITE)
    )
    try:
        denied = client.call("delete_file", path="allowed.txt")
        assert denied["ok"] is False
        assert "delete capability" in denied["error"]
        assert (repo / "allowed.txt").exists()
    finally:
        _stop(plane, server, thread, observation_key)
        plane.close()


def test_extend_capability_only_allows_append(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    plane, server, thread, client, observation_key, _ = _broker(
        tmp_path, repo, _intent(base, AccessMode.EXTEND)
    )
    try:
        assert (
            client.call("write_file", path="allowed.txt", content="replace\n")["ok"]
            is False
        )
        appended = client.call("append_file", path="allowed.txt", content="three\n")
        assert appended["ok"] is True
        assert (repo / "allowed.txt").read_text(encoding="utf-8").endswith("three\n")
    finally:
        _stop(plane, server, thread, observation_key)
        plane.close()


def test_rename_requires_declared_destination(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    plane, server, thread, client, observation_key, _ = _broker(
        tmp_path,
        repo,
        _intent(base, AccessMode.RENAME, metadata={"rename_to": "renamed.txt"}),
    )
    try:
        denied = client.call("rename_file", path="allowed.txt", target_path="wrong.txt")
        assert denied["ok"] is False
        renamed = client.call(
            "rename_file", path="allowed.txt", target_path="renamed.txt"
        )
        assert renamed["ok"] is True
        assert not (repo / "allowed.txt").exists()
        assert (repo / "renamed.txt").exists()
    finally:
        _stop(plane, server, thread, observation_key)
        plane.close()


def test_write_ahead_journal_rolls_back_when_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _repo(tmp_path / "repo")
    original = (repo / "allowed.txt").read_text(encoding="utf-8")
    plane, server, thread, client, observation_key, _ = _broker(
        tmp_path, repo, _intent(base)
    )

    def fail_commit(self: Plane, operation_id: str, **kwargs: object) -> dict:  # noqa: ARG001
        raise RuntimeError("simulated observation commit failure")

    monkeypatch.setattr(Plane, "commit_broker_operation", fail_commit)
    try:
        response = client.call("write_file", path="allowed.txt", content="changed\n")
        assert response["ok"] is False
        assert (repo / "allowed.txt").read_text(encoding="utf-8") == original
        operation = plane.broker_operation_for_request(
            "broker-instance", response.get("request_id", "")
        )
        # The response may not echo the request id on error, so inspect the audit.
        if operation is None:
            operations = plane.pending_broker_operations("broker-instance")
            assert operations == []
    finally:
        monkeypatch.undo()
        _stop(plane, server, thread, observation_key)
        audit = tmp_path / "audit.json"
        plane.export_audit(audit)
        assert '"state": "rolled_back"' in audit.read_text(encoding="utf-8")
        plane.close()


def test_live_release_revokes_running_broker(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    original = (repo / "allowed.txt").read_text(encoding="utf-8")
    plane, server, thread, client, observation_key, _ = _broker(
        tmp_path, repo, _intent(base)
    )
    try:
        plane.release_intent("worker")
        denied = client.call("write_file", path="allowed.txt", content="changed\n")
        assert denied["ok"] is False
        assert "revoked" in denied["error"]
        assert (repo / "allowed.txt").read_text(encoding="utf-8") == original
    finally:
        _stop(plane, server, thread, observation_key)
        plane.close()


def test_amendment_revokes_old_content_capability(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    plane, server, thread, client, observation_key, _ = _broker(
        tmp_path, repo, _intent(base)
    )
    try:
        amended = _intent(
            base,
            AccessMode.WRITE,
            extra=(
                IntentOperation(
                    AccessMode.READ, ResourceRef(ResourceKind.FILE, "outside.txt")
                ),
            ),
        )
        assert plane.amend(amended).allowed
        denied = client.call("write_file", path="allowed.txt", content="changed\n")
        assert denied["ok"] is False
        assert "amendment" in denied["error"]
    finally:
        _stop(plane, server, thread, observation_key)
        plane.close()


def test_broker_registration_rejects_wrong_root_head(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    db = tmp_path / "plane.db"
    plane = Plane.open(db)
    assert plane.admit(_intent(base)).allowed
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "advance")
    with pytest.raises(BrokerError, match="does not match intent base"):
        BrokerServer(
            BrokerPolicy(
                root=str(repo),
                intent_id="worker",
                session_id="session",
                socket_path=str(tmp_path / "broker.sock"),
                token="token",
                observation_key=b"observation",
                broker_key=b"broker",
                db_path=str(db),
            )
        )
    plane.close()


def test_broker_session_requires_registered_instance(tmp_path: Path) -> None:
    _, base = _repo(tmp_path / "repo")
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent(base)).allowed
    plane.start_observation_session(
        "forged",
        "worker",
        monitor_id="claimed-broker",
        coverage="brokered_proxy",
    )
    plane.seal_observation_session("forged", key=b"observation")
    verified = plane.verify_broker_session("forged", broker_key=b"broker")
    assert verified["valid"] is False
    assert "not bound" in verified["errors"][0]
    plane.close()


def test_broker_operation_attestation_verifies_after_shutdown(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    plane, server, thread, client, observation_key, broker_key = _broker(
        tmp_path, repo, _intent(base)
    )
    try:
        assert client.call("read_file", path="allowed.txt")["ok"]
        assert client.call("write_file", path="allowed.txt", content="changed\n")["ok"]
    finally:
        _stop(plane, server, thread, observation_key)
    verified = plane.verify_broker_session("session", broker_key=broker_key)
    assert verified["valid"] is True
    assert len(verified["operations"]) == 2
    plane.close()


def test_allowlisted_command_runs_on_snapshot_and_cannot_mutate_root(
    tmp_path: Path,
) -> None:
    repo, base = _repo(tmp_path / "repo")
    intent = _intent(
        base,
        AccessMode.READ,
        extra=(
            IntentOperation(
                AccessMode.TEST, ResourceRef(ResourceKind.FILE, "allowed.txt")
            ),
        ),
    )
    plane, server, thread, client, observation_key, _ = _broker(
        tmp_path,
        repo,
        intent,
        commands={
            "check": {
                "argv": [
                    "python",
                    "-c",
                    "from pathlib import Path; print(Path('allowed.txt').read_text())",
                ]
            },
            "mutate": {
                "argv": [
                    "python",
                    "-c",
                    "from pathlib import Path; Path('allowed.txt').write_text('bad')",
                ]
            },
        },
    )
    original = (repo / "allowed.txt").read_text(encoding="utf-8")
    try:
        checked = client.call("run_command", name="check")
        assert checked["ok"] is True
        assert "one" in checked["stdout_tail"]
        mutated = client.call("run_command", name="mutate")
        assert mutated["ok"] is False
        assert mutated["snapshot_immutable"] is False
        assert (repo / "allowed.txt").read_text(encoding="utf-8") == original
        denied = client.call("run_command", name="arbitrary")
        assert denied["ok"] is False
    finally:
        _stop(plane, server, thread, observation_key)
        plane.close()


def test_broker_shortens_long_unix_socket_path_portably(tmp_path: Path) -> None:
    """Long macOS/CloudStorage paths must not exceed sockaddr_un.sun_path."""

    long_root = tmp_path / ("nested-" + "a" * 48) / ("nested-" + "b" * 48)
    long_root.mkdir(parents=True)
    repo, base = _repo(long_root / "repo")
    requested_socket = long_root / "broker.sock"
    assert len(str(requested_socket).encode()) > 100

    db = long_root / "plane.db"
    plane = Plane.open(db)
    intent = _intent(base)
    assert plane.admit(intent).allowed
    server = BrokerServer(
        BrokerPolicy(
            root=str(repo),
            intent_id=intent.intent_id,
            session_id="long-path-session",
            socket_path=str(requested_socket),
            token="token",
            observation_key=b"observation-key",
            broker_key=b"broker-key",
            db_path=str(db),
            instance_id="long-path-broker",
        )
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # The public caller may continue to use the original long path.  The
        # client deterministically maps it to the same short path as the server.
        client = BrokerClient(requested_socket, "token")
        assert client.call("health")["ok"] is True
        assert len(server.policy.socket_path.encode()) <= 100
        assert server.policy.socket_path != str(requested_socket)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        plane.close()
