from __future__ import annotations

import json

import pytest

from claim_plane import (
    RUNTIME_RECOVERY_PROTOCOL,
    AccessMode,
    ChangeIntent,
    IntentOperation,
    Plane,
    ResourceKind,
    ResourceRef,
    RuntimeRecovery,
    RuntimeRecoveryState,
)


BASE = "a" * 40
REFRESHED_BASE = "d" * 40


def _op(
    access: AccessMode,
    kind: ResourceKind,
    identifier: str,
    **kwargs,
) -> IntentOperation:
    return IntentOperation(access, ResourceRef(kind, identifier, **kwargs))


def _intent(
    intent_id: str,
    owner: str,
    *operations: IntentOperation,
    base_commit: str = BASE,
) -> ChangeIntent:
    return ChangeIntent(
        intent_id=intent_id,
        task_id=intent_id,
        owner=owner,
        base_revision=base_commit,
        base_commit=base_commit,
        operations=tuple(operations),
    )


def _producer(signature: str) -> ChangeIntent:
    return _intent(
        "producer",
        "agent-producer",
        _op(AccessMode.WRITE, ResourceKind.FILE, "producer.py"),
        _op(
            AccessMode.WRITE,
            ResourceKind.CONTRACT,
            "load_x",
            signature=signature,
            subject_concept_id="X",
        ),
    )


def _consumer(
    signature: str = "load_x()->X", *, base_commit: str = BASE
) -> ChangeIntent:
    return _intent(
        "consumer",
        "agent-consumer",
        _op(AccessMode.WRITE, ResourceKind.FILE, "consumer.py"),
        _op(
            AccessMode.READ,
            ResourceKind.CONTRACT,
            "load_x",
            signature=signature,
            subject_concept_id="X",
        ),
        base_commit=base_commit,
    )


def _register_broker(
    plane: Plane,
    *,
    instance_id: str,
    session_id: str,
    base_commit: str,
) -> dict:
    return plane.register_broker_instance(
        instance_id=instance_id,
        intent_id="consumer",
        session_id=session_id,
        monitor_id="runtime-broker",
        key_id="test",
        root_path="/tmp/claim-plane-runtime-recovery",
        repo_identity="b" * 64,
        base_commit=base_commit,
        initial_tree_hash="tree-" + instance_id,
        writer_lease_seconds=300,
        policy={"mode": "governed"},
        binary_digest="c" * 64,
        broker_key=b"broker-key",
    )


def _make_stale_consumer(
    *, live_broker: bool = True, complete_producer: bool = True
) -> tuple[Plane, int | None]:
    plane = Plane.open(":memory:")
    assert plane.admit(_producer("load_x()->X")).allowed
    assert plane.admit(_consumer()).allowed
    old_token: int | None = None
    if live_broker:
        old = _register_broker(
            plane,
            instance_id="broker-old",
            session_id="session-old",
            base_commit=BASE,
        )
        old_token = int(old["fencing_token"])
    assert plane.amend(_producer("load_x(context)->X")).allowed
    assert (
        next(item for item in plane.intents() if item["intent_id"] == "consumer")[
            "state"
        ]
        == "stale"
    )
    if complete_producer:
        plane.complete("producer")
    return plane, old_token


def test_stale_intent_refreshes_then_resumes_with_fresh_broker_token() -> None:
    plane, old_token = _make_stale_consumer()
    assert old_token is not None

    # A stale worker may amend its declaration, but amendment alone cannot revive it.
    amended = plane.amend(_consumer("load_x(context)->X"))
    assert amended.allowed
    record = next(item for item in plane.intents() if item["intent_id"] == "consumer")
    assert record["state"] == "stale"

    refreshed_intent = _consumer("load_x(context)->X", base_commit=REFRESHED_BASE)
    decision, recovery = plane.refresh_runtime(refreshed_intent)
    assert decision.allowed
    assert recovery is not None
    assert recovery.protocol == RUNTIME_RECOVERY_PROTOCOL
    assert recovery.state is RuntimeRecoveryState.REFRESHED
    assert recovery.from_base_commit == BASE
    assert recovery.to_base_commit == REFRESHED_BASE
    assert recovery.metadata["authority_preserved"] is True
    assert recovery.metadata["requires_fresh_broker"] is True
    assert "producer" in recovery.producer_versions

    record = next(item for item in plane.intents() if item["intent_id"] == "consumer")
    assert record["state"] == "admitted"

    with pytest.raises(ValueError, match="must resume through"):
        plane.activate("consumer")
    with pytest.raises(ValueError, match="must resume before"):
        _register_broker(
            plane,
            instance_id="broker-too-early",
            session_id="session-too-early",
            base_commit=REFRESHED_BASE,
        )

    resumed = plane.resume_runtime("consumer")
    assert resumed.state is RuntimeRecoveryState.RESUMED
    assert resumed.resumed_at is not None
    record = next(item for item in plane.intents() if item["intent_id"] == "consumer")
    assert record["state"] == "active"

    fresh = _register_broker(
        plane,
        instance_id="broker-fresh",
        session_id="session-fresh",
        base_commit=REFRESHED_BASE,
    )
    assert int(fresh["fencing_token"]) > old_token
    assert plane.broker_instance("broker-old")["state"] == "fenced"

    stored = plane.runtime_recoveries("consumer")
    assert len(stored) == 1
    assert stored[0]["state"] == "resumed"
    assert len(stored[0]["fingerprint"]) == 64
    assert RuntimeRecovery.from_dict(stored[0]).recovery_id == recovery.recovery_id


def test_runtime_refresh_waits_for_stale_causing_producer_to_complete() -> None:
    plane, _ = _make_stale_consumer(live_broker=False, complete_producer=False)
    assert plane.amend(_consumer("load_x(context)->X")).allowed

    decision, recovery = plane.refresh_runtime(
        _consumer("load_x(context)->X", base_commit=REFRESHED_BASE)
    )
    assert not decision.allowed
    assert recovery is None
    assert "producers to complete" in decision.guidance
    record = next(item for item in plane.intents() if item["intent_id"] == "consumer")
    assert record["state"] == "stale"


def test_runtime_refresh_rejects_authority_expansion() -> None:
    plane, _ = _make_stale_consumer(live_broker=False)
    assert plane.amend(_consumer("load_x(context)->X")).allowed

    expanded = ChangeIntent(
        **{
            **_consumer("load_x(context)->X", base_commit=REFRESHED_BASE).to_dict(),
            "operations": [
                *_consumer("load_x(context)->X", base_commit=REFRESHED_BASE).to_dict()[
                    "operations"
                ],
                _op(AccessMode.WRITE, ResourceKind.FILE, "extra.py").to_dict(),
            ],
        }
    )
    with pytest.raises(ValueError, match="cannot change declared authority"):
        plane.refresh_runtime(expanded)


def test_manual_runtime_pause_is_durable_and_idempotent() -> None:
    plane = Plane.open(":memory:")
    assert plane.admit(_consumer()).allowed
    plane.activate("consumer")

    first = plane.pause_runtime(
        "consumer", reason="ordered_dependency", resource_keys=("contract:x",)
    )
    assert len(first) == 1
    assert first[0]["reason"] == "ordered_dependency"
    assert first[0]["broker_instance_id"] is None
    record = next(item for item in plane.intents() if item["intent_id"] == "consumer")
    assert record["state"] == "stale"

    second = plane.pause_runtime("consumer")
    assert [item["fence_id"] for item in second] == [item["fence_id"] for item in first]


def test_runtime_recovery_is_exported_with_audit(tmp_path) -> None:
    plane, _ = _make_stale_consumer(live_broker=False)
    assert plane.amend(_consumer("load_x(context)->X")).allowed
    decision, recovery = plane.refresh_runtime(
        _consumer("load_x(context)->X", base_commit=REFRESHED_BASE)
    )
    assert decision.allowed and recovery is not None
    plane.resume_runtime("consumer")

    target = tmp_path / "audit.json"
    plane.export_audit(target)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["runtime_recoveries"] == plane.runtime_recoveries()
    assert payload["runtime_recoveries"][0]["protocol"] == RUNTIME_RECOVERY_PROTOCOL
