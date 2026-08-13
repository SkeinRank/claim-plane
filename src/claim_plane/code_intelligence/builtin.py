"""Built-in Python code-intelligence provider."""

from __future__ import annotations

from claim_plane.code_intelligence.base import (
    CodeIntelligenceCapability,
    CodeIntelligenceProviderManifest,
    CodeIntelligenceRequest,
    CodeIntelligenceSnapshot,
    CodeIntelligenceUnsupportedRequest,
)
from claim_plane.core.python_dependency import (
    build_python_dependency_graph,
    build_python_repository_dependency_graph,
)

BUILTIN_PYTHON_PROVIDER_ID = "builtin-python"
BUILTIN_PYTHON_PROVIDER_VERSION = "1.0.0"


class BuiltinPythonCodeIntelligenceProvider:
    """Adapter around Claim Plane's non-executing Python structural analysis."""

    manifest = CodeIntelligenceProviderManifest(
        provider_id=BUILTIN_PYTHON_PROVIDER_ID,
        provider_version=BUILTIN_PYTHON_PROVIDER_VERSION,
        languages=("python",),
        capabilities=(
            CodeIntelligenceCapability.SYMBOLS,
            CodeIntelligenceCapability.DEPENDENCIES,
            CodeIntelligenceCapability.SOURCE_MAP,
            CodeIntelligenceCapability.REPOSITORY,
            CodeIntelligenceCapability.NON_EXECUTING,
        ),
        priority=0,
        metadata={"implementation": "claim-plane-python-ast"},
    )

    def analyze(self, request: CodeIntelligenceRequest) -> CodeIntelligenceSnapshot:
        if not self.manifest.supports(
            language=request.language,
            capabilities=(request.input_mode,),
        ):
            raise CodeIntelligenceUnsupportedRequest(
                f"{self.manifest.provider_id} does not support "
                f"{request.language}/{request.input_mode.value}"
            )

        if request.sources is not None:
            sources = request.sources
            if request.paths:
                sources = {path: sources[path] for path in request.paths}
            graph = build_python_dependency_graph(sources)
        else:
            assert request.repository_root is not None
            graph = build_python_repository_dependency_graph(
                request.repository_root,
                paths=request.paths or None,
            )

        return CodeIntelligenceSnapshot(
            provider_id=self.manifest.provider_id,
            provider_version=self.manifest.provider_version,
            language=request.language,
            revision=request.revision,
            graph=graph,
            metadata={
                "input_mode": request.input_mode.value,
                "non_executing": True,
            },
        )
