"""Stable provider contract for repository code intelligence.

Providers translate language-specific indexing or static-analysis evidence into the
language-neutral Semantic Dependency Graph consumed by Claim Plane.  The contract is
intentionally small: providers do not receive mutation authority and must not execute
repository source as part of analysis.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from claim_plane.core.dependency_graph import SemanticDependencyGraph

CODE_INTELLIGENCE_PROVIDER_PROTOCOL = "claim-plane.code-intelligence-provider.v1"
CODE_INTELLIGENCE_SNAPSHOT_PROTOCOL = "claim-plane.code-intelligence-snapshot.v1"

_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class CodeIntelligenceCapability(str, Enum):
    """Capabilities a provider can prove through the v1 contract."""

    SYMBOLS = "symbols"
    DEPENDENCIES = "dependencies"
    SOURCE_MAP = "source_map"
    REPOSITORY = "repository"
    NON_EXECUTING = "non_executing"


class CodeIntelligenceProviderError(RuntimeError):
    """Base error for provider registration, selection, or execution."""


class CodeIntelligenceUnsupportedRequest(CodeIntelligenceProviderError):
    """Raised when a provider cannot safely satisfy an analysis request."""


def _normal_language(value: object) -> str:
    language = str(value).strip().casefold()
    if not language:
        raise ValueError("code-intelligence language must not be empty")
    return language


def _normal_repo_path(value: object) -> str:
    text = str(value).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    normalized = posixpath.normpath(text)
    if not normalized or normalized == "." or normalized.startswith("../"):
        raise ValueError(f"invalid repository-relative path {value!r}")
    if normalized.startswith("/"):
        raise ValueError("code-intelligence paths must be repository-relative")
    return normalized


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class CodeIntelligenceProviderManifest:
    """Stable identity and capability declaration for one provider implementation."""

    provider_id: str
    provider_version: str
    languages: tuple[str, ...]
    capabilities: tuple[CodeIntelligenceCapability, ...]
    priority: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = CODE_INTELLIGENCE_PROVIDER_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != CODE_INTELLIGENCE_PROVIDER_PROTOCOL:
            raise ValueError(
                f"unsupported code-intelligence protocol {self.protocol!r}"
            )
        provider_id = self.provider_id.strip().casefold()
        if not _PROVIDER_ID.fullmatch(provider_id):
            raise ValueError(
                f"invalid code-intelligence provider id {self.provider_id!r}"
            )
        version = self.provider_version.strip()
        if not version:
            raise ValueError("code-intelligence provider version must not be empty")
        languages = tuple(sorted({_normal_language(item) for item in self.languages}))
        if not languages:
            raise ValueError("code-intelligence provider must declare a language")
        capabilities = tuple(
            sorted(
                {CodeIntelligenceCapability(item) for item in self.capabilities},
                key=lambda item: item.value,
            )
        )
        if not capabilities:
            raise ValueError("code-intelligence provider must declare a capability")
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "provider_version", version)
        object.__setattr__(self, "languages", languages)
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "priority", int(self.priority))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def supports(
        self,
        *,
        language: str,
        capabilities: tuple[CodeIntelligenceCapability | str, ...] = (),
    ) -> bool:
        required = {CodeIntelligenceCapability(item) for item in capabilities}
        return (
            _normal_language(language) in self.languages
            and required.issubset(set(self.capabilities))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "languages": list(self.languages),
            "capabilities": [item.value for item in self.capabilities],
            "priority": self.priority,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CodeIntelligenceRequest:
    """One source-bound analysis request.

    Exactly one input mode is allowed. ``sources`` supports deterministic in-memory
    analysis and fixtures; ``repository_root`` allows providers that need repository
    state.  ``paths`` always uses repository-relative coordinates.
    """

    language: str
    sources: Mapping[str, str] | None = None
    repository_root: str | Path | None = None
    paths: tuple[str, ...] = ()
    revision: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "language", _normal_language(self.language))
        has_sources = self.sources is not None
        has_repository = self.repository_root is not None
        if has_sources == has_repository:
            raise ValueError(
                "code-intelligence request requires exactly one of sources or "
                "repository_root"
            )

        normalized_paths = tuple(
            sorted({_normal_repo_path(item) for item in self.paths})
        )
        object.__setattr__(self, "paths", normalized_paths)
        revision = None if self.revision is None else str(self.revision).strip() or None
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "metadata", dict(self.metadata))

        if self.sources is not None:
            normalized_sources: dict[str, str] = {}
            for raw_path, source in self.sources.items():
                path = _normal_repo_path(raw_path)
                if path in normalized_sources:
                    raise ValueError(
                        f"duplicate source path after normalization: {path}"
                    )
                normalized_sources[path] = str(source)
            if not normalized_sources:
                raise ValueError("code-intelligence source map must not be empty")
            if normalized_paths:
                missing = sorted(set(normalized_paths) - set(normalized_sources))
                if missing:
                    raise ValueError(
                        "requested paths are absent from source map: "
                        + ", ".join(missing)
                    )
            object.__setattr__(
                self, "sources", dict(sorted(normalized_sources.items()))
            )
        else:
            assert self.repository_root is not None
            root = Path(self.repository_root).expanduser().resolve()
            if not root.exists() or not root.is_dir():
                raise ValueError(f"repository_root is not a directory: {root}")
            object.__setattr__(self, "repository_root", root)

    @property
    def input_mode(self) -> CodeIntelligenceCapability:
        return (
            CodeIntelligenceCapability.SOURCE_MAP
            if self.sources is not None
            else CodeIntelligenceCapability.REPOSITORY
        )


@dataclass(frozen=True, slots=True)
class CodeIntelligenceSnapshot:
    """Provider-bound graph evidence returned to Claim Plane core."""

    provider_id: str
    provider_version: str
    language: str
    graph: SemanticDependencyGraph
    revision: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = CODE_INTELLIGENCE_SNAPSHOT_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != CODE_INTELLIGENCE_SNAPSHOT_PROTOCOL:
            raise ValueError(
                f"unsupported code-intelligence snapshot {self.protocol!r}"
            )
        provider_id = self.provider_id.strip().casefold()
        if not _PROVIDER_ID.fullmatch(provider_id):
            raise ValueError(
                f"invalid code-intelligence provider id {self.provider_id!r}"
            )
        version = self.provider_version.strip()
        if not version:
            raise ValueError("code-intelligence provider version must not be empty")
        graph = (
            self.graph
            if isinstance(self.graph, SemanticDependencyGraph)
            else SemanticDependencyGraph.from_dict(self.graph)
        )
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "provider_version", version)
        object.__setattr__(self, "language", _normal_language(self.language))
        object.__setattr__(self, "graph", graph)
        object.__setattr__(
            self,
            "revision",
            None if self.revision is None else str(self.revision).strip() or None,
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def fingerprint(self) -> str:
        payload = {
            "protocol": self.protocol,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "language": self.language,
            "revision": self.revision,
            "graph_fingerprint": self.graph.fingerprint,
            "metadata": dict(self.metadata),
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "language": self.language,
            "revision": self.revision,
            "fingerprint": self.fingerprint,
            "graph": self.graph.to_dict(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CodeIntelligenceSnapshot":
        snapshot = cls(
            protocol=str(data.get("protocol", CODE_INTELLIGENCE_SNAPSHOT_PROTOCOL)),
            provider_id=str(data["provider_id"]),
            provider_version=str(data["provider_version"]),
            language=str(data["language"]),
            revision=data.get("revision"),
            graph=SemanticDependencyGraph.from_dict(data["graph"]),
            metadata=dict(data.get("metadata") or {}),
        )
        recorded = data.get("fingerprint")
        if recorded is not None and str(recorded) != snapshot.fingerprint:
            raise ValueError("code-intelligence snapshot fingerprint mismatch")
        return snapshot


@runtime_checkable
class CodeIntelligenceProvider(Protocol):
    """Runtime contract implemented by built-in and external intelligence backends."""

    @property
    def manifest(self) -> CodeIntelligenceProviderManifest: ...

    def analyze(self, request: CodeIntelligenceRequest) -> CodeIntelligenceSnapshot: ...
