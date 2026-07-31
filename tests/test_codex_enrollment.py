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


def _bootstrap_task(repo: Path, session_id: str = "thr_bootstrap") -> dict[str, object]:
    codex.init_project(repo)
    codex.connect_codex(repo)
    assert (
        codex.handle_codex_hook(
            {
                "hook_event_name": "SessionStart",
                "session_id": session_id,
                "cwd": str(repo),
                "source": "startup",
            }
        )
        == 0
    )
    output = __import__("io").StringIO()
    assert (
        codex.handle_codex_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session_id,
                "cwd": str(repo),
                "prompt": "Fix the cache race without changing the public API.",
            },
            output=output,
        )
        == 0
    )
    payload = json.loads(output.getvalue())
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    return payload


def _proposal(*, path: str = "src/cache.py") -> dict[str, object]:
    return {
        "protocol": codex.CODEX_INTENT_PROPOSAL_PROTOCOL,
        "goal": "Fix the session cache race condition",
        "operations": [
            {
                "access": "write",
                "kind": "file",
                "identifier": path,
                "commitment": "committed",
            },
            {
                "access": "write",
                "kind": "file",
                "identifier": "src/locking.py",
                "commitment": "contingent",
                "required": False,
            },
            {
                "access": "test",
                "kind": "file",
                "identifier": "tests/test_cache.py",
            },
        ],
        "preserves": ["path-unchanged:src/public_api/**"],
        "acceptance": ["pytest tests/test_cache.py"],
    }


def test_user_prompt_bootstraps_session_bound_task_without_persisting_prompt(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_private_prompt"
    payload = _bootstrap_task(repo, session_id)

    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "Before the first repository mutation" in context
    assert "claim-plane codex-intent admit" in context
    assert codex.CODEX_INTENT_PROPOSAL_PROTOCOL in context

    session_files = list((repo / ".claim-plane/codex/sessions").glob("*.json"))
    assert len(session_files) == 1
    text = session_files[0].read_text(encoding="utf-8")
    state = json.loads(text)
    assert state["protocol"] == codex.CODEX_SESSION_PROTOCOL
    assert state["session_id"] == session_id
    assert state["task_id"].startswith("codex-task-")
    assert state["reserved_intent_id"].startswith("codex-intent-")
    assert state["task_base_commit"] == subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert state["task_state"] == "awaiting_intent"
    assert "Fix the cache race" not in text
    assert state["prompt_sha256"]
    assert state["prompt_length"] > 0


def test_codex_intent_admission_binds_identity_base_and_scope_to_session(
    tmp_path: Path,
) -> None:
    from claim_plane.core import Plane

    repo = _repo(tmp_path)
    session_id = "thr_admit"
    _bootstrap_task(repo, session_id)

    result = codex.admit_codex_intent(
        repo, session_id=session_id, proposal=_proposal()
    )

    assert result["protocol"] == codex.CODEX_INTENT_ADMISSION_PROTOCOL
    assert result["allowed"] is True
    assert result["state"] == "active"
    assert result["intent_id"].startswith("codex-intent-")
    assert result["owner"].startswith("codex:")
    assert result["goal"] == "Fix the session cache race condition"
    assert [item["identifier"] for item in result["committed_scope"]] == [
        "src/cache.py",
        "tests/test_cache.py",
    ]
    assert [item["identifier"] for item in result["contingent_scope"]] == [
        "src/locking.py"
    ]

    plane = Plane.open(repo / ".claim-plane/plane.db")
    try:
        intent = plane.intent(str(result["intent_id"]))
        assert intent is not None
        assert intent.base_commit == result["base_commit"]
        assert intent.base_revision == result["base_commit"]
        assert intent.task_id == result["task_id"]
        assert intent.metadata["goal"] == result["goal"]
        record = next(
            item
            for item in plane.intents()
            if item["intent_id"] == result["intent_id"]
        )
        assert record["state"] == "active"
    finally:
        plane.close()

    status = codex.codex_intent_status(repo, session_id=session_id)
    assert status["intent_id"] == result["intent_id"]
    assert status["state"] == "active"
    assert status["goal"] == result["goal"]


def test_codex_intent_admission_is_idempotent_for_identical_proposal(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_idempotent_intent"
    _bootstrap_task(repo, session_id)
    proposal = _proposal()

    first = codex.admit_codex_intent(repo, session_id=session_id, proposal=proposal)
    second = codex.admit_codex_intent(repo, session_id=session_id, proposal=proposal)

    assert first["allowed"] is True
    assert second["allowed"] is True
    assert second["intent_id"] == first["intent_id"]
    assert second["base_commit"] == first["base_commit"]


def test_codex_intent_admission_rejects_changed_head_before_plan_is_admitted(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_stale_base"
    _bootstrap_task(repo, session_id)

    (repo / "after-bootstrap.txt").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "after-bootstrap.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "advance base"], cwd=repo, check=True)

    with pytest.raises(ValueError, match="base revision changed before admission"):
        codex.admit_codex_intent(repo, session_id=session_id, proposal=_proposal())


def test_codex_intent_proposal_cannot_escape_repository(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_escape"
    _bootstrap_task(repo, session_id)

    with pytest.raises(ValueError, match="cannot escape the repository"):
        codex.admit_codex_intent(
            repo,
            session_id=session_id,
            proposal=_proposal(path="../outside.py"),
        )


def test_second_prompt_reuses_session_task_and_active_intent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_followup"
    first_hook = _bootstrap_task(repo, session_id)
    first_context = first_hook["hookSpecificOutput"]["additionalContext"]
    assert "Before the first repository mutation" in first_context

    admitted = codex.admit_codex_intent(
        repo, session_id=session_id, proposal=_proposal()
    )
    output = __import__("io").StringIO()
    codex.handle_codex_hook(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": session_id,
            "cwd": str(repo),
            "prompt": "Please also explain the locking choice.",
        },
        output=output,
    )
    second_context = json.loads(output.getvalue())["hookSpecificOutput"][
        "additionalContext"
    ]

    assert "execution contract is active" in second_context
    assert admitted["intent_id"] in second_context
    assert "src/cache.py" in second_context
    assert "src/locking.py" in second_context
    assert "path-unchanged:src/public_api/**" in second_context


def test_codex_intent_proposal_cannot_override_authority_fields(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_authority"
    _bootstrap_task(repo, session_id)
    proposal = _proposal()
    proposal["owner"] = "model-selected-owner"
    proposal["base_commit"] = "0" * 40

    with pytest.raises(ValueError, match="unsupported Codex intent proposal field"):
        codex.admit_codex_intent(repo, session_id=session_id, proposal=proposal)
