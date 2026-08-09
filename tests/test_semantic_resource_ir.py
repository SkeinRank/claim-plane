"""Semantic Resource IR v2 normalization and stability guarantees."""

from __future__ import annotations

import pytest

from claim_plane import (
    AccessMode,
    ChangeIntent,
    IntentOperation,
    ResourceKind,
    ResourceLayer,
    ResourceRef,
    ScopeCommitment,
    SemanticResource,
    SemanticResourceIR,
    normalize_change_intent,
    normalize_resource_ref,
)


def test_file_and_region_resources_have_explicit_hierarchy() -> None:
    file_resource = normalize_resource_ref(
        ResourceRef(ResourceKind.FILE, r"./src\\parser.py")
    )
    region_resource = normalize_resource_ref(
        ResourceRef(ResourceKind.FILE, "src/parser.py", region="lines 40 - 60")
    )

    assert file_resource.layer is ResourceLayer.FILE
    assert file_resource.identity == "file:src/parser.py"
    assert file_resource.parent_identity is None
    assert region_resource.layer is ResourceLayer.REGION
    assert region_resource.identity == "region:src/parser.py#lines:40-60"
    assert region_resource.parent_identity == "file:src/parser.py"


def test_symbol_identity_is_stable_across_line_and_signature_changes() -> None:
    before = normalize_resource_ref(
        ResourceRef(
            ResourceKind.SYMBOL,
            "validate",
            signature="validate(value: str)->bool",
            region="lines:10-18",
            metadata={
                "path": "src/parser.py",
                "qualified_identifier": "Parser.validate",
                "language": "Python",
            },
        )
    )
    after = normalize_resource_ref(
        ResourceRef(
            ResourceKind.SYMBOL,
            "validate",
            signature="validate(value: str, strict: bool=False)->bool",
            region="lines:31-43",
            metadata={
                "path": "./src/parser.py",
                "qualified_identifier": "Parser.validate",
                "language": "python",
            },
        )
    )

    assert before.layer is ResourceLayer.SYMBOL
    assert before.identity == "symbol:src/parser.py#Parser.validate"
    assert before.stable_id == after.stable_id
    assert before.region != after.region
    assert before.signature != after.signature
    assert before.parent_identity == "file:src/parser.py"
    assert before.language == "python"


def test_contract_identity_does_not_change_when_signature_evolves() -> None:
    v1 = normalize_resource_ref(
        ResourceRef(
            ResourceKind.CONTRACT,
            "allow",
            signature="allow(request)->Decision",
            subject_concept_id="RateLimiter",
            metadata={
                "path": "src/rate_limit.py",
                "qualified_identifier": "RateLimiter.allow",
                "subject_qualified_identifier": "RateLimiter",
            },
        )
    )
    v2 = normalize_resource_ref(
        ResourceRef(
            ResourceKind.CONTRACT,
            "allow",
            signature="allow(request, context)->Decision",
            subject_concept_id="RateLimiter",
            metadata={
                "path": "src/rate_limit.py",
                "qualified_identifier": "RateLimiter.allow",
                "subject_qualified_identifier": "RateLimiter",
            },
        )
    )

    assert v1.layer is ResourceLayer.CONTRACT
    assert v1.identity == "contract:src/rate_limit.py#RateLimiter.allow"
    assert v1.stable_id == v2.stable_id
    assert v1.parent_identity == "symbol:src/rate_limit.py#RateLimiter"
    assert v1.signature != v2.signature


def test_legacy_semantic_resources_get_deterministic_fallback_identity() -> None:
    symbol = normalize_resource_ref(ResourceRef(ResourceKind.SYMBOL, "Parser.validate"))
    contract = normalize_resource_ref(
        ResourceRef(
            ResourceKind.CONTRACT,
            "allow",
            subject_concept_id="RateLimiter",
            signature="allow(request)->Decision",
        )
    )

    assert symbol.identity == "symbol:Parser.validate"
    assert contract.identity == "contract:RateLimiter#allow"
    assert contract.parent_identity == "symbol:RateLimiter"


def test_intent_projection_is_deterministic_across_operation_order() -> None:
    operations = (
        IntentOperation(
            AccessMode.WRITE,
            ResourceRef(ResourceKind.FILE, "src/parser.py", region="lines:1-20"),
        ),
        IntentOperation(
            AccessMode.READ,
            ResourceRef(
                ResourceKind.CONTRACT,
                "parse",
                signature="parse(text)->Result",
                subject_concept_id="Parser",
                metadata={
                    "path": "src/parser.py",
                    "qualified_identifier": "Parser.parse",
                    "subject_qualified_identifier": "Parser",
                },
            ),
            commitment=ScopeCommitment.CONTINGENT,
        ),
    )
    first = ChangeIntent(
        intent_id="intent-a",
        task_id="task-a",
        owner="agent",
        base_revision="main",
        operations=operations,
    )
    second = ChangeIntent(
        intent_id="intent-a",
        task_id="task-a",
        owner="agent",
        base_revision="main",
        operations=tuple(reversed(operations)),
    )

    left = normalize_change_intent(first)
    right = normalize_change_intent(second)

    assert left.to_dict() == right.to_dict()
    assert left.fingerprint() == right.fingerprint()
    assert [item.resource.layer for item in left.authorities] == [
        ResourceLayer.CONTRACT,
        ResourceLayer.REGION,
    ]


def test_semantic_resource_round_trip_checks_stable_id() -> None:
    original = normalize_resource_ref(
        ResourceRef(
            ResourceKind.SYMBOL,
            "parse",
            metadata={
                "path": "src/parser.py",
                "qualified_identifier": "Parser.parse",
            },
        )
    )
    restored = SemanticResource.from_dict(original.to_dict())

    assert restored == original
    corrupted = original.to_dict()
    corrupted["stable_id"] = "sr2_deadbeef"
    with pytest.raises(ValueError, match="stable_id"):
        SemanticResource.from_dict(corrupted)


def test_semantic_resource_ir_round_trip() -> None:
    intent = ChangeIntent(
        intent_id="intent",
        task_id="task",
        owner="agent",
        base_revision="main",
        operations=(
            IntentOperation(
                AccessMode.WRITE,
                ResourceRef(
                    ResourceKind.SYMBOL,
                    "parse",
                    metadata={
                        "path": "src/parser.py",
                        "qualified_identifier": "Parser.parse",
                    },
                ),
            ),
        ),
    )
    ir = normalize_change_intent(intent)

    assert SemanticResourceIR.from_dict(ir.to_dict()) == ir
