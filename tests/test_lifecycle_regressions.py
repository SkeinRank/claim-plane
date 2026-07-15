"""Regression coverage for intent retry, terminal cleanup, and payload validation."""

from __future__ import annotations

import pytest

from claim_plane import (
    AccessMode,
    ChangeIntent,
    IntentOperation,
    Plane,
    ResourceKind,
    ResourceRef,
)


def _intent(intent_id: str, owner: str, path: str = "src/core.py") -> ChangeIntent:
    return ChangeIntent(
        intent_id=intent_id,
        task_id=intent_id,
        owner=owner,
        base_revision="0" * 40,
        operations=(
            IntentOperation(
                AccessMode.WRITE,
                ResourceRef(ResourceKind.FILE, path),
            ),
        ),
    )


def test_blocked_intent_is_re_evaluated_after_blocker_completes() -> None:
    plane = Plane.open(":memory:")
    blocker = _intent("a", "agent-a")
    waiting = _intent("b", "agent-b")

    assert plane.admit(blocker).allowed
    first = plane.admit(waiting)
    assert not first.allowed
    assert plane.intents()[-1]["state"] == "blocked"

    plane.complete("a")
    retry = plane.admit(waiting)

    assert retry.allowed
    record = next(item for item in plane.intents() if item["intent_id"] == "b")
    assert record["state"] == "admitted"
    assert record["version"] == 2


def test_identical_retry_remains_blocked_while_blocker_is_active() -> None:
    plane = Plane.open(":memory:")
    assert plane.admit(_intent("a", "agent-a")).allowed
    waiting = _intent("b", "agent-b")
    assert not plane.admit(waiting).allowed

    retry = plane.admit(waiting)

    assert not retry.allowed
    record = next(item for item in plane.intents() if item["intent_id"] == "b")
    assert record["state"] == "blocked"
    assert record["version"] == 2


def test_release_after_completion_is_safe_and_preserves_terminal_state() -> None:
    plane = Plane.open(":memory:")
    assert plane.admit(_intent("done", "agent-a")).allowed
    plane.complete("done")

    plane.release_intent("done")
    plane.release_intent("done")

    record = next(item for item in plane.intents() if item["intent_id"] == "done")
    assert record["state"] == "completed"


def test_change_intent_missing_operations_has_human_readable_error() -> None:
    payload = {
        "intent_id": "missing-ops",
        "owner": "agent-a",
        "base_revision": "main",
    }

    with pytest.raises(
        ValueError, match="missing required ChangeIntent field: operations"
    ):
        ChangeIntent.from_dict(payload)


def test_nested_operation_validation_does_not_leak_keyerror() -> None:
    payload = {
        "intent_id": "bad-operation",
        "owner": "agent-a",
        "base_revision": "main",
        "operations": [{"kind": "file", "identifier": "src/core.py"}],
    }

    with pytest.raises(
        ValueError, match="missing required IntentOperation field: access"
    ):
        ChangeIntent.from_dict(payload)
