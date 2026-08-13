"""Deterministic code-intelligence provider registration and selection."""

from __future__ import annotations

from collections.abc import Iterable

from claim_plane.code_intelligence.base import (
    CodeIntelligenceCapability,
    CodeIntelligenceProvider,
    CodeIntelligenceProviderError,
    CodeIntelligenceProviderManifest,
    CodeIntelligenceRequest,
    CodeIntelligenceSnapshot,
)
from claim_plane.code_intelligence.builtin import BuiltinPythonCodeIntelligenceProvider


class CodeIntelligenceProviderRegistry:
    """Explicit registry for built-in and future external providers.

    Selection is deterministic: highest declared priority wins, with provider id as
    the stable tie-breaker.  Callers can always pin a provider id to avoid selection.
    """

    def __init__(self, providers: Iterable[CodeIntelligenceProvider] = ()) -> None:
        self._providers: dict[str, CodeIntelligenceProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(
        self,
        provider: CodeIntelligenceProvider,
        *,
        replace: bool = False,
    ) -> None:
        manifest = self._manifest(provider)
        if manifest.provider_id in self._providers and not replace:
            raise CodeIntelligenceProviderError(
                f"code-intelligence provider already registered: {manifest.provider_id}"
            )
        self._providers[manifest.provider_id] = provider

    def get(self, provider_id: str) -> CodeIntelligenceProvider:
        key = provider_id.strip().casefold()
        try:
            return self._providers[key]
        except KeyError as exc:
            raise CodeIntelligenceProviderError(
                f"unknown code-intelligence provider: {provider_id}"
            ) from exc

    def manifests(self) -> tuple[CodeIntelligenceProviderManifest, ...]:
        return tuple(
            sorted(
                (self._manifest(provider) for provider in self._providers.values()),
                key=lambda item: item.provider_id,
            )
        )

    def select(
        self,
        *,
        language: str,
        capabilities: Iterable[CodeIntelligenceCapability | str] = (),
        provider_id: str | None = None,
    ) -> CodeIntelligenceProvider:
        required = tuple(CodeIntelligenceCapability(item) for item in capabilities)
        if provider_id is not None:
            provider = self.get(provider_id)
            manifest = self._manifest(provider)
            if not manifest.supports(language=language, capabilities=required):
                raise CodeIntelligenceProviderError(
                    f"provider {manifest.provider_id} does not support "
                    f"{language} with {[item.value for item in required]}"
                )
            return provider

        candidates = [
            provider
            for provider in self._providers.values()
            if self._manifest(provider).supports(
                language=language,
                capabilities=required,
            )
        ]
        if not candidates:
            raise CodeIntelligenceProviderError(
                f"no code-intelligence provider supports {language} with "
                f"{[item.value for item in required]}"
            )
        return sorted(
            candidates,
            key=lambda provider: (
                -self._manifest(provider).priority,
                self._manifest(provider).provider_id,
            ),
        )[0]

    def analyze(
        self,
        request: CodeIntelligenceRequest,
        *,
        provider_id: str | None = None,
    ) -> CodeIntelligenceSnapshot:
        provider = self.select(
            language=request.language,
            capabilities=(
                request.input_mode,
                CodeIntelligenceCapability.NON_EXECUTING,
            ),
            provider_id=provider_id,
        )
        result = provider.analyze(request)
        manifest = self._manifest(provider)
        if result.provider_id != manifest.provider_id:
            raise CodeIntelligenceProviderError(
                "provider returned a snapshot bound to a different provider id"
            )
        if result.provider_version != manifest.provider_version:
            raise CodeIntelligenceProviderError(
                "provider returned a snapshot bound to a different provider version"
            )
        if result.language != request.language:
            raise CodeIntelligenceProviderError(
                "provider returned a snapshot for a different language"
            )
        return result

    @staticmethod
    def _manifest(
        provider: CodeIntelligenceProvider,
    ) -> CodeIntelligenceProviderManifest:
        manifest = getattr(provider, "manifest", None)
        analyze = getattr(provider, "analyze", None)
        if not isinstance(manifest, CodeIntelligenceProviderManifest) or not callable(
            analyze
        ):
            raise CodeIntelligenceProviderError(
                "provider must expose a CodeIntelligenceProviderManifest and analyze()"
            )
        return manifest


def default_code_intelligence_registry() -> CodeIntelligenceProviderRegistry:
    """Return an isolated registry containing Claim Plane's built-in providers."""

    return CodeIntelligenceProviderRegistry((BuiltinPythonCodeIntelligenceProvider(),))


def analyze_code_intelligence(
    request: CodeIntelligenceRequest,
    *,
    provider_id: str | None = None,
    registry: CodeIntelligenceProviderRegistry | None = None,
) -> CodeIntelligenceSnapshot:
    """Analyze one request through the selected provider."""

    active = registry if registry is not None else default_code_intelligence_registry()
    return active.analyze(request, provider_id=provider_id)
