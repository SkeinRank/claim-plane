from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claim_plane import (
    AccessMode,
    ChangeIntent,
    IntentOperation,
    Plane,
    PlaneStore,
    ResourceKind,
    ResourceRef,
    SQLitePlaneStore,
)
from claim_plane.runtime import BrokerPolicy, BrokerServer


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
    db_name: str,
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
        db_path=str(tmp_path / db_name),
        instance_id=instance_id,
        writer_lease_seconds=300,
    )


def test_os_lock_blocks_same_worktree_across_separate_sqlite_registries(
    tmp_path: Path,
) -> None:
    repo, base = _repo(tmp_path / "repo")
    for db_name in ("plane-a.db", "plane-b.db"):
        plane = Plane.open(tmp_path / db_name)
        assert plane.admit(_intent(base)).allowed
        plane.close()

    first = BrokerServer(
        _policy(
            tmp_path,
            repo,
            db_name="plane-a.db",
            instance_id="broker-a",
            session_id="session-a",
        )
    )
    try:
        with pytest.raises(ValueError, match="active broker writer"):
            BrokerServer(
                _policy(
                    tmp_path,
                    repo,
                    db_name="plane-b.db",
                    instance_id="broker-b",
                    session_id="session-b",
                )
            )
    finally:
        first.server_close()

    second = BrokerServer(
        _policy(
            tmp_path,
            repo,
            db_name="plane-b.db",
            instance_id="broker-b",
            session_id="session-b",
        )
    )
    second.server_close()


def test_fencing_token_increases_and_stale_token_is_rejected(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    plane = Plane.open(tmp_path / "plane.db")
    assert plane.admit(_intent(base)).allowed
    plane.close()

    first = BrokerServer(
        _policy(
            tmp_path,
            repo,
            db_name="plane.db",
            instance_id="broker-a",
            session_id="session-a",
        )
    )
    first_token = int(first.core._registration["fencing_token"])
    first.server_close()

    second = BrokerServer(
        _policy(
            tmp_path,
            repo,
            db_name="plane.db",
            instance_id="broker-b",
            session_id="session-b",
        )
    )
    second_token = int(second.core._registration["fencing_token"])
    assert second_token > first_token

    plane = Plane.open(tmp_path / "plane.db")
    try:
        with pytest.raises(ValueError, match="stale broker fencing token"):
            plane.prepare_broker_operation(
                operation_id="stale-op",
                instance_id="broker-b",
                request_id="stale-request",
                operation="write_file",
                mode=AccessMode.WRITE,
                path="allowed.txt",
                target_path=None,
                payload={},
                broker_key=b"broker-key",
                fencing_token=first_token,
                pre_tree_hash=second.core._registration["expected_tree_hash"],
            )
    finally:
        plane.close()
        second.server_close()


def test_sqlite_store_implements_public_store_boundary(tmp_path: Path) -> None:
    store = SQLitePlaneStore(tmp_path / "plane.db")
    try:
        assert isinstance(store, PlaneStore)
        assert store.backend_name == "sqlite"
        assert store.single_host is True
    finally:
        store.close()
