from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claim_plane.connectors import codex


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
    return repo


def _handlers(payload: dict[str, object], event: str) -> list[dict[str, object]]:
    hooks = payload["hooks"]
    assert isinstance(hooks, dict)
    groups = hooks[event]
    assert isinstance(groups, list)
    result: list[dict[str, object]] = []
    for group in groups:
        assert isinstance(group, dict)
        handlers = group.get("hooks", [])
        assert isinstance(handlers, list)
        result.extend(item for item in handlers if isinstance(item, dict))
    return result


def test_init_keeps_local_state_out_of_git_status(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    first = codex.init_project(repo)
    second = codex.init_project(repo)

    state = json.loads((repo / ".claim-plane/project.json").read_text())
    assert state["protocol"] == "claim-plane.project.v1"
    assert first["root"] == str(repo.resolve())
    assert second["root"] == str(repo.resolve())

    exclude_path = Path(first["exclude"])
    exclude = exclude_path.read_text(encoding="utf-8")
    assert exclude.count(".claim-plane/") == 1
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        == ""
    )


def test_connect_codex_preserves_foreign_hooks_and_is_idempotent(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    codex.init_project(repo)
    hooks_path = repo / ".codex/hooks.json"
    hooks_path.parent.mkdir()
    hooks_path.write_text(
        json.dumps(
            {
                "description": "workspace hooks",
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "./tools/check-command",
                                }
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    codex.connect_codex(repo)
    codex.connect_codex(repo)

    payload = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert payload["description"] == "workspace hooks"
    pre_handlers = _handlers(payload, "PreToolUse")
    assert any(item.get("command") == "./tools/check-command" for item in pre_handlers)
    assert (
        sum(item.get("command") == codex.CODEX_HOOK_COMMAND for item in pre_handlers)
        == 1
    )
    for event in codex.CODEX_HOOK_EVENTS:
        handlers = _handlers(payload, event)
        assert (
            sum(item.get("command") == codex.CODEX_HOOK_COMMAND for item in handlers)
            == 1
        )

    state = json.loads((repo / ".claim-plane/codex.json").read_text())
    assert state["protocol"] == codex.CODEX_ENROLLMENT_PROTOCOL
    assert state["events"] == list(codex.CODEX_HOOK_EVENTS)


def test_connect_from_subdirectory_enrolls_git_root(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    nested = repo / "src" / "service"
    nested.mkdir(parents=True)

    codex.init_project(nested)
    result = codex.connect_codex(nested)

    assert result["root"] == str(repo.resolve())
    assert (repo / ".codex/hooks.json").is_file()
    assert not (nested / ".codex").exists()


def test_disconnect_removes_only_claim_plane_handlers(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    codex.init_project(repo)
    codex.connect_codex(repo)
    hooks_path = repo / ".codex/hooks.json"
    payload = json.loads(hooks_path.read_text(encoding="utf-8"))
    payload["description"] = "keep me"
    payload["hooks"]["Stop"].append(
        {"hooks": [{"type": "command", "command": "./tools/after-turn"}]}
    )
    hooks_path.write_text(json.dumps(payload), encoding="utf-8")

    result = codex.disconnect_codex(repo)

    assert result["removed_handlers"] == len(codex.CODEX_HOOK_EVENTS)
    remaining = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert remaining["description"] == "keep me"
    assert _handlers(remaining, "Stop") == [
        {"type": "command", "command": "./tools/after-turn"}
    ]
    assert not (repo / ".claim-plane/codex.json").exists()


def test_connect_refuses_project_that_explicitly_disables_hooks(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    codex.init_project(repo)
    config = repo / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text(
        'model = "gpt-5"\n\n[features]\nhooks = false\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match="disables Codex hooks"):
        codex.connect_codex(repo)

    assert not (repo / ".codex/hooks.json").exists()
    assert not (repo / ".claim-plane/codex.json").exists()


def test_connect_respects_deprecated_codex_hooks_disable_alias(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    codex.init_project(repo)
    config = repo / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text("[features]\ncodex_hooks = false\n", encoding="utf-8")

    with pytest.raises(ValueError, match="disables Codex hooks"):
        codex.connect_codex(repo)


def test_doctor_reports_enrollment_and_missing_lifecycle_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    codex.init_project(repo)
    codex.connect_codex(repo)
    monkeypatch.setattr(
        codex, "_codex_version", lambda: ("/usr/bin/codex", "codex 1.2.3")
    )

    healthy = codex.doctor_codex(repo)
    assert healthy.ready is True
    assert healthy.codex_version == "codex 1.2.3"

    hooks_path = repo / ".codex/hooks.json"
    payload = json.loads(hooks_path.read_text(encoding="utf-8"))
    payload["hooks"].pop("PreToolUse")
    hooks_path.write_text(json.dumps(payload), encoding="utf-8")

    unhealthy = codex.doctor_codex(repo)
    assert unhealthy.ready is False
    lifecycle = next(
        item for item in unhealthy.checks if item["name"] == "lifecycle_hooks"
    )
    assert lifecycle["status"] == "error"
    assert lifecycle["missing_events"] == ["PreToolUse"]


def test_session_start_records_handshake_without_storing_prompt_or_tool_input(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    codex.init_project(repo)
    codex.connect_codex(repo)

    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "thr_123",
        "cwd": str(repo),
        "prompt": "secret prompt should not persist",
        "tool_input": {"token": "secret"},
    }
    assert codex.handle_codex_hook(payload) == 0

    state_text = (repo / ".claim-plane/codex.json").read_text(encoding="utf-8")
    state = json.loads(state_text)
    assert state["last_session_id"] == "thr_123"
    assert state["last_event"] == "SessionStart"
    assert "secret prompt" not in state_text
    assert '"token"' not in state_text


def test_connect_detects_inline_hooks_without_modifying_config(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    codex.init_project(repo)
    config = repo / ".codex/config.toml"
    config.parent.mkdir()
    original = (
        '[[hooks.PreToolUse]]\nmatcher = "^Bash$"\n\n'
        '[[hooks.PreToolUse.hooks]]\ntype = "command"\n'
        'command = "./existing"\n'
    )
    config.write_text(original, encoding="utf-8")

    result = codex.connect_codex(repo)

    assert result["inline_hooks_present"] is True
    assert config.read_text(encoding="utf-8") == original
