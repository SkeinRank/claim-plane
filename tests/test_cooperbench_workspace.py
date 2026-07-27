from __future__ import annotations

from pathlib import Path

from experiments.cooperbench.paper_6pair import coder


def test_workspace_root_adds_required_agent_workspace_parent(tmp_path: Path) -> None:
    root = coder.configure_workspace_root(tmp_path / "worktrees")

    assert root == (tmp_path / "worktrees" / "agent_workspace").resolve()
    assert root.name == "agent_workspace"
    assert root.is_dir()


def test_workspace_root_does_not_double_nest_agent_workspace(tmp_path: Path) -> None:
    requested = tmp_path / "agent_workspace"
    root = coder.configure_workspace_root(requested)

    assert root == requested.resolve()
    assert root.parent == tmp_path.resolve()
