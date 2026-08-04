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
    assert (
        state["task_base_commit"]
        == subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
    )
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

    result = codex.admit_codex_intent(repo, session_id=session_id, proposal=_proposal())

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
            item for item in plane.intents() if item["intent_id"] == result["intent_id"]
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


def _pretool(
    repo: Path,
    session_id: str,
    *,
    tool_name: str,
    tool_input: dict[str, object],
) -> str:
    output = __import__("io").StringIO()
    assert (
        codex.handle_codex_hook(
            {
                "hook_event_name": "PreToolUse",
                "session_id": session_id,
                "cwd": str(repo),
                "tool_name": tool_name,
                "tool_input": tool_input,
            },
            output=output,
        )
        == 0
    )
    return output.getvalue()


def _patch(*lines: str) -> str:
    return "\n".join(("*** Begin Patch", *lines, "*** End Patch"))


def test_pretool_read_only_is_allowed_before_intent_admission(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_guard_read"
    _bootstrap_task(repo, session_id)

    assert (
        _pretool(
            repo,
            session_id,
            tool_name="exec_command",
            tool_input={"command": "rg SessionCache src"},
        )
        == ""
    )

    status = codex.codex_intent_status(repo, session_id=session_id)
    assert status["guard"]["authorized_calls"] == 1
    assert status["guard"]["denied_calls"] == 0


def test_project_acceptance_is_mandatory_and_reserved_for_final_verifier(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'acceptance-fixture'\nversion = '0.1.0'\n"
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    codex.init_project(repo)
    codex.connect_codex(repo)
    session_id = "thr_guard_acceptance"
    _bootstrap_task(repo, session_id)
    proposal = _proposal()
    proposal["acceptance"] = []
    admitted = codex.admit_codex_intent(repo, session_id=session_id, proposal=proposal)

    assert admitted["acceptance"] == ["python -m pytest"]
    for command in (
        "pytest",
        "PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider",
        "python -m pytest -q",
    ):
        raw = _pretool(
            repo,
            session_id,
            tool_name="exec_command",
            tool_input={"command": command},
        )
        decision = json.loads(raw)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "trusted final verifier" in decision["permissionDecisionReason"]

    for command in (
        "git diff -- README.md",
        "git status --short",
        "claim-plane --help",
        "command -v pytest",
    ):
        assert (
            _pretool(
                repo,
                session_id,
                tool_name="exec_command",
                tool_input={"command": command},
            )
            == ""
        )

    status = codex.codex_intent_status(repo, session_id=session_id)
    assert status["guard"]["denied_calls"] == 3
    assert status["guard"].get("denied_mutation_calls", 0) == 0


def test_pretool_mutation_is_denied_before_intent_admission(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_guard_no_intent"
    _bootstrap_task(repo, session_id)

    raw = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch("*** Update File: src/cache.py", "@@", "-a", "+b")
        },
    )

    payload = json.loads(raw)
    decision = payload["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert "No active ChangeIntent" in decision["permissionDecisionReason"]


def test_pretool_committed_apply_patch_is_authorized(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_guard_committed"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(repo, session_id=session_id, proposal=_proposal())

    raw = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch("*** Update File: src/cache.py", "@@", "-a", "+b")
        },
    )

    assert raw == ""
    status = codex.codex_intent_status(repo, session_id=session_id)
    assert status["guard"]["authorized_calls"] == 1
    assert status["guard"]["last_paths"] == ["src/cache.py"]


def test_pretool_undeclared_apply_patch_is_denied(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_guard_outside"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(repo, session_id=session_id, proposal=_proposal())

    raw = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch("*** Update File: auth/token.py", "@@", "-a", "+b")
        },
    )

    decision = json.loads(raw)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "auth/token.py" in decision["permissionDecisionReason"]
    assert "outside the admitted ChangeIntent" in decision["permissionDecisionReason"]


def test_pretool_contingent_path_is_atomically_promoted_before_allow(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_guard_promote"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(repo, session_id=session_id, proposal=_proposal())

    raw = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch("*** Update File: src/locking.py", "@@", "-a", "+b")
        },
    )

    assert raw == ""
    status = codex.codex_intent_status(repo, session_id=session_id)
    assert status["guard"]["promotions"] == 1
    assert "src/locking.py" in {
        item["identifier"] for item in status["committed_scope"]
    }
    assert "src/locking.py" not in {
        item["identifier"] for item in status["contingent_scope"]
    }


def test_pretool_multiple_contingent_promotions_are_denied_without_partial_change(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_guard_multi_promote"
    _bootstrap_task(repo, session_id)
    proposal = _proposal()
    proposal["operations"].append(
        {
            "access": "write",
            "kind": "file",
            "identifier": "src/fallback.py",
            "commitment": "contingent",
            "required": False,
        }
    )
    codex.admit_codex_intent(repo, session_id=session_id, proposal=proposal)

    raw = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch(
                "*** Update File: src/locking.py",
                "@@",
                "-a",
                "+b",
                "*** Update File: src/fallback.py",
                "@@",
                "-c",
                "+d",
            )
        },
    )

    decision = json.loads(raw)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert (
        "more than one contingent scope promotion"
        in decision["permissionDecisionReason"]
    )
    status = codex.codex_intent_status(repo, session_id=session_id)
    assert status["guard"]["promotions"] == 0
    assert {item["identifier"] for item in status["contingent_scope"]} >= {
        "src/locking.py",
        "src/fallback.py",
    }


def test_pretool_shell_mutation_requires_matching_capability(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_guard_shell"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(repo, session_id=session_id, proposal=_proposal())

    allowed = _pretool(
        repo,
        session_id,
        tool_name="exec_command",
        tool_input={"command": "touch src/cache.py"},
    )
    denied = _pretool(
        repo,
        session_id,
        tool_name="exec_command",
        tool_input={"command": "rm src/cache.py"},
    )

    assert allowed == ""
    assert json.loads(denied)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_opaque_shell_and_unknown_tools_fail_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_guard_opaque"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(repo, session_id=session_id, proposal=_proposal())

    opaque = _pretool(
        repo,
        session_id,
        tool_name="exec_command",
        tool_input={"command": 'python -c \'open("src/cache.py", "w").write("x")\''},
    )
    unknown = _pretool(
        repo,
        session_id,
        tool_name="future_mutator",
        tool_input={"path": "src/cache.py"},
    )

    assert json.loads(opaque)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert json.loads(unknown)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pretool_changed_head_denies_mutation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_guard_stale_base"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(repo, session_id=session_id, proposal=_proposal())
    (repo / "HEAD_CHANGE.txt").write_text("change\n", encoding="utf-8")
    subprocess.run(["git", "add", "HEAD_CHANGE.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "advance head"], cwd=repo, check=True)

    raw = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch("*** Update File: src/cache.py", "@@", "-a", "+b")
        },
    )

    decision = json.loads(raw)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "HEAD no longer matches" in decision["permissionDecisionReason"]


def test_guard_state_does_not_persist_raw_tool_input(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_guard_private"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(repo, session_id=session_id, proposal=_proposal())
    secret = "VERY_PRIVATE_TOOL_ARGUMENT_123"

    _pretool(
        repo,
        session_id,
        tool_name="exec_command",
        tool_input={"command": f"python -c '{secret}'"},
    )

    session_files = list((repo / ".claim-plane/codex/sessions").glob("*.json"))
    assert len(session_files) == 1
    text = session_files[0].read_text(encoding="utf-8")
    assert secret not in text
    state = json.loads(text)
    assert state["guard_last_reason_code"] == "opaque_shell"


def test_doctor_rejects_codex_without_apply_patch_hook_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    codex.init_project(repo)
    codex.connect_codex(repo)
    monkeypatch.setattr(
        codex, "_codex_version", lambda: ("/usr/bin/codex", "codex-cli 0.122.0")
    )

    report = codex.doctor_codex(repo)

    assert report.ready is False
    check = next(
        item
        for item in report.checks
        if item["name"] == "pre_mutation_guard_compatibility"
    )
    assert check["status"] == "error"


def test_scope_denial_issues_reusable_exact_amendment_ticket(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_amend_ticket"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(repo, session_id=session_id, proposal=_proposal())

    first = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch("*** Update File: auth/token.py", "@@", "-a", "+b")
        },
    )
    first_reason = json.loads(first)["hookSpecificOutput"]["permissionDecisionReason"]
    status = codex.codex_intent_status(repo, session_id=session_id)
    pending = status["scope_amendment"]["pending"]

    assert "codex-intent amend" in first_reason
    assert pending["protocol"] == codex.CODEX_SCOPE_AMENDMENT_PROTOCOL
    assert pending["mutations"] == [
        {"access": "write", "path": "auth/token.py", "target_path": None}
    ]
    assert status["scope_amendment"]["tickets_issued"] == 1

    second = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch("*** Update File: auth/token.py", "@@", "-a", "+b")
        },
    )
    second_status = codex.codex_intent_status(repo, session_id=session_id)
    assert (
        second_status["scope_amendment"]["pending"]["ticket_id"] == pending["ticket_id"]
    )
    assert second_status["scope_amendment"]["tickets_issued"] == 1
    second_reason = json.loads(second)["hookSpecificOutput"]["permissionDecisionReason"]
    assert pending["ticket_id"] in second_reason


def test_scope_amendment_readmits_exact_denied_resource_and_retry_is_allowed(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_amend_allow"
    _bootstrap_task(repo, session_id)
    admitted = codex.admit_codex_intent(
        repo, session_id=session_id, proposal=_proposal()
    )

    _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch("*** Update File: auth/token.py", "@@", "-a", "+b")
        },
    )
    status = codex.codex_intent_status(repo, session_id=session_id)
    ticket = status["scope_amendment"]["pending"]["ticket_id"]

    result = codex.amend_codex_scope(
        repo,
        session_id=session_id,
        ticket_id=ticket,
        reason="Cache invalidation is implemented by TokenStore and must be updated atomically.",
    )

    assert result["allowed"] is True
    assert result["intent_id"] == admitted["intent_id"]
    assert result["operations"] == [
        {"access": "write", "path": "auth/token.py", "target_path": None}
    ]
    assert "auth/token.py" in {item["identifier"] for item in result["committed_scope"]}
    assert result["decision"]["allowed"] is True

    retry = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch("*** Update File: auth/token.py", "@@", "-a", "+b")
        },
    )
    assert retry == ""

    status = codex.codex_intent_status(repo, session_id=session_id)
    assert status["scope_amendment"]["admitted"] == 1
    assert status["scope_amendment"]["pending"] == {}
    assert status["scope_amendment"]["last"]["ticket_id"] == ticket
    assert status["preserves"] == ["path-unchanged:src/public_api/**"]
    assert status["acceptance"] == ["pytest tests/test_cache.py"]

    from claim_plane.core import Plane

    plane = Plane.open(repo / ".claim-plane/plane.db")
    try:
        intent = plane.intent(str(admitted["intent_id"]))
        assert intent is not None
        history = intent.metadata["scope_amendments"]
        assert history[-1]["ticket_id"] == ticket
        assert history[-1]["reason"].startswith("Cache invalidation")
    finally:
        plane.close()


def test_multiple_contingent_mutations_can_be_committed_atomically_by_ticket(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_amend_multi"
    _bootstrap_task(repo, session_id)
    proposal = _proposal()
    proposal["operations"].append(
        {
            "access": "write",
            "kind": "file",
            "identifier": "src/fallback.py",
            "commitment": "contingent",
            "required": False,
        }
    )
    codex.admit_codex_intent(repo, session_id=session_id, proposal=proposal)

    denied = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch(
                "*** Update File: src/locking.py",
                "@@",
                "-a",
                "+b",
                "*** Update File: src/fallback.py",
                "@@",
                "-c",
                "+d",
            )
        },
    )
    assert json.loads(denied)["hookSpecificOutput"]["permissionDecision"] == "deny"
    status = codex.codex_intent_status(repo, session_id=session_id)
    ticket = status["scope_amendment"]["pending"]["ticket_id"]
    assert {
        item["path"] for item in status["scope_amendment"]["pending"]["mutations"]
    } == {
        "src/locking.py",
        "src/fallback.py",
    }

    result = codex.amend_codex_scope(
        repo,
        session_id=session_id,
        ticket_id=ticket,
        reason="The implementation requires both lock ownership and fallback state changes.",
    )
    assert result["allowed"] is True
    committed = {item["identifier"] for item in result["committed_scope"]}
    contingent = {item["identifier"] for item in result["contingent_scope"]}
    assert {"src/locking.py", "src/fallback.py"} <= committed
    assert "src/locking.py" not in contingent
    assert "src/fallback.py" not in contingent

    retry = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch(
                "*** Update File: src/locking.py",
                "@@",
                "-a",
                "+b",
                "*** Update File: src/fallback.py",
                "@@",
                "-c",
                "+d",
            )
        },
    )
    assert retry == ""


def test_scope_amendment_is_rejected_when_ticket_is_stale_after_scope_change(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_amend_stale"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(repo, session_id=session_id, proposal=_proposal())

    _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch("*** Update File: auth/token.py", "@@", "-a", "+b")
        },
    )
    stale_status = codex.codex_intent_status(repo, session_id=session_id)
    ticket = stale_status["scope_amendment"]["pending"]["ticket_id"]

    assert (
        _pretool(
            repo,
            session_id,
            tool_name="apply_patch",
            tool_input={
                "command": _patch("*** Update File: src/locking.py", "@@", "-a", "+b")
            },
        )
        == ""
    )

    with pytest.raises(ValueError, match="ticket is stale"):
        codex.amend_codex_scope(
            repo,
            session_id=session_id,
            ticket_id=ticket,
            reason="TokenStore is also required.",
        )


def test_scope_amendment_ticket_integrity_is_checked(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_amend_integrity"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(repo, session_id=session_id, proposal=_proposal())
    _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch("*** Update File: auth/token.py", "@@", "-a", "+b")
        },
    )
    status = codex.codex_intent_status(repo, session_id=session_id)
    ticket = status["scope_amendment"]["pending"]["ticket_id"]

    session_path = next((repo / ".claim-plane/codex/sessions").glob("*.json"))
    state = json.loads(session_path.read_text(encoding="utf-8"))
    state["pending_scope_amendment"]["mutations"].append(
        {"access": "write", "path": "billing/ledger.py", "target_path": None}
    )
    session_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ValueError, match="integrity check failed"):
        codex.amend_codex_scope(
            repo,
            session_id=session_id,
            ticket_id=ticket,
            reason="Attempted ticket widening must not work.",
        )


def test_scope_amendment_can_be_rejected_by_normal_coordination_admission(
    tmp_path: Path,
) -> None:
    from claim_plane.core import (
        ChangeIntent,
        IntentOperation,
        Plane,
        ResourceKind,
        ResourceRef,
    )

    repo = _repo(tmp_path)
    session_id = "thr_amend_conflict"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(repo, session_id=session_id, proposal=_proposal())

    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    plane = Plane.open(repo / ".claim-plane/plane.db")
    try:
        other = ChangeIntent(
            intent_id="other-auth-writer",
            task_id="other-task",
            owner="other-agent",
            base_revision=base,
            base_commit=base,
            operations=(
                IntentOperation(
                    access="write",
                    resource=ResourceRef(
                        kind=ResourceKind.FILE, identifier="auth/token.py"
                    ),
                ),
            ),
        )
        decision = plane.admit(other)
        assert decision.allowed is True
        plane.activate(other.intent_id)
    finally:
        plane.close()

    _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch("*** Update File: auth/token.py", "@@", "-a", "+b")
        },
    )
    status = codex.codex_intent_status(repo, session_id=session_id)
    ticket = status["scope_amendment"]["pending"]["ticket_id"]

    result = codex.amend_codex_scope(
        repo,
        session_id=session_id,
        ticket_id=ticket,
        reason="TokenStore owns the invalidation path.",
    )
    assert result["allowed"] is False
    assert "auth/token.py" not in {
        item["identifier"] for item in result["committed_scope"]
    }
    status = codex.codex_intent_status(repo, session_id=session_id)
    assert status["scope_amendment"]["denied"] == 1
    assert status["scope_amendment"]["pending"] == {}


def test_codex_connector_control_commands_are_allowed_without_broad_shell_authority(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_control_channel"
    _bootstrap_task(repo, session_id)

    allowed = _pretool(
        repo,
        session_id,
        tool_name="exec_command",
        tool_input={
            "command": (
                "claim-plane codex-intent admit --session-id thr_control_channel "
                "--repo . --proposal-json '{}'"
            )
        },
    )
    denied = _pretool(
        repo,
        session_id,
        tool_name="exec_command",
        tool_input={"command": "claim-plane amend arbitrary-intent"},
    )

    assert allowed == ""
    assert json.loads(denied)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_expired_scope_amendment_ticket_is_consumed_and_denied(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_amend_expired"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(repo, session_id=session_id, proposal=_proposal())
    _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch("*** Update File: auth/token.py", "@@", "-a", "+b")
        },
    )
    status = codex.codex_intent_status(repo, session_id=session_id)
    ticket = status["scope_amendment"]["pending"]["ticket_id"]

    session_path = next((repo / ".claim-plane/codex/sessions").glob("*.json"))
    state = json.loads(session_path.read_text(encoding="utf-8"))
    state["pending_scope_amendment"]["expires_at"] = "2000-01-01T00:00:00Z"
    session_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ValueError, match="has expired"):
        codex.amend_codex_scope(
            repo,
            session_id=session_id,
            ticket_id=ticket,
            reason="This should not be accepted after expiry.",
        )

    status = codex.codex_intent_status(repo, session_id=session_id)
    assert status["scope_amendment"]["pending"] == {}
    assert status["scope_amendment"]["denied"] == 1


def test_control_channel_cannot_target_another_session_or_repository(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_control_scope"
    _bootstrap_task(repo, session_id)

    wrong_session = _pretool(
        repo,
        session_id,
        tool_name="exec_command",
        tool_input={
            "command": (
                "claim-plane codex-intent status --session-id another-session --repo . --json"
            )
        },
    )
    wrong_repo = _pretool(
        repo,
        session_id,
        tool_name="exec_command",
        tool_input={
            "command": (
                "claim-plane codex-intent status --session-id thr_control_scope "
                "--repo /tmp/other --json"
            )
        },
    )

    assert (
        json.loads(wrong_session)["hookSpecificOutput"]["permissionDecision"] == "deny"
    )
    assert json.loads(wrong_repo)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_line_bounded_scope_denial_does_not_offer_unbounded_amendment_ticket(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_bounded_amend"
    _bootstrap_task(repo, session_id)
    proposal = _proposal()
    proposal["operations"][0]["region"] = "lines:10-20"
    codex.admit_codex_intent(repo, session_id=session_id, proposal=proposal)

    raw = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch("*** Update File: src/cache.py", "@@", "-a", "+b")
        },
    )
    reason = json.loads(raw)["hookSpecificOutput"]["permissionDecisionReason"]
    status = codex.codex_intent_status(repo, session_id=session_id)

    assert "line-bounded" in reason
    assert "codex-intent amend" not in reason
    assert status["scope_amendment"]["pending"] == {}


def test_codex_intent_cannot_claim_connector_control_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_protected_proposal"
    _bootstrap_task(repo, session_id)

    for path in (
        ".claim-plane/plane.db",
        ".git/config",
        ".codex/hooks.json",
        ".codex/config.toml",
    ):
        with pytest.raises(ValueError, match="connector control state"):
            codex.admit_codex_intent(
                repo,
                session_id=session_id,
                proposal=_proposal(path=path),
            )


def test_pretool_protected_connector_surface_is_denied_without_amendment_ticket(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_protected_guard"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(repo, session_id=session_id, proposal=_proposal())

    raw = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch(
                "*** Update File: .codex/hooks.json",
                "@@",
                "-a",
                "+b",
            )
        },
    )
    decision = json.loads(raw)["hookSpecificOutput"]
    status = codex.codex_intent_status(repo, session_id=session_id)

    assert decision["permissionDecision"] == "deny"
    assert "connector control state" in decision["permissionDecisionReason"]
    assert status["guard"]["last_reason_code"] == "protected_control_surface"
    assert status["scope_amendment"]["pending"] == {}


def _completion_proposal(*, acceptance: list[str] | None = None) -> dict[str, object]:
    return {
        "protocol": codex.CODEX_INTENT_PROPOSAL_PROTOCOL,
        "goal": "Update the project fixture safely",
        "operations": [
            {
                "access": "write",
                "kind": "file",
                "identifier": "README.md",
                "commitment": "committed",
            }
        ],
        "preserves": [],
        "acceptance": acceptance if acceptance is not None else ["git diff --check"],
    }


def _stop(repo: Path, session_id: str, *, stop_hook_active: bool = False) -> str:
    output = __import__("io").StringIO()
    assert (
        codex.handle_codex_hook(
            {
                "hook_event_name": "Stop",
                "session_id": session_id,
                "cwd": str(repo),
                "last_assistant_message": "Task complete.",
                "model": "gpt-5",
                "permission_mode": "default",
                "stop_hook_active": stop_hook_active,
                "transcript_path": None,
                "turn_id": "turn_verify",
            },
            output=output,
        )
        == 0
    )
    return output.getvalue()


def test_stop_verifies_clean_codex_task_and_completes_intent(tmp_path: Path) -> None:
    from claim_plane.core import Plane

    repo = _repo(tmp_path)
    session_id = "thr_completion_clean"
    _bootstrap_task(repo, session_id)
    admitted = codex.admit_codex_intent(
        repo, session_id=session_id, proposal=_completion_proposal()
    )
    assert admitted["allowed"] is True

    raw_guard = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch(
                "*** Update File: README.md",
                "@@",
                "-# fixture",
                "+# fixture updated",
            )
        },
    )
    assert raw_guard == ""
    (repo / "README.md").write_text("# fixture updated\n", encoding="utf-8")

    raw_stop = _stop(repo, session_id)
    stop_payload = json.loads(raw_stop)
    assert "decision" not in stop_payload
    assert "Claim Plane — VERIFIED" in stop_payload["systemMessage"]

    status = codex.codex_intent_status(repo, session_id=session_id)
    completion = status["completion"]
    assert status["state"] == "verified"
    assert completion["protocol"] == codex.CODEX_COMPLETION_PROTOCOL
    assert completion["verified"] is True
    assert completion["changed_files"] == 1
    assert completion["changed_paths"] == ["README.md"]
    assert completion["authorized_mutation_calls"] == 1
    assert completion["executed_violations"] == 0
    assert completion["acceptance_passed"] is True

    plane = Plane.open(repo / ".claim-plane/plane.db")
    try:
        record = next(
            item
            for item in plane.intents()
            if item["intent_id"] == admitted["intent_id"]
        )
        assert record["state"] == "completed"
    finally:
        plane.close()


def test_stop_blocks_failed_acceptance_then_verifies_after_repair(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_completion_repair"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(
        repo, session_id=session_id, proposal=_completion_proposal()
    )
    _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch(
                "*** Update File: README.md",
                "@@",
                "-# fixture",
                "+# fixture bad",
            )
        },
    )
    (repo / "README.md").write_text("# fixture bad   \n", encoding="utf-8")

    first = json.loads(_stop(repo, session_id))
    assert first["decision"] == "block"
    assert "did not verify completion" in first["reason"]
    assert "Claim Plane — UNVERIFIED" in first["systemMessage"]
    status = codex.codex_intent_status(repo, session_id=session_id)
    assert status["state"] == "verification_failed"
    assert status["completion"]["acceptance_passed"] is False

    # A continuation that makes no progress is not blocked forever.
    second = json.loads(_stop(repo, session_id, stop_hook_active=True))
    assert "decision" not in second
    assert "remains UNVERIFIED" in second["systemMessage"]

    (repo / "README.md").write_text("# fixture repaired\n", encoding="utf-8")
    final = json.loads(_stop(repo, session_id, stop_hook_active=True))
    assert "Claim Plane — VERIFIED" in final["systemMessage"]
    assert codex.codex_intent_status(repo, session_id=session_id)["state"] == "verified"


def test_completion_evidence_catches_external_undeclared_change(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_completion_bypass"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(
        repo, session_id=session_id, proposal=_completion_proposal(acceptance=[])
    )
    _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch(
                "*** Update File: README.md",
                "@@",
                "-# fixture",
                "+# fixture updated",
            )
        },
    )
    (repo / "README.md").write_text("# fixture updated\n", encoding="utf-8")
    # Simulate an out-of-band write that never passed through the Codex hook.
    (repo / "auth.py").write_text("SECRET = True\n", encoding="utf-8")

    stop_payload = json.loads(_stop(repo, session_id))
    assert stop_payload["decision"] == "block"
    status = codex.codex_intent_status(repo, session_id=session_id)
    completion = status["completion"]
    assert completion["verified"] is False
    assert completion["executed_violations"] >= 1
    assert any(item["code"] == "undeclared_change" for item in completion["findings"])


def test_verified_completion_is_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_completion_idempotent"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(
        repo, session_id=session_id, proposal=_completion_proposal(acceptance=[])
    )
    _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch(
                "*** Update File: README.md",
                "@@",
                "-# fixture",
                "+# fixture completed",
            )
        },
    )
    (repo / "README.md").write_text("# fixture completed\n", encoding="utf-8")

    first = codex.verify_codex_completion(repo, session_id=session_id)
    second = codex.verify_codex_completion(repo, session_id=session_id)
    assert first == second
    assert first["verified"] is True
    assert (
        codex.codex_intent_status(repo, session_id=session_id)["completion_attempts"]
        == 1
    )


def test_codex_verify_control_command_is_session_local(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_verify_control"
    _bootstrap_task(repo, session_id)

    allowed = _pretool(
        repo,
        session_id,
        tool_name="exec_command",
        tool_input={
            "command": (
                "claim-plane codex-intent verify --session-id thr_verify_control "
                "--repo . --acceptance-timeout 120"
            )
        },
    )
    denied = _pretool(
        repo,
        session_id,
        tool_name="exec_command",
        tool_input={
            "command": (
                "claim-plane codex-intent verify --session-id another-session --repo ."
            )
        },
    )

    assert allowed == ""
    assert json.loads(denied)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_completion_detects_connector_control_tamper_after_bootstrap(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_completion_control_tamper"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(
        repo, session_id=session_id, proposal=_completion_proposal(acceptance=[])
    )
    _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch(
                "*** Update File: README.md",
                "@@",
                "-# fixture",
                "+# fixture changed",
            )
        },
    )
    (repo / "README.md").write_text("# fixture changed\n", encoding="utf-8")

    hooks = repo / ".codex/hooks.json"
    hooks.write_text(hooks.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    result = codex.verify_codex_completion(repo, session_id=session_id)
    assert result["verified"] is False
    assert any(
        item["code"] == "undeclared_change" and item["path"] == ".codex/hooks.json"
        for item in result["findings"]
    )


def _read_deny_reason(raw: str) -> str:
    payload = json.loads(raw)
    return str(
        payload.get("reason")
        or payload.get("hookSpecificOutput", {}).get("permissionDecisionReason")
        or ""
    )


def _readme_proposal() -> dict[str, object]:
    return {
        "protocol": codex.CODEX_INTENT_PROPOSAL_PROTOCOL,
        "goal": "Update the fixture README",
        "operations": [
            {
                "access": "write",
                "kind": "file",
                "identifier": "README.md",
                "commitment": "committed",
            }
        ],
        "preserves": [],
        "acceptance": [],
    }


def test_connect_repairs_legacy_claim_plane_hook_definition(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    codex.init_project(repo)
    hooks_path = repo / ".codex/hooks.json"
    hooks_path.parent.mkdir()
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "claim-plane codex-hook --legacy",
                                    "timeout": 5,
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    codex.connect_codex(repo)

    payload = json.loads(hooks_path.read_text(encoding="utf-8"))
    handlers = _handlers(payload, "PreToolUse")
    owned = [
        item for item in handlers if item.get("command") == codex.CODEX_HOOK_COMMAND
    ]
    assert len(owned) == 1
    assert all(
        item.get("command") != "claim-plane codex-hook --legacy" for item in handlers
    )
    state = json.loads((repo / ".claim-plane/codex.json").read_text(encoding="utf-8"))
    assert state["connector_revision"] == codex.CODEX_CONNECTOR_REVISION


def test_doctor_detects_connector_hook_definition_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    codex.init_project(repo)
    codex.connect_codex(repo)
    monkeypatch.setattr(
        codex, "_codex_version", lambda: ("/usr/bin/codex", "codex-cli 0.123.0")
    )
    hooks_path = repo / ".codex/hooks.json"
    payload = json.loads(hooks_path.read_text(encoding="utf-8"))
    payload["hooks"]["PreToolUse"][-1]["hooks"][0]["timeout"] = 1
    hooks_path.write_text(json.dumps(payload), encoding="utf-8")

    report = codex.doctor_codex(repo)
    check = next(
        item for item in report.checks if item["name"] == "connector_hook_definition"
    )
    assert check["status"] == "error"
    assert report.ready is False


def test_pretool_fails_closed_when_enrollment_state_disappears(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_missing_enrollment"
    _bootstrap_task(repo, session_id)
    (repo / ".claim-plane/codex.json").unlink()

    raw = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch(
                "*** Update File: README.md", "@@", "-# fixture", "+# changed"
            )
        },
    )

    assert raw
    assert "enrollment state is missing" in _read_deny_reason(raw)


def test_pretool_fails_closed_when_session_state_is_corrupt(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_corrupt_state"
    _bootstrap_task(repo, session_id)
    session_file = next((repo / ".claim-plane/codex/sessions").glob("*.json"))
    session_file.write_text("{not-json\n", encoding="utf-8")

    raw = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch(
                "*** Update File: README.md", "@@", "-# fixture", "+# changed"
            )
        },
    )

    assert raw
    assert "session state could not be loaded" in _read_deny_reason(raw)


def test_preexisting_dirty_path_is_protected_but_unrelated_dirty_change_is_not_attributed(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "notes.txt").write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "notes.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add notes"], cwd=repo, check=True)
    (repo / "notes.txt").write_text("user work\n", encoding="utf-8")
    session_id = "thr_dirty_baseline"
    _bootstrap_task(repo, session_id)
    admitted = codex.admit_codex_intent(
        repo, session_id=session_id, proposal=_readme_proposal()
    )
    assert admitted["allowed"] is True

    dirty_raw = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch(
                "*** Update File: notes.txt", "@@", "-user work", "+agent work"
            )
        },
    )
    assert "already had user changes" in _read_deny_reason(dirty_raw)

    allowed_raw = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch(
                "*** Update File: README.md", "@@", "-# fixture", "+# changed"
            )
        },
    )
    assert allowed_raw == ""
    (repo / "README.md").write_text("# changed\n", encoding="utf-8")

    result = codex.verify_codex_completion(repo, session_id=session_id)
    assert result["verified"] is True
    assert result["changed_paths"] == ["README.md"]


def test_second_codex_session_cannot_admit_mutation_authority_in_same_worktree(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _bootstrap_task(repo, "thr_owner")
    first = codex.admit_codex_intent(
        repo, session_id="thr_owner", proposal=_readme_proposal()
    )
    assert first["allowed"] is True

    _bootstrap_task(repo, "thr_second")
    with pytest.raises(ValueError, match="another active Codex session"):
        codex.admit_codex_intent(
            repo, session_id="thr_second", proposal=_readme_proposal()
        )

    status = codex.codex_intent_status(repo, session_id="thr_second")
    assert status["state"] == "blocked_concurrent_session"
    assert status["hardening"]["concurrent_sessions"] == ["thr_owner"]


def test_resume_re_admits_expired_session_intent_on_same_base(tmp_path: Path) -> None:
    import sqlite3

    repo = _repo(tmp_path)
    session_id = "thr_resume_expired"
    _bootstrap_task(repo, session_id)
    admitted = codex.admit_codex_intent(
        repo, session_id=session_id, proposal=_readme_proposal()
    )
    old_intent = str(admitted["intent_id"])
    with sqlite3.connect(repo / ".claim-plane/plane.db") as conn:
        conn.execute(
            "UPDATE intents SET lease_expires_at=? WHERE intent_id=?",
            ("2000-01-01T00:00:00+00:00", old_intent),
        )

    assert (
        codex.handle_codex_hook(
            {
                "hook_event_name": "SessionStart",
                "session_id": session_id,
                "cwd": str(repo),
                "source": "resume",
            }
        )
        == 0
    )

    status = codex.codex_intent_status(repo, session_id=session_id)
    assert status["state"] == "active"
    assert status["intent_id"] != old_intent
    assert str(status["intent_id"]).startswith(old_intent + "-resume-")
    assert status["hardening"]["resume_recoveries"] == 1
    assert status["hardening"]["recovered_from_intent_id"] == old_intent


def test_resume_fails_closed_when_head_changed_during_inactivity(
    tmp_path: Path,
) -> None:
    import sqlite3

    repo = _repo(tmp_path)
    session_id = "thr_resume_changed_head"
    _bootstrap_task(repo, session_id)
    admitted = codex.admit_codex_intent(
        repo, session_id=session_id, proposal=_readme_proposal()
    )
    old_intent = str(admitted["intent_id"])
    with sqlite3.connect(repo / ".claim-plane/plane.db") as conn:
        conn.execute(
            "UPDATE intents SET lease_expires_at=? WHERE intent_id=?",
            ("2000-01-01T00:00:00+00:00", old_intent),
        )
    (repo / "advance.txt").write_text("advance\n", encoding="utf-8")
    subprocess.run(["git", "add", "advance.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "advance"], cwd=repo, check=True)

    codex.handle_codex_hook(
        {
            "hook_event_name": "SessionStart",
            "session_id": session_id,
            "cwd": str(repo),
            "source": "resume",
        }
    )

    status = codex.codex_intent_status(repo, session_id=session_id)
    assert status["state"] == "recovery_required"
    assert "HEAD changed" in str(status["hardening"]["recovery_reason"])


def test_pretool_denies_mutation_after_branch_switch_even_at_same_commit(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    session_id = "thr_branch_switch"
    _bootstrap_task(repo, session_id)
    codex.admit_codex_intent(repo, session_id=session_id, proposal=_readme_proposal())
    subprocess.run(["git", "switch", "-qc", "other"], cwd=repo, check=True)

    raw = _pretool(
        repo,
        session_id,
        tool_name="apply_patch",
        tool_input={
            "command": _patch(
                "*** Update File: README.md", "@@", "-# fixture", "+# changed"
            )
        },
    )
    assert "Git branch changed" in _read_deny_reason(raw)


def test_abandon_releases_worktree_authority_for_next_codex_session(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _bootstrap_task(repo, "thr_abandon_owner")
    first = codex.admit_codex_intent(
        repo, session_id="thr_abandon_owner", proposal=_readme_proposal()
    )
    assert first["allowed"] is True

    _bootstrap_task(repo, "thr_abandon_next")
    with pytest.raises(ValueError, match="another active Codex session"):
        codex.admit_codex_intent(
            repo, session_id="thr_abandon_next", proposal=_readme_proposal()
        )

    abandoned = codex.abandon_codex_intent(repo, session_id="thr_abandon_owner")
    assert abandoned["state"] == "abandoned"
    assert abandoned["released"] is True

    second = codex.admit_codex_intent(
        repo, session_id="thr_abandon_next", proposal=_readme_proposal()
    )
    assert second["allowed"] is True
    assert (
        codex.codex_intent_status(repo, session_id="thr_abandon_owner")["state"]
        == "abandoned"
    )
