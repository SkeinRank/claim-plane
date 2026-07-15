"""Core tests for Claim Plane: arbitration rules, registry, extraction, merge."""

from __future__ import annotations


from claim_plane import Claim, ClaimType, Plane, VerdictKind
from claim_plane.core.extract import extract_artifacts, artifacts_to_claims


# --- arbitration ---------------------------------------------------------- #


def test_first_claim_granted():
    plane = Plane.open(":memory:")
    v = plane.claim(Claim(ClaimType.NAME, "RateLimiter", owner="a"))
    assert v.kind is VerdictKind.GRANTED
    assert v.granted


def test_second_owner_conflicts():
    plane = Plane.open(":memory:")
    plane.claim(Claim(ClaimType.NAME, "RateLimiter", owner="a"))
    v = plane.claim(Claim(ClaimType.NAME, "RateLimiter", owner="b"))
    assert v.kind is VerdictKind.CONFLICT
    assert v.incumbent == "a"


def test_normalized_collision():
    """rate_limiter, RateLimiter, ratelimiter all collide."""
    plane = Plane.open(":memory:")
    plane.claim(Claim(ClaimType.NAME, "RateLimiter", owner="a"))
    for spelling in ("rate_limiter", "ratelimiter", "Rate_Limiter"):
        v = plane.claim(Claim(ClaimType.NAME, spelling, owner="b"))
        assert v.kind is VerdictKind.CONFLICT, spelling


def test_same_owner_idempotent():
    plane = Plane.open(":memory:")
    plane.claim(Claim(ClaimType.NAME, "Foo", owner="a"))
    v = plane.claim(Claim(ClaimType.NAME, "Foo", owner="a"))
    assert v.granted


def test_contract_mismatch():
    plane = Plane.open(":memory:")
    plane.claim(
        Claim(ClaimType.CONTRACT, "validate", owner="a", signature="validate(token)")
    )
    v = plane.claim(
        Claim(
            ClaimType.CONTRACT,
            "validate",
            owner="b",
            signature="validate(token, scope)",
        )
    )
    assert v.kind is VerdictKind.CONTRACT_MISMATCH
    assert v.incumbent == "a"


def test_matching_contract_conflicts_not_mismatch():
    """Same signature, different owner -> plain conflict (reuse), not mismatch."""
    plane = Plane.open(":memory:")
    plane.claim(
        Claim(ClaimType.CONTRACT, "validate", owner="a", signature="validate(token)")
    )
    v = plane.claim(
        Claim(ClaimType.CONTRACT, "validate", owner="b", signature="validate(token)")
    )
    assert v.kind is VerdictKind.CONFLICT


def test_different_types_do_not_collide():
    plane = Plane.open(":memory:")
    plane.claim(Claim(ClaimType.NAME, "export", owner="a"))
    v = plane.claim(Claim(ClaimType.SCOPE, "export", owner="b"))
    assert v.granted  # NAME and SCOPE namespaces are independent


# --- registry / audit ----------------------------------------------------- #


def test_audit_log_records_every_decision():
    plane = Plane.open(":memory:")
    plane.claim(Claim(ClaimType.NAME, "A", owner="x"))
    plane.claim(Claim(ClaimType.NAME, "A", owner="y"))  # conflict
    log = plane.audit()
    assert len(log) == 2
    assert log[0]["verdict"] == "granted"
    assert log[1]["verdict"] == "conflict"


def test_release_frees_grants():
    plane = Plane.open(":memory:")
    plane.claim(Claim(ClaimType.NAME, "A", owner="x"))
    assert len(plane.grants()) == 1
    plane.release("x")
    assert len(plane.grants()) == 0
    # now another owner can claim it cleanly
    v = plane.claim(Claim(ClaimType.NAME, "A", owner="y"))
    assert v.granted


# --- extraction ----------------------------------------------------------- #


def test_extract_classes_and_functions():
    code = """
class RateLimiter:
    def check(self, token, scope):
        return True

def helper(x): ...

class _Private: ...
"""
    arts = extract_artifacts(code)
    names = {a.identifier for a in arts if a.kind is ClaimType.NAME}
    assert "RateLimiter" in names
    assert "helper" in names
    assert "check" in names
    assert "_Private" not in names  # private skipped


def test_extract_signature_drops_self():
    code = "class C:\n    def m(self, token, scope): ...\n"
    contracts = [a for a in extract_artifacts(code) if a.kind is ClaimType.CONTRACT]
    sig = next(a.signature for a in contracts if a.identifier == "m")
    assert "self" not in sig
    assert "token" in sig and "scope" in sig


def test_extract_from_fenced_block():
    text = "Here is the code:\n```python\nclass Foo: ...\n```\nDone."
    names = {a.identifier for a in extract_artifacts(text)}
    assert "Foo" in names


# --- merge verification --------------------------------------------------- #


def test_verify_merge_clean():
    plane = Plane.open(":memory:")
    plane.claim(Claim(ClaimType.NAME, "ContextSpace", owner="a"))
    defined = artifacts_to_claims("class ContextSpace: ...", owner="a")
    assert plane.verify_merge(defined) == []


def test_verify_merge_detects_cross_owner_collision():
    plane = Plane.open(":memory:")
    plane.claim(Claim(ClaimType.NAME, "ContextSpace", owner="a"))
    # branch b defines the same name it doesn't own
    defined = artifacts_to_claims("class ContextSpace: ...", owner="b")
    problems = plane.verify_merge(defined)
    assert len(problems) == 1
    assert problems[0].incumbent == "a"
