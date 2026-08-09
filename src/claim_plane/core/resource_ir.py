"""Versioned semantic resource normal form for deterministic authority reasoning.

The v2 IR separates a resource's stable semantic identity from mutable source
coordinates such as line ranges and signatures.  It is intentionally language
neutral: language-specific extractors populate structured path and qualified
symbol metadata, while the core produces deterministic identities that later
admission and dependency layers can consume without depending on parser details.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from claim_plane.core.models import (
    AccessMode,
    ChangeIntent,
    IntentOperation,
    ResourceKind,
    ResourceRef,
    ScopeCommitment,
)

SEMANTIC_RESOURCE_IR_PROTOCOL = "claim-plane.semantic-resource-ir.v2"


class ResourceLayer(str, Enum):
    """Semantic authority granularity represented by the normalized resource."""

    FILE = "file"
    REGION = "region"
    SYMBOL = "symbol"
    CONTRACT = "contract"
    RESOURCE = "resource"


def _clean_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value)).strip()
    return text or None


def _normal_path(value: object | None) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    normalized = posixpath.normpath(text)
    return "" if normalized == "." else normalized.rstrip("/")


def _normal_region(value: object | None) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    compact = " ".join(text.split())
    match = re.fullmatch(
        r"lines?\s*:?[\s]*(\d+)\s*(?:-|\.\.)\s*(\d+)", compact, re.IGNORECASE
    )
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        if start <= end:
            return f"lines:{start}-{end}"
    return compact


def _metadata_text(metadata: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _clean_text(metadata.get(key))
        if value is not None:
            return value
    return None


def _resource_path(resource: ResourceRef) -> str | None:
    if resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}:
        return _normal_path(resource.identifier)
    return _normal_path(
        _metadata_text(
            resource.metadata,
            "path",
            "file",
            "source_path",
            "repository_path",
        )
    )


def _qualified_name(resource: ResourceRef) -> str | None:
    structured = _metadata_text(
        resource.metadata,
        "qualified_identifier",
        "qualified_name",
        "symbol",
        "symbol_id",
    )
    if structured is not None:
        return structured
    if resource.kind in {ResourceKind.SYMBOL, ResourceKind.CONTRACT}:
        return _clean_text(resource.concept_id or resource.identifier)
    return None


def _subject_name(resource: ResourceRef) -> str | None:
    return _clean_text(
        resource.subject_concept_id
        or resource.metadata.get("subject_concept_id")
        or resource.metadata.get("subject_qualified_identifier")
        or resource.metadata.get("subject_qualified_name")
    )


def _language(resource: ResourceRef) -> str | None:
    value = _metadata_text(resource.metadata, "language", "language_id")
    return value.casefold() if value is not None else None


def _fallback_key(resource: ResourceRef) -> str:
    value = _clean_text(resource.concept_id or resource.identifier)
    assert value is not None
    return value


def _layer(
    resource: ResourceRef, path: str | None, region: str | None
) -> ResourceLayer:
    if resource.kind in {ResourceKind.FILE, ResourceKind.DOCUMENT}:
        if path is not None and region is not None:
            return ResourceLayer.REGION
        return ResourceLayer.FILE
    if resource.kind is ResourceKind.SYMBOL:
        return ResourceLayer.SYMBOL
    if resource.kind is ResourceKind.CONTRACT:
        return ResourceLayer.CONTRACT
    return ResourceLayer.RESOURCE


def _identity(
    resource: ResourceRef,
    *,
    layer: ResourceLayer,
    path: str | None,
    region: str | None,
    qualified_name: str | None,
    subject_name: str | None,
) -> str:
    if layer is ResourceLayer.FILE:
        return f"file:{path or _fallback_key(resource)}"
    if layer is ResourceLayer.REGION:
        assert region is not None
        return f"region:{path or _fallback_key(resource)}#{region}"
    if layer is ResourceLayer.SYMBOL:
        symbol = qualified_name or _fallback_key(resource)
        return f"symbol:{path}#{symbol}" if path else f"symbol:{symbol}"
    if layer is ResourceLayer.CONTRACT:
        contract = qualified_name or _fallback_key(resource)
        if path:
            return f"contract:{path}#{contract}"
        if subject_name:
            return f"contract:{subject_name}#{_fallback_key(resource)}"
        return f"contract:{contract}"
    coordinate = path or _fallback_key(resource)
    return f"{resource.kind.value}:{coordinate}"


def _parent_identity(
    resource: ResourceRef,
    *,
    layer: ResourceLayer,
    path: str | None,
    qualified_name: str | None,
    subject_name: str | None,
) -> str | None:
    if layer is ResourceLayer.REGION and path:
        return f"file:{path}"
    if layer is ResourceLayer.SYMBOL and path:
        return f"file:{path}"
    if layer is ResourceLayer.CONTRACT:
        explicit_parent = _metadata_text(resource.metadata, "parent_identity")
        if explicit_parent:
            return explicit_parent
        parent_symbol = _metadata_text(
            resource.metadata,
            "subject_qualified_identifier",
            "subject_qualified_name",
        )
        if parent_symbol is None and qualified_name and "." in qualified_name:
            parent_symbol = qualified_name.rsplit(".", 1)[0]
        if parent_symbol is None:
            parent_symbol = subject_name
        if parent_symbol:
            return (
                f"symbol:{path}#{parent_symbol}"
                if path
                else f"symbol:{parent_symbol}"
            )
        if path:
            return f"file:{path}"
    return None


def _stable_id(identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"sr2_{digest}"


@dataclass(frozen=True, slots=True)
class SemanticResource:
    """One deterministic semantic resource coordinate.

    ``identity`` names the semantic object and deliberately excludes mutable
    details such as a function signature or current line range for symbols and
    contracts.  Those details remain evidence fields, so an edit can change a
    signature without making the underlying contract look like a different object.
    """

    layer: ResourceLayer
    kind: ResourceKind
    identity: str
    identifier: str
    path: str | None = None
    region: str | None = None
    language: str | None = None
    qualified_name: str | None = None
    signature: str | None = None
    concept_id: str | None = None
    subject_concept_id: str | None = None
    parent_identity: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SEMANTIC_RESOURCE_IR_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SEMANTIC_RESOURCE_IR_PROTOCOL:
            raise ValueError(
                f"unsupported semantic resource protocol {self.protocol!r}"
            )
        object.__setattr__(self, "layer", ResourceLayer(self.layer))
        object.__setattr__(self, "kind", ResourceKind(self.kind))
        identity = _clean_text(self.identity)
        identifier = _clean_text(self.identifier)
        if identity is None or identifier is None:
            raise ValueError(
                "semantic resource identity and identifier must not be empty"
            )
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "identifier", identifier)
        object.__setattr__(self, "path", _normal_path(self.path))
        object.__setattr__(self, "region", _normal_region(self.region))
        object.__setattr__(self, "language", _clean_text(self.language))
        object.__setattr__(self, "qualified_name", _clean_text(self.qualified_name))
        object.__setattr__(self, "signature", _clean_text(self.signature))
        object.__setattr__(self, "concept_id", _clean_text(self.concept_id))
        object.__setattr__(
            self, "subject_concept_id", _clean_text(self.subject_concept_id)
        )
        object.__setattr__(self, "parent_identity", _clean_text(self.parent_identity))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def stable_id(self) -> str:
        return _stable_id(self.identity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "layer": self.layer.value,
            "kind": self.kind.value,
            "stable_id": self.stable_id,
            "identity": self.identity,
            "identifier": self.identifier,
            "path": self.path,
            "region": self.region,
            "language": self.language,
            "qualified_name": self.qualified_name,
            "signature": self.signature,
            "concept_id": self.concept_id,
            "subject_concept_id": self.subject_concept_id,
            "parent_identity": self.parent_identity,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SemanticResource":
        resource = cls(
            protocol=str(data.get("protocol") or SEMANTIC_RESOURCE_IR_PROTOCOL),
            layer=ResourceLayer(data["layer"]),
            kind=ResourceKind(data["kind"]),
            identity=str(data["identity"]),
            identifier=str(data["identifier"]),
            path=data.get("path"),
            region=data.get("region"),
            language=data.get("language"),
            qualified_name=data.get("qualified_name"),
            signature=data.get("signature"),
            concept_id=data.get("concept_id"),
            subject_concept_id=data.get("subject_concept_id"),
            parent_identity=data.get("parent_identity"),
            metadata=dict(data.get("metadata") or {}),
        )
        supplied = data.get("stable_id")
        if supplied is not None and str(supplied) != resource.stable_id:
            raise ValueError("semantic resource stable_id does not match identity")
        return resource


@dataclass(frozen=True, slots=True)
class ResourceAuthority:
    """An intent operation projected onto one normalized semantic resource."""

    access: AccessMode
    resource: SemanticResource
    commitment: ScopeCommitment = ScopeCommitment.COMMITTED
    required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "access", AccessMode(self.access))
        object.__setattr__(self, "commitment", ScopeCommitment(self.commitment))
        if not isinstance(self.resource, SemanticResource):
            object.__setattr__(
                self,
                "resource",
                SemanticResource.from_dict(self.resource),  # type: ignore[arg-type]
            )

    @property
    def mutating(self) -> bool:
        return self.access.mutating

    def to_dict(self) -> dict[str, Any]:
        return {
            "access": self.access.value,
            "commitment": self.commitment.value,
            "required": self.required,
            "resource": self.resource.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResourceAuthority":
        return cls(
            access=AccessMode(data["access"]),
            commitment=ScopeCommitment(
                data.get("commitment", ScopeCommitment.COMMITTED.value)
            ),
            required=bool(data.get("required", True)),
            resource=SemanticResource.from_dict(data["resource"]),
        )


@dataclass(frozen=True, slots=True)
class SemanticResourceIR:
    """Deterministic intent-level projection used by later concurrency stages."""

    intent_id: str
    task_id: str
    base_revision: str
    authorities: tuple[ResourceAuthority, ...]
    protocol: str = SEMANTIC_RESOURCE_IR_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SEMANTIC_RESOURCE_IR_PROTOCOL:
            raise ValueError(f"unsupported semantic resource IR {self.protocol!r}")
        for field_name in ("intent_id", "task_id", "base_revision"):
            value = _clean_text(getattr(self, field_name))
            if value is None:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)
        authorities = tuple(
            item
            if isinstance(item, ResourceAuthority)
            else ResourceAuthority.from_dict(item)  # type: ignore[arg-type]
            for item in self.authorities
        )
        ordered = tuple(
            sorted(
                authorities,
                key=lambda item: (
                    item.resource.stable_id,
                    item.access.value,
                    item.commitment.value,
                    not item.required,
                ),
            )
        )
        object.__setattr__(self, "authorities", ordered)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "intent_id": self.intent_id,
            "task_id": self.task_id,
            "base_revision": self.base_revision,
            "authorities": [item.to_dict() for item in self.authorities],
        }

    def fingerprint(self) -> str:
        raw = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SemanticResourceIR":
        return cls(
            protocol=str(data.get("protocol") or SEMANTIC_RESOURCE_IR_PROTOCOL),
            intent_id=str(data["intent_id"]),
            task_id=str(data["task_id"]),
            base_revision=str(data["base_revision"]),
            authorities=tuple(
                ResourceAuthority.from_dict(item)
                for item in data.get("authorities") or ()
            ),
        )


def normalize_resource_ref(resource: ResourceRef) -> SemanticResource:
    """Normalize a legacy/public ``ResourceRef`` into the v2 semantic resource IR."""

    if not isinstance(resource, ResourceRef):
        resource = ResourceRef.from_dict(resource)  # type: ignore[arg-type]
    path = _resource_path(resource)
    region = _normal_region(resource.region or resource.metadata.get("region"))
    qualified_name = _qualified_name(resource)
    subject_name = _subject_name(resource)
    if (
        resource.kind is ResourceKind.CONTRACT
        and subject_name
        and _metadata_text(
            resource.metadata,
            "qualified_identifier",
            "qualified_name",
            "symbol",
            "symbol_id",
        )
        is None
    ):
        qualified_name = f"{subject_name}.{resource.identifier}"
    layer = _layer(resource, path, region)
    identity = _identity(
        resource,
        layer=layer,
        path=path,
        region=region,
        qualified_name=qualified_name,
        subject_name=subject_name,
    )
    return SemanticResource(
        layer=layer,
        kind=resource.kind,
        identity=identity,
        identifier=resource.identifier,
        path=path,
        region=region,
        language=_language(resource),
        qualified_name=qualified_name,
        signature=resource.signature,
        concept_id=resource.concept_id,
        subject_concept_id=subject_name,
        parent_identity=_parent_identity(
            resource,
            layer=layer,
            path=path,
            qualified_name=qualified_name,
            subject_name=subject_name,
        ),
        metadata=resource.metadata,
    )


def normalize_intent_operation(operation: IntentOperation) -> ResourceAuthority:
    """Project one intent operation onto a normalized authority record."""

    if not isinstance(operation, IntentOperation):
        operation = IntentOperation.from_dict(operation)  # type: ignore[arg-type]
    return ResourceAuthority(
        access=operation.access,
        resource=normalize_resource_ref(operation.resource),
        commitment=operation.commitment,
        required=operation.required,
    )


def normalize_change_intent(intent: ChangeIntent) -> SemanticResourceIR:
    """Create a deterministic, parser-neutral authority projection for an intent."""

    if not isinstance(intent, ChangeIntent):
        intent = ChangeIntent.from_dict(intent)  # type: ignore[arg-type]
    return SemanticResourceIR(
        intent_id=intent.intent_id,
        task_id=intent.task_id,
        base_revision=intent.base_revision,
        authorities=tuple(normalize_intent_operation(op) for op in intent.operations),
    )
