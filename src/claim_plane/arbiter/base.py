"""Legacy claim arbiter backed by atomic registry transactions."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from claim_plane.core.models import Claim, Verdict
from claim_plane.core.store import ClaimStore


@runtime_checkable
class Arbiter(Protocol):
    def arbitrate(self, claim: Claim) -> Verdict: ...


class ExactMatchArbiter:
    """Reference normalized-name arbiter."""

    def __init__(self, registry: ClaimStore) -> None:
        self._registry = registry

    def arbitrate(self, claim: Claim) -> Verdict:
        return self._registry.arbitrate_claim(claim)
