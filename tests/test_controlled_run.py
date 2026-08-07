from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping

import pytest

from claim_plane import cli
from claim_plane.connectors import codex
from claim_plane.connectors.codex_adapter import CodexAdapter
from claim_plane.controlled_run import (
    CONTROLLED_RUN_PROTOCOL,
    ControlledRunOutcome,
    controlled_run_path,
    load_controlled_run,
    run_controlled_task,
    run_interactive_codex,
)
from claim_plane.project import dump_project_config, load_project_config
from claim_plane.protocol import AdapterOperation, AdapterRequest, LifecycleEventStore


class _CompletedProcess:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self.pid = 999999
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self._returncode = returncode

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self._returncode

    def terminate(self) -> None:
        self._returncode = -15

    def kill(self) -> None:
        self._returncode = -9


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
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
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def _request(
    operation: AdapterOperation,
    *,
    repo: Path,
    run_id: str,
    session_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> AdapterRequest:
    return AdapterRequest.create(
        operation,
        adapter="codex",
        project_root=str(repo),
        request_id=f"test-{operation.value}-{run_id}",
        session_id=session_id,
        run_id=run_id,
        intent_version=0 if operation is AdapterOperation.PROPOSE_INTENT else None,
        payload=payload,
    )


def _prepare(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[CodexAdapter, object]:
    monkeypatch.setattr(
        codex, "_codex_version", lambda: ("/opt/codex/bin/codex", "codex 1.2.3")
    )
    monkeypatch.setattr(
        codex,
        "_codex_auth_status",
        lambda executable: ("ok", "authentication available"),
    )
    monkeypatch.setattr(
        "claim_plane.controlled_run.shutil.which",
        lambda executable: "/opt/codex/bin/codex" if executable == "codex" else None,
    )
    codex.init_project(repo)
    codex.connect_codex(repo)
    adapter = CodexAdapter()
    return adapter, adapter.registry_handshake(str(repo))


def _successful_process_factory(
    adapter: CodexAdapter,
    *,
    repo: Path,
    task: str,
):
    def factory(command: list[str], *, root: Path, env: Mapping[str, str]):
        assert command[1:4] == ["--ask-for-approval", "never", "exec"]
        assert command[4:6] == ["--json", "--color"]
        assert "workspace-write" in command
        assert command[-1] == task
        assert root == repo
        run_id = env["CLAIM_PLANE_CONTROLLED_RUN_ID"]
        assert "CLAIM_PLANE_CONTROLLED_INTERACTIVE" not in env
        policy_manifest = json.loads(env["CLAIM_PLANE_CONTROLLED_POLICY_MANIFEST"])
        assert policy_manifest["preset"]["name"] == "guarded"
        assert policy_manifest["digest"]
        session_id = "thread_controlled_success"
        adapter.start_session(
            _request(
                AdapterOperation.START_SESSION,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={"source": "startup"},
            )
        )
        adapter.submit_task(
            _request(
                AdapterOperation.SUBMIT_TASK,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={"prompt": task},
            )
        )
        adapter.propose_intent(
            _request(
                AdapterOperation.PROPOSE_INTENT,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={
                    "proposal": {
                        "protocol": codex.CODEX_INTENT_PROPOSAL_PROTOCOL,
                        "goal": "Update the fixture value",
                        "operations": [
                            {
                                "access": "write",
                                "kind": "file",
                                "identifier": "app.py",
                                "commitment": "committed",
                            }
                        ],
                        "preserves": [],
                        "acceptance": [],
                    }
                },
            )
        )
        patch = (
            "*** Begin Patch\n"
            "*** Update File: app.py\n"
            "@@\n"
            "-VALUE = 1\n"
            "+VALUE = 2\n"
            "*** End Patch"
        )
        mutation = adapter.request_mutation(
            _request(
                AdapterOperation.REQUEST_MUTATION,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={"tool_name": "apply_patch", "tool_input": {"command": patch}},
            )
        )
        if mutation.status.value != "succeeded":
            raise AssertionError(json.dumps(mutation.to_dict(), indent=2))
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        observed = adapter.observe_result(
            _request(
                AdapterOperation.OBSERVE_RESULT,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={"tool_name": "apply_patch"},
            )
        )
        verified = adapter.verify_completion(
            AdapterRequest.create(
                AdapterOperation.VERIFY_COMPLETION,
                adapter="codex",
                project_root=str(repo),
                request_id=f"verify-{run_id}",
                session_id=session_id,
                run_id=run_id,
                intent_id=observed.intent_id,
                intent_version=observed.intent_version,
                timeout_seconds=30,
            )
        )
        assert verified.payload["verified"] is True
        adapter.stop_session(
            AdapterRequest.create(
                AdapterOperation.STOP_SESSION,
                adapter="codex",
                project_root=str(repo),
                request_id=f"stop-{run_id}",
                session_id=session_id,
                run_id=run_id,
            )
        )
        stream = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": session_id}),
                json.dumps({"type": "turn.started"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_1",
                            "type": "agent_message",
                            "text": "Updated app.py and verified the change.",
                        },
                    }
                ),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
            )
        )
        return _CompletedProcess(stream + "\n")

    return factory


def _unbound_hook_process_factory(
    adapter: CodexAdapter,
    *,
    repo: Path,
    task: str,
    session_id: str = "thread_controlled_unbound_hooks",
):
    def request(
        operation: AdapterOperation,
        *,
        payload: Mapping[str, Any] | None = None,
        intent_version: int | None = None,
    ) -> AdapterRequest:
        return AdapterRequest.create(
            operation,
            adapter="codex",
            project_root=str(repo),
            request_id=f"unbound-{operation.value}-{session_id}",
            session_id=session_id,
            run_id=None,
            intent_version=intent_version,
            payload=payload,
        )

    def factory(command: list[str], *, root: Path, env: Mapping[str, str]):
        assert root == repo
        assert env["CLAIM_PLANE_CONTROLLED_RUN_ID"]
        adapter.start_session(
            request(
                AdapterOperation.START_SESSION,
                payload={"source": "startup"},
            )
        )
        adapter.submit_task(
            request(
                AdapterOperation.SUBMIT_TASK,
                payload={"prompt": task},
            )
        )
        adapter.propose_intent(
            request(
                AdapterOperation.PROPOSE_INTENT,
                intent_version=0,
                payload={
                    "proposal": {
                        "protocol": codex.CODEX_INTENT_PROPOSAL_PROTOCOL,
                        "goal": "Update the fixture value",
                        "operations": [
                            {
                                "access": "write",
                                "kind": "file",
                                "identifier": "app.py",
                                "commitment": "committed",
                            }
                        ],
                        "preserves": [],
                        "acceptance": [],
                    }
                },
            )
        )
        patch = (
            "*** Begin Patch\n"
            "*** Update File: app.py\n"
            "@@\n"
            "-VALUE = 1\n"
            "+VALUE = 2\n"
            "*** End Patch"
        )
        mutation = adapter.request_mutation(
            request(
                AdapterOperation.REQUEST_MUTATION,
                payload={"tool_name": "apply_patch", "tool_input": {"command": patch}},
            )
        )
        assert mutation.status.value == "succeeded"
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        adapter.observe_result(
            request(
                AdapterOperation.OBSERVE_RESULT,
                payload={"tool_name": "apply_patch"},
            )
        )
        stream = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": session_id}),
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10}}),
            )
        )
        return _CompletedProcess(stream + "\n")

    return factory


def test_one_command_run_verifies_and_persists_git_bound_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)
    task = "Update the fixture value."
    output = io.StringIO()

    result = run_controlled_task(
        task,
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        timeout_seconds=30,
        acceptance_timeout=30,
        stdout=output,
        stderr=io.StringIO(),
        process_factory=_successful_process_factory(adapter, repo=repo, task=task),
    )

    assert result.protocol == CONTROLLED_RUN_PROTOCOL
    assert result.outcome is ControlledRunOutcome.VERIFIED
    assert result.exit_code == 0
    assert result.session_id == "thread_controlled_success"
    assert result.completion["verified"] is True
    assert result.determinism["completeness"]["complete"] is True
    assert result.determinism["verdict"]["reason_code"] == "verified"
    assert result.determinism["verdict"]["digest"]
    assert result.start_git.digest != result.result_git.digest
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    persisted = load_controlled_run(repo, result.run_id)
    assert persisted["outcome"] == "VERIFIED"
    assert task not in controlled_run_path(repo, result.run_id).read_text(
        encoding="utf-8"
    )
    assert "DELIVERY VERIFIED" in output.getvalue()
    with LifecycleEventStore.for_project(repo) as store:
        events = store.list_events(adapter="codex", session_id=result.session_id)
    assert events
    assert {event.run_id for event in events} == {result.run_id}


def test_controlled_hook_environment_binds_run_id_to_session_and_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    _prepare(repo, monkeypatch)
    run_id = "cpr_0123456789abcdef01234567"
    monkeypatch.setenv("CLAIM_PLANE_CONTROLLED_RUN_ID", run_id)
    monkeypatch.setenv("CLAIM_PLANE_CONTROLLED_POLICY", "guarded")
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "thread_env_binding",
                    "cwd": str(repo),
                    "source": "startup",
                }
            )
        ),
    )

    assert cli.main(["codex-hook"]) == 0

    session_path = next((repo / ".claim-plane/codex/sessions").glob("*.json"))
    session = json.loads(session_path.read_text(encoding="utf-8"))
    assert session["controlled_run_id"] == run_id
    assert session["controlled_policy"] == "guarded"
    with LifecycleEventStore.for_project(repo) as store:
        events = store.list_events(adapter="codex", session_id="thread_env_binding")
    assert len(events) == 1
    assert events[0].run_id == run_id


def test_cli_run_returns_machine_readable_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo(tmp_path)
    adapter, _ = _prepare(repo, monkeypatch)
    task = "Update the fixture value."
    monkeypatch.setattr(cli, "_CODEX_ADAPTER", adapter)
    monkeypatch.setattr(
        "claim_plane.controlled_run._spawn_codex",
        _successful_process_factory(adapter, repo=repo, task=task),
    )
    # Default arguments capture the function object, so route the CLI call through a
    # small wrapper that supplies the deterministic process factory.
    original = cli.run_controlled_task

    def wrapped(*args: Any, **kwargs: Any):
        kwargs["process_factory"] = _successful_process_factory(
            adapter, repo=repo, task=task
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(cli, "run_controlled_task", wrapped)

    exit_code = cli.main(
        ["run", task, "--repo", str(repo), "--policy", "guarded", "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["protocol"] == CONTROLLED_RUN_PROTOCOL
    assert payload["verified"] is True
    assert payload["outcome"] == "VERIFIED"


def test_timeout_revokes_active_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)
    session_id = "thread_controlled_timeout"

    def process_factory(command: list[str], *, root: Path, env: Mapping[str, str]):
        del command
        assert root == repo
        run_id = env["CLAIM_PLANE_CONTROLLED_RUN_ID"]
        adapter.start_session(
            _request(
                AdapterOperation.START_SESSION,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={"source": "startup"},
            )
        )
        adapter.submit_task(
            _request(
                AdapterOperation.SUBMIT_TASK,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={"prompt": "Wait forever."},
            )
        )
        adapter.propose_intent(
            _request(
                AdapterOperation.PROPOSE_INTENT,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={
                    "proposal": {
                        "protocol": codex.CODEX_INTENT_PROPOSAL_PROTOCOL,
                        "goal": "Wait forever",
                        "operations": [
                            {
                                "access": "write",
                                "kind": "file",
                                "identifier": "app.py",
                                "commitment": "committed",
                            }
                        ],
                        "preserves": [],
                        "acceptance": [],
                    }
                },
            )
        )
        return _CompletedProcess("")

    def timeout(*args: Any, **kwargs: Any) -> int:
        del args, kwargs
        raise TimeoutError("controlled run exceeded its wall-time limit")

    monkeypatch.setattr("claim_plane.controlled_run._stream_runtime", timeout)
    result = run_controlled_task(
        "Wait forever.",
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        timeout_seconds=1,
        acceptance_timeout=30,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        process_factory=process_factory,
    )

    assert result.outcome is ControlledRunOutcome.TIMED_OUT
    assert result.exit_code == 124
    assert result.session_id == session_id
    assert result.cancellation is not None
    assert result.cancellation["status"] == "cancelled"
    status = codex.codex_intent_status(repo, session_id=session_id)
    assert status["state"] == "abandoned"


def test_controlled_run_retries_failed_stop_verification_with_project_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'controlled-retry'\nversion = '0.1.0'\n"
        "[tool.pytest.ini_options]\npythonpath = ['.']\n",
        encoding="utf-8",
    )
    (repo / "test_app.py").write_text(
        "from app import VALUE\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add acceptance"], cwd=repo, check=True)
    adapter, handshake = _prepare(repo, monkeypatch)
    task = "Update the fixture value."

    def process_factory(command: list[str], *, root: Path, env: Mapping[str, str]):
        assert command[1:4] == ["--ask-for-approval", "never", "exec"]
        run_id = env["CLAIM_PLANE_CONTROLLED_RUN_ID"]
        session_id = "thread_controlled_retry"
        adapter.start_session(
            _request(
                AdapterOperation.START_SESSION,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={"source": "startup"},
            )
        )
        adapter.submit_task(
            _request(
                AdapterOperation.SUBMIT_TASK,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={"prompt": task},
            )
        )
        admitted = adapter.propose_intent(
            _request(
                AdapterOperation.PROPOSE_INTENT,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={
                    "proposal": {
                        "protocol": codex.CODEX_INTENT_PROPOSAL_PROTOCOL,
                        "goal": "Update the fixture value",
                        "operations": [
                            {
                                "access": "write",
                                "kind": "file",
                                "identifier": "app.py",
                                "commitment": "committed",
                            }
                        ],
                        "preserves": [],
                        "acceptance": [],
                    }
                },
            )
        )
        first = adapter.verify_completion(
            AdapterRequest.create(
                AdapterOperation.VERIFY_COMPLETION,
                adapter="codex",
                project_root=str(repo),
                request_id=f"failed-stop-{run_id}",
                session_id=session_id,
                run_id=run_id,
                intent_id=admitted.intent_id,
                intent_version=admitted.intent_version,
                timeout_seconds=30,
                payload={"hook_event_name": "Stop", "lifecycle": True},
            )
        )
        assert first.payload["verified"] is False
        assert first.payload["acceptance_passed"] is False

        patch = (
            "*** Begin Patch\n"
            "*** Update File: app.py\n"
            "@@\n"
            "-VALUE = 1\n"
            "+VALUE = 2\n"
            "*** End Patch"
        )
        mutation = adapter.request_mutation(
            _request(
                AdapterOperation.REQUEST_MUTATION,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={"tool_name": "apply_patch", "tool_input": {"command": patch}},
            )
        )
        assert mutation.status.value == "succeeded"
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        adapter.observe_result(
            _request(
                AdapterOperation.OBSERVE_RESULT,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={"tool_name": "apply_patch"},
            )
        )
        stream = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": session_id}),
                json.dumps({"type": "turn.completed"}),
            )
        )
        return _CompletedProcess(stream + "\n")

    result = run_controlled_task(
        task,
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        timeout_seconds=30,
        acceptance_timeout=30,
        quiet=True,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        process_factory=process_factory,
    )

    assert result.outcome is ControlledRunOutcome.VERIFIED
    assert result.completion["acceptance_passed"] is True
    assert result.acceptance["command_count"] == 1
    assert result.acceptance["commands"] == ["python -m pytest"]
    assert len(result.acceptance["results"]) == 1
    acceptance_result = result.acceptance["results"][0]
    assert acceptance_result["command"] == "python -m pytest"
    assert acceptance_result["returncode"] == 0
    assert acceptance_result["passed"] is True
    assert acceptance_result["sandbox_backend"] == "tree"
    assert acceptance_result["sandbox_enforced"] is False
    persisted = load_controlled_run(repo, result.run_id)
    assert persisted["acceptance"]["results"] == result.acceptance["results"]
    assert result.lifecycle is not None
    assert result.lifecycle["valid"] is True
    assert result.lifecycle["verified"] is True


def test_guarded_run_requires_review_for_configured_critical_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)
    config = load_project_config(repo)
    config["risk"] = {
        "default": "medium",
        "include_builtin_rules": True,
        "rules": [
            {
                "match": "app.py",
                "level": "critical",
                "reason": "fixture policy boundary",
            }
        ],
    }
    (repo / ".claim-plane/config.yaml").write_text(
        dump_project_config(config), encoding="utf-8"
    )
    task = "Update the fixture value."

    result = run_controlled_task(
        task,
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        timeout_seconds=30,
        acceptance_timeout=30,
        quiet=True,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        process_factory=_successful_process_factory(adapter, repo=repo, task=task),
    )

    assert result.outcome is ControlledRunOutcome.REVIEW_REQUIRED
    assert result.exit_code == 2
    assert result.effective_policy["preset"]["name"] == "guarded"
    assert result.risk["highest_risk"] == "critical"
    assert result.risk["final_action"] == "REVIEW_REQUIRED"
    assert any(item["path"] == "app.py" for item in result.risk["findings"])


def test_evidence_report_and_replay_are_deterministic_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claim_plane.evidence import (
        build_evidence_replay,
        build_evidence_report,
        render_evidence_replay,
    )

    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)
    task = "Update the fixture value without exposing this text."
    result = run_controlled_task(
        task,
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        timeout_seconds=30,
        acceptance_timeout=30,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        process_factory=_successful_process_factory(adapter, repo=repo, task=task),
    )

    first = build_evidence_report(repo, result.run_id)
    second = build_evidence_report(repo, "latest")
    replay = build_evidence_replay(repo, result.run_id)

    assert first == second
    assert first["evidence_digest"] == second["evidence_digest"]
    assert first["integrity"]["valid"] is True
    assert first["determinism"]["verification"]["valid"] is True
    assert first["determinism"]["record"]["verdict"]["reason_code"] == "verified"
    assert replay["determinism"]["replay_equivalent"] is True
    assert replay["determinism"]["decision_digest"]
    assert first["changes"]["file_count"] == 1
    assert first["changes"]["files"][0]["path"] == "app.py"
    assert first["changes"]["files"][0]["hunks"]
    assert first["decisions"]["observed_count"] == 1
    assert replay["event_count"] >= 1
    assert render_evidence_replay(replay)[0].startswith("RUN ")
    serialized = json.dumps(first, ensure_ascii=False)
    assert task not in serialized
    assert "Updated app.py and verified the change." not in serialized


def test_cli_report_surfaces_failed_acceptance_command_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "run_id": "cpr_failed_acceptance",
        "outcome": "REJECTED",
        "evidence_digest": "e" * 64,
        "task": {"sha256": "a" * 64, "length": 12},
        "agent": {"adapter": "codex", "session_id": "session-1"},
        "inspection": {},
        "intent": {},
        "policy": {
            "name": "guarded",
            "risk": {"highest_risk": "medium", "final_action": "ALLOW"},
        },
        "changes": {
            "file_count": 0,
            "total_additions": 0,
            "total_deletions": 0,
            "total_hunks": 0,
            "files": [],
        },
        "decisions": {"blocked_count": 0, "observed_count": 0, "amendment_count": 0},
        "acceptance": {
            "passed": False,
            "classification": "COMMAND_FAILED",
            "command_count": 1,
            "results": [
                {
                    "command": "./scripts/check.sh",
                    "returncode": 1,
                    "passed": False,
                    "duration_ms": 1430,
                    "stdout_tail": "[2/10] Ruff format\n3 files would be reformatted\n",
                    "stderr_tail": "",
                }
            ],
        },
        "determinism": {},
        "execution": {"duration_seconds": 2.0, "runtime_returncode": 0},
        "integrity": {"valid": True, "findings": []},
    }
    monkeypatch.setattr(cli, "build_evidence_report", lambda root, selector: payload)

    assert cli.main(["report", "latest", "--repo", "."]) == 0
    output = capsys.readouterr().out
    assert "Acceptance: COMMAND_FAILED (1 configured commands)" in output
    assert "Failed: ./scripts/check.sh · exit 1 · 1430ms" in output
    assert "Last output: 3 files would be reformatted" in output


def test_cli_report_and_replay_support_latest_selector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)
    task = "Update the fixture value."
    result = run_controlled_task(
        task,
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        timeout_seconds=30,
        acceptance_timeout=30,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        process_factory=_successful_process_factory(adapter, repo=repo, task=task),
    )

    assert cli.main(["report", "latest", "--repo", str(repo), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["run_id"] == result.run_id
    assert report["protocol"] == "claim-plane.evidence-report.v1"

    assert cli.main(["replay", "latest", "--repo", str(repo), "--json"]) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["run_id"] == result.run_id
    assert replay["protocol"] == "claim-plane.evidence-replay.v1"


def test_latest_evidence_accepts_unbound_hook_events_with_run_bound_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claim_plane.evidence import build_evidence_replay, build_evidence_report

    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)
    first_task = "Update the fixture value in the first controlled run."
    first = run_controlled_task(
        first_task,
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        timeout_seconds=30,
        acceptance_timeout=30,
        quiet=True,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        process_factory=_successful_process_factory(
            adapter, repo=repo, task=first_task
        ),
    )
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo, check=True)

    second_task = "Update the fixture value with native hook events."
    second = run_controlled_task(
        second_task,
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        timeout_seconds=30,
        acceptance_timeout=30,
        quiet=True,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        process_factory=_unbound_hook_process_factory(
            adapter, repo=repo, task=second_task
        ),
    )

    with LifecycleEventStore.for_project(repo) as store:
        events = store.list_events(adapter="codex", session_id=second.session_id or "")
    assert any(event.run_id is None for event in events)
    assert any(event.run_id == second.run_id for event in events)

    report = build_evidence_report(repo, "latest")
    replay = build_evidence_replay(repo, "latest")

    assert first.run_id != second.run_id
    assert report["run_id"] == second.run_id
    assert report["integrity"]["valid"] is True
    assert replay["run_id"] == second.run_id
    assert replay["event_count"] == len(events)


def test_evidence_rejects_explicit_foreign_run_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claim_plane.evidence import EvidenceError, build_evidence_report

    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)
    task = "Update the fixture value."
    result = run_controlled_task(
        task,
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        timeout_seconds=30,
        acceptance_timeout=30,
        quiet=True,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        process_factory=_successful_process_factory(adapter, repo=repo, task=task),
    )
    path = controlled_run_path(repo, result.run_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["run_id"] = "cpr_foreign"
    foreign_path = controlled_run_path(repo, "cpr_foreign")
    foreign_path.parent.mkdir(parents=True)
    foreign_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvidenceError, match="different controlled run"):
        build_evidence_report(repo, "cpr_foreign")


def test_evidence_report_fails_closed_for_corrupt_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    from claim_plane.evidence import EvidenceError, build_evidence_report

    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)
    task = "Update the fixture value."
    result = run_controlled_task(
        task,
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        timeout_seconds=30,
        acceptance_timeout=30,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        process_factory=_successful_process_factory(adapter, repo=repo, task=task),
    )
    database = repo / ".claim-plane/lifecycle/events.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE lifecycle_events SET digest='broken' WHERE sequence=1"
        )
        connection.commit()

    with pytest.raises(EvidenceError):
        build_evidence_report(repo, result.run_id)


def test_controlled_run_records_optional_operator_initial_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)
    task = "Update the fixture value."

    result = run_controlled_task(
        task,
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        timeout_seconds=30,
        acceptance_timeout=30,
        initial_scope=("app.py",),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        process_factory=_successful_process_factory(adapter, repo=repo, task=task),
    )

    assert result.scope == {
        "protocol": "claim-plane.controlled-scope.v1",
        "mode": "operator",
        "initial": ["app.py"],
        "final": ["app.py"],
        "locked": False,
        "amendments": {
            "tickets_issued": 0,
            "requests": 0,
            "admitted": 0,
            "denied": 0,
            "history": [],
        },
    }
    persisted = load_controlled_run(repo, result.run_id)
    assert persisted["scope"] == result.scope


def test_controlled_scope_validation_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)

    with pytest.raises(ValueError, match="requires at least one"):
        run_controlled_task(
            "Update the fixture value.",
            root=repo,
            adapter=adapter,
            handshake=handshake,
            lock_scope=True,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
    with pytest.raises(ValueError, match="control state"):
        run_controlled_task(
            "Update the fixture value.",
            root=repo,
            adapter=adapter,
            handshake=handshake,
            initial_scope=(".claim-plane/config.yaml",),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )


def test_controlled_run_explicit_scope_records_real_brokered_amendment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    (repo / "test_app.py").write_text("EXPECTED = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "test_app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "add test fixture"], cwd=repo, check=True)
    adapter, handshake = _prepare(repo, monkeypatch)
    task = "Update app.py and keep its test fixture aligned."

    def factory(command: list[str], *, root: Path, env: Mapping[str, str]):
        assert root == repo
        assert command[-1] == task
        assert json.loads(env["CLAIM_PLANE_CONTROLLED_INITIAL_SCOPE"]) == ["app.py"]
        run_id = env["CLAIM_PLANE_CONTROLLED_RUN_ID"]
        session_id = "thread_controlled_operator_amendment"
        scope = json.loads(env["CLAIM_PLANE_CONTROLLED_INITIAL_SCOPE"])

        def scoped_request(
            operation: AdapterOperation,
            suffix: str,
            payload: Mapping[str, Any],
        ) -> AdapterRequest:
            return AdapterRequest.create(
                operation,
                adapter="codex",
                project_root=str(repo),
                request_id=f"{suffix}-{run_id}",
                session_id=session_id,
                run_id=run_id,
                payload=payload,
            )

        adapter.start_session(
            _request(
                AdapterOperation.START_SESSION,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={
                    "source": "startup",
                    "_claim_plane_initial_scope": scope,
                },
            )
        )
        adapter.submit_task(
            _request(
                AdapterOperation.SUBMIT_TASK,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={
                    "prompt": task,
                    "_claim_plane_initial_scope": scope,
                },
            )
        )
        adapter.propose_intent(
            _request(
                AdapterOperation.PROPOSE_INTENT,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={
                    "proposal": {
                        "protocol": codex.CODEX_INTENT_PROPOSAL_PROTOCOL,
                        "goal": "Update the value and matching test fixture",
                        "operations": [
                            {
                                "access": "write",
                                "kind": "file",
                                "identifier": "app.py",
                                "commitment": "committed",
                            },
                            {
                                "access": "test",
                                "kind": "file",
                                "identifier": "test_app.py",
                                "commitment": "committed",
                            },
                        ],
                        "preserves": [],
                        "acceptance": [],
                    }
                },
            )
        )

        app_patch = (
            "*** Begin Patch\n"
            "*** Update File: app.py\n"
            "@@\n"
            "-VALUE = 1\n"
            "+VALUE = 2\n"
            "*** End Patch"
        )
        allowed = adapter.request_mutation(
            scoped_request(
                AdapterOperation.REQUEST_MUTATION,
                "mutate-app",
                {
                    "tool_name": "apply_patch",
                    "tool_input": {"command": app_patch},
                },
            )
        )
        assert allowed.status.value == "succeeded"
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        adapter.observe_result(
            scoped_request(
                AdapterOperation.OBSERVE_RESULT,
                "observe-app",
                {"tool_name": "apply_patch"},
            )
        )

        test_patch = (
            "*** Begin Patch\n"
            "*** Update File: test_app.py\n"
            "@@\n"
            "-EXPECTED = 1\n"
            "+EXPECTED = 2\n"
            "*** End Patch"
        )
        denied = adapter.request_mutation(
            scoped_request(
                AdapterOperation.REQUEST_MUTATION,
                "mutate-test-denied",
                {
                    "tool_name": "apply_patch",
                    "tool_input": {"command": test_patch},
                },
            )
        )
        assert denied.status.value == "denied"
        status = codex.codex_intent_status(repo, session_id=session_id)
        ticket = status["scope_amendment"]["pending"]["ticket_id"]
        amended = codex.amend_codex_scope(
            repo,
            session_id=session_id,
            ticket_id=ticket,
            reason="The behavior change requires its test fixture to stay aligned.",
        )
        assert amended["allowed"] is True

        retry = adapter.request_mutation(
            scoped_request(
                AdapterOperation.REQUEST_MUTATION,
                "mutate-test-retry",
                {
                    "tool_name": "apply_patch",
                    "tool_input": {"command": test_patch},
                },
            )
        )
        assert retry.status.value == "succeeded"
        (repo / "test_app.py").write_text("EXPECTED = 2\n", encoding="utf-8")
        observed = adapter.observe_result(
            scoped_request(
                AdapterOperation.OBSERVE_RESULT,
                "observe-test",
                {"tool_name": "apply_patch"},
            )
        )
        verified = adapter.verify_completion(
            AdapterRequest.create(
                AdapterOperation.VERIFY_COMPLETION,
                adapter="codex",
                project_root=str(repo),
                request_id=f"verify-{run_id}",
                session_id=session_id,
                run_id=run_id,
                intent_id=observed.intent_id,
                intent_version=observed.intent_version,
                timeout_seconds=30,
            )
        )
        assert verified.payload["verified"] is True
        adapter.stop_session(
            AdapterRequest.create(
                AdapterOperation.STOP_SESSION,
                adapter="codex",
                project_root=str(repo),
                request_id=f"stop-{run_id}",
                session_id=session_id,
                run_id=run_id,
            )
        )
        stream = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": session_id}),
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "turn.completed"}),
            )
        )
        return _CompletedProcess(stream + "\n")

    output = io.StringIO()
    result = run_controlled_task(
        task,
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        timeout_seconds=30,
        acceptance_timeout=30,
        initial_scope=("app.py",),
        stdout=output,
        stderr=io.StringIO(),
        process_factory=factory,
    )

    assert result.outcome is ControlledRunOutcome.VERIFIED
    assert result.scope["amendments"] == {
        "tickets_issued": 1,
        "requests": 1,
        "admitted": 1,
        "denied": 0,
        "history": [
            {
                "allowed": True,
                "resources": ["test_app.py"],
                "reason_sha256": hashlib.sha256(
                    b"The behavior change requires its test fixture to stay aligned."
                ).hexdigest(),
            }
        ],
    }
    assert "Scope amendment admitted" in output.getvalue()


@pytest.mark.parametrize("defer_acceptance", (False, True))
def test_interactive_codex_launcher_preserves_tui_and_seals_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    defer_acceptance: bool,
) -> None:
    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)
    task = "Update the fixture value interactively."
    output = io.StringIO()

    def factory(command: list[str], *, root: Path, env: Mapping[str, str]):
        assert root == repo
        assert command[1:3] == ["--ask-for-approval", "never"]
        assert command[3:5] == ["--sandbox", "workspace-write"]
        assert "exec" not in command
        assert command[-1] == task
        run_id = env["CLAIM_PLANE_CONTROLLED_RUN_ID"]
        assert env["CLAIM_PLANE_CONTROLLED_INTERACTIVE"] == "1"
        session_id = "thread_interactive_success"
        adapter.start_session(
            _request(
                AdapterOperation.START_SESSION,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={"source": "startup"},
            )
        )
        adapter.submit_task(
            _request(
                AdapterOperation.SUBMIT_TASK,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={"prompt": task},
            )
        )
        adapter.propose_intent(
            _request(
                AdapterOperation.PROPOSE_INTENT,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={
                    "proposal": {
                        "protocol": codex.CODEX_INTENT_PROPOSAL_PROTOCOL,
                        "goal": "Update the fixture value",
                        "operations": [
                            {
                                "access": "write",
                                "kind": "file",
                                "identifier": "app.py",
                                "commitment": "committed",
                            }
                        ],
                        "preserves": [],
                        "acceptance": [],
                    }
                },
            )
        )
        patch = (
            "*** Begin Patch\n"
            "*** Update File: app.py\n"
            "@@\n"
            "-VALUE = 1\n"
            "+VALUE = 2\n"
            "*** End Patch"
        )
        mutation = adapter.request_mutation(
            _request(
                AdapterOperation.REQUEST_MUTATION,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={"tool_name": "apply_patch", "tool_input": {"command": patch}},
            )
        )
        assert mutation.status.value == "succeeded"
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        observed = adapter.observe_result(
            _request(
                AdapterOperation.OBSERVE_RESULT,
                repo=repo,
                run_id=run_id,
                session_id=session_id,
                payload={"tool_name": "apply_patch"},
            )
        )
        assert observed.intent_id is not None
        pending_output = io.StringIO()
        assert (
            adapter.dispatch_hook(
                {
                    "hook_event_name": "Stop",
                    "session_id": session_id,
                    "cwd": str(repo),
                    "event_id": "interactive-turn-1",
                    "_claim_plane_run_id": run_id,
                    "_claim_plane_interactive": True,
                },
                output=pending_output,
            )
            == 0
        )
        assert "AGENT TURN COMPLETED" in pending_output.getvalue()
        assert "final verification pending" in pending_output.getvalue()
        status = codex.codex_intent_status(repo, session_id=session_id)
        assert status["state"] == "awaiting_final_verification"
        assert not status.get("completion")
        assert (
            adapter.dispatch_hook(
                {
                    "hook_event_name": "SessionEnd",
                    "session_id": session_id,
                    "cwd": str(repo),
                    "event_id": "interactive-session-end",
                    "_claim_plane_run_id": run_id,
                    "_claim_plane_interactive": True,
                    "reason": "user_exit",
                }
            )
            == 0
        )
        return _CompletedProcess("")

    result = run_interactive_codex(
        task,
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        model="gpt-test",
        stdout=output,
        stderr=io.StringIO(),
        require_tty=False,
        process_factory=factory,
        defer_acceptance=defer_acceptance,
    )

    expected_outcome = (
        ControlledRunOutcome.REVIEW_REQUIRED
        if defer_acceptance
        else ControlledRunOutcome.VERIFIED
    )
    assert result.outcome is expected_outcome
    assert result.exit_code == 0
    assert result.runtime["interactive"] is True
    assert result.runtime["launcher"] == "codex_tui"
    assert result.task_sha256 == hashlib.sha256(task.encode("utf-8")).hexdigest()
    assert "Claim Plane · Interactive Codex" in output.getvalue()
    stored = load_controlled_run(repo, result.run_id)
    if defer_acceptance:
        assert "DELIVERY AWAITING EXTERNAL ACCEPTANCE" in output.getvalue()
        assert result.completion["authority_verified"] is True
        assert result.completion["acceptance_deferred"] is True
        assert stored["verified"] is False
    else:
        assert "DELIVERY VERIFIED" in output.getvalue()
        assert stored["verified"] is True


def test_interactive_codex_rejects_passthrough_authority_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)

    with pytest.raises(ValueError, match="owned by Claim Plane"):
        run_interactive_codex(
            root=repo,
            adapter=adapter,
            handshake=handshake,
            codex_args=("--sandbox", "danger-full-access"),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            require_tty=False,
        )
