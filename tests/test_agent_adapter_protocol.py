from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from claim_plane.connectors import CodexAdapter
from claim_plane.connectors.codex import init_project
from claim_plane.core import Plane
from claim_plane.protocol import (
    AdapterErrorCode,
    AdapterOperation,
    AdapterProtocolError,
    AdapterRequest,
    AdapterStatus,
    AgentAdapter,
)


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


def _request(
    operation: AdapterOperation,
    repo: Path,
    request_id: str,
    *,
    session_id: str | None = None,
    intent_id: str | None = None,
    intent_version: int | None = None,
    payload: dict[str, object] | None = None,
) -> AdapterRequest:
    return AdapterRequest.create(
        operation,
        adapter="codex",
        project_root=str(repo),
        request_id=request_id,
        session_id=session_id,
        intent_id=intent_id,
        intent_version=intent_version,
        payload=payload,
    )


def _enrolled(tmp_path: Path) -> tuple[Path, CodexAdapter]:
    repo = _repo(tmp_path)
    init_project(repo)
    adapter = CodexAdapter()
    adapter.enroll_project(_request(AdapterOperation.ENROLL_PROJECT, repo, "enroll-1"))
    return repo, adapter


def _start_task(
    repo: Path, adapter: CodexAdapter, session_id: str = "session-1"
) -> None:
    adapter.start_session(
        _request(
            AdapterOperation.START_SESSION,
            repo,
            "session-start-1",
            session_id=session_id,
            payload={"source": "startup"},
        )
    )
    adapter.submit_task(
        _request(
            AdapterOperation.SUBMIT_TASK,
            repo,
            "task-submit-1",
            session_id=session_id,
            payload={"prompt": "Update the repository title."},
        )
    )


def _proposal() -> dict[str, object]:
    return {
        "protocol": "claim-plane.codex-intent-proposal.v1",
        "goal": "Update the repository title",
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


def test_codex_implements_public_agent_adapter_protocol(tmp_path: Path) -> None:
    repo, adapter = _enrolled(tmp_path)

    assert isinstance(adapter, AgentAdapter)
    response = adapter.doctor(_request(AdapterOperation.DOCTOR, repo, "doctor-1"))
    assert response.status is AdapterStatus.SUCCEEDED
    assert response.payload["root"] == str(repo.resolve())


def test_adapter_request_replay_is_idempotent_and_conflicts_fail_closed(
    tmp_path: Path,
) -> None:
    repo, adapter = _enrolled(tmp_path)
    request = _request(
        AdapterOperation.START_SESSION,
        repo,
        "same-request",
        session_id="session-replay",
        payload={"source": "startup"},
    )

    first = adapter.start_session(request)
    second = adapter.start_session(request)

    assert first.replayed is False
    assert second.replayed is True
    assert first.payload == second.payload
    assert len(list((repo / ".claim-plane/codex/sessions").glob("*.json"))) == 1

    changed = _request(
        AdapterOperation.START_SESSION,
        repo,
        "same-request",
        session_id="different-session",
        payload={"source": "startup"},
    )
    with pytest.raises(AdapterProtocolError) as caught:
        adapter.start_session(changed)
    assert caught.value.code is AdapterErrorCode.IDEMPOTENCY_CONFLICT


def test_stale_intent_version_is_rejected_before_mutation(tmp_path: Path) -> None:
    repo, adapter = _enrolled(tmp_path)
    session_id = "session-stale"
    _start_task(repo, adapter, session_id)
    admitted = adapter.propose_intent(
        _request(
            AdapterOperation.PROPOSE_INTENT,
            repo,
            "intent-1",
            session_id=session_id,
            intent_version=0,
            payload={"proposal": _proposal()},
        )
    )
    assert admitted.intent_version is not None
    assert admitted.intent_version > 0

    with pytest.raises(AdapterProtocolError) as caught:
        adapter.request_mutation(
            _request(
                AdapterOperation.REQUEST_MUTATION,
                repo,
                "mutation-stale",
                session_id=session_id,
                intent_id=admitted.intent_id,
                intent_version=0,
                payload={
                    "tool_name": "apply_patch",
                    "tool_input": {
                        "command": "\n".join(
                            (
                                "*** Begin Patch",
                                "*** Update File: README.md",
                                "@@",
                                "-# fixture",
                                "+# updated",
                                "*** End Patch",
                            )
                        )
                    },
                },
            )
        )
    assert caught.value.code is AdapterErrorCode.STALE_INTENT_VERSION

    status = adapter.inspect(
        _request(
            AdapterOperation.INSPECT,
            repo,
            "inspect-stale",
            session_id=session_id,
        )
    )
    assert status.payload["guard"]["pretool_calls"] == 0


def test_guarded_codex_delivery_completes_through_adapter_protocol(
    tmp_path: Path,
) -> None:
    repo, adapter = _enrolled(tmp_path)
    session_id = "session-delivery"
    _start_task(repo, adapter, session_id)
    admitted = adapter.propose_intent(
        _request(
            AdapterOperation.PROPOSE_INTENT,
            repo,
            "intent-delivery",
            session_id=session_id,
            intent_version=0,
            payload={"proposal": _proposal()},
        )
    )
    assert admitted.status is AdapterStatus.SUCCEEDED
    assert admitted.intent_id
    assert admitted.intent_version is not None
    assert admitted.intent_version > 0

    mutation = _request(
        AdapterOperation.REQUEST_MUTATION,
        repo,
        "mutation-delivery",
        session_id=session_id,
        intent_id=admitted.intent_id,
        intent_version=admitted.intent_version,
        payload={
            "tool_name": "apply_patch",
            "tool_input": {
                "command": "\n".join(
                    (
                        "*** Begin Patch",
                        "*** Update File: README.md",
                        "@@",
                        "-# fixture",
                        "+# updated",
                        "*** End Patch",
                    )
                )
            },
        },
    )
    allowed = adapter.request_mutation(mutation)
    replayed = adapter.request_mutation(mutation)
    assert allowed.status is AdapterStatus.SUCCEEDED
    assert replayed.replayed is True

    (repo / "README.md").write_text("# updated\n", encoding="utf-8")
    completed = adapter.verify_completion(
        _request(
            AdapterOperation.VERIFY_COMPLETION,
            repo,
            "verify-delivery",
            session_id=session_id,
            intent_id=admitted.intent_id,
            intent_version=allowed.intent_version,
        )
    )
    assert completed.status is AdapterStatus.SUCCEEDED
    assert completed.payload["verified"] is True

    status = adapter.inspect(
        _request(
            AdapterOperation.INSPECT,
            repo,
            "inspect-delivery",
            session_id=session_id,
        )
    )
    assert status.payload["state"] == "verified"
    assert status.payload["guard"]["pretool_calls"] == 1


def test_cancellation_releases_authority_and_resume_never_invents_it(
    tmp_path: Path,
) -> None:
    repo, adapter = _enrolled(tmp_path)
    session_id = "session-cancel"
    _start_task(repo, adapter, session_id)
    admitted = adapter.propose_intent(
        _request(
            AdapterOperation.PROPOSE_INTENT,
            repo,
            "intent-cancel",
            session_id=session_id,
            intent_version=0,
            payload={"proposal": _proposal()},
        )
    )

    cancelled = adapter.cancel(
        _request(
            AdapterOperation.CANCEL,
            repo,
            "cancel-1",
            session_id=session_id,
            intent_id=admitted.intent_id,
            intent_version=admitted.intent_version,
        )
    )
    assert cancelled.status is AdapterStatus.CANCELLED

    plane = Plane.open(repo / ".claim-plane/plane.db")
    try:
        record = next(
            item for item in plane.intents() if item["intent_id"] == admitted.intent_id
        )
    finally:
        plane.close()
    assert record["state"] == "released"

    resumed = adapter.resume(
        _request(
            AdapterOperation.RESUME,
            repo,
            "resume-unknown",
            session_id="new-after-crash",
            payload={"source": "resume"},
        )
    )
    assert resumed.status is AdapterStatus.SUCCEEDED
    inspected = adapter.inspect(
        _request(
            AdapterOperation.INSPECT,
            repo,
            "inspect-resumed",
            session_id="new-after-crash",
        )
    )
    assert inspected.intent_id is None
    assert inspected.intent_version is None
    assert inspected.payload["state"] == "awaiting_prompt"


def test_adapter_cache_contains_no_raw_request_payload(tmp_path: Path) -> None:
    repo, adapter = _enrolled(tmp_path)
    secret = "do-not-persist-this-prompt"
    adapter.start_session(
        _request(
            AdapterOperation.START_SESSION,
            repo,
            "secret-request",
            session_id="secret-session",
            payload={"source": "startup", "prompt": secret},
        )
    )

    cache_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (repo / ".claim-plane/adapters/codex/requests").glob("*.json")
    )
    assert secret not in cache_text
