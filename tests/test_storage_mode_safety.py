from __future__ import annotations

import os
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from claim_plane import (
    AccessMode,
    ChangeIntent,
    IntentOperation,
    Plane,
    ResourceKind,
    ResourceRef,
    SQLitePlaneStore,
    validate_plane_store,
)
from claim_plane.runtime import (
    BrokerClient,
    BrokerPolicy,
    BrokerServer,
    canonical_worktree_lock_dir,
    worktree_lock_path,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def _repo(path: Path, *, executable: bool = False) -> tuple[Path, str]:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    target = path / "allowed.txt"
    target.write_text("base\n", encoding="utf-8")
    if executable and os.name != "nt":
        target.chmod(0o755)
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
    tmp_path: Path, repo: Path, *, lock_dir: Path | None = None
) -> BrokerPolicy:
    return BrokerPolicy(
        root=str(repo),
        intent_id="worker",
        session_id="session",
        socket_path=str(tmp_path / "broker.sock"),
        token="token",
        observation_key=b"observation-key",
        broker_key=b"broker-key",
        db_path=str(tmp_path / "plane.db"),
        instance_id="broker-instance",
        worktree_lock_dir=None if lock_dir is None else str(lock_dir),
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


def test_lock_namespace_is_derived_from_git_common_dir(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path / "repo")
    common_raw = _git(repo, "rev-parse", "--git-common-dir")
    common = Path(common_raw)
    if not common.is_absolute():
        common = repo / common
    expected_dir = common.resolve() / "claim-plane" / "worktree-locks"

    assert canonical_worktree_lock_dir(repo) == expected_dir
    assert worktree_lock_path(repo).parent == expected_dir


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics differ on Windows")
def test_canonical_lock_namespace_rejects_symlinked_parent(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path / "repo")
    common_raw = _git(repo, "rev-parse", "--git-common-dir")
    common = Path(common_raw)
    if not common.is_absolute():
        common = repo / common
    redirected = tmp_path / "redirected-locks"
    redirected.mkdir()
    (common.resolve() / "claim-plane").symlink_to(redirected, target_is_directory=True)

    with pytest.raises(ValueError, match="must not contain a symlink"):
        _policy(tmp_path, repo)


def test_custom_lock_namespace_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = _repo(tmp_path / "repo")
    with pytest.raises(ValueError, match="custom worktree lock directories"):
        _policy(tmp_path, repo, lock_dir=tmp_path / "independent-locks")

    monkeypatch.setenv("CLAIM_PLANE_LOCK_DIR", str(tmp_path / "other-locks"))
    with pytest.raises(ValueError, match="custom worktree lock directories"):
        _policy(tmp_path, repo)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits are not portable")
def test_broker_write_preserves_posix_mode_and_records_it(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo", executable=True)
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent(base)).allowed
    server = BrokerServer(_policy(tmp_path, repo))
    thread, client = _start(server)
    try:
        response = client.call("write_file", path="allowed.txt", content="updated\n")
        assert response["ok"] is True
        assert response["file_mode"] == "0755"
        assert stat.S_IMODE((repo / "allowed.txt").stat().st_mode) == 0o755
        operation = plane.broker_operation_for_request(
            "broker-instance", str(response["request_id"])
        )
        assert operation is not None
        journal = operation["payload_json"]["journal"]
        assert journal["old_mode"] == 0o755
        assert journal["new_mode"] == 0o755
    finally:
        _stop(server, thread)
        plane.seal_observation_session("session", key=b"observation-key")
        plane.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits are not portable")
def test_broker_rollback_restores_posix_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _repo(tmp_path / "repo", executable=True)
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent(base)).allowed
    server = BrokerServer(_policy(tmp_path, repo))
    thread, client = _start(server)

    def fail_commit(self: Plane, operation_id: str, **kwargs: object) -> dict[str, Any]:
        raise RuntimeError(f"simulated commit failure: {operation_id}")

    monkeypatch.setattr(Plane, "commit_broker_operation", fail_commit)
    try:
        response = client.call("write_file", path="allowed.txt", content="updated\n")
        assert response["ok"] is False
        assert (repo / "allowed.txt").read_text(encoding="utf-8") == "base\n"
        assert stat.S_IMODE((repo / "allowed.txt").stat().st_mode) == 0o755
    finally:
        monkeypatch.undo()
        _stop(server, thread)
        plane.seal_observation_session("session", key=b"observation-key")
        plane.close()


class _DelegatingStore:
    backend_name = "delegating-test"
    single_host = True

    def __init__(self, inner: SQLitePlaneStore) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def test_complete_store_contract_accepts_non_sqlite_adapter(tmp_path: Path) -> None:
    backend = _DelegatingStore(SQLitePlaneStore(tmp_path / "plane.db"))
    store = validate_plane_store(backend)
    assert store is backend
    plane = Plane.from_store(store)
    try:
        _, base = _repo(tmp_path / "repo")
        assert plane.admit(_intent(base)).allowed
    finally:
        plane.close()


def test_incomplete_store_fails_before_plane_startup() -> None:
    class IncompleteStore:
        backend_name = "incomplete"
        single_host = True

        def close(self) -> None:
            return None

    with pytest.raises(TypeError, match="incomplete PlaneStore backend") as error:
        validate_plane_store(IncompleteStore())
    assert "admit_intent" in str(error.value)
    assert "register_broker_instance" in str(error.value)
