"""Adapter discovery, protocol negotiation, and reproducible project pins.

The registry keeps adapter loading outside Claim Plane Core. Built-in adapters and
third-party entry points use the same descriptor and handshake path, while project pins
bind the selected adapter, runtime, and protocol versions before authority is granted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from claim_plane.protocol.adapter import (
    AGENT_ADAPTER_PROTOCOL_VERSION,
    AgentAdapter,
)

ADAPTER_REGISTRY_PROTOCOL = "claim-plane.adapter-registry.v1"
ADAPTER_REGISTRY_VERSION = "1.0"
ADAPTER_HANDSHAKE_PROTOCOL = "claim-plane.adapter-handshake.v1"
ADAPTER_PIN_PROTOCOL = "claim-plane.adapter-pin.v1"
ADAPTER_ENTRY_POINT_GROUP = "claim_plane.adapters"
CORE_ADAPTER_PROTOCOL_VERSIONS = (AGENT_ADAPTER_PROTOCOL_VERSION,)

_VERSION_RE = re.compile(
    r"^v?(?P<major>0|[1-9][0-9]*)"
    r"(?:\.(?P<minor>0|[1-9][0-9]*))?"
    r"(?:\.(?P<patch>0|[1-9][0-9]*))?"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)
_SPEC_RE = re.compile(r"^(>=|<=|==|=|>|<|~=)?\s*(.+)$")


def _required_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                dict(payload),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@dataclass(frozen=True, slots=True)
class SemanticVersion:
    """Small dependency-free semantic version used by adapter negotiation."""

    major: int
    minor: int = 0
    patch: int = 0
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = field(default=(), compare=False)

    @classmethod
    def parse(cls, value: str) -> "SemanticVersion":
        text = _required_text(value, field_name="version")
        match = _VERSION_RE.fullmatch(text)
        if match is None:
            raise ValueError(f"invalid semantic version: {value!r}")
        prerelease = tuple(
            part for part in (match.group("prerelease") or "").split(".") if part
        )
        build = tuple(
            part for part in (match.group("build") or "").split(".") if part
        )
        return cls(
            int(match.group("major")),
            int(match.group("minor") or 0),
            int(match.group("patch") or 0),
            prerelease,
            build,
        )

    def _precedence(self) -> tuple[Any, ...]:
        if not self.prerelease:
            prerelease: tuple[Any, ...] = ((1, ""),)
        else:
            parts: list[tuple[int, Any]] = [(0, "")]
            for item in self.prerelease:
                if item.isdigit():
                    parts.append((0, int(item)))
                else:
                    parts.append((1, item))
            prerelease = tuple(parts)
        return self.major, self.minor, self.patch, prerelease

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return self._precedence() < other._precedence()

    def __le__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return self == other or self < other

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return not self <= other

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        return not self < other

    def __str__(self) -> str:
        result = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            result += "-" + ".".join(self.prerelease)
        if self.build:
            result += "+" + ".".join(self.build)
        return result


@dataclass(frozen=True, slots=True)
class _Comparator:
    operator: str
    version: SemanticVersion

    def matches(self, candidate: SemanticVersion) -> bool:
        if self.operator in {"=", "=="}:
            return candidate == self.version
        if self.operator == ">=":
            return candidate >= self.version
        if self.operator == "<=":
            return candidate <= self.version
        if self.operator == ">":
            return candidate > self.version
        if self.operator == "<":
            return candidate < self.version
        raise ValueError(f"unsupported version comparator: {self.operator}")


@dataclass(frozen=True, slots=True)
class VersionRange:
    """Comma-separated semantic version range used by adapter descriptors."""

    raw: str
    comparators: tuple[_Comparator, ...]

    @classmethod
    def parse(cls, value: str) -> "VersionRange":
        raw = _required_text(value, field_name="protocol_range")
        comparators: list[_Comparator] = []
        for part in (item.strip() for item in raw.split(",")):
            if not part:
                continue
            match = _SPEC_RE.fullmatch(part)
            if match is None:
                raise ValueError(f"invalid version range item: {part!r}")
            operator = match.group(1) or "=="
            version_text = match.group(2).strip()
            if version_text.endswith((".*", ".x", ".X")):
                prefix = version_text[:-2]
                components = prefix.split(".")
                if len(components) == 1:
                    lower = SemanticVersion.parse(prefix)
                    upper = SemanticVersion(lower.major + 1, 0, 0)
                elif len(components) == 2:
                    lower = SemanticVersion.parse(prefix)
                    upper = SemanticVersion(lower.major, lower.minor + 1, 0)
                else:
                    raise ValueError(f"invalid wildcard version: {version_text!r}")
                comparators.extend(
                    (_Comparator(">=", lower), _Comparator("<", upper))
                )
                continue
            version = SemanticVersion.parse(version_text)
            if operator == "~=":
                lower = _Comparator(">=", version)
                release_text = version_text.split("-", 1)[0].split("+", 1)[0]
                component_count = len(release_text.lstrip("v").split("."))
                if component_count >= 3:
                    upper_version = SemanticVersion(version.major, version.minor + 1, 0)
                else:
                    upper_version = SemanticVersion(version.major + 1, 0, 0)
                comparators.extend((lower, _Comparator("<", upper_version)))
            else:
                comparators.append(_Comparator(operator, version))
        if not comparators:
            raise ValueError("protocol_range must contain at least one comparator")
        return cls(raw=raw, comparators=tuple(comparators))

    def contains(self, version: str | SemanticVersion) -> bool:
        candidate = (
            version
            if isinstance(version, SemanticVersion)
            else SemanticVersion.parse(version)
        )
        return all(item.matches(candidate) for item in self.comparators)


class AdapterSource(str, Enum):
    BUILTIN = "builtin"
    ENTRY_POINT = "entry_point"
    PROGRAMMATIC = "programmatic"


class HandshakeSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class HandshakeCode(str, Enum):
    COMPATIBLE = "compatible"
    UNKNOWN_ADAPTER = "unknown_adapter"
    LOAD_FAILED = "load_failed"
    INVALID_ADAPTER = "invalid_adapter"
    PROTOCOL_INCOMPATIBLE = "protocol_incompatible"
    MANIFEST_MISMATCH = "manifest_mismatch"
    PIN_ADAPTER_VERSION_MISMATCH = "pin_adapter_version_mismatch"
    PIN_PROTOCOL_VERSION_MISMATCH = "pin_protocol_version_mismatch"
    PIN_RUNTIME_VERSION_MISMATCH = "pin_runtime_version_mismatch"
    PIN_SOURCE_MISMATCH = "pin_source_mismatch"
    PIN_MANIFEST_CHANGED = "pin_manifest_changed"


@dataclass(frozen=True, slots=True)
class AdapterHandshakeFinding:
    code: HandshakeCode
    severity: HandshakeSeverity
    message: str
    expected: str | None = None
    actual: str | None = None
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
            "remediation": self.remediation,
        }


AdapterFactory = Callable[[], AgentAdapter]


@dataclass(frozen=True, slots=True)
class AdapterRegistration:
    name: str
    factory: AdapterFactory
    protocol_range: str
    source: AdapterSource = AdapterSource.PROGRAMMATIC
    distribution: str | None = None
    entry_point: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required_text(self.name, field_name="name"))
        if not callable(self.factory):
            raise TypeError("adapter factory must be callable")
        VersionRange.parse(self.protocol_range)
        object.__setattr__(self, "source", AdapterSource(self.source))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "protocol_range": self.protocol_range,
            "source": self.source.value,
            "distribution": self.distribution,
            "entry_point": self.entry_point,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class AdapterPin:
    adapter: str
    adapter_version: str
    protocol_version: str
    protocol_range: str
    runtime_name: str
    runtime_version: str | None
    source: AdapterSource
    manifest_digest: str
    distribution: str | None = None
    created_at: str = field(default_factory=_utc_now)
    protocol: str = ADAPTER_PIN_PROTOCOL

    def __post_init__(self) -> None:
        _required_text(self.adapter, field_name="adapter")
        SemanticVersion.parse(self.adapter_version)
        SemanticVersion.parse(self.protocol_version)
        VersionRange.parse(self.protocol_range)
        _required_text(self.runtime_name, field_name="runtime_name")
        object.__setattr__(self, "source", AdapterSource(self.source))
        if not re.fullmatch(r"[0-9a-f]{64}", self.manifest_digest):
            raise ValueError("manifest_digest must be a lowercase SHA-256 digest")
        if self.protocol != ADAPTER_PIN_PROTOCOL:
            raise ValueError(f"pin protocol must be {ADAPTER_PIN_PROTOCOL!r}")

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "protocol": self.protocol,
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "adapter_version_normalized": str(
                SemanticVersion.parse(self.adapter_version)
            ),
            "protocol_version": self.protocol_version,
            "protocol_range": self.protocol_range,
            "runtime": {
                "name": self.runtime_name,
                "version": self.runtime_version,
            },
            "source": self.source.value,
            "distribution": self.distribution,
            "manifest_digest": self.manifest_digest,
            "created_at": self.created_at,
        }
        if include_digest:
            payload["digest"] = _canonical_digest(payload)
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdapterPin":
        payload = dict(data)
        digest = payload.pop("digest", None)
        if digest is not None and digest != _canonical_digest(payload):
            raise ValueError("adapter pin digest does not match its contents")
        runtime = payload.get("runtime")
        if not isinstance(runtime, Mapping):
            raise ValueError("adapter pin runtime must be an object")
        return cls(
            protocol=str(payload.get("protocol") or ""),
            adapter=str(payload.get("adapter") or ""),
            adapter_version=str(payload.get("adapter_version") or ""),
            protocol_version=str(payload.get("protocol_version") or ""),
            protocol_range=str(payload.get("protocol_range") or ""),
            runtime_name=str(runtime.get("name") or ""),
            runtime_version=(
                str(runtime["version"]) if runtime.get("version") is not None else None
            ),
            source=AdapterSource(str(payload.get("source") or "")),
            distribution=(
                str(payload["distribution"])
                if payload.get("distribution") is not None
                else None
            ),
            manifest_digest=str(payload.get("manifest_digest") or ""),
            created_at=str(payload.get("created_at") or ""),
        )


@dataclass(frozen=True, slots=True)
class AdapterHandshake:
    adapter: str
    adapter_version: str
    protocol_range: str
    negotiated_protocol_version: str | None
    runtime_name: str
    runtime_version: str | None
    runtime_detected: bool
    manifest_digest: str
    capabilities: Mapping[str, str]
    guarantees: Mapping[str, str]
    source: AdapterSource
    distribution: str | None
    pin: AdapterPin | None
    findings: tuple[AdapterHandshakeFinding, ...]
    protocol: str = ADAPTER_HANDSHAKE_PROTOCOL
    handshake_version: str = ADAPTER_REGISTRY_VERSION

    @property
    def compatible(self) -> bool:
        return self.negotiated_protocol_version is not None and not any(
            item.severity is HandshakeSeverity.ERROR for item in self.findings
        )

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "protocol": self.protocol,
            "handshake_version": self.handshake_version,
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "adapter_version_normalized": str(
                SemanticVersion.parse(self.adapter_version)
            ),
            "adapter_protocol_range": self.protocol_range,
            "core_protocol_versions": list(CORE_ADAPTER_PROTOCOL_VERSIONS),
            "negotiated_protocol_version": self.negotiated_protocol_version,
            "runtime": {
                "name": self.runtime_name,
                "version": self.runtime_version,
                "detected": self.runtime_detected,
            },
            "manifest_digest": self.manifest_digest,
            "capabilities": dict(self.capabilities),
            "guarantees": dict(self.guarantees),
            "source": self.source.value,
            "distribution": self.distribution,
            "pin": self.pin.to_dict() if self.pin is not None else None,
            "compatible": self.compatible,
            "findings": [item.to_dict() for item in self.findings],
        }
        if include_digest:
            payload["digest"] = _canonical_digest(payload)
        return payload

    def evidence_summary(self) -> dict[str, Any]:
        """Return the stable version and pin facts suitable for lifecycle evidence."""

        return {
            "protocol": self.protocol,
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "adapter_protocol_range": self.protocol_range,
            "negotiated_protocol_version": self.negotiated_protocol_version,
            "runtime_name": self.runtime_name,
            "runtime_version": self.runtime_version,
            "source": self.source.value,
            "distribution": self.distribution,
            "manifest_digest": self.manifest_digest,
            "pin_digest": (
                self.pin.to_dict().get("digest") if self.pin is not None else None
            ),
            "compatible": self.compatible,
        }

    def require_compatible(self) -> "AdapterHandshake":
        if self.compatible:
            return self
        messages = "; ".join(
            item.message
            for item in self.findings
            if item.severity is HandshakeSeverity.ERROR
        )
        raise AdapterRegistryError(messages or "adapter handshake is incompatible")


class AdapterRegistryError(RuntimeError):
    pass


def adapter_pin_path(project_root: str | Path, adapter: str) -> Path:
    root = Path(project_root).expanduser().resolve()
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", adapter.strip()).strip("-")
    if not safe_name:
        raise ValueError("adapter name does not produce a safe pin path")
    return root / ".claim-plane" / "adapters" / "pins" / f"{safe_name}.json"


def load_adapter_pin(
    project_root: str | Path, adapter: str
) -> AdapterPin | None:
    path = adapter_pin_path(project_root, adapter)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    pin = AdapterPin.from_dict(payload)
    if pin.adapter != adapter:
        raise ValueError(
            f"adapter pin targets {pin.adapter!r}, expected {adapter!r}"
        )
    return pin


def save_adapter_pin(project_root: str | Path, pin: AdapterPin) -> Path:
    path = adapter_pin_path(project_root, pin.adapter)
    _atomic_write_json(path, pin.to_dict())
    return path


def remove_adapter_pin(project_root: str | Path, adapter: str) -> bool:
    path = adapter_pin_path(project_root, adapter)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _entry_points() -> Iterable[Any]:
    discovered = metadata.entry_points()
    if hasattr(discovered, "select"):
        return discovered.select(group=ADAPTER_ENTRY_POINT_GROUP)
    return discovered.get(ADAPTER_ENTRY_POINT_GROUP, ())


def _factory_from_loaded(loaded: Any) -> AdapterFactory:
    if isinstance(loaded, type):
        return loaded
    if callable(loaded) and not isinstance(loaded, AgentAdapter):
        return loaded
    return lambda: loaded


class AdapterRegistry:
    """Discover and negotiate built-in, programmatic, and entry-point adapters."""

    def __init__(self) -> None:
        self._registrations: dict[str, AdapterRegistration] = {}
        self._discovery_findings: list[AdapterHandshakeFinding] = []

    @property
    def discovery_findings(self) -> tuple[AdapterHandshakeFinding, ...]:
        return tuple(self._discovery_findings)

    def register(
        self,
        name: str,
        factory: AdapterFactory,
        *,
        protocol_range: str,
        source: AdapterSource = AdapterSource.PROGRAMMATIC,
        distribution: str | None = None,
        entry_point: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        replace: bool = False,
    ) -> AdapterRegistration:
        registration = AdapterRegistration(
            name=name,
            factory=factory,
            protocol_range=protocol_range,
            source=source,
            distribution=distribution,
            entry_point=entry_point,
            metadata=dict(metadata or {}),
        )
        if registration.name in self._registrations and not replace:
            raise AdapterRegistryError(
                f"adapter {registration.name!r} is already registered"
            )
        self._registrations[registration.name] = registration
        return registration

    def discover_entry_points(self) -> tuple[AdapterRegistration, ...]:
        registered: list[AdapterRegistration] = []
        for entry_point in _entry_points():
            try:
                loaded = entry_point.load()
                factory = _factory_from_loaded(loaded)
                probe = factory()
                name = _required_text(
                    str(getattr(probe, "name", entry_point.name)),
                    field_name="adapter name",
                )
                exact = str(
                    getattr(probe, "protocol_version", AGENT_ADAPTER_PROTOCOL_VERSION)
                )
                protocol_range = str(
                    getattr(probe, "supported_protocol_range", f"=={exact}")
                )
                distribution = None
                dist = getattr(entry_point, "dist", None)
                if dist is not None:
                    distribution = getattr(dist, "name", None)
                registration = self.register(
                    name,
                    factory,
                    protocol_range=protocol_range,
                    source=AdapterSource.ENTRY_POINT,
                    distribution=distribution,
                    entry_point=(
                        f"{entry_point.module}:{entry_point.attr or ''}".rstrip(":")
                    ),
                    replace=False,
                )
                registered.append(registration)
            except Exception as exc:  # noqa: BLE001
                self._discovery_findings.append(
                    AdapterHandshakeFinding(
                        HandshakeCode.LOAD_FAILED,
                        HandshakeSeverity.WARNING,
                        (
                            "Could not load adapter entry point "
                            f"{entry_point.name!r}: {exc}"
                        ),
                        remediation="Repair or remove the third-party adapter package.",
                    )
                )
        return tuple(registered)

    def registrations(self) -> tuple[AdapterRegistration, ...]:
        return tuple(self._registrations[name] for name in sorted(self._registrations))

    def registration(self, name: str) -> AdapterRegistration:
        try:
            return self._registrations[name]
        except KeyError as exc:
            raise AdapterRegistryError(f"unknown adapter: {name}") from exc

    def create(self, name: str) -> AgentAdapter:
        registration = self.registration(name)
        try:
            adapter = registration.factory()
        except Exception as exc:  # noqa: BLE001
            raise AdapterRegistryError(
                f"failed to load adapter {name!r}: {exc}"
            ) from exc
        if not isinstance(adapter, AgentAdapter):
            raise AdapterRegistryError(
                f"registered adapter {name!r} does not implement AgentAdapter"
            )
        if adapter.name != name:
            raise AdapterRegistryError(
                f"registered adapter name {name!r} does not match implementation "
                f"name {adapter.name!r}"
            )
        return adapter

    @staticmethod
    def _negotiate(range_text: str) -> str | None:
        supported = VersionRange.parse(range_text)
        candidates = sorted(
            (SemanticVersion.parse(item), item)
            for item in CORE_ADAPTER_PROTOCOL_VERSIONS
            if supported.contains(item)
        )
        return candidates[-1][1] if candidates else None

    def handshake(
        self,
        name: str,
        *,
        project_root: str | Path = ".",
        enforce_pin: bool = True,
    ) -> AdapterHandshake:
        registration = self.registration(name)
        adapter = self.create(name)
        manifest = adapter.capability_manifest(str(project_root))
        findings: list[AdapterHandshakeFinding] = []
        negotiated = self._negotiate(registration.protocol_range)
        if negotiated is None:
            findings.append(
                AdapterHandshakeFinding(
                    HandshakeCode.PROTOCOL_INCOMPATIBLE,
                    HandshakeSeverity.ERROR,
                    "No Claim Plane protocol version is accepted by this adapter.",
                    expected=registration.protocol_range,
                    actual=", ".join(CORE_ADAPTER_PROTOCOL_VERSIONS),
                    remediation=(
                        "Install an adapter release whose protocol range overlaps the "
                        "installed Claim Plane release."
                    ),
                )
            )
        elif not VersionRange.parse(registration.protocol_range).contains(
            manifest.adapter_protocol_version
        ):
            findings.append(
                AdapterHandshakeFinding(
                    HandshakeCode.MANIFEST_MISMATCH,
                    HandshakeSeverity.ERROR,
                    (
                        "The adapter manifest protocol version is outside its "
                        "registered range."
                    ),
                    expected=registration.protocol_range,
                    actual=manifest.adapter_protocol_version,
                    remediation="Upgrade or repair the adapter package metadata.",
                )
            )
        if manifest.adapter != registration.name:
            findings.append(
                AdapterHandshakeFinding(
                    HandshakeCode.MANIFEST_MISMATCH,
                    HandshakeSeverity.ERROR,
                    "The adapter manifest identity does not match the registry entry.",
                    expected=registration.name,
                    actual=manifest.adapter,
                    remediation=(
                        "Repair the adapter implementation or registry descriptor."
                    ),
                )
            )

        pin = load_adapter_pin(project_root, name) if enforce_pin else None
        if pin is not None:
            if SemanticVersion.parse(pin.adapter_version) != SemanticVersion.parse(
                manifest.adapter_version
            ):
                findings.append(
                    AdapterHandshakeFinding(
                        HandshakeCode.PIN_ADAPTER_VERSION_MISMATCH,
                        HandshakeSeverity.ERROR,
                        "The installed adapter version does not match the project pin.",
                        expected=pin.adapter_version,
                        actual=manifest.adapter_version,
                        remediation=(
                            "Install the pinned adapter version or refresh the pin "
                            "after reviewing the migration."
                        ),
                    )
                )
            if negotiated != pin.protocol_version:
                findings.append(
                    AdapterHandshakeFinding(
                        HandshakeCode.PIN_PROTOCOL_VERSION_MISMATCH,
                        HandshakeSeverity.ERROR,
                        (
                            "The negotiated protocol version does not match the "
                            "project pin."
                        ),
                        expected=pin.protocol_version,
                        actual=negotiated,
                        remediation=(
                            "Use a compatible Claim Plane release or refresh the pin."
                        ),
                    )
                )
            if pin.runtime_version is not None and (
                manifest.runtime.version != pin.runtime_version
            ):
                findings.append(
                    AdapterHandshakeFinding(
                        HandshakeCode.PIN_RUNTIME_VERSION_MISMATCH,
                        HandshakeSeverity.ERROR,
                        "The detected runtime version does not match the project pin.",
                        expected=pin.runtime_version,
                        actual=manifest.runtime.version,
                        remediation=(
                            "Install the pinned runtime version or refresh the pin "
                            "after validating the new runtime."
                        ),
                    )
                )
            if pin.source is not registration.source:
                findings.append(
                    AdapterHandshakeFinding(
                        HandshakeCode.PIN_SOURCE_MISMATCH,
                        HandshakeSeverity.ERROR,
                        "The adapter source does not match the project pin.",
                        expected=pin.source.value,
                        actual=registration.source.value,
                        remediation=(
                            "Restore the pinned provider or create a reviewed new pin."
                        ),
                    )
                )
            if pin.manifest_digest != manifest.digest():
                findings.append(
                    AdapterHandshakeFinding(
                        HandshakeCode.PIN_MANIFEST_CHANGED,
                        HandshakeSeverity.WARNING,
                        (
                            "The effective capability manifest changed since the "
                            "adapter was pinned."
                        ),
                        expected=pin.manifest_digest,
                        actual=manifest.digest(),
                        remediation=(
                            "Run adapter inspection and conformance before "
                            "refreshing the pin."
                        ),
                    )
                )

        if not findings:
            findings.append(
                AdapterHandshakeFinding(
                    HandshakeCode.COMPATIBLE,
                    HandshakeSeverity.INFO,
                    "Adapter, runtime, protocol, and project pin are compatible.",
                )
            )

        return AdapterHandshake(
            adapter=manifest.adapter,
            adapter_version=manifest.adapter_version,
            protocol_range=registration.protocol_range,
            negotiated_protocol_version=negotiated,
            runtime_name=manifest.runtime.name,
            runtime_version=manifest.runtime.version,
            runtime_detected=manifest.runtime.detected,
            manifest_digest=manifest.digest(),
            capabilities={
                key: value.value for key, value in manifest.capabilities.items()
            },
            guarantees={
                key: value.level.value for key, value in manifest.guarantees.items()
            },
            source=registration.source,
            distribution=registration.distribution,
            pin=pin,
            findings=tuple(findings),
        )

    def pin(
        self,
        name: str,
        *,
        project_root: str | Path = ".",
    ) -> tuple[AdapterPin, Path]:
        handshake = self.handshake(
            name, project_root=project_root, enforce_pin=False
        ).require_compatible()
        registration = self.registration(name)
        if handshake.negotiated_protocol_version is None:
            raise AdapterRegistryError("adapter protocol negotiation did not resolve")
        if not handshake.runtime_detected or handshake.runtime_version is None:
            raise AdapterRegistryError(
                "cannot pin an adapter before its runtime and exact runtime version "
                "are detected"
            )
        pin = AdapterPin(
            adapter=handshake.adapter,
            adapter_version=handshake.adapter_version,
            protocol_version=handshake.negotiated_protocol_version,
            protocol_range=handshake.protocol_range,
            runtime_name=handshake.runtime_name,
            runtime_version=handshake.runtime_version,
            source=handshake.source,
            distribution=registration.distribution,
            manifest_digest=handshake.manifest_digest,
        )
        return pin, save_adapter_pin(project_root, pin)

    def list_payload(
        self, *, project_root: str | Path = ".", inspect: bool = False
    ) -> dict[str, Any]:
        adapters: list[dict[str, Any]] = []
        for registration in self.registrations():
            payload = registration.to_dict()
            pin = load_adapter_pin(project_root, registration.name)
            payload["pinned"] = pin is not None
            if pin is not None:
                payload["pin"] = pin.to_dict()
            if inspect:
                try:
                    payload["handshake"] = self.handshake(
                        registration.name, project_root=project_root
                    ).to_dict()
                except Exception as exc:  # noqa: BLE001
                    payload["handshake_error"] = str(exc)
            adapters.append(payload)
        result = {
            "protocol": ADAPTER_REGISTRY_PROTOCOL,
            "registry_version": ADAPTER_REGISTRY_VERSION,
            "entry_point_group": ADAPTER_ENTRY_POINT_GROUP,
            "core_protocol_versions": list(CORE_ADAPTER_PROTOCOL_VERSIONS),
            "adapters": adapters,
            "discovery_findings": [
                item.to_dict() for item in self.discovery_findings
            ],
        }
        result["digest"] = _canonical_digest(result)
        return result
