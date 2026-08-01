"""Swarm session and work-graph planning foundation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claim_plane.connectors.codex import init_project
from claim_plane.swarm import (
    WorkGraph,
    create_swarm_session,
    get_swarm_session,
    list_swarm_sessions,
    replace_swarm_work_graph,
    validate_work_graph,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Claim Plane Tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src/auth.py").write_text("def authenticate():\n    return True\n")
    (repo / "tests/test_auth.py").write_text("def test_auth():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "initial")
    init_project(repo)
    return repo


def _operation(path: str, *, commitment: str = "committed") -> dict[str, object]:
    payload: dict[str, object] = {
        "access": "write",
        "resource": {"kind": "file", "identifier": path},
    }
    if commitment != "committed":
        payload["commitment"] = commitment
    return payload


def _graph(*, reverse: bool = False) -> dict[str, object]:
    items = [
        {
            "work_id": "implementation",
            "title": "Implement authentication change",
            "goal": "Update the authentication implementation.",
            "operations": [_operation("src/auth.py")],
            "preserves": ["public API"],
            "acceptance": ["pytest -q tests/test_auth.py"],
        },
        {
            "work_id": "tests",
            "title": "Extend authentication tests",
            "goal": "Cover the updated authentication behavior.",
            "depends_on": ["implementation"],
            "operations": [_operation("tests/test_auth.py")],
            "acceptance": ["pytest -q tests/test_auth.py"],
        },
    ]
    if reverse:
        items.reverse()
    return {
        "protocol": "claim-plane.swarm-work-graph.v1",
        "work_items": items,
    }


def _spec() -> dict[str, object]:
    return {
        "protocol": "claim-plane.swarm-session-spec.v1",
        "root_task": {
            "title": "Harden authentication",
            "goal": "Improve authentication behavior and tests.",
            "acceptance": ["pytest -q tests/test_auth.py"],
        },
        "integration_target": {"branch": "main"},
        "work_graph": _graph(),
    }


def test_create_session_pins_repository_and_deterministic_graph(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = create_swarm_session(repo / "src", spec=_spec(), session_id="swm-test")

    assert result["created"] is True
    session = get_swarm_session(repo, "swm-test")
    assert session.base_commit == _git(repo, "rev-parse", "HEAD")
    assert session.repository_root == str(repo.resolve())
    assert session.graph_version == 1
    assert session.work_graph.topological_order() == ("implementation", "tests")
    assert session.work_graph.dependency_layers() == (
        ("implementation",),
        ("tests",),
    )
    assert session.work_graph.roots == ("implementation",)
    assert session.work_graph.leaves == ("tests",)
    assert (repo / ".claim-plane/swarm.db").is_file()


def test_graph_fingerprint_is_independent_of_input_item_order() -> None:
    left = WorkGraph.from_dict(_graph())
    right = WorkGraph.from_dict(_graph(reverse=True))

    assert left.to_dict() == right.to_dict()
    assert left.fingerprint() == right.fingerprint()


def test_graph_rejects_cycles_and_unknown_dependencies() -> None:
    cyclic = _graph()
    cyclic["work_items"][0]["depends_on"] = ["tests"]  # type: ignore[index]
    with pytest.raises(ValueError, match="dependency cycle"):
        WorkGraph.from_dict(cyclic)

    unknown = _graph()
    unknown["work_items"][1]["depends_on"] = ["missing"]  # type: ignore[index]
    with pytest.raises(ValueError, match="unknown dependencies: missing"):
        WorkGraph.from_dict(unknown)


def test_graph_rejects_duplicate_ids_and_protected_control_paths() -> None:
    duplicate = _graph()
    duplicate["work_items"][1]["work_id"] = "implementation"  # type: ignore[index]
    with pytest.raises(ValueError, match="duplicate work_id"):
        WorkGraph.from_dict(duplicate)

    protected = _graph()
    protected["work_items"][0]["operations"] = [  # type: ignore[index]
        _operation(".codex/hooks.json")
    ]
    with pytest.raises(ValueError, match="control state"):
        WorkGraph.from_dict(protected)

    protected_metadata = _graph()
    protected_metadata["work_items"][0]["operations"] = [  # type: ignore[index]
        {
            "access": "write",
            "resource": {
                "kind": "symbol",
                "identifier": "HookConfig",
                "metadata": {"path": ".codex/hooks.json"},
            },
        }
    ]
    with pytest.raises(ValueError, match="control state"):
        WorkGraph.from_dict(protected_metadata)

    escaping = _graph()
    escaping["work_items"][0]["operations"] = [  # type: ignore[index]
        _operation("../outside.py")
    ]
    with pytest.raises(ValueError, match="escapes the repository"):
        WorkGraph.from_dict(escaping)


def test_create_and_graph_replacement_are_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = create_swarm_session(repo, spec=_spec(), session_id="swm-idempotent")
    second = create_swarm_session(repo, spec=_spec(), session_id="swm-idempotent")

    assert first["created"] is True
    assert second["created"] is False
    assert second["session"]["created_at"] == first["session"]["created_at"]

    unchanged = replace_swarm_work_graph(
        repo,
        "swm-idempotent",
        graph_data=_graph(reverse=True),
        expected_version=1,
    )
    assert unchanged.graph_version == 1


def test_replace_graph_uses_optimistic_version_check(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    create_swarm_session(repo, spec=_spec(), session_id="swm-versioned")
    replacement = _graph()
    replacement["work_items"].append(  # type: ignore[union-attr]
        {
            "work_id": "docs",
            "title": "Document authentication behavior",
            "goal": "Update the authentication documentation.",
            "depends_on": ["implementation"],
            "operations": [
                {
                    "access": "document",
                    "resource": {"kind": "document", "identifier": "docs/auth.md"},
                }
            ],
        }
    )

    updated = replace_swarm_work_graph(
        repo,
        "swm-versioned",
        graph_data=replacement,
        expected_version=1,
    )
    assert updated.graph_version == 2
    assert len(updated.work_graph.work_items) == 3

    with pytest.raises(ValueError, match="stale work graph"):
        replace_swarm_work_graph(
            repo,
            "swm-versioned",
            graph_data=_graph(),
            expected_version=1,
        )


def test_sessions_are_listed_and_unknown_session_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    create_swarm_session(repo, spec=_spec(), session_id="swm-a")
    create_swarm_session(repo, spec=_spec(), session_id="swm-b")

    assert {session.session_id for session in list_swarm_sessions(repo)} == {
        "swm-a",
        "swm-b",
    }
    with pytest.raises(KeyError, match="unknown swarm session"):
        get_swarm_session(repo, "swm-missing")


def test_validate_work_graph_returns_machine_readable_topology() -> None:
    result = validate_work_graph(_graph())

    assert result["work_items"] == 2
    assert result["dependency_edges"] == 1
    assert result["topological_order"] == ["implementation", "tests"]
    assert result["dependency_layers"] == [["implementation"], ["tests"]]
    assert len(result["fingerprint"]) == 64


def test_swarm_database_is_private_local_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    create_swarm_session(repo, spec=_spec(), session_id="swm-private")

    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    assert ".claim-plane" not in status
    assert json.loads((repo / ".claim-plane/project.json").read_text())["protocol"] == (
        "claim-plane.project.v1"
    )
