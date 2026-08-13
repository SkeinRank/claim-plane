"""Translate cached SCIP protobuf artifacts into Claim Plane semantic resources.

The decoder intentionally implements only the stable SCIP wire fields Claim Plane needs
for resource identity, source occurrences, and declared symbol relationships. Unknown
protobuf fields are skipped by wire type so newer SCIP producers can add data without
forcing Claim Plane to vendor generated bindings.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from claim_plane.code_intelligence.scip import ScipIndexArtifact
from claim_plane.core.models import ResourceKind, ResourceRef
from claim_plane.core.resource_ir import SemanticResource, normalize_resource_ref

SCIP_SEMANTIC_RESOURCE_INDEX_PROTOCOL = (
    "claim-plane.scip-semantic-resource-index.v1"
)
SCIP_SYMBOL_RESOURCE_PROTOCOL = "claim-plane.scip-symbol-resource.v1"
SCIP_OCCURRENCE_PROTOCOL = "claim-plane.scip-occurrence.v1"
SCIP_RELATIONSHIP_PROTOCOL = "claim-plane.scip-relationship.v1"

SCIP_ROLE_DEFINITION = 0x1
SCIP_ROLE_IMPORT = 0x2
SCIP_ROLE_WRITE = 0x4
SCIP_ROLE_READ = 0x8
SCIP_ROLE_GENERATED = 0x10
SCIP_ROLE_TEST = 0x20
SCIP_ROLE_FORWARD_DEFINITION = 0x40

_SCIP_KIND_NAMES: Mapping[int, str] = {
    0: "unspecified",
    1: "array",
    2: "assertion",
    3: "associated_type",
    4: "attribute",
    5: "axiom",
    6: "boolean",
    7: "class",
    8: "constant",
    9: "constructor",
    10: "data_family",
    11: "enum",
    12: "enum_member",
    13: "event",
    14: "fact",
    15: "field",
    16: "file",
    17: "function",
    18: "getter",
    19: "grammar",
    20: "instance",
    21: "interface",
    22: "key",
    23: "lang",
    24: "lemma",
    25: "macro",
    26: "method",
    27: "method_receiver",
    28: "message",
    29: "module",
    30: "namespace",
    31: "null",
    32: "number",
    33: "object",
    34: "operator",
    35: "package",
    36: "package_object",
    37: "parameter",
    38: "parameter_label",
    39: "pattern",
    40: "predicate",
    41: "property",
    42: "protocol",
    43: "quasiquoter",
    44: "self_parameter",
    45: "setter",
    46: "signature",
    47: "subscript",
    48: "string",
    49: "struct",
    50: "tactic",
    51: "theorem",
    52: "this_parameter",
    53: "trait",
    54: "type",
    55: "type_alias",
    56: "type_class",
    57: "type_family",
    58: "type_parameter",
    59: "union",
    60: "value",
    61: "variable",
    62: "contract",
    63: "error",
    64: "library",
    65: "modifier",
    66: "abstract_method",
    67: "method_specification",
    68: "protocol_method",
    69: "pure_virtual_method",
    70: "trait_method",
    71: "type_class_method",
    72: "accessor",
    73: "delegate",
    74: "method_alias",
    75: "singleton_class",
    76: "singleton_method",
    77: "static_data_member",
    78: "static_event",
    79: "static_field",
    80: "static_method",
    81: "static_property",
    82: "static_variable",
    84: "extension",
    85: "mixin",
    86: "concept",
}


class ScipSemanticResourceError(RuntimeError):
    """Base error while decoding or normalizing SCIP semantic resources."""


class ScipDecodeError(ScipSemanticResourceError):
    """Raised when ``index.scip`` is malformed or violates required invariants."""


class ScipArtifactMismatch(ScipSemanticResourceError):
    """Raised when a cached SCIP artifact no longer matches its sealed metadata."""


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_varint(data: memoryview, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    start = offset
    while offset < len(data) and shift < 70:
        byte = int(data[offset])
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ScipDecodeError(f"invalid protobuf varint at byte offset {start}")


def _wire_fields(data: memoryview) -> Iterator[tuple[int, int, int | memoryview]]:
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        field_number = key >> 3
        wire_type = key & 0x7
        if field_number <= 0:
            raise ScipDecodeError("protobuf field number must be positive")
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
            yield field_number, wire_type, value
            continue
        if wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ScipDecodeError("truncated protobuf fixed64 field")
            yield field_number, wire_type, data[offset:end]
            offset = end
            continue
        if wire_type == 2:
            size, offset = _read_varint(data, offset)
            end = offset + size
            if end > len(data):
                raise ScipDecodeError("truncated protobuf length-delimited field")
            yield field_number, wire_type, data[offset:end]
            offset = end
            continue
        if wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ScipDecodeError("truncated protobuf fixed32 field")
            yield field_number, wire_type, data[offset:end]
            offset = end
            continue
        raise ScipDecodeError(f"unsupported protobuf wire type {wire_type}")


def _expect_bytes(value: int | memoryview, *, field: str) -> memoryview:
    if not isinstance(value, memoryview):
        raise ScipDecodeError(f"SCIP {field} has an unexpected protobuf wire type")
    return value


def _expect_varint(value: int | memoryview, *, field: str) -> int:
    if not isinstance(value, int):
        raise ScipDecodeError(f"SCIP {field} has an unexpected protobuf wire type")
    return value


def _text(value: int | memoryview, *, field: str) -> str:
    raw = _expect_bytes(value, field=field)
    try:
        return raw.tobytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScipDecodeError(f"SCIP {field} is not valid UTF-8") from exc


def _packed_varints(value: int | memoryview, *, field: str) -> tuple[int, ...]:
    raw = _expect_bytes(value, field=field)
    values: list[int] = []
    offset = 0
    while offset < len(raw):
        item, offset = _read_varint(raw, offset)
        values.append(item)
    return tuple(values)


def _normal_document_path(value: str) -> str:
    text = value.strip().replace("\\", "/")
    normalized = posixpath.normpath(text)
    if (
        not text
        or text.startswith("/")
        or normalized in {"", "."}
        or normalized.startswith("../")
        or normalized != text
    ):
        raise ScipDecodeError(f"invalid SCIP document relative_path {value!r}")
    return normalized


def _role_names(roles: int) -> tuple[str, ...]:
    values = (
        (SCIP_ROLE_DEFINITION, "definition"),
        (SCIP_ROLE_IMPORT, "import"),
        (SCIP_ROLE_WRITE, "write"),
        (SCIP_ROLE_READ, "read"),
        (SCIP_ROLE_GENERATED, "generated"),
        (SCIP_ROLE_TEST, "test"),
        (SCIP_ROLE_FORWARD_DEFINITION, "forward_definition"),
    )
    return tuple(name for bit, name in values if roles & bit)


@dataclass(frozen=True, slots=True)
class ScipSourceRange:
    """Zero-based half-open source range copied from one SCIP occurrence."""

    start_line: int
    start_character: int
    end_line: int
    end_character: int

    def __post_init__(self) -> None:
        values = (
            self.start_line,
            self.start_character,
            self.end_line,
            self.end_character,
        )
        if any(int(item) < 0 for item in values):
            raise ValueError("SCIP source ranges must be non-negative")
        start = (int(self.start_line), int(self.start_character))
        end = (int(self.end_line), int(self.end_character))
        if end < start:
            raise ValueError("SCIP source range end precedes its start")
        object.__setattr__(self, "start_line", start[0])
        object.__setattr__(self, "start_character", start[1])
        object.__setattr__(self, "end_line", end[0])
        object.__setattr__(self, "end_character", end[1])

    def to_dict(self) -> dict[str, int]:
        return {
            "start_line": self.start_line,
            "start_character": self.start_character,
            "end_line": self.end_line,
            "end_character": self.end_character,
        }


@dataclass(frozen=True, slots=True)
class ScipSymbolResource:
    """One SCIP symbol bound to one stable Claim Plane semantic resource."""

    scip_symbol: str
    resource: SemanticResource
    display_name: str | None = None
    scip_kind: int = 0
    external: bool = False
    protocol: str = SCIP_SYMBOL_RESOURCE_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SCIP_SYMBOL_RESOURCE_PROTOCOL:
            raise ValueError(f"unsupported SCIP symbol resource {self.protocol!r}")
        symbol = str(self.scip_symbol).strip()
        if not symbol:
            raise ValueError("SCIP symbol must not be empty")
        resource = (
            self.resource
            if isinstance(self.resource, SemanticResource)
            else SemanticResource.from_dict(self.resource)
        )
        display_name = (
            None
            if self.display_name is None
            else str(self.display_name).strip() or None
        )
        object.__setattr__(self, "scip_symbol", symbol)
        object.__setattr__(self, "resource", resource)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "scip_kind", int(self.scip_kind))
        object.__setattr__(self, "external", bool(self.external))

    @property
    def scip_kind_name(self) -> str:
        return _SCIP_KIND_NAMES.get(self.scip_kind, f"kind_{self.scip_kind}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "scip_symbol": self.scip_symbol,
            "display_name": self.display_name,
            "scip_kind": self.scip_kind,
            "scip_kind_name": self.scip_kind_name,
            "external": self.external,
            "resource": self.resource.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ScipOccurrence:
    """One source occurrence retained as provenance for later graph enrichment."""

    path: str
    scip_symbol: str
    symbol_roles: int
    source_range: ScipSourceRange | None = None
    resource_stable_id: str | None = None
    protocol: str = SCIP_OCCURRENCE_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SCIP_OCCURRENCE_PROTOCOL:
            raise ValueError(f"unsupported SCIP occurrence {self.protocol!r}")
        object.__setattr__(self, "path", _normal_document_path(self.path))
        object.__setattr__(self, "scip_symbol", str(self.scip_symbol).strip())
        object.__setattr__(self, "symbol_roles", int(self.symbol_roles))
        if self.source_range is not None and not isinstance(
            self.source_range, ScipSourceRange
        ):
            object.__setattr__(
                self, "source_range", ScipSourceRange(**dict(self.source_range))
            )
        if self.resource_stable_id is not None:
            object.__setattr__(
                self,
                "resource_stable_id",
                str(self.resource_stable_id).strip() or None,
            )

    @property
    def role_names(self) -> tuple[str, ...]:
        return _role_names(self.symbol_roles)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "path": self.path,
            "scip_symbol": self.scip_symbol,
            "symbol_roles": self.symbol_roles,
            "role_names": list(self.role_names),
            "source_range": (
                None if self.source_range is None else self.source_range.to_dict()
            ),
            "resource_stable_id": self.resource_stable_id,
        }


@dataclass(frozen=True, slots=True)
class ScipRelationship:
    """A raw SCIP symbol relationship retained without creating graph edges yet."""

    source_symbol: str
    target_symbol: str
    is_reference: bool = False
    is_implementation: bool = False
    is_type_definition: bool = False
    is_definition: bool = False
    source_resource_stable_id: str | None = None
    target_resource_stable_id: str | None = None
    protocol: str = SCIP_RELATIONSHIP_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SCIP_RELATIONSHIP_PROTOCOL:
            raise ValueError(f"unsupported SCIP relationship {self.protocol!r}")
        source = str(self.source_symbol).strip()
        target = str(self.target_symbol).strip()
        if not source or not target:
            raise ValueError("SCIP relationship symbols must not be empty")
        object.__setattr__(self, "source_symbol", source)
        object.__setattr__(self, "target_symbol", target)
        for field_name in (
            "is_reference",
            "is_implementation",
            "is_type_definition",
            "is_definition",
        ):
            object.__setattr__(self, field_name, bool(getattr(self, field_name)))
        for field_name in (
            "source_resource_stable_id",
            "target_resource_stable_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, str(value).strip() or None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "source_symbol": self.source_symbol,
            "target_symbol": self.target_symbol,
            "is_reference": self.is_reference,
            "is_implementation": self.is_implementation,
            "is_type_definition": self.is_type_definition,
            "is_definition": self.is_definition,
            "source_resource_stable_id": self.source_resource_stable_id,
            "target_resource_stable_id": self.target_resource_stable_id,
        }


@dataclass(frozen=True, slots=True)
class ScipSemanticResourceIndex:
    """Revision-bound semantic resource projection of one cached SCIP artifact."""

    revision: str
    workspace_fingerprint: str
    artifact_sha256: str
    project_name: str
    project_root: str | None
    tool_name: str | None
    tool_version: str | None
    file_resources: tuple[SemanticResource, ...]
    symbols: tuple[ScipSymbolResource, ...]
    occurrences: tuple[ScipOccurrence, ...]
    relationships: tuple[ScipRelationship, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol: str = SCIP_SEMANTIC_RESOURCE_INDEX_PROTOCOL

    def __post_init__(self) -> None:
        if self.protocol != SCIP_SEMANTIC_RESOURCE_INDEX_PROTOCOL:
            raise ValueError(
                f"unsupported SCIP semantic resource index {self.protocol!r}"
            )
        for field_name in (
            "revision",
            "workspace_fingerprint",
            "artifact_sha256",
            "project_name",
        ):
            value = str(getattr(self, field_name)).strip()
            if not value:
                raise ValueError(f"{field_name} must not be empty")
            object.__setattr__(self, field_name, value)
        for field_name in ("project_root", "tool_name", "tool_version"):
            value = getattr(self, field_name)
            object.__setattr__(
                self, field_name, None if value is None else str(value).strip() or None
            )
        files = tuple(
            item
            if isinstance(item, SemanticResource)
            else SemanticResource.from_dict(item)
            for item in self.file_resources
        )
        symbols = tuple(
            item if isinstance(item, ScipSymbolResource) else ScipSymbolResource(**item)
            for item in self.symbols
        )
        occurrences = tuple(
            item if isinstance(item, ScipOccurrence) else ScipOccurrence(**item)
            for item in self.occurrences
        )
        relationships = tuple(
            item
            if isinstance(item, ScipRelationship)
            else ScipRelationship(**item)
            for item in self.relationships
        )
        object.__setattr__(
            self, "file_resources", tuple(sorted(files, key=lambda r: r.identity))
        )
        object.__setattr__(
            self, "symbols", tuple(sorted(symbols, key=lambda r: r.scip_symbol))
        )
        object.__setattr__(
            self,
            "occurrences",
            tuple(
                sorted(
                    occurrences,
                    key=lambda item: (
                        item.path,
                        (
                            -1
                            if item.source_range is None
                            else item.source_range.start_line
                        ),
                        -1
                        if item.source_range is None
                        else item.source_range.start_character,
                        item.scip_symbol,
                        item.symbol_roles,
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "relationships",
            tuple(
                sorted(
                    relationships,
                    key=lambda item: (
                        item.source_symbol,
                        item.target_symbol,
                        item.is_reference,
                        item.is_implementation,
                        item.is_type_definition,
                        item.is_definition,
                    ),
                )
            ),
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def resources(self) -> tuple[SemanticResource, ...]:
        by_identity = {item.identity: item for item in self.file_resources}
        for item in self.symbols:
            by_identity[item.resource.identity] = item.resource
        return tuple(by_identity[key] for key in sorted(by_identity))

    @property
    def fingerprint(self) -> str:
        payload = self.to_dict(include_fingerprint=False)
        return hashlib.sha256(_canonical_json(payload)).hexdigest()

    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "protocol": self.protocol,
            "revision": self.revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "artifact_sha256": self.artifact_sha256,
            "project_name": self.project_name,
            "project_root": self.project_root,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "file_resources": [item.to_dict() for item in self.file_resources],
            "symbols": [item.to_dict() for item in self.symbols],
            "occurrences": [item.to_dict() for item in self.occurrences],
            "relationships": [item.to_dict() for item in self.relationships],
            "metadata": dict(self.metadata),
        }
        if include_fingerprint:
            payload["fingerprint"] = self.fingerprint
        return payload


@dataclass(frozen=True, slots=True)
class _RawRelationship:
    symbol: str
    is_reference: bool
    is_implementation: bool
    is_type_definition: bool
    is_definition: bool


@dataclass(frozen=True, slots=True)
class _RawSymbol:
    symbol: str
    documentation: tuple[str, ...]
    relationships: tuple[_RawRelationship, ...]
    kind: int
    display_name: str | None
    signature_language: str | None
    signature_text: str | None
    enclosing_symbol: str | None


@dataclass(frozen=True, slots=True)
class _RawOccurrence:
    symbol: str
    symbol_roles: int
    source_range: ScipSourceRange | None


@dataclass(frozen=True, slots=True)
class _RawDocument:
    path: str
    language: str
    position_encoding: int
    symbols: tuple[_RawSymbol, ...]
    occurrences: tuple[_RawOccurrence, ...]


@dataclass(frozen=True, slots=True)
class _RawIndex:
    project_root: str | None
    tool_name: str | None
    tool_version: str | None
    tool_arguments: tuple[str, ...]
    documents: tuple[_RawDocument, ...]
    external_symbols: tuple[_RawSymbol, ...]


def _parse_single_line_range(data: memoryview) -> ScipSourceRange:
    line = start = end = 0
    for number, _, value in _wire_fields(data):
        if number == 1:
            line = _expect_varint(value, field="single_line_range.line")
        elif number == 2:
            start = _expect_varint(value, field="single_line_range.start_character")
        elif number == 3:
            end = _expect_varint(value, field="single_line_range.end_character")
    return ScipSourceRange(line, start, line, end)


def _parse_multi_line_range(data: memoryview) -> ScipSourceRange:
    start_line = start_character = end_line = end_character = 0
    for number, _, value in _wire_fields(data):
        if number == 1:
            start_line = _expect_varint(value, field="multi_line_range.start_line")
        elif number == 2:
            start_character = _expect_varint(
                value, field="multi_line_range.start_character"
            )
        elif number == 3:
            end_line = _expect_varint(value, field="multi_line_range.end_line")
        elif number == 4:
            end_character = _expect_varint(
                value, field="multi_line_range.end_character"
            )
    return ScipSourceRange(start_line, start_character, end_line, end_character)


def _legacy_range(values: tuple[int, ...]) -> ScipSourceRange | None:
    if len(values) == 3:
        return ScipSourceRange(values[0], values[1], values[0], values[2])
    if len(values) == 4:
        return ScipSourceRange(values[0], values[1], values[2], values[3])
    if values:
        raise ScipDecodeError("SCIP occurrence range must contain 3 or 4 integers")
    return None


def _parse_occurrence(data: memoryview) -> _RawOccurrence:
    legacy: tuple[int, ...] = ()
    typed: ScipSourceRange | None = None
    symbol = ""
    roles = 0
    for number, wire_type, value in _wire_fields(data):
        if number == 1:
            if wire_type == 2:
                legacy = _packed_varints(value, field="occurrence.range")
            elif wire_type == 0:
                legacy = (*legacy, _expect_varint(value, field="occurrence.range"))
            else:
                raise ScipDecodeError("SCIP occurrence range has invalid wire type")
        elif number == 2:
            symbol = _text(value, field="occurrence.symbol")
        elif number == 3:
            roles = _expect_varint(value, field="occurrence.symbol_roles")
        elif number == 8:
            typed = _parse_single_line_range(
                _expect_bytes(value, field="occurrence.single_line_range")
            )
        elif number == 9:
            typed = _parse_multi_line_range(
                _expect_bytes(value, field="occurrence.multi_line_range")
            )
    return _RawOccurrence(
        symbol=symbol.strip(),
        symbol_roles=roles,
        source_range=typed if typed is not None else _legacy_range(legacy),
    )


def _parse_relationship(data: memoryview) -> _RawRelationship:
    symbol = ""
    values = {2: False, 3: False, 4: False, 5: False}
    for number, _, value in _wire_fields(data):
        if number == 1:
            symbol = _text(value, field="relationship.symbol")
        elif number in values:
            values[number] = bool(
                _expect_varint(value, field=f"relationship.flag_{number}")
            )
    if not symbol.strip():
        raise ScipDecodeError("SCIP relationship is missing its target symbol")
    return _RawRelationship(
        symbol=symbol.strip(),
        is_reference=values[2],
        is_implementation=values[3],
        is_type_definition=values[4],
        is_definition=values[5],
    )


def _parse_signature(data: memoryview) -> tuple[str | None, str | None]:
    language: str | None = None
    text: str | None = None
    for number, _, value in _wire_fields(data):
        if number == 4:
            language = _text(value, field="signature.language").strip() or None
        elif number == 5:
            text = _text(value, field="signature.text").strip() or None
    return language, text


def _parse_symbol(data: memoryview) -> _RawSymbol:
    symbol = ""
    documentation: list[str] = []
    relationships: list[_RawRelationship] = []
    kind = 0
    display_name: str | None = None
    signature_language: str | None = None
    signature_text: str | None = None
    enclosing_symbol: str | None = None
    for number, _, value in _wire_fields(data):
        if number == 1:
            symbol = _text(value, field="symbol_information.symbol")
        elif number == 3:
            documentation.append(_text(value, field="symbol_information.documentation"))
        elif number == 4:
            relationships.append(
                _parse_relationship(
                    _expect_bytes(value, field="symbol_information.relationship")
                )
            )
        elif number == 5:
            kind = _expect_varint(value, field="symbol_information.kind")
        elif number == 6:
            display_name = (
                _text(value, field="symbol_information.display_name").strip() or None
            )
        elif number == 7:
            signature_language, signature_text = _parse_signature(
                _expect_bytes(value, field="symbol_information.signature_documentation")
            )
        elif number == 8:
            enclosing_symbol = (
                _text(value, field="symbol_information.enclosing_symbol").strip()
                or None
            )
    if not symbol.strip():
        raise ScipDecodeError("SCIP SymbolInformation is missing symbol")
    return _RawSymbol(
        symbol=symbol.strip(),
        documentation=tuple(documentation),
        relationships=tuple(relationships),
        kind=kind,
        display_name=display_name,
        signature_language=signature_language,
        signature_text=signature_text,
        enclosing_symbol=enclosing_symbol,
    )


def _parse_document(data: memoryview) -> _RawDocument:
    path = ""
    language = ""
    position_encoding = 0
    symbols: list[_RawSymbol] = []
    occurrences: list[_RawOccurrence] = []
    for number, _, value in _wire_fields(data):
        if number == 1:
            path = _text(value, field="document.relative_path")
        elif number == 2:
            occurrences.append(
                _parse_occurrence(_expect_bytes(value, field="document.occurrence"))
            )
        elif number == 3:
            symbols.append(_parse_symbol(_expect_bytes(value, field="document.symbol")))
        elif number == 4:
            language = _text(value, field="document.language")
        elif number == 6:
            position_encoding = _expect_varint(
                value, field="document.position_encoding"
            )
    if not path.strip():
        raise ScipDecodeError("SCIP Document is missing relative_path")
    return _RawDocument(
        path=_normal_document_path(path),
        language=language.strip().casefold(),
        position_encoding=position_encoding,
        symbols=tuple(symbols),
        occurrences=tuple(occurrences),
    )


def _parse_tool_info(
    data: memoryview,
) -> tuple[str | None, str | None, tuple[str, ...]]:
    name: str | None = None
    version: str | None = None
    arguments: list[str] = []
    for number, _, value in _wire_fields(data):
        if number == 1:
            name = _text(value, field="tool_info.name").strip() or None
        elif number == 2:
            version = _text(value, field="tool_info.version").strip() or None
        elif number == 3:
            arguments.append(_text(value, field="tool_info.arguments"))
    return name, version, tuple(arguments)


def _parse_metadata(
    data: memoryview,
) -> tuple[str | None, str | None, str | None, tuple[str, ...]]:
    project_root: str | None = None
    tool_name: str | None = None
    tool_version: str | None = None
    arguments: tuple[str, ...] = ()
    for number, _, value in _wire_fields(data):
        if number == 2:
            tool_name, tool_version, arguments = _parse_tool_info(
                _expect_bytes(value, field="metadata.tool_info")
            )
        elif number == 3:
            project_root = _text(value, field="metadata.project_root").strip() or None
    return project_root, tool_name, tool_version, arguments


def _parse_index(path: Path) -> _RawIndex:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ScipDecodeError(f"cannot read SCIP index {path}: {exc}") from exc
    if not payload:
        raise ScipDecodeError("SCIP index is empty")
    project_root: str | None = None
    tool_name: str | None = None
    tool_version: str | None = None
    tool_arguments: tuple[str, ...] = ()
    documents: list[_RawDocument] = []
    external_symbols: list[_RawSymbol] = []
    view = memoryview(payload)
    for number, _, value in _wire_fields(view):
        if number == 1:
            project_root, tool_name, tool_version, tool_arguments = _parse_metadata(
                _expect_bytes(value, field="index.metadata")
            )
        elif number == 2:
            documents.append(
                _parse_document(_expect_bytes(value, field="index.document"))
            )
        elif number == 3:
            external_symbols.append(
                _parse_symbol(_expect_bytes(value, field="index.external_symbol"))
            )
    if not documents:
        raise ScipDecodeError("SCIP index contains no documents")
    paths = [item.path for item in documents]
    if len(paths) != len(set(paths)):
        raise ScipDecodeError("SCIP index contains duplicate document paths")
    return _RawIndex(
        project_root=project_root,
        tool_name=tool_name,
        tool_version=tool_version,
        tool_arguments=tool_arguments,
        documents=tuple(documents),
        external_symbols=tuple(external_symbols),
    )


def _read_component(text: str, start: int) -> tuple[str, int]:
    output: list[str] = []
    offset = start
    while offset < len(text):
        char = text[offset]
        if char != " ":
            output.append(char)
            offset += 1
            continue
        if offset + 1 < len(text) and text[offset + 1] == " ":
            output.append(" ")
            offset += 2
            continue
        return "".join(output), offset + 1
    raise ScipDecodeError(f"invalid global SCIP symbol {text!r}")


@dataclass(frozen=True, slots=True)
class _SymbolParts:
    local: bool
    local_id: str | None = None
    scheme: str | None = None
    manager: str | None = None
    package_name: str | None = None
    version: str | None = None
    descriptors: str | None = None


def _symbol_parts(symbol: str) -> _SymbolParts:
    if symbol.startswith("local "):
        local_id = symbol[6:].strip()
        if not local_id or " " in local_id:
            raise ScipDecodeError(f"invalid local SCIP symbol {symbol!r}")
        return _SymbolParts(local=True, local_id=local_id)
    values: list[str] = []
    offset = 0
    for _ in range(4):
        value, offset = _read_component(symbol, offset)
        if not value:
            raise ScipDecodeError(f"invalid global SCIP symbol {symbol!r}")
        values.append(value)
    descriptors = symbol[offset:]
    if not descriptors:
        raise ScipDecodeError(f"global SCIP symbol has no descriptors: {symbol!r}")
    return _SymbolParts(
        local=False,
        scheme=values[0],
        manager=values[1],
        package_name=values[2],
        version=values[3],
        descriptors=descriptors,
    )


def _fallback_display_name(parts: _SymbolParts) -> str:
    if parts.local:
        return f"local:{parts.local_id}"
    assert parts.descriptors is not None
    descriptors = parts.descriptors
    if descriptors.endswith(")."):
        method = descriptors[:-2]
        open_paren = method.rfind("(")
        if open_paren >= 0:
            method = method[:open_paren]
        for suffix in ("/", "#", ".", "!", ":"):
            if suffix in method:
                method = method.rsplit(suffix, 1)[-1]
        return method.strip("`") or descriptors
    if descriptors.endswith("]") and "[" in descriptors:
        return descriptors.rsplit("[", 1)[-1][:-1].strip("`") or descriptors
    if descriptors.endswith(")") and "(" in descriptors:
        return descriptors.rsplit("(", 1)[-1][:-1].strip("`") or descriptors
    value = descriptors[:-1] if descriptors[-1:] in "/#.!:" else descriptors
    for suffix in ("/", "#", ".", "!", ":"):
        if suffix in value:
            value = value.rsplit(suffix, 1)[-1]
    return value.strip("`") or descriptors


def _qualified_name(parts: _SymbolParts, *, external: bool, path: str | None) -> str:
    if parts.local:
        if path is None:
            raise ScipDecodeError("local SCIP symbols require a document path")
        return f"local:{path}:{parts.local_id}"
    assert parts.descriptors is not None
    if not external:
        # Local project symbols deliberately exclude package version. The SCIP
        # indexer uses the Git revision as project version, which is provenance,
        # not semantic identity.
        return parts.descriptors
    assert parts.scheme is not None
    assert parts.manager is not None
    assert parts.package_name is not None
    return (
        f"external:{parts.scheme}:{parts.manager}:{parts.package_name}:"
        f"{parts.descriptors}"
    )


def _definition_range(
    occurrences: Iterable[_RawOccurrence], symbol: str
) -> ScipSourceRange | None:
    candidates = [
        item.source_range
        for item in occurrences
        if item.symbol == symbol
        and item.symbol_roles & SCIP_ROLE_DEFINITION
        and item.source_range is not None
    ]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (
            item.start_line,
            item.start_character,
            item.end_line,
            item.end_character,
        ),
    )[0]


def _symbol_resource(
    raw: _RawSymbol,
    *,
    path: str | None,
    language: str | None,
    external: bool,
    definition_range: ScipSourceRange | None,
    revision: str,
    workspace_fingerprint: str,
) -> ScipSymbolResource | None:
    parts = _symbol_parts(raw.symbol)
    if parts.local:
        return None
    qualified_name = _qualified_name(parts, external=external, path=path)
    display_name = raw.display_name or _fallback_display_name(parts)
    effective_language = (
        (language or raw.signature_language or "").strip().casefold() or None
    )
    metadata: dict[str, Any] = {
        "code_intelligence_provider": "scip",
        "scip_symbol": raw.symbol,
        "scip_kind": _SCIP_KIND_NAMES.get(raw.kind, f"kind_{raw.kind}"),
        "scip_kind_value": raw.kind,
        "scip_external": external,
        "scip_revision": revision,
        "scip_workspace_fingerprint": workspace_fingerprint,
        "scip_documentation_count": len(raw.documentation),
    }
    if parts.scheme is not None:
        metadata["scip_scheme"] = parts.scheme
    if parts.manager is not None:
        metadata["scip_package_manager"] = parts.manager
    if parts.package_name is not None:
        metadata["scip_package_name"] = parts.package_name
    if parts.version is not None:
        metadata["scip_package_version"] = parts.version
    if definition_range is not None:
        metadata["scip_definition_range"] = definition_range.to_dict()
    if raw.enclosing_symbol:
        metadata["scip_enclosing_symbol"] = raw.enclosing_symbol
    if path is not None:
        metadata["path"] = path
    if effective_language is not None:
        metadata["language"] = effective_language
    metadata["qualified_name"] = qualified_name
    resource = normalize_resource_ref(
        ResourceRef(
            kind=ResourceKind.SYMBOL,
            identifier=display_name,
            signature=raw.signature_text,
            concept_id=qualified_name,
            metadata=metadata,
        )
    )
    return ScipSymbolResource(
        scip_symbol=raw.symbol,
        resource=resource,
        display_name=display_name,
        scip_kind=raw.kind,
        external=external,
    )


def build_scip_semantic_resource_index(
    artifact: ScipIndexArtifact,
    *,
    include_external_symbols: bool = True,
) -> ScipSemanticResourceIndex:
    """Decode one sealed SCIP artifact into stable Claim Plane semantic resources.

    This stage deliberately does not create dependency edges. Occurrences and SCIP
    relationships are retained as revision-bound evidence for the next graph-enrichment
    stage.
    """

    if not isinstance(artifact, ScipIndexArtifact):
        raise TypeError("artifact must be a ScipIndexArtifact")
    path = Path(artifact.index_path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ScipArtifactMismatch(f"SCIP artifact is unavailable: {path}") from exc
    digest = _sha256_path(path)
    if digest != artifact.sha256 or size != artifact.size_bytes:
        raise ScipArtifactMismatch(
            "SCIP artifact content no longer matches its sealed cache metadata"
        )

    raw = _parse_index(path)
    file_resources: list[SemanticResource] = []
    symbols: list[ScipSymbolResource] = []
    by_symbol: dict[str, ScipSymbolResource] = {}

    for document in raw.documents:
        file_resource = normalize_resource_ref(
            ResourceRef(
                kind=ResourceKind.FILE,
                identifier=document.path,
                metadata={
                    "language": document.language or None,
                    "code_intelligence_provider": "scip",
                    "scip_revision": artifact.revision,
                    "scip_workspace_fingerprint": artifact.workspace_fingerprint,
                    "scip_position_encoding": document.position_encoding,
                },
            )
        )
        file_resources.append(file_resource)
        for item in document.symbols:
            resource = _symbol_resource(
                item,
                path=document.path,
                language=document.language,
                external=False,
                definition_range=_definition_range(document.occurrences, item.symbol),
                revision=artifact.revision,
                workspace_fingerprint=artifact.workspace_fingerprint,
            )
            if resource is None:
                continue
            previous = by_symbol.get(resource.scip_symbol)
            if (
                previous is not None
                and previous.resource.identity != resource.resource.identity
            ):
                raise ScipDecodeError(
                    "SCIP symbol is defined with conflicting semantic identities: "
                    f"{resource.scip_symbol}"
                )
            by_symbol[resource.scip_symbol] = resource

    if include_external_symbols:
        for item in raw.external_symbols:
            resource = _symbol_resource(
                item,
                path=None,
                language=item.signature_language,
                external=True,
                definition_range=None,
                revision=artifact.revision,
                workspace_fingerprint=artifact.workspace_fingerprint,
            )
            if resource is not None and resource.scip_symbol not in by_symbol:
                by_symbol[resource.scip_symbol] = resource

    # Occurrence-only global symbols are retained as external references when their
    # definitions are absent from this index. Local symbols stay document-local noise
    # and are intentionally excluded from authority resources.
    for document in raw.documents:
        for occurrence in document.occurrences:
            if not occurrence.symbol or occurrence.symbol in by_symbol:
                continue
            parts = _symbol_parts(occurrence.symbol)
            if parts.local or not include_external_symbols:
                continue
            synthetic = _RawSymbol(
                symbol=occurrence.symbol,
                documentation=(),
                relationships=(),
                kind=0,
                display_name=None,
                signature_language=document.language or None,
                signature_text=None,
                enclosing_symbol=None,
            )
            resource = _symbol_resource(
                synthetic,
                path=None,
                language=document.language,
                external=True,
                definition_range=None,
                revision=artifact.revision,
                workspace_fingerprint=artifact.workspace_fingerprint,
            )
            if resource is not None:
                by_symbol[resource.scip_symbol] = resource

    symbols.extend(by_symbol.values())
    occurrences: list[ScipOccurrence] = []
    relationships: list[ScipRelationship] = []
    for document in raw.documents:
        for item in document.occurrences:
            bound = by_symbol.get(item.symbol)
            occurrences.append(
                ScipOccurrence(
                    path=document.path,
                    scip_symbol=item.symbol,
                    symbol_roles=item.symbol_roles,
                    source_range=item.source_range,
                    resource_stable_id=(
                        None if bound is None else bound.resource.stable_id
                    ),
                )
            )
        for item in document.symbols:
            source = by_symbol.get(item.symbol)
            if source is None:
                continue
            for relation in item.relationships:
                target = by_symbol.get(relation.symbol)
                relationships.append(
                    ScipRelationship(
                        source_symbol=item.symbol,
                        target_symbol=relation.symbol,
                        is_reference=relation.is_reference,
                        is_implementation=relation.is_implementation,
                        is_type_definition=relation.is_type_definition,
                        is_definition=relation.is_definition,
                        source_resource_stable_id=source.resource.stable_id,
                        target_resource_stable_id=(
                            None if target is None else target.resource.stable_id
                        ),
                    )
                )
    if include_external_symbols:
        for item in raw.external_symbols:
            source = by_symbol.get(item.symbol)
            if source is None:
                continue
            for relation in item.relationships:
                target = by_symbol.get(relation.symbol)
                relationships.append(
                    ScipRelationship(
                        source_symbol=item.symbol,
                        target_symbol=relation.symbol,
                        is_reference=relation.is_reference,
                        is_implementation=relation.is_implementation,
                        is_type_definition=relation.is_type_definition,
                        is_definition=relation.is_definition,
                        source_resource_stable_id=source.resource.stable_id,
                        target_resource_stable_id=(
                            None if target is None else target.resource.stable_id
                        ),
                    )
                )

    return ScipSemanticResourceIndex(
        revision=artifact.revision,
        workspace_fingerprint=artifact.workspace_fingerprint,
        artifact_sha256=artifact.sha256,
        project_name=artifact.project_name,
        project_root=raw.project_root,
        tool_name=raw.tool_name,
        tool_version=raw.tool_version,
        file_resources=tuple(file_resources),
        symbols=tuple(symbols),
        occurrences=tuple(occurrences),
        relationships=tuple(relationships),
        metadata={
            "artifact_protocol": artifact.protocol,
            "cache_key": artifact.cache_key,
            "dirty": artifact.dirty,
            "indexer_id": artifact.indexer_id,
            "indexer_version": artifact.indexer_version,
            "tool_arguments": list(raw.tool_arguments),
            "external_symbols_included": include_external_symbols,
        },
    )
