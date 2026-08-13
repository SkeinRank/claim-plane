"""Provider-neutral code intelligence for Claim Plane."""

from claim_plane.code_intelligence.base import (
    CODE_INTELLIGENCE_PROVIDER_PROTOCOL,
    CODE_INTELLIGENCE_SNAPSHOT_PROTOCOL,
    CodeIntelligenceCapability,
    CodeIntelligenceProvider,
    CodeIntelligenceProviderError,
    CodeIntelligenceProviderManifest,
    CodeIntelligenceRequest,
    CodeIntelligenceSnapshot,
    CodeIntelligenceUnsupportedRequest,
)
from claim_plane.code_intelligence.builtin import (
    BUILTIN_PYTHON_PROVIDER_ID,
    BUILTIN_PYTHON_PROVIDER_VERSION,
    BuiltinPythonCodeIntelligenceProvider,
)
from claim_plane.code_intelligence.registry import (
    CodeIntelligenceProviderRegistry,
    analyze_code_intelligence,
    default_code_intelligence_registry,
)

__all__ = [
    "BUILTIN_PYTHON_PROVIDER_ID",
    "BUILTIN_PYTHON_PROVIDER_VERSION",
    "CODE_INTELLIGENCE_PROVIDER_PROTOCOL",
    "CODE_INTELLIGENCE_SNAPSHOT_PROTOCOL",
    "BuiltinPythonCodeIntelligenceProvider",
    "CodeIntelligenceCapability",
    "CodeIntelligenceProvider",
    "CodeIntelligenceProviderError",
    "CodeIntelligenceProviderManifest",
    "CodeIntelligenceProviderRegistry",
    "CodeIntelligenceRequest",
    "CodeIntelligenceSnapshot",
    "CodeIntelligenceUnsupportedRequest",
    "analyze_code_intelligence",
    "default_code_intelligence_registry",
]
