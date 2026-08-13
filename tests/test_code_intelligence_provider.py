from __future__ import annotations

import json
from pathlib import Path

import pytest

from claim_plane import (
    BUILTIN_PYTHON_PROVIDER_ID,
    CODE_INTELLIGENCE_PROVIDER_PROTOCOL,
    CODE_INTELLIGENCE_SNAPSHOT_PROTOCOL,
    BuiltinPythonCodeIntelligenceProvider,
    CodeIntelligenceCapability,
    CodeIntelligenceProviderError,
    CodeIntelligenceProviderManifest,
    CodeIntelligenceProviderRegistry,
    CodeIntelligenceRequest,
    CodeIntelligenceSnapshot,
    analyze_code_intelligence,
    build_python_dependency_graph,
    default_code_intelligence_registry,
)


def test_builtin_provider_preserves_existing_python_graph_semantics() -> None:
    sources = {
        "src/models.py": "class User:\n    pass\n",
        "src/service.py": (
            "from .models import User\n\n"
            "def load() -> User:\n"
            "    return User()\n"
        ),
    }
    request = CodeIntelligenceRequest(
        language="Python",
        sources=sources,
        revision="abc123",
    )

    snapshot = analyze_code_intelligence(request)
    direct = build_python_dependency_graph(sources)

    assert snapshot.protocol == CODE_INTELLIGENCE_SNAPSHOT_PROTOCOL
    assert snapshot.provider_id == BUILTIN_PYTHON_PROVIDER_ID
    assert snapshot.language == "python"
    assert snapshot.revision == "abc123"
    assert snapshot.graph == direct
    assert snapshot.metadata == {"input_mode": "source_map", "non_executing": True}


def test_snapshot_round_trip_is_provider_and_graph_bound() -> None:
    snapshot = analyze_code_intelligence(
        CodeIntelligenceRequest(
            language="python",
            sources={"app.py": "def run():\n    return 1\n"},
            revision="deadbeef",
        )
    )

    payload = json.loads(json.dumps(snapshot.to_dict()))
    restored = CodeIntelligenceSnapshot.from_dict(payload)

    assert restored == snapshot
    assert restored.fingerprint == snapshot.fingerprint
    payload["revision"] = "changed"
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        CodeIntelligenceSnapshot.from_dict(payload)


def test_repository_request_uses_non_executing_builtin_analysis(tmp_path: Path) -> None:
    marker = tmp_path / "executed.txt"
    (tmp_path / "app.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('bad')\n",
        encoding="utf-8",
    )

    snapshot = analyze_code_intelligence(
        CodeIntelligenceRequest(language="python", repository_root=tmp_path)
    )

    assert snapshot.graph.node("file:app.py") is not None
    assert not marker.exists()


def test_request_paths_are_normalized_and_bound_to_source_map() -> None:
    request = CodeIntelligenceRequest(
        language="PYTHON",
        sources={"./b.py": "B = 1\n", "a.py": "A = 1\n"},
        paths=("./b.py",),
    )
    snapshot = analyze_code_intelligence(request)

    assert request.language == "python"
    assert request.paths == ("b.py",)
    assert set(snapshot.graph.source_digests) == {"b.py"}

    with pytest.raises(ValueError, match="absent from source map"):
        CodeIntelligenceRequest(
            language="python",
            sources={"app.py": "VALUE = 1\n"},
            paths=("missing.py",),
        )


def test_default_registry_declares_stable_builtin_capabilities() -> None:
    registry = default_code_intelligence_registry()
    manifests = registry.manifests()

    assert len(manifests) == 1
    manifest = manifests[0]
    assert manifest.protocol == CODE_INTELLIGENCE_PROVIDER_PROTOCOL
    assert manifest.provider_id == BUILTIN_PYTHON_PROVIDER_ID
    assert manifest.supports(
        language="python",
        capabilities=(
            CodeIntelligenceCapability.DEPENDENCIES,
            CodeIntelligenceCapability.REPOSITORY,
            CodeIntelligenceCapability.NON_EXECUTING,
        ),
    )


def test_registry_selection_is_deterministic_and_can_be_pinned() -> None:
    class Provider:
        def __init__(self, provider_id: str, priority: int) -> None:
            self.manifest = CodeIntelligenceProviderManifest(
                provider_id=provider_id,
                provider_version="1",
                languages=("python",),
                capabilities=(CodeIntelligenceCapability.SOURCE_MAP,),
                priority=priority,
            )

        def analyze(self, request: CodeIntelligenceRequest) -> CodeIntelligenceSnapshot:
            graph = build_python_dependency_graph(request.sources or {})
            return CodeIntelligenceSnapshot(
                provider_id=self.manifest.provider_id,
                provider_version=self.manifest.provider_version,
                language=request.language,
                graph=graph,
            )

    alpha = Provider("alpha", 10)
    beta = Provider("beta", 20)
    registry = CodeIntelligenceProviderRegistry((alpha, beta))

    assert registry.select(language="python").manifest.provider_id == "beta"
    assert registry.select(language="python", provider_id="alpha") is alpha

    with pytest.raises(CodeIntelligenceProviderError, match="already registered"):
        registry.register(alpha)
    with pytest.raises(CodeIntelligenceProviderError, match="unknown"):
        registry.get("missing")


def test_registry_rejects_provider_snapshot_identity_drift() -> None:
    class BadProvider:
        manifest = CodeIntelligenceProviderManifest(
            provider_id="bad",
            provider_version="1",
            languages=("python",),
            capabilities=(
                CodeIntelligenceCapability.SOURCE_MAP,
                CodeIntelligenceCapability.NON_EXECUTING,
            ),
        )

        def analyze(self, request: CodeIntelligenceRequest) -> CodeIntelligenceSnapshot:
            return CodeIntelligenceSnapshot(
                provider_id="other",
                provider_version="1",
                language="python",
                graph=build_python_dependency_graph(request.sources or {}),
            )

    registry = CodeIntelligenceProviderRegistry((BadProvider(),))
    with pytest.raises(CodeIntelligenceProviderError, match="different provider id"):
        registry.analyze(
            CodeIntelligenceRequest(
                language="python",
                sources={"app.py": "VALUE = 1\n"},
            )
        )


def test_builtin_provider_rejects_unsupported_language() -> None:
    registry = CodeIntelligenceProviderRegistry(
        (BuiltinPythonCodeIntelligenceProvider(),)
    )
    with pytest.raises(
        CodeIntelligenceProviderError, match="no code-intelligence provider"
    ):
        registry.analyze(
            CodeIntelligenceRequest(
                language="rust",
                sources={"src/lib.rs": "pub fn run() {}\n"},
            )
        )
