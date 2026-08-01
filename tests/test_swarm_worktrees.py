"""Managed Git worktree provisioning, ownership, inspection, and cleanup."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claim_plane.connectors.codex import init_project
from claim_plane.swarm import (
    cleanup_swarm_worktrees,
    create_swarm_session,
    inspect_swarm_worktrees,
    managed_branch_name,
    managed_session_component,
    managed_worktree_path,
    plan_swarm_concurrency,
    provision_swarm_worktrees,
    replace_swarm_work_graph,
)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=check
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Claim Plane Tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "src" / "b.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    init_project(repo)
    return repo


def _item(work_id: str, path: str) -> dict[str, object]:
    return {
        "work_id": work_id,
        "title": f"Work {work_id}",
        "goal": f"Update {path}.",
        "operations": [
            {
                "access": "write",
                "resource": {"kind": "file", "identifier": path},
            }
        ],
    }


def _spec(*items: dict[str, object]) -> dict[str, object]:
    return {
        "protocol": "claim-plane.swarm-session-spec.v1",
        "root_task": {"title": "Worktree test", "goal": "Update files."},
        "work_graph": {
            "protocol": "claim-plane.swarm-work-graph.v1",
            "work_items": list(items),
        },
        "budget_policy": {
            "protocol": "claim-plane.swarm-budget-policy.v1",
            "workers": {"max_active": 2, "max_work_items": 16},
        },
    }


def _session(repo: Path, session_id: str = "swm-worktrees") -> None:
    create_swarm_session(
        repo,
        spec=_spec(_item("a", "src/a.py"), _item("b", "src/b.py")),
        session_id=session_id,
    )
    plan_swarm_concurrency(repo, session_id)


def test_provision_creates_isolated_owned_worktrees_at_pinned_base(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _session(repo)
    base = _git(repo, "rev-parse", "HEAD")

    result = provision_swarm_worktrees(repo, "swm-worktrees")

    assert result["created"] == 2
    assert result["reused"] == 0
    assert result["summary"]["health"] == {"ready": 2}
    for work_id in ("a", "b"):
        path = managed_worktree_path(repo, "swm-worktrees", work_id)
        branch = managed_branch_name("swm-worktrees", work_id)
        assert path.is_dir()
        assert _git(path, "rev-parse", "HEAD") == base
        assert _git(path, "branch", "--show-current") == branch
        assert _git(repo, "show-ref", "--verify", f"refs/heads/{branch}")


def test_provision_is_idempotent_and_reuses_owned_worktrees(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo)

    first = provision_swarm_worktrees(repo, "swm-worktrees")
    second = provision_swarm_worktrees(repo, "swm-worktrees")

    assert first["created"] == 2
    assert second["created"] == 0
    assert second["reused"] == 2
    assert [item["work_id"] for item in second["records"]] == ["a", "b"]


def test_provision_requires_a_current_ready_concurrency_plan(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    create_swarm_session(
        repo,
        spec=_spec(_item("a", "src/a.py")),
        session_id="swm-no-plan",
    )

    with pytest.raises(ValueError, match="no concurrency plan"):
        provision_swarm_worktrees(repo, "swm-no-plan")


def test_provision_refuses_unowned_path_or_branch_collisions(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    create_swarm_session(
        repo,
        spec=_spec(_item("a", "src/a.py")),
        session_id="swm-collision",
    )
    plan_swarm_concurrency(repo, "swm-collision")
    path = managed_worktree_path(repo, "swm-collision", "a")
    path.mkdir(parents=True)
    (path / "user-data.txt").write_text("keep\n", encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to overwrite existing path"):
        provision_swarm_worktrees(repo, "swm-collision")
    assert (path / "user-data.txt").read_text(encoding="utf-8") == "keep\n"

    path.rename(path.with_name(path.name + "-saved"))
    branch = managed_branch_name("swm-collision", "a")
    _git(repo, "branch", branch)
    with pytest.raises(ValueError, match="refusing to reuse unowned branch"):
        provision_swarm_worktrees(repo, "swm-collision")


def test_status_detects_dirty_and_graph_stale_worktrees(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo)
    provision_swarm_worktrees(repo, "swm-worktrees")
    path = managed_worktree_path(repo, "swm-worktrees", "a")
    (path / "src" / "a.py").write_text("value = 2\n", encoding="utf-8")

    dirty = inspect_swarm_worktrees(repo, "swm-worktrees")
    health = {
        item["record"]["work_id"]: item["health"] for item in dirty["worktrees"]
    }
    assert health == {"a": "dirty", "b": "ready"}

    replacement = {
        "protocol": "claim-plane.swarm-work-graph.v1",
        "work_items": [
            _item("a", "src/a.py"),
            _item("b", "src/b.py"),
            _item("c", "src/new.py"),
        ],
    }
    replace_swarm_work_graph(
        repo,
        "swm-worktrees",
        graph_data=replacement,
        expected_version=1,
    )
    stale = inspect_swarm_worktrees(repo, "swm-worktrees")
    assert {item["health"] for item in stale["worktrees"]} == {"stale_graph"}


def test_cleanup_refuses_dirty_worktree_without_force(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo)
    provision_swarm_worktrees(repo, "swm-worktrees")
    path = managed_worktree_path(repo, "swm-worktrees", "a")
    (path / "src" / "a.py").write_text("value = 2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="without --force"):
        cleanup_swarm_worktrees(
            repo, "swm-worktrees", work_ids=("a",), force=False
        )
    assert path.exists()

    result = cleanup_swarm_worktrees(
        repo, "swm-worktrees", work_ids=("a",), force=True
    )
    assert result["removed"] == 1
    assert not path.exists()
    assert (
        subprocess.run(
            [
                "git",
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{managed_branch_name('swm-worktrees', 'a')}",
            ],
            cwd=repo,
            check=False,
        ).returncode
        != 0
    )
    remaining = inspect_swarm_worktrees(repo, "swm-worktrees")
    assert [item["record"]["work_id"] for item in remaining["worktrees"]] == ["b"]


def test_cleanup_never_removes_unowned_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _session(repo)
    provision_swarm_worktrees(repo, "swm-worktrees")
    session_root = (
        repo
        / ".claim-plane"
        / "worktrees"
        / managed_session_component("swm-worktrees")
    )
    foreign = session_root / "foreign"
    _git(repo, "worktree", "add", "-b", "user/foreign", str(foreign), "HEAD")

    status = inspect_swarm_worktrees(repo, "swm-worktrees")
    assert len(status["orphans"]) == 1
    assert status["orphans"][0]["worktree_path"] == str(foreign.resolve())

    cleanup_swarm_worktrees(repo, "swm-worktrees", force=True)
    assert foreign.exists()
    assert _git(foreign, "branch", "--show-current") == "user/foreign"


def test_database_migrates_to_worktree_schema_without_losing_session(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _session(repo)
    database = repo / ".claim-plane" / "swarm.db"
    _git(repo, "worktree", "prune")

    import sqlite3

    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version=3")
    connection.commit()
    connection.close()

    result = provision_swarm_worktrees(repo, "swm-worktrees")
    assert result["created"] == 2
    connection = sqlite3.connect(database)
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    count = int(
        connection.execute("SELECT COUNT(*) FROM swarm_worktrees").fetchone()[0]
    )
    connection.close()
    assert version == 4
    assert count == 2
