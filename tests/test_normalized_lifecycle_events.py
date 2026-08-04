"""Runtime-neutral lifecycle ordering, replay, and redaction."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from claim_plane.connectors import CodexAdapter, init_project
from claim_plane.protocol import (
    AdapterErrorCode,
    AdapterOperation,
    AdapterProtocolError,
    AdapterRequest,
    AdapterResponse,
    AdapterStatus,
    LifecycleConflictError,
    LifecycleCorruptError,
    LifecycleEvent,
    LifecycleEventDraft,
    LifecycleEventStore,
    LifecycleEventType,
    build_lifecycle_report,
    lifecycle_event_drafts,
    load_lifecycle_ndjson,
    render_lifecycle_replay,
)


def _draft(
    event_type: LifecycleEventType,
    **payload: object,
) -> LifecycleEventDraft:
    return LifecycleEventDraft(event_type, payload)


def _append_reference_session(
    store: LifecycleEventStore,
    *,
    session_id: str = "reference-session",
) -> tuple[LifecycleEvent, ...]:
    batches = (
        (
            "start",
            (_draft(LifecycleEventType.SESSION_STARTED, source="test"),),
        ),
        (
            "task",
            (_draft(LifecycleEventType.TASK_SUBMITTED, task_digest="abc"),),
        ),
        (
            "intent",
            (
                _draft(LifecycleEventType.INTENT_PROPOSED, proposal_digest="def"),
                _draft(LifecycleEventType.ADMISSION_REQUESTED, request_id="intent"),
                _draft(LifecycleEventType.ADMISSION_GRANTED, status="succeeded"),
            ),
        ),
        (
            "verify",
            (
                _draft(LifecycleEventType.VERIFICATION_STARTED),
                _draft(
                    LifecycleEventType.VERIFICATION_COMPLETED,
                    verified=True,
                    status="succeeded",
                ),
            ),
        ),
    )
    events: list[LifecycleEvent] = []
    for request_id, drafts in batches:
        events.extend(
            store.append_batch(
                adapter="reference",
                session_id=session_id,
                request_id=request_id,
                drafts=drafts,
                run_id="run-reference",
                default_intent_id="intent-reference",
                default_intent_version=1,
            )
        )
    return tuple(events)


def test_reference_adapter_report_replay_and_export_use_generic_code(
    tmp_path: Path,
) -> None:
    with LifecycleEventStore(tmp_path / "events.sqlite3") as store:
        events = _append_reference_session(store)
        report = store.report(
            adapter="reference",
            session_id="reference-session",
        )
        replay = store.replay(
            adapter="reference",
            session_id="reference-session",
        )
        exported = store.export_ndjson(
            adapter="reference",
            session_id="reference-session",
            destination=tmp_path / "events.ndjson",
        )

    assert report.valid is True
    assert report.verified is True
    assert report.outcome == "verified"
    assert report.event_count == 7
    assert len(replay) == len(events)
    assert replay[0].endswith("SessionStarted intent=intent-reference@1")
    assert replay[-1].endswith("VerificationCompleted intent=intent-reference@1")
    loaded = load_lifecycle_ndjson(exported)
    assert loaded == events


def test_duplicate_request_events_are_suppressed_and_conflicts_fail_closed(
    tmp_path: Path,
) -> None:
    with LifecycleEventStore(tmp_path / "events.sqlite3") as store:
        original = store.append_batch(
            adapter="reference",
            session_id="session-duplicate",
            request_id="start-once",
            drafts=(_draft(LifecycleEventType.SESSION_STARTED, source="test"),),
        )
        replayed = store.append_batch(
            adapter="reference",
            session_id="session-duplicate",
            request_id="start-once",
            drafts=(_draft(LifecycleEventType.SESSION_STARTED, source="test"),),
        )

        assert replayed == original
        assert (
            len(
                store.list_events(
                    adapter="reference",
                    session_id="session-duplicate",
                )
            )
            == 1
        )

        with pytest.raises(LifecycleConflictError):
            store.append_batch(
                adapter="reference",
                session_id="session-duplicate",
                request_id="start-once",
                drafts=(_draft(LifecycleEventType.SESSION_STARTED, source="changed"),),
            )


def test_out_of_order_append_is_rejected_atomically(tmp_path: Path) -> None:
    with LifecycleEventStore(tmp_path / "events.sqlite3") as store:
        with pytest.raises(LifecycleConflictError):
            store.append_batch(
                adapter="reference",
                session_id="session-order",
                request_id="task-before-session",
                drafts=(_draft(LifecycleEventType.TASK_SUBMITTED),),
            )
        assert (
            store.list_events(
                adapter="reference",
                session_id="session-order",
            )
            == ()
        )


def test_corrupt_order_can_never_project_to_verified() -> None:
    first = LifecycleEvent(
        event_id="evt-first",
        event_type=LifecycleEventType.SESSION_STARTED,
        adapter="reference",
        session_id="session-corrupt",
        sequence=1,
        timestamp="2026-08-03T00:00:00Z",
    )
    completed = LifecycleEvent(
        event_id="evt-completed",
        event_type=LifecycleEventType.VERIFICATION_COMPLETED,
        adapter="reference",
        session_id="session-corrupt",
        sequence=2,
        caused_by="evt-first",
        timestamp="2026-08-03T00:00:01Z",
        payload={"verified": True},
    )

    report = build_lifecycle_report((first, completed))
    replay = render_lifecycle_replay((first, completed))

    assert report.valid is False
    assert report.verified is False
    assert report.outcome == "corrupt"
    assert replay[0].startswith("INVALID LIFECYCLE:")


def test_lifecycle_payloads_redact_prompts_commands_and_credentials() -> None:
    secret = "sk-secret-value-never-export"
    request = AdapterRequest.create(
        AdapterOperation.REQUEST_MUTATION,
        adapter="reference",
        project_root=".",
        request_id="mutation-secret",
        session_id="session-secret",
        intent_id="intent-secret",
        intent_version=2,
        payload={
            "tool_name": "shell",
            "prompt": secret,
            "api_token": secret,
            "tool_input": {
                "command": f"curl -H 'Authorization: Bearer {secret}' example.invalid"
            },
        },
    )
    response = AdapterResponse(
        request_id=request.request_id,
        operation=request.operation,
        adapter="reference",
        status=AdapterStatus.DENIED,
        session_id=request.session_id,
        intent_id=request.intent_id,
        intent_version=request.intent_version,
        payload={"allowed": False, "hook_output": secret},
    )

    serialized = json.dumps(
        [
            {"event_type": draft.event_type.value, "payload": dict(draft.payload)}
            for draft in lifecycle_event_drafts(request, response=response)
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    assert secret not in serialized
    assert "Authorization" not in serialized
    assert "curl" not in serialized


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
    init_project(repo)
    return repo


def _request(
    operation: AdapterOperation,
    repo: Path,
    request_id: str,
    *,
    session_id: str,
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


def test_codex_uses_normalized_events_for_report_replay_and_resume(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    adapter = CodexAdapter()
    session_id = "session-lifecycle"
    adapter.enroll_project(
        AdapterRequest.create(
            AdapterOperation.ENROLL_PROJECT,
            adapter="codex",
            project_root=str(repo),
            request_id="enroll-lifecycle",
        )
    )
    adapter.start_session(
        _request(
            AdapterOperation.START_SESSION,
            repo,
            "start-lifecycle",
            session_id=session_id,
            payload={"source": "startup"},
        )
    )
    adapter.submit_task(
        _request(
            AdapterOperation.SUBMIT_TASK,
            repo,
            "task-lifecycle",
            session_id=session_id,
            payload={"prompt": "Update the repository title."},
        )
    )
    admitted = adapter.propose_intent(
        _request(
            AdapterOperation.PROPOSE_INTENT,
            repo,
            "intent-lifecycle",
            session_id=session_id,
            intent_version=0,
            payload={"proposal": _proposal()},
        )
    )
    resumed = adapter.resume(
        _request(
            AdapterOperation.RESUME,
            repo,
            "resume-lifecycle",
            session_id=session_id,
            intent_id=admitted.intent_id,
            intent_version=admitted.intent_version,
            payload={"source": "resume"},
        )
    )
    mutation_payload = {
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
    }
    allowed = adapter.request_mutation(
        _request(
            AdapterOperation.REQUEST_MUTATION,
            repo,
            "mutation-lifecycle",
            session_id=session_id,
            intent_id=resumed.intent_id,
            intent_version=resumed.intent_version,
            payload=mutation_payload,
        )
    )
    (repo / "README.md").write_text("# updated\n", encoding="utf-8")
    observed = adapter.observe_result(
        _request(
            AdapterOperation.OBSERVE_RESULT,
            repo,
            "observed-lifecycle",
            session_id=session_id,
            intent_id=allowed.intent_id,
            intent_version=allowed.intent_version,
            payload=mutation_payload,
        )
    )
    completed = adapter.verify_completion(
        _request(
            AdapterOperation.VERIFY_COMPLETION,
            repo,
            "verify-lifecycle",
            session_id=session_id,
            intent_id=observed.intent_id,
            intent_version=observed.intent_version,
        )
    )
    assert completed.status is AdapterStatus.SUCCEEDED

    with LifecycleEventStore.for_project(repo) as store:
        events = store.list_events(adapter="codex", session_id=session_id)
        report = store.report(adapter="codex", session_id=session_id)
        replay = store.replay(adapter="codex", session_id=session_id)
        exported = store.export_ndjson(
            adapter="codex",
            session_id=session_id,
            destination=tmp_path / "codex-events.ndjson",
        )

    assert report.valid is True
    assert report.outcome == "verified"
    assert report.verified is True
    assert [event.event_type for event in events] == [
        LifecycleEventType.SESSION_STARTED,
        LifecycleEventType.TASK_SUBMITTED,
        LifecycleEventType.INTENT_PROPOSED,
        LifecycleEventType.ADMISSION_REQUESTED,
        LifecycleEventType.ADMISSION_GRANTED,
        LifecycleEventType.SESSION_STARTED,
        LifecycleEventType.MUTATION_REQUESTED,
        LifecycleEventType.MUTATION_ALLOWED,
        LifecycleEventType.MUTATION_OBSERVED,
        LifecycleEventType.VERIFICATION_STARTED,
        LifecycleEventType.VERIFICATION_COMPLETED,
    ]
    assert events[5].payload["resume"] is True
    assert len(replay) == 11
    raw = exported.read_text(encoding="utf-8")
    assert "Update the repository title." not in raw
    assert "task_digest" in raw


def test_invalid_export_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "invalid.ndjson"
    path.write_text('{"event_id":"broken"}\n', encoding="utf-8")
    with pytest.raises(LifecycleCorruptError):
        load_lifecycle_ndjson(path)


def test_codex_fails_closed_before_using_a_corrupt_lifecycle_stream(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    adapter = CodexAdapter()
    session_id = "session-corrupt-store"
    adapter.enroll_project(
        AdapterRequest.create(
            AdapterOperation.ENROLL_PROJECT,
            adapter="codex",
            project_root=str(repo),
            request_id="enroll-corrupt-store",
        )
    )
    adapter.start_session(
        _request(
            AdapterOperation.START_SESSION,
            repo,
            "start-corrupt-store",
            session_id=session_id,
            payload={"source": "startup"},
        )
    )

    database = repo / ".claim-plane/lifecycle/events.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE lifecycle_events SET payload_json = ? WHERE sequence = 1",
            ('{"source":"tampered"}',),
        )
        connection.commit()

    with pytest.raises(AdapterProtocolError) as caught:
        adapter.submit_task(
            _request(
                AdapterOperation.SUBMIT_TASK,
                repo,
                "task-after-corruption",
                session_id=session_id,
                payload={"prompt": "This must not be accepted."},
            )
        )
    assert caught.value.code is AdapterErrorCode.CORRUPT_STATE
