from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Any, Mapping

import pytest

from claim_plane.connectors import codex
from claim_plane.connectors.codex_adapter import CodexAdapter
from claim_plane.controlled_run import (
    ControlledRunOutcome,
    run_controlled_task,
    run_interactive_codex,
)
from claim_plane.project import dump_project_config, load_project_config
from claim_plane.protocol import (
    AdapterOperation,
    AdapterRequest,
    LifecycleEventStore,
    LifecycleEventType,
)


class _CompletedProcess:
    def __init__(self, returncode: int = 0) -> None:
        self.pid = 999991
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
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


class _TimeoutProcess(_CompletedProcess):
    def __init__(self) -> None:
        super().__init__(0)
        self._returncode = None  # type: ignore[assignment]

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        if self._returncode is None:
            raise subprocess.TimeoutExpired("codex", timeout)
        return self._returncode


class _InterruptProcess(_CompletedProcess):
    def __init__(self) -> None:
        super().__init__(0)
        self._interrupted = False

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if not self._interrupted:
            self._interrupted = True
            raise KeyboardInterrupt
        return self._returncode


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
    (repo / "test_app.py").write_text("EXPECTED = 1\n", encoding="utf-8")
    (repo / "notes.txt").write_text("user baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


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


def _request(
    operation: AdapterOperation,
    *,
    repo: Path,
    run_id: str,
    session_id: str,
    suffix: str,
    payload: Mapping[str, Any] | None = None,
    intent_id: str | None = None,
    intent_version: int | None = None,
    timeout_seconds: float = 30.0,
) -> AdapterRequest:
    return AdapterRequest.create(
        operation,
        adapter="codex",
        project_root=str(repo),
        request_id=f"safety-{suffix}-{run_id}",
        session_id=session_id,
        run_id=run_id,
        intent_id=intent_id,
        intent_version=(
            0 if operation is AdapterOperation.PROPOSE_INTENT else intent_version
        ),
        timeout_seconds=timeout_seconds,
        payload=payload,
    )


def _bootstrap(
    adapter: CodexAdapter,
    *,
    repo: Path,
    run_id: str,
    session_id: str,
    initial_scope: tuple[str, ...] = (),
    lock_scope: bool = False,
    acceptance: tuple[str, ...] = (),
    interactive: bool = False,
    prompt: str = "Update app.py.",
) -> None:
    start_payload: dict[str, Any] = {
        "source": "startup",
        "_claim_plane_run_id": run_id,
    }
    if initial_scope:
        start_payload["_claim_plane_initial_scope"] = list(initial_scope)
    if lock_scope:
        start_payload["_claim_plane_scope_locked"] = True
    if interactive:
        start_payload["_claim_plane_interactive"] = True
    adapter.start_session(
        _request(
            AdapterOperation.START_SESSION,
            repo=repo,
            run_id=run_id,
            session_id=session_id,
            suffix="start",
            payload=start_payload,
        )
    )
    adapter.submit_task(
        _request(
            AdapterOperation.SUBMIT_TASK,
            repo=repo,
            run_id=run_id,
            session_id=session_id,
            suffix="task",
            payload={"prompt": prompt},
        )
    )
    adapter.propose_intent(
        _request(
            AdapterOperation.PROPOSE_INTENT,
            repo=repo,
            run_id=run_id,
            session_id=session_id,
            suffix="intent",
            payload={
                "proposal": {
                    "protocol": codex.CODEX_INTENT_PROPOSAL_PROTOCOL,
                    "goal": "Update app.py and keep required test coverage aligned",
                    "operations": [
                        {
                            "access": "write",
                            "kind": "file",
                            "identifier": "app.py",
                            "commitment": "committed",
                        },
                        {
                            "access": "write",
                            "kind": "file",
                            "identifier": "test_app.py",
                            "commitment": "committed",
                        },
                    ],
                    "preserves": [],
                    "acceptance": list(acceptance),
                }
            },
        )
    )


def _mutation(
    adapter: CodexAdapter,
    *,
    repo: Path,
    run_id: str,
    session_id: str,
    path: str,
    suffix: str,
):
    command = "\n".join(
        (
            "*** Begin Patch",
            f"*** Update File: {path}",
            "@@",
            "-VALUE = 1",
            "+VALUE = 2",
            "*** End Patch",
        )
    )
    return adapter.request_mutation(
        _request(
            AdapterOperation.REQUEST_MUTATION,
            repo=repo,
            run_id=run_id,
            session_id=session_id,
            suffix=suffix,
            payload={"tool_name": "apply_patch", "tool_input": {"command": command}},
        )
    )


def test_locked_scope_denies_expansion_without_issuing_a_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    adapter, _ = _prepare(repo, monkeypatch)
    _bootstrap(
        adapter,
        repo=repo,
        run_id="locked",
        session_id="session-locked",
        initial_scope=("app.py",),
        lock_scope=True,
    )

    denied = _mutation(
        adapter,
        repo=repo,
        run_id="locked",
        session_id="session-locked",
        path="test_app.py",
        suffix="outside",
    )
    status = codex.codex_intent_status(repo, session_id="session-locked")

    assert denied.status.value == "denied"
    assert (
        denied.payload["hook_result"]["hookSpecificOutput"]["permissionDecision"]
        == "deny"
    )
    assert not status["scope_amendment"]["pending"]
    assert status["operator_scope"]["locked"] is True


def test_unjustified_amendment_is_denied_and_old_authority_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    adapter, _ = _prepare(repo, monkeypatch)
    _bootstrap(
        adapter,
        repo=repo,
        run_id="ungrounded",
        session_id="session-ungrounded",
        initial_scope=("app.py",),
    )
    denied = _mutation(
        adapter,
        repo=repo,
        run_id="ungrounded",
        session_id="session-ungrounded",
        path="test_app.py",
        suffix="outside",
    )
    assert denied.status.value == "denied"
    status = codex.codex_intent_status(repo, session_id="session-ungrounded")
    ticket = status["scope_amendment"]["pending"]["ticket_id"]

    with pytest.raises(ValueError, match="not grounded"):
        codex.amend_codex_scope(
            repo,
            session_id="session-ungrounded",
            ticket_id=ticket,
            reason="While here, do general cleanup.",
        )

    status = codex.codex_intent_status(repo, session_id="session-ungrounded")
    assert status["scope_amendment"]["denied"] == 1
    assert not status["scope_amendment"]["pending"]
    assert status["scope_amendment"]["history"][-1]["reason_code"] == "reason_is_vague"
    allowed = _mutation(
        adapter,
        repo=repo,
        run_id="ungrounded",
        session_id="session-ungrounded",
        path="app.py",
        suffix="old-authority",
    )
    assert allowed.status.value == "succeeded"


def test_grounded_amendment_expands_authority_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    adapter, _ = _prepare(repo, monkeypatch)
    _bootstrap(
        adapter,
        repo=repo,
        run_id="grounded",
        session_id="session-grounded",
        initial_scope=("app.py",),
    )
    denied = _mutation(
        adapter,
        repo=repo,
        run_id="grounded",
        session_id="session-grounded",
        path="test_app.py",
        suffix="outside",
    )
    assert denied.status.value == "denied"
    status = codex.codex_intent_status(repo, session_id="session-grounded")
    ticket = status["scope_amendment"]["pending"]["ticket_id"]

    amendment = codex.amend_codex_scope(
        repo,
        session_id="session-grounded",
        ticket_id=ticket,
        reason=(
            "The supporting test file is required to preserve regression coverage "
            "for the requested behavior."
        ),
    )

    assert amendment["allowed"] is True
    assert amendment["reason_code"] == "grounded"
    status = codex.codex_intent_status(repo, session_id="session-grounded")
    assert status["scope_amendment"]["admitted"] == 1
    assert not status["scope_amendment"]["pending"]
    retried = _mutation(
        adapter,
        repo=repo,
        run_id="grounded",
        session_id="session-grounded",
        path="test_app.py",
        suffix="retry",
    )
    assert retried.status.value == "succeeded"


def test_unchanged_user_dirty_file_is_preserved_and_not_attributed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)
    (repo / "notes.txt").write_text("user work in progress\n", encoding="utf-8")
    task = "Update the fixture value without touching user work."

    def factory(command: list[str], *, root: Path, env: Mapping[str, str]):
        del command
        run_id = env["CLAIM_PLANE_CONTROLLED_RUN_ID"]
        session_id = "session-dirty"
        _bootstrap(
            adapter,
            repo=root,
            run_id=run_id,
            session_id=session_id,
            initial_scope=("app.py",),
            interactive=True,
        )
        mutation = _mutation(
            adapter,
            repo=root,
            run_id=run_id,
            session_id=session_id,
            path="app.py",
            suffix="app",
        )
        assert mutation.status.value == "succeeded"
        (root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        return _CompletedProcess()

    result = run_controlled_task(
        task,
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        initial_scope=("app.py",),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        process_factory=factory,
    )

    assert result.outcome is ControlledRunOutcome.VERIFIED
    assert [item["path"] for item in result.changes["files"]] == ["app.py"]
    assert {item["path"] for item in result.risk["findings"]} == {"app.py"}
    assert (repo / "notes.txt").read_text(encoding="utf-8") == "user work in progress\n"


def test_project_acceptance_cannot_be_extended_by_agent_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)
    config = load_project_config(repo)
    config["acceptance"]["commands"] = ['python -c "raise SystemExit(0)"']
    (repo / ".claim-plane/config.yaml").write_text(
        dump_project_config(config), encoding="utf-8"
    )

    def factory(command: list[str], *, root: Path, env: Mapping[str, str]):
        del command
        run_id = env["CLAIM_PLANE_CONTROLLED_RUN_ID"]
        session_id = "session-project-acceptance-authority"
        _bootstrap(
            adapter,
            repo=root,
            run_id=run_id,
            session_id=session_id,
            initial_scope=("app.py",),
            acceptance=('farewell() returns "goodbye"',),
            interactive=True,
        )
        mutation = _mutation(
            adapter,
            repo=root,
            run_id=run_id,
            session_id=session_id,
            path="app.py",
            suffix="app",
        )
        assert mutation.status.value == "succeeded"
        (root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        return _CompletedProcess()

    result = run_controlled_task(
        "Update the fixture value.",
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        initial_scope=("app.py",),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        process_factory=factory,
    )

    assert result.outcome is ControlledRunOutcome.VERIFIED
    assert result.acceptance["passed"] is True
    assert result.acceptance["commands"] == ['python -c "raise SystemExit(0)"']
    session = codex.codex_intent_status(
        repo, session_id="session-project-acceptance-authority"
    )
    assert session["acceptance"] == ['python -c "raise SystemExit(0)"']


def test_acceptance_failure_records_duration_and_rejects_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)
    config = load_project_config(repo)
    config["acceptance"]["commands"] = [
        'python -c "import time; time.sleep(0.02); raise SystemExit(7)"'
    ]
    (repo / ".claim-plane/config.yaml").write_text(
        dump_project_config(config), encoding="utf-8"
    )

    def factory(command: list[str], *, root: Path, env: Mapping[str, str]):
        del command
        run_id = env["CLAIM_PLANE_CONTROLLED_RUN_ID"]
        session_id = "session-acceptance-fail"
        _bootstrap(
            adapter,
            repo=root,
            run_id=run_id,
            session_id=session_id,
            initial_scope=("app.py",),
            acceptance=("agent claims the delivery is acceptable",),
            interactive=True,
        )
        mutation = _mutation(
            adapter,
            repo=root,
            run_id=run_id,
            session_id=session_id,
            path="app.py",
            suffix="app",
        )
        assert mutation.status.value == "succeeded"
        (root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        return _CompletedProcess()

    output = io.StringIO()
    result = run_controlled_task(
        "Update the fixture value.",
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        initial_scope=("app.py",),
        stdout=output,
        stderr=io.StringIO(),
        process_factory=factory,
    )

    assert result.outcome is ControlledRunOutcome.REJECTED
    assert result.acceptance["passed"] is False
    assert result.acceptance["duration_ms"] >= 10
    assert "Acceptance not verified" in output.getvalue()


@pytest.mark.parametrize(
    ("process_type", "expected_outcome", "expected_code"),
    [
        (_TimeoutProcess, ControlledRunOutcome.TIMED_OUT, 124),
        (_InterruptProcess, ControlledRunOutcome.CANCELLED, 130),
    ],
)
def test_interactive_abort_revokes_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process_type: type[_CompletedProcess],
    expected_outcome: ControlledRunOutcome,
    expected_code: int,
) -> None:
    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)
    session_id = f"session-{expected_outcome.value.lower()}"

    def factory(command: list[str], *, root: Path, env: Mapping[str, str]):
        del command
        _bootstrap(
            adapter,
            repo=root,
            run_id=env["CLAIM_PLANE_CONTROLLED_RUN_ID"],
            session_id=session_id,
            initial_scope=("app.py",),
            interactive=True,
        )
        return process_type()

    result = run_interactive_codex(
        "Update the fixture value.",
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        timeout_seconds=0.01,
        initial_scope=("app.py",),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        require_tty=False,
        process_factory=factory,
    )

    assert result.outcome is expected_outcome
    assert result.exit_code == expected_code
    assert result.cancellation is not None
    assert result.cancellation["status"] == "cancelled"
    status = codex.codex_intent_status(repo, session_id=session_id)
    assert status["state"] == "abandoned"


def test_interactive_multi_turn_session_finalizes_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)
    session_id = "session-multi-turn"

    def factory(command: list[str], *, root: Path, env: Mapping[str, str]):
        del command
        run_id = env["CLAIM_PLANE_CONTROLLED_RUN_ID"]
        _bootstrap(
            adapter,
            repo=root,
            run_id=run_id,
            session_id=session_id,
            initial_scope=("app.py",),
            interactive=True,
        )
        mutation = _mutation(
            adapter,
            repo=root,
            run_id=run_id,
            session_id=session_id,
            path="app.py",
            suffix="app",
        )
        assert mutation.status.value == "succeeded"
        (root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        for turn in (1, 2):
            output = io.StringIO()
            assert (
                adapter.dispatch_hook(
                    {
                        "hook_event_name": "Stop",
                        "session_id": session_id,
                        "cwd": str(root),
                        "event_id": f"turn-{turn}",
                        "_claim_plane_run_id": run_id,
                        "_claim_plane_interactive": True,
                    },
                    output=output,
                )
                == 0
            )
            assert "AGENT TURN COMPLETED" in output.getvalue()
            if turn == 1:
                adapter.submit_task(
                    _request(
                        AdapterOperation.SUBMIT_TASK,
                        repo=root,
                        run_id=run_id,
                        session_id=session_id,
                        suffix="follow-up",
                        payload={"prompt": "Review the change and finish."},
                    )
                )
        return _CompletedProcess()

    result = run_interactive_codex(
        "Update the fixture value.",
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        initial_scope=("app.py",),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        require_tty=False,
        process_factory=factory,
    )

    assert result.outcome is ControlledRunOutcome.VERIFIED
    assert result.lifecycle is not None and result.lifecycle["valid"] is True
    with LifecycleEventStore.for_project(repo) as store:
        events = store.list_events(adapter="codex", session_id=session_id)
    event_types = [event.event_type for event in events]
    assert event_types.count(LifecycleEventType.AGENT_STOPPED) == 2
    assert event_types.count(LifecycleEventType.SESSION_ENDED) == 1
    assert event_types.count(LifecycleEventType.VERIFICATION_COMPLETED) == 1


def test_locked_scope_rejects_clean_diff_when_operator_test_obligation_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    adapter, handshake = _prepare(repo, monkeypatch)
    prompt = (
        'Add a farewell() function to app.py that returns "goodbye". '
        "Update the appropriate existing test coverage. "
        "Do not run pytest yourself; Claim Plane will perform final verification."
    )

    def factory(command: list[str], *, root: Path, env: Mapping[str, str]):
        del command
        run_id = env["CLAIM_PLANE_CONTROLLED_RUN_ID"]
        session_id = "session-locked-obligation"
        _bootstrap(
            adapter,
            repo=root,
            run_id=run_id,
            session_id=session_id,
            initial_scope=("app.py",),
            lock_scope=True,
            interactive=True,
            prompt=prompt,
        )
        mutation = _mutation(
            adapter,
            repo=root,
            run_id=run_id,
            session_id=session_id,
            path="app.py",
            suffix="app",
        )
        assert mutation.status.value == "succeeded"
        (root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        return _CompletedProcess()

    output = io.StringIO()
    result = run_interactive_codex(
        None,
        root=repo,
        adapter=adapter,
        handshake=handshake,
        policy="guarded",
        initial_scope=("app.py",),
        lock_scope=True,
        stdout=output,
        stderr=io.StringIO(),
        require_tty=False,
        process_factory=factory,
    )

    assert result.outcome is ControlledRunOutcome.REJECTED
    assert result.acceptance["passed"] is True
    assert result.completion["task_obligations"]["unsatisfied"] == ["test_change"]
    assert result.error is not None
    assert result.error["code"] == "task_obligation_unsatisfied"
    rendered = output.getvalue()
    assert "Task incomplete" in rendered
    assert "DELIVERY REJECTED" in rendered
    assert "DELIVERY VERIFIED" not in rendered
    status = codex.codex_intent_status(repo, session_id="session-locked-obligation")
    assert status["state"] == "verification_failed"


def test_brokered_test_change_satisfies_operator_obligation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    adapter, _ = _prepare(repo, monkeypatch)
    prompt = "Update app.py and update the appropriate existing test coverage."
    run_id = "obligation-satisfied"
    session_id = "session-obligation-satisfied"
    _bootstrap(
        adapter,
        repo=repo,
        run_id=run_id,
        session_id=session_id,
        initial_scope=("app.py",),
        interactive=True,
        prompt=prompt,
    )
    app_mutation = _mutation(
        adapter,
        repo=repo,
        run_id=run_id,
        session_id=session_id,
        path="app.py",
        suffix="app",
    )
    assert app_mutation.status.value == "succeeded"
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    denied = _mutation(
        adapter,
        repo=repo,
        run_id=run_id,
        session_id=session_id,
        path="test_app.py",
        suffix="test",
    )
    assert denied.status.value == "denied"
    status = codex.codex_intent_status(repo, session_id=session_id)
    ticket = status["scope_amendment"]["pending"]["ticket_id"]
    amendment = codex.amend_codex_scope(
        repo,
        session_id=session_id,
        ticket_id=ticket,
        reason=(
            "The test file is required to provide the requested regression coverage."
        ),
    )
    assert amendment["allowed"] is True
    retried = _mutation(
        adapter,
        repo=repo,
        run_id=run_id,
        session_id=session_id,
        path="test_app.py",
        suffix="test-retry",
    )
    assert retried.status.value == "succeeded"
    (repo / "test_app.py").write_text("EXPECTED = 2\n", encoding="utf-8")

    completion = codex.verify_codex_completion(repo, session_id=session_id)

    assert completion["verified"] is True
    assert completion["task_obligations"]["all_satisfied"] is True
    assert completion["task_obligations"]["satisfied"] == ["test_change"]
