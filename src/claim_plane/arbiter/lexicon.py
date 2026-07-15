"""Agent Lexicon backed semantic claim arbitration."""

from __future__ import annotations

from claim_plane.core.models import Claim, ClaimType, Verdict
from claim_plane.core.store import ClaimStore
from claim_plane.core.semantic import SemanticIdentityResolver


class LexiconArbiter:
    """Resolve a claim to a canonical concept before the atomic grant decision."""

    def __init__(self, registry: ClaimStore, lexicon_path: str | None = None) -> None:
        self._registry = registry
        self._resolver = SemanticIdentityResolver(lexicon_path)

    @property
    def semantic_enabled(self) -> bool:
        return self._resolver.enabled

    def arbitrate(self, claim: Claim) -> Verdict:
        canonical_key = None
        if claim.claim_type in {ClaimType.NAME, ClaimType.CONTRACT}:
            resolution = self._resolver.resolve(claim.identifier)
            canonical_key = resolution.concept_id
        return self._registry.arbitrate_claim(claim, canonical_key=canonical_key)
