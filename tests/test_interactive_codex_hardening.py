from __future__ import annotations

import io
import subprocess
from pathlib import Path

from claim_plane.connectors import codex
from claim_plane.connectors.codex_adapter import CodexAdapter


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "claim-plane@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Claim Plane Tests"],
        cwd=repo,
        check=True,
    )
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    codex.init_project(repo)
    codex.connect_codex(repo)
    return repo


def test_codex_hook_cache_keys_are_namespaced_per_lifecycle_event(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    adapter = CodexAdapter()
    session_id = "thread_shared_turn_id"
    shared_event_id = "turn-1"

    assert (
        adapter.dispatch_hook(
            {
                "hook_event_name": "SessionStart",
                "session_id": session_id,
                "cwd": str(repo),
                "event_id": shared_event_id,
                "source": "startup",
            }
        )
        == 0
    )
    prompt_output = io.StringIO()
    assert (
        adapter.dispatch_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session_id,
                "cwd": str(repo),
                "event_id": shared_event_id,
                "prompt": "Inspect the fixture before editing.",
            },
            output=prompt_output,
        )
        == 0
    )
    assert "Claim Plane is enrolled" in prompt_output.getvalue()

    pretool = {
        "hook_event_name": "PreToolUse",
        "session_id": session_id,
        "cwd": str(repo),
        "event_id": shared_event_id,
        "tool_use_id": "tool-1",
        "tool_name": "shell",
        "tool_input": {
            "command": "pwd; printf '%s\\n' '--- files ---'; git status --short"
        },
    }
    assert adapter.dispatch_hook(pretool, output=io.StringIO()) == 0
    # An exact hook replay remains idempotent.
    assert adapter.dispatch_hook(pretool, output=io.StringIO()) == 0
    assert (
        adapter.dispatch_hook(
            {
                **pretool,
                "hook_event_name": "PostToolUse",
                "tool_response": {"exit_code": 0},
            },
            output=io.StringIO(),
        )
        == 0
    )


def test_doctor_distinguishes_managed_codex_hooks_from_user_dirty_state(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)

    report = codex.doctor_codex(repo)
    checks = {str(item["name"]): item for item in report.checks}

    assert checks["working_tree"]["status"] == "info"
    assert "managed Codex connector state" in checks["working_tree"]["detail"]
    assert checks["managed_connector_state"]["status"] == "info"
    assert checks["managed_connector_state"]["detail"] == ".codex/hooks.json"
