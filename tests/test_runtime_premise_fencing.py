from __future__ import annotations

import json

import pytest

from claim_plane import (
    RUNTIME_FENCE_PROTOCOL,
    AccessMode,
    ChangeIntent,
    IntentOperation,
    Plane,
    ResourceKind,
    ResourceRef,
    RuntimeFence,
)


BASE = "a" * 40


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
    dependencies: tuple[str, ...] = (),
) -> ChangeIntent:
    return ChangeIntent(
        intent_id=intent_id,
        task_id=intent_id,
        owner=owner,
        base_revision="main",
        base_commit=BASE,
        operations=tuple(operations),
        dependencies=dependencies,
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


def _consumer() -> ChangeIntent:
    return _intent(
        "consumer",
        "agent-consumer",
        _op(AccessMode.WRITE, ResourceKind.FILE, "consumer.py"),
        _op(
            AccessMode.READ,
            ResourceKind.CONTRACT,
            "load_x",
            signature="load_x()->X",
            subject_concept_id="X",
        ),
    )


def _register_consumer_broker(plane: Plane) -> dict:
    return plane.register_broker_instance(
        instance_id="broker-consumer",
        intent_id="consumer",
        session_id="session-consumer",
        monitor_id="runtime-broker",
        key_id="test",
        root_path="/tmp/claim-plane-runtime-fence",
        repo_identity="b" * 64,
        base_commit=BASE,
        initial_tree_hash="tree-before",
        writer_lease_seconds=300,
        policy={"mode": "governed"},
        binary_digest="c" * 64,
        broker_key=b"broker-key",
    )


def test_premise_invalidation_atomically_fences_live_broker() -> None:
    plane = Plane.open(":memory:")
    assert plane.admit(_producer("load_x()->X")).allowed
    assert plane.admit(_consumer()).allowed
    broker = _register_consumer_broker(plane)
    token = int(broker["fencing_token"])

    pending = plane.prepare_broker_operation(
        operation_id="pending-write",
        instance_id="broker-consumer",
        request_id="request-1",
        operation="write_file",
        mode=AccessMode.WRITE,
        path="consumer.py",
        target_path=None,
        payload={},
        broker_key=b"broker-key",
        fencing_token=token,
        pre_tree_hash="tree-before",
    )
    assert pending["state"] == "pending"

    decision = plane.amend(_producer("load_x(context)->X"), expected_version=1)
    assert decision.allowed

    consumer = next(item for item in plane.intents() if item["intent_id"] == "consumer")
    assert consumer["state"] == "stale"
    assert plane.broker_instance("broker-consumer")["state"] == "fenced"

    operation = plane.broker_operation_for_request("broker-consumer", "request-1")
    assert operation is not None
    assert operation["state"] == "failed"
    assert "premise became stale" in operation["error"]

    fences = plane.runtime_fences("consumer")
    assert len(fences) == 1
    fence = RuntimeFence.from_dict(fences[0])
    assert fence.protocol == RUNTIME_FENCE_PROTOCOL
    assert fence.intent_id == "consumer"
    assert fence.producer_intent_id == "producer"
    assert fence.broker_instance_id == "broker-consumer"
    assert fence.fencing_token == token
    assert fence.dependency_chain == ("producer", "consumer")
    assert fence.metadata["live_writer_revoked"] is True
    assert len(fences[0]["fingerprint"]) == 64

    notice = plane.notices("consumer")[0]
    assert notice["payload_json"]["runtime_writer_fenced"] is True
    assert notice["payload_json"]["runtime_fence_ids"] == [fence.fence_id]

    with pytest.raises(ValueError, match="broker instance broker-consumer is fenced"):
        plane.validate_broker_instance(
            "broker-consumer",
            broker_key=b"broker-key",
            current_tree_hash="tree-before",
        )


def test_stale_intent_records_logical_fence_without_live_writer() -> None:
    plane = Plane.open(":memory:")
    assert plane.admit(_producer("load_x()->X")).allowed
    assert plane.admit(_consumer()).allowed

    decision = plane.amend(_producer("load_x(context)->X"), expected_version=1)
    assert decision.allowed

    fences = plane.runtime_fences("consumer")
    assert len(fences) == 1
    assert fences[0]["broker_instance_id"] is None
    assert fences[0]["fencing_token"] is None
    assert fences[0]["metadata"]["live_writer_revoked"] is False
    assert fences[0]["metadata"]["resume_requires_fresh_authority"] is True


def test_runtime_fences_are_exported_with_audit(tmp_path) -> None:
    plane = Plane.open(":memory:")
    assert plane.admit(_producer("load_x()->X")).allowed
    assert plane.admit(_consumer()).allowed
    assert plane.amend(_producer("load_x(context)->X"), expected_version=1).allowed

    target = tmp_path / "audit.json"
    plane.export_audit(target)
    payload = json.loads(target.read_text(encoding="utf-8"))

    assert payload["runtime_fences"] == plane.runtime_fences()
    assert payload["runtime_fences"][0]["protocol"] == RUNTIME_FENCE_PROTOCOL
