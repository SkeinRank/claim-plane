"""Optional Agent Lexicon semantic identity bridge.

When semantic mode is explicitly requested, startup is fail-closed: a missing
package, invalid lexicon, or unresolved runtime error is surfaced immediately.
The bridge also binds unscoped contracts to a single mutated concept when that
relationship is unambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from claim_plane.core.models import (
    ChangeIntent,
    ResourceKind,
    ResourceRef,
)


@dataclass(frozen=True, slots=True)
class SemanticResolution:
    surface: str
    concept_id: str | None
    canonical: str | None
    status: str
    candidates: tuple[str, ...] = ()
    deprecated: bool = False


class SemanticIdentityResolver:
    def __init__(
        self, lexicon_path: str | None = None, *, required: bool = False
    ) -> None:
        self.lexicon_path = lexicon_path
        self.required = required
        self._lexicon: Any | None = None
        self._resolve_text: Any | None = None
        self._matcher: Any | None = None
        self._load_error: str | None = None
        if required and not lexicon_path:
            raise ValueError("semantic mode requires --lexicon PATH")
        if lexicon_path:
            self._load(lexicon_path)
        if required and not self.enabled:
            raise RuntimeError(
                f"semantic mode unavailable: {self._load_error or 'lexicon did not load'}"
            )

    def _load(self, lexicon_path: str) -> None:
        try:
            from agent_lexicon.core import (
                get_cached_surface_matcher,
                load_cached_lexicon,
                resolve_text,
            )

            self._lexicon = load_cached_lexicon(Path(lexicon_path))
            self._resolve_text = resolve_text
            self._matcher = get_cached_surface_matcher(self._lexicon)
        except Exception as exc:
            self._load_error = f"{type(exc).__name__}: {exc}"
            self._lexicon = None
            self._resolve_text = None
            self._matcher = None
            if self.required:
                raise RuntimeError(
                    f"semantic mode unavailable: {self._load_error}"
                ) from exc

    @property
    def enabled(self) -> bool:
        return self._lexicon is not None and self._resolve_text is not None

    @property
    def load_error(self) -> str | None:
        return self._load_error

    def resolve(self, surface: str) -> SemanticResolution:
        resolver = self._resolve_text
        lexicon = self._lexicon
        if resolver is None or lexicon is None:
            if self.required:
                raise RuntimeError(
                    f"semantic resolver unavailable: {self._load_error or 'not loaded'}"
                )
            return SemanticResolution(surface, None, None, "disabled")
        try:
            decision = resolver(lexicon, surface, include_near_misses=False)
        except Exception as exc:
            if self.required:
                raise RuntimeError(
                    f"semantic resolution failed for {surface!r}: {exc}"
                ) from exc
            return SemanticResolution(
                surface, None, None, f"error:{type(exc).__name__}"
            )

        status = getattr(getattr(decision, "status", None), "value", None) or str(
            getattr(decision, "status", "unknown")
        )
        candidates = tuple(getattr(decision, "candidates", ()) or ())
        if status == "resolved" and len(candidates) == 1:
            candidate = candidates[0]
            return SemanticResolution(
                surface=surface,
                concept_id=getattr(candidate, "term_id", None),
                canonical=getattr(candidate, "canonical", None),
                status=status,
                candidates=(getattr(candidate, "term_id", ""),),
                deprecated=bool(getattr(candidate, "deprecated", False)),
            )
        return SemanticResolution(
            surface=surface,
            concept_id=None,
            canonical=None,
            status=status,
            candidates=tuple(
                candidate_id
                for candidate_id in (
                    getattr(candidate, "term_id", None) for candidate in candidates
                )
                if candidate_id
            ),
        )

    def scan_text(self, text: str) -> tuple[dict[str, Any], ...]:
        """Return known canonical/alias occurrences for code or documentation."""
        if self._matcher is None:
            if self.required:
                raise RuntimeError("semantic matcher unavailable")
            return ()
        matches = self._matcher.match(text, include_deprecated=True, longest_only=True)
        return tuple(match.to_dict() for match in matches)

    def enrich_resource(self, resource: ResourceRef) -> ResourceRef:
        if resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}:
            return resource
        enriched = resource
        if not resource.concept_id:
            resolution = self.resolve(resource.identifier)
            metadata = dict(resource.metadata)
            metadata.setdefault("semantic_status", resolution.status)
            if resolution.candidates:
                metadata.setdefault("semantic_candidates", list(resolution.candidates))
            if resolution.concept_id:
                metadata.update(
                    {
                        "semantic_status": resolution.status,
                        "canonical": resolution.canonical,
                        "deprecated": resolution.deprecated,
                    }
                )
                enriched = replace(
                    resource, concept_id=resolution.concept_id, metadata=metadata
                )
            else:
                enriched = replace(resource, metadata=metadata)

        subject = enriched.subject_concept_id
        if subject and not subject.startswith("project."):
            subject_resolution = self.resolve(subject)
            if subject_resolution.concept_id:
                enriched = replace(
                    enriched, subject_concept_id=subject_resolution.concept_id
                )
        return enriched

    def enrich_intent(self, intent: ChangeIntent) -> ChangeIntent:
        operations = tuple(
            replace(operation, resource=self.enrich_resource(operation.resource))
            for operation in intent.operations
        )

        metadata = dict(intent.metadata)
        metadata["semantic_resolution"] = {
            "enabled": self.enabled,
            "required": self.required,
            "lexicon_path": self.lexicon_path,
            "load_error": self.load_error,
        }
        return replace(intent, operations=operations, metadata=metadata)
