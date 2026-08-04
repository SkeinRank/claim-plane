"""Machine-readable adapter capabilities and effective guarantees.

Capability manifests separate what a runtime can expose from what Claim Plane can
prove.  The manifest is descriptive evidence: it never grants mutation authority.
Policy compatibility is evaluated deterministically before a controlled run starts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

ADAPTER_CAPABILITY_MANIFEST_PROTOCOL = "claim-plane.adapter-capabilities.v1"
ADAPTER_CAPABILITY_MANIFEST_VERSION = "1.0"
ADAPTER_POLICY_COMPATIBILITY_PROTOCOL = "claim-plane.adapter-policy-compatibility.v1"


class CapabilityLevel(str, Enum):
    """Observed implementation level for one adapter capability."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    MANAGED = "managed"
    EXTERNAL = "external"
    UNAVAILABLE = "unavailable"


class EnforcementLevel(str, Enum):
    """How one declared guarantee is enforced or established."""

    HARD_BLOCKED = "HARD_BLOCKED"
    OBSERVED = "OBSERVED"
    POST_VERIFIED = "POST_VERIFIED"
    UNAVAILABLE = "UNAVAILABLE"


class GuaranteeProvider(str, Enum):
    """Component responsible for one effective guarantee."""

    CLAIM_PLANE = "claim_plane"
    ADAPTER = "adapter"
    RUNTIME = "runtime"
    COMPOSITE = "composite"


_ENFORCEMENT_STRENGTH = {
    EnforcementLevel.UNAVAILABLE: 0,
    EnforcementLevel.POST_VERIFIED: 1,
    EnforcementLevel.OBSERVED: 2,
    EnforcementLevel.HARD_BLOCKED: 3,
}


def _required_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _optional_text(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name=field_name)


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """Detected coding-agent runtime identity."""

    name: str
    version: str | None = None
    detected: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required_text(self.name, field_name="name"))
        object.__setattr__(
            self,
            "version",
            _optional_text(self.version, field_name="version"),
        )
        if self.version is not None and not self.detected:
            raise ValueError("runtime version cannot be present when detected is false")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "detected": self.detected,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RuntimeIdentity":
        return cls(
            name=str(data.get("name") or ""),
            version=(str(data["version"]) if data.get("version") is not None else None),
            detected=bool(data.get("detected")),
        )


@dataclass(frozen=True, slots=True)
class GuaranteeDeclaration:
    """One effective guarantee with its owner and supporting capability."""

    level: EnforcementLevel
    provided_by: GuaranteeProvider
    evidence: tuple[str, ...]
    required_capability: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "level", EnforcementLevel(self.level))
        object.__setattr__(self, "provided_by", GuaranteeProvider(self.provided_by))
        normalized_evidence = tuple(
            _required_text(item, field_name="evidence item") for item in self.evidence
        )
        object.__setattr__(self, "evidence", normalized_evidence)
        object.__setattr__(
            self,
            "required_capability",
            _optional_text(
                self.required_capability,
                field_name="required_capability",
            ),
        )
        object.__setattr__(
            self,
            "detail",
            _optional_text(self.detail, field_name="detail"),
        )
        if self.level is EnforcementLevel.UNAVAILABLE and normalized_evidence:
            raise ValueError("unavailable guarantees cannot cite enforcement evidence")
        if self.level is not EnforcementLevel.UNAVAILABLE and not normalized_evidence:
            raise ValueError("available guarantees require enforcement evidence")
        if self.level is EnforcementLevel.HARD_BLOCKED:
            if self.provided_by is GuaranteeProvider.CLAIM_PLANE:
                # Core-owned fail-closed transitions may not require a runtime
                # capability.
                return
            if self.required_capability is None:
                raise ValueError(
                    "hard-blocked adapter/runtime guarantees require a "
                    "capability binding"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "provided_by": self.provided_by.value,
            "evidence": list(self.evidence),
            "required_capability": self.required_capability,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GuaranteeDeclaration":
        raw_evidence = data.get("evidence") or []
        if not isinstance(raw_evidence, Sequence) or isinstance(
            raw_evidence, (str, bytes)
        ):
            raise ValueError("guarantee evidence must be an array")
        return cls(
            level=EnforcementLevel(str(data.get("level") or "")),
            provided_by=GuaranteeProvider(str(data.get("provided_by") or "")),
            evidence=tuple(str(item) for item in raw_evidence),
            required_capability=(
                str(data["required_capability"])
                if data.get("required_capability") is not None
                else None
            ),
            detail=(str(data["detail"]) if data.get("detail") is not None else None),
        )


@dataclass(frozen=True, slots=True)
class AdapterCapabilityManifest:
    """Canonical adapter/runtime capability and guarantee declaration."""

    adapter: str
    adapter_version: str
    adapter_protocol_version: str
    runtime: RuntimeIdentity
    capabilities: Mapping[str, CapabilityLevel]
    guarantees: Mapping[str, GuaranteeDeclaration]
    protocol: str = ADAPTER_CAPABILITY_MANIFEST_PROTOCOL
    manifest_version: str = ADAPTER_CAPABILITY_MANIFEST_VERSION
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.protocol != ADAPTER_CAPABILITY_MANIFEST_PROTOCOL:
            raise ValueError(
                "capability manifest protocol must be "
                f"{ADAPTER_CAPABILITY_MANIFEST_PROTOCOL!r}"
            )
        if self.manifest_version != ADAPTER_CAPABILITY_MANIFEST_VERSION:
            raise ValueError(
                "capability manifest version must be "
                f"{ADAPTER_CAPABILITY_MANIFEST_VERSION!r}"
            )
        object.__setattr__(
            self,
            "adapter",
            _required_text(self.adapter, field_name="adapter"),
        )
        object.__setattr__(
            self,
            "adapter_version",
            _required_text(self.adapter_version, field_name="adapter_version"),
        )
        object.__setattr__(
            self,
            "adapter_protocol_version",
            _required_text(
                self.adapter_protocol_version,
                field_name="adapter_protocol_version",
            ),
        )
        normalized_capabilities = {
            _required_text(str(name), field_name="capability name"): CapabilityLevel(
                level
            )
            for name, level in self.capabilities.items()
        }
        normalized_guarantees = {
            _required_text(str(name), field_name="guarantee name"): declaration
            for name, declaration in self.guarantees.items()
        }
        for name, declaration in normalized_guarantees.items():
            required = declaration.required_capability
            if required is None:
                continue
            actual = normalized_capabilities.get(required, CapabilityLevel.UNAVAILABLE)
            if declaration.level is EnforcementLevel.HARD_BLOCKED and actual not in {
                CapabilityLevel.COMPLETE,
                CapabilityLevel.MANAGED,
            }:
                raise ValueError(
                    f"guarantee {name!r} cannot be HARD_BLOCKED because capability "
                    f"{required!r} is {actual.value!r}"
                )
            if (
                declaration.level is not EnforcementLevel.UNAVAILABLE
                and actual is CapabilityLevel.UNAVAILABLE
            ):
                raise ValueError(
                    f"guarantee {name!r} requires unavailable capability {required!r}"
                )
        object.__setattr__(self, "capabilities", normalized_capabilities)
        object.__setattr__(self, "guarantees", normalized_guarantees)
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "protocol": self.protocol,
            "manifest_version": self.manifest_version,
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "adapter_protocol_version": self.adapter_protocol_version,
            "runtime": self.runtime.to_dict(),
            "capabilities": {
                name: level.value for name, level in sorted(self.capabilities.items())
            },
            "guarantees": {
                name: declaration.to_dict()
                for name, declaration in sorted(self.guarantees.items())
            },
            "metadata": dict(self.metadata),
        }
        if include_digest:
            payload["digest"] = self.digest()
        return payload

    def digest(self) -> str:
        return hashlib.sha256(
            _canonical_json(self.to_dict(include_digest=False)).encode("utf-8")
        ).hexdigest()

    def evidence_summary(self) -> dict[str, Any]:
        """Small immutable projection suitable for lifecycle evidence."""

        return {
            "manifest_protocol": self.protocol,
            "manifest_version": self.manifest_version,
            "manifest_digest": self.digest(),
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "adapter_protocol_version": self.adapter_protocol_version,
            "runtime_name": self.runtime.name,
            "runtime_version": self.runtime.version,
            "runtime_detected": self.runtime.detected,
            "capabilities": {
                name: level.value for name, level in sorted(self.capabilities.items())
            },
            "guarantees": {
                name: {
                    "level": declaration.level.value,
                    "provided_by": declaration.provided_by.value,
                }
                for name, declaration in sorted(self.guarantees.items())
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AdapterCapabilityManifest":
        raw_capabilities = data.get("capabilities") or {}
        raw_guarantees = data.get("guarantees") or {}
        raw_runtime = data.get("runtime") or {}
        if not isinstance(raw_capabilities, Mapping):
            raise ValueError("manifest capabilities must be an object")
        if not isinstance(raw_guarantees, Mapping):
            raise ValueError("manifest guarantees must be an object")
        if not isinstance(raw_runtime, Mapping):
            raise ValueError("manifest runtime must be an object")
        manifest = cls(
            protocol=str(data.get("protocol") or ""),
            manifest_version=str(data.get("manifest_version") or ""),
            adapter=str(data.get("adapter") or ""),
            adapter_version=str(data.get("adapter_version") or ""),
            adapter_protocol_version=str(data.get("adapter_protocol_version") or ""),
            runtime=RuntimeIdentity.from_dict(raw_runtime),
            capabilities={
                str(name): CapabilityLevel(str(level))
                for name, level in raw_capabilities.items()
            },
            guarantees={
                str(name): GuaranteeDeclaration.from_dict(declaration)
                for name, declaration in raw_guarantees.items()
                if isinstance(declaration, Mapping)
            },
            metadata=(
                dict(data["metadata"])
                if isinstance(data.get("metadata"), Mapping)
                else {}
            ),
        )
        supplied_digest = data.get("digest")
        if supplied_digest is not None and str(supplied_digest) != manifest.digest():
            raise ValueError("capability manifest digest does not match content")
        if len(manifest.guarantees) != len(raw_guarantees):
            raise ValueError("every guarantee declaration must be an object")
        return manifest


@dataclass(frozen=True, slots=True)
class AdapterPolicyRequirements:
    """Minimum guarantee levels required by one user-facing policy preset."""

    policy: str
    guarantees: Mapping[str, EnforcementLevel]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy",
            _required_text(self.policy, field_name="policy"),
        )
        object.__setattr__(
            self,
            "guarantees",
            {
                _required_text(
                    str(name), field_name="guarantee name"
                ): EnforcementLevel(level)
                for name, level in self.guarantees.items()
            },
        )

    @classmethod
    def preset(cls, policy: str) -> "AdapterPolicyRequirements":
        normalized = _required_text(policy, field_name="policy").casefold()
        presets: dict[str, dict[str, EnforcementLevel]] = {
            "observe": {
                "completion_verification": EnforcementLevel.POST_VERIFIED,
            },
            "guarded": {
                "undeclared_tool_write": EnforcementLevel.HARD_BLOCKED,
                "completion_verification": EnforcementLevel.POST_VERIFIED,
                "corrupted_session_state": EnforcementLevel.HARD_BLOCKED,
                "stale_intent_version": EnforcementLevel.HARD_BLOCKED,
            },
            "strict": {
                "undeclared_tool_write": EnforcementLevel.HARD_BLOCKED,
                "bypassed_host_write": EnforcementLevel.HARD_BLOCKED,
                "completion_verification": EnforcementLevel.POST_VERIFIED,
                "corrupted_session_state": EnforcementLevel.HARD_BLOCKED,
                "stale_intent_version": EnforcementLevel.HARD_BLOCKED,
                "cancellation_revokes_authority": EnforcementLevel.HARD_BLOCKED,
            },
            "critical": {
                "undeclared_tool_write": EnforcementLevel.HARD_BLOCKED,
                "bypassed_host_write": EnforcementLevel.HARD_BLOCKED,
                "subagent_mutation": EnforcementLevel.HARD_BLOCKED,
                "completion_verification": EnforcementLevel.POST_VERIFIED,
                "corrupted_session_state": EnforcementLevel.HARD_BLOCKED,
                "stale_intent_version": EnforcementLevel.HARD_BLOCKED,
                "cancellation_revokes_authority": EnforcementLevel.HARD_BLOCKED,
            },
        }
        try:
            return cls(normalized, presets[normalized])
        except KeyError as exc:
            raise ValueError(
                "unknown adapter policy; expected observe, guarded, strict, or critical"
            ) from exc


@dataclass(frozen=True, slots=True)
class AdapterCompatibilityFinding:
    guarantee: str
    required: EnforcementLevel
    actual: EnforcementLevel
    provided_by: GuaranteeProvider | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "guarantee": self.guarantee,
            "required": self.required.value,
            "actual": self.actual.value,
            "provided_by": (
                self.provided_by.value if self.provided_by is not None else None
            ),
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class AdapterPolicyCompatibility:
    """Deterministic manifest-to-policy compatibility result."""

    policy: str
    compatible: bool
    manifest_digest: str
    findings: tuple[AdapterCompatibilityFinding, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": ADAPTER_POLICY_COMPATIBILITY_PROTOCOL,
            "policy": self.policy,
            "compatible": self.compatible,
            "manifest_digest": self.manifest_digest,
            "findings": [finding.to_dict() for finding in self.findings],
        }


def evaluate_adapter_policy(
    manifest: AdapterCapabilityManifest,
    requirements: AdapterPolicyRequirements | str,
) -> AdapterPolicyCompatibility:
    """Evaluate required guarantee levels without trusting adapter prose."""

    resolved = (
        AdapterPolicyRequirements.preset(requirements)
        if isinstance(requirements, str)
        else requirements
    )
    findings: list[AdapterCompatibilityFinding] = []
    for name, required in sorted(resolved.guarantees.items()):
        declaration = manifest.guarantees.get(name)
        actual = (
            declaration.level
            if declaration is not None
            else EnforcementLevel.UNAVAILABLE
        )
        if _ENFORCEMENT_STRENGTH[actual] >= _ENFORCEMENT_STRENGTH[required]:
            continue
        provider = declaration.provided_by if declaration is not None else None
        findings.append(
            AdapterCompatibilityFinding(
                guarantee=name,
                required=required,
                actual=actual,
                provided_by=provider,
                message=(
                    f"policy {resolved.policy!r} requires {name} at "
                    f"{required.value}, but adapter provides {actual.value}"
                ),
            )
        )
    return AdapterPolicyCompatibility(
        policy=resolved.policy,
        compatible=not findings,
        manifest_digest=manifest.digest(),
        findings=tuple(findings),
    )


def require_adapter_policy(
    manifest: AdapterCapabilityManifest,
    requirements: AdapterPolicyRequirements | str,
) -> AdapterPolicyCompatibility:
    """Return compatibility or fail before the adapter session starts."""

    result = evaluate_adapter_policy(manifest, requirements)
    if not result.compatible:
        detail = "; ".join(finding.message for finding in result.findings)
        raise ValueError(f"adapter capability policy is unavailable: {detail}")
    return result
