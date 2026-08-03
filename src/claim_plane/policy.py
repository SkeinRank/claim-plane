"""Stable single-agent policy presets and deterministic repository risk classes.

The policy layer translates a user-facing preset into machine-readable runtime
semantics. It does not grant authority: adapter guarantees are checked separately,
and Claim Plane Core remains the only component that admits mutations.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import posixpath
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

POLICY_PROTOCOL = "claim-plane.policy.v1"
RISK_CLASSIFICATION_PROTOCOL = "claim-plane.risk-classification.v1"
POLICY_NAMES = ("observe", "guarded", "strict", "critical")


class RiskLevel(str, Enum):
    """Repository mutation risk, ordered from routine to human-gated."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PolicyAction(str, Enum):
    """Deterministic action selected for a policy decision."""

    ALLOW = "ALLOW"
    REPORT = "REPORT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    DENY = "DENY"


class PreWriteMode(str, Enum):
    """Whether supported pre-write findings block the runtime call."""

    OBSERVE = "observe"
    ENFORCE = "enforce"


_RISK_STRENGTH = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}

_DEFAULT_RISK_RULES: tuple[tuple[str, RiskLevel, str], ...] = (
    (".github/workflows/**", RiskLevel.CRITICAL, "CI workflow authority"),
    ("migrations/**", RiskLevel.CRITICAL, "database migration"),
    ("**/migrations/**", RiskLevel.CRITICAL, "database migration"),
    (".env", RiskLevel.CRITICAL, "secret-bearing environment file"),
    (".env.*", RiskLevel.CRITICAL, "secret-bearing environment file"),
    ("**/*.pem", RiskLevel.CRITICAL, "private key or certificate material"),
    ("**/*.key", RiskLevel.CRITICAL, "private key material"),
    ("CODEOWNERS", RiskLevel.HIGH, "review authority configuration"),
    (".github/CODEOWNERS", RiskLevel.HIGH, "review authority configuration"),
    ("pyproject.toml", RiskLevel.HIGH, "package and build contract"),
    ("package.json", RiskLevel.HIGH, "package and script contract"),
    ("Cargo.toml", RiskLevel.HIGH, "package and build contract"),
    ("go.mod", RiskLevel.HIGH, "module dependency contract"),
    ("Dockerfile", RiskLevel.HIGH, "runtime image contract"),
    ("docker-compose*.yml", RiskLevel.HIGH, "runtime topology"),
    ("compose*.yaml", RiskLevel.HIGH, "runtime topology"),
)


def _required_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty")
    return cleaned


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _normalize_path(value: str) -> str:
    path = _required_text(value, field_name="path").replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    normalized = posixpath.normpath(path)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise ValueError("path must be repository-relative")
    if normalized.startswith("/"):
        raise ValueError("path must be repository-relative")
    return normalized


def _glob_matches(path: str, pattern: str) -> bool:
    # PurePath.match gives useful ** semantics, while fnmatch preserves common
    # root-relative patterns such as '.env.*' and 'compose*.yaml'.
    return PurePosixPath(path).match(pattern) or fnmatch.fnmatchcase(path, pattern)


@dataclass(frozen=True, slots=True)
class RiskRule:
    """One deterministic path-to-risk rule."""

    match: str
    level: RiskLevel
    reason: str
    source: str = "project"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "match", _required_text(self.match, field_name="match")
        )
        object.__setattr__(self, "level", RiskLevel(self.level))
        object.__setattr__(
            self, "reason", _required_text(self.reason, field_name="reason")
        )
        object.__setattr__(
            self, "source", _required_text(self.source, field_name="source")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "match": self.match,
            "level": self.level.value,
            "reason": self.reason,
            "source": self.source,
        }

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any], *, source: str = "project"
    ) -> "RiskRule":
        return cls(
            match=str(data.get("match") or ""),
            level=RiskLevel(str(data.get("level") or "")),
            reason=str(data.get("reason") or f"matched {data.get('match')!r}"),
            source=str(data.get("source") or source),
        )


@dataclass(frozen=True, slots=True)
class RiskFinding:
    """Risk classification for one repository-relative path."""

    path: str
    level: RiskLevel
    action: PolicyAction
    matched_rules: tuple[RiskRule, ...]
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "level": self.level.value,
            "action": self.action.value,
            "matched_rules": [rule.to_dict() for rule in self.matched_rules],
            "explanation": self.explanation,
        }


@dataclass(frozen=True, slots=True)
class PolicyPreset:
    """Stable behavior for one public policy name."""

    name: str
    pre_write_mode: PreWriteMode
    unknown_action: PolicyAction
    destructive_action: PolicyAction
    network_action: PolicyAction
    secret_action: PolicyAction
    scope_expansion_action: PolicyAction
    completion_verification_required: bool
    human_gate: bool
    auto_merge_allowed: bool
    risk_actions: Mapping[RiskLevel, PolicyAction]
    summary: str

    def __post_init__(self) -> None:
        normalized = _required_text(self.name, field_name="name").casefold()
        if normalized not in POLICY_NAMES:
            raise ValueError(f"unknown policy preset: {normalized}")
        object.__setattr__(self, "name", normalized)
        object.__setattr__(self, "pre_write_mode", PreWriteMode(self.pre_write_mode))
        object.__setattr__(self, "unknown_action", PolicyAction(self.unknown_action))
        object.__setattr__(
            self, "destructive_action", PolicyAction(self.destructive_action)
        )
        object.__setattr__(self, "network_action", PolicyAction(self.network_action))
        object.__setattr__(self, "secret_action", PolicyAction(self.secret_action))
        object.__setattr__(
            self, "scope_expansion_action", PolicyAction(self.scope_expansion_action)
        )
        normalized_actions = {
            RiskLevel(level): PolicyAction(action)
            for level, action in self.risk_actions.items()
        }
        missing = set(RiskLevel) - set(normalized_actions)
        if missing:
            raise ValueError(
                "risk_actions must define every risk level: "
                + ", ".join(sorted(item.value for item in missing))
            )
        object.__setattr__(self, "risk_actions", normalized_actions)
        object.__setattr__(
            self, "summary", _required_text(self.summary, field_name="summary")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pre_write_mode": self.pre_write_mode.value,
            "unknown_action": self.unknown_action.value,
            "destructive_action": self.destructive_action.value,
            "network_action": self.network_action.value,
            "secret_action": self.secret_action.value,
            "scope_expansion_action": self.scope_expansion_action.value,
            "completion_verification_required": self.completion_verification_required,
            "human_gate": self.human_gate,
            "auto_merge_allowed": self.auto_merge_allowed,
            "risk_actions": {
                level.value: self.risk_actions[level].value for level in RiskLevel
            },
            "summary": self.summary,
        }

    @classmethod
    def named(cls, name: str) -> "PolicyPreset":
        normalized = _required_text(name, field_name="policy").casefold()
        presets: dict[str, PolicyPreset] = {
            "observe": cls(
                name="observe",
                pre_write_mode=PreWriteMode.OBSERVE,
                unknown_action=PolicyAction.REPORT,
                destructive_action=PolicyAction.REPORT,
                network_action=PolicyAction.REPORT,
                secret_action=PolicyAction.REPORT,
                scope_expansion_action=PolicyAction.REPORT,
                completion_verification_required=True,
                human_gate=False,
                auto_merge_allowed=False,
                risk_actions={level: PolicyAction.REPORT for level in RiskLevel},
                summary=(
                    "Shadow mode: supported pre-write violations are recorded but do "
                    "not block the runtime; the final Git state is still verified."
                ),
            ),
            "guarded": cls(
                name="guarded",
                pre_write_mode=PreWriteMode.ENFORCE,
                unknown_action=PolicyAction.DENY,
                destructive_action=PolicyAction.REVIEW_REQUIRED,
                network_action=PolicyAction.REVIEW_REQUIRED,
                secret_action=PolicyAction.DENY,
                scope_expansion_action=PolicyAction.ALLOW,
                completion_verification_required=True,
                human_gate=False,
                auto_merge_allowed=False,
                risk_actions={
                    RiskLevel.LOW: PolicyAction.ALLOW,
                    RiskLevel.MEDIUM: PolicyAction.ALLOW,
                    RiskLevel.HIGH: PolicyAction.REVIEW_REQUIRED,
                    RiskLevel.CRITICAL: PolicyAction.REVIEW_REQUIRED,
                },
                summary=(
                    "Daily development mode: supported undeclared writes are blocked, "
                    "scope growth requires re-admission, and high-risk changes "
                    "require review."
                ),
            ),
            "strict": cls(
                name="strict",
                pre_write_mode=PreWriteMode.ENFORCE,
                unknown_action=PolicyAction.DENY,
                destructive_action=PolicyAction.DENY,
                network_action=PolicyAction.DENY,
                secret_action=PolicyAction.DENY,
                scope_expansion_action=PolicyAction.REVIEW_REQUIRED,
                completion_verification_required=True,
                human_gate=False,
                auto_merge_allowed=False,
                risk_actions={
                    RiskLevel.LOW: PolicyAction.ALLOW,
                    RiskLevel.MEDIUM: PolicyAction.ALLOW,
                    RiskLevel.HIGH: PolicyAction.REVIEW_REQUIRED,
                    RiskLevel.CRITICAL: PolicyAction.DENY,
                },
                summary=(
                    "Fail-closed mode: unknown, destructive, network, secret, and "
                    "critical-resource actions require stronger authority or review."
                ),
            ),
            "critical": cls(
                name="critical",
                pre_write_mode=PreWriteMode.ENFORCE,
                unknown_action=PolicyAction.DENY,
                destructive_action=PolicyAction.DENY,
                network_action=PolicyAction.DENY,
                secret_action=PolicyAction.DENY,
                scope_expansion_action=PolicyAction.REVIEW_REQUIRED,
                completion_verification_required=True,
                human_gate=True,
                auto_merge_allowed=False,
                risk_actions={
                    RiskLevel.LOW: PolicyAction.REVIEW_REQUIRED,
                    RiskLevel.MEDIUM: PolicyAction.REVIEW_REQUIRED,
                    RiskLevel.HIGH: PolicyAction.REVIEW_REQUIRED,
                    RiskLevel.CRITICAL: PolicyAction.DENY,
                },
                summary=(
                    "Human-gated mode for protected changes: no delivery is accepted "
                    "without explicit review and independent verification."
                ),
            ),
        }
        try:
            return presets[normalized]
        except KeyError as exc:
            raise ValueError(
                "unknown policy preset; expected observe, guarded, strict, or critical"
            ) from exc


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """Project risk defaults and path-based overrides."""

    default: RiskLevel = RiskLevel.MEDIUM
    rules: tuple[RiskRule, ...] = ()
    include_builtin_rules: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "default", RiskLevel(self.default))
        object.__setattr__(self, "rules", tuple(self.rules))

    @classmethod
    def from_config(cls, data: Mapping[str, Any] | None) -> "RiskPolicy":
        raw = dict(data or {})
        raw_rules = raw.get("rules") or []
        if not isinstance(raw_rules, Sequence) or isinstance(raw_rules, (str, bytes)):
            raise ValueError("risk.rules must be an array")
        rules: list[RiskRule] = []
        for item in raw_rules:
            if not isinstance(item, Mapping):
                raise ValueError("each risk rule must be an object")
            rules.append(RiskRule.from_dict(item))
        return cls(
            default=RiskLevel(str(raw.get("default") or "medium")),
            rules=tuple(rules),
            include_builtin_rules=bool(raw.get("include_builtin_rules", True)),
        )

    def all_rules(self) -> tuple[RiskRule, ...]:
        builtin = (
            tuple(
                RiskRule(match=match, level=level, reason=reason, source="builtin")
                for match, level, reason in _DEFAULT_RISK_RULES
            )
            if self.include_builtin_rules
            else ()
        )
        return builtin + self.rules

    def to_dict(self) -> dict[str, Any]:
        return {
            "default": self.default.value,
            "include_builtin_rules": self.include_builtin_rules,
            "rules": [rule.to_dict() for rule in self.rules],
            "effective_rules": [rule.to_dict() for rule in self.all_rules()],
        }


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    """Resolved preset and project risk rules included in run evidence."""

    preset: PolicyPreset
    risk: RiskPolicy
    source: str
    protocol: str = POLICY_PROTOCOL
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.protocol != POLICY_PROTOCOL:
            raise ValueError(f"policy protocol must be {POLICY_PROTOCOL!r}")
        object.__setattr__(
            self, "source", _required_text(self.source, field_name="source")
        )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def name(self) -> str:
        return self.preset.name

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "protocol": self.protocol,
            "preset": self.preset.to_dict(),
            "risk": self.risk.to_dict(),
            "source": self.source,
            "metadata": dict(self.metadata),
        }
        if include_digest:
            payload["digest"] = self.digest()
        return payload

    def digest(self) -> str:
        return hashlib.sha256(
            _canonical_json(self.to_dict(include_digest=False)).encode("utf-8")
        ).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EffectivePolicy":
        raw_preset = data.get("preset")
        raw_risk = data.get("risk")
        if not isinstance(raw_preset, Mapping):
            raise ValueError("effective policy preset must be an object")
        if not isinstance(raw_risk, Mapping):
            raise ValueError("effective policy risk must be an object")
        preset = PolicyPreset.named(str(raw_preset.get("name") or ""))
        if dict(raw_preset) != preset.to_dict():
            raise ValueError(
                "effective policy preset does not match canonical semantics"
            )
        policy = cls(
            protocol=str(data.get("protocol") or ""),
            preset=preset,
            risk=RiskPolicy.from_config(raw_risk),
            source=str(data.get("source") or ""),
            metadata=(
                dict(data["metadata"])
                if isinstance(data.get("metadata"), Mapping)
                else {}
            ),
        )
        supplied_digest = data.get("digest")
        if supplied_digest is not None and str(supplied_digest) != policy.digest():
            raise ValueError("effective policy digest does not match content")
        return policy

    def action_for(self, level: RiskLevel) -> PolicyAction:
        return self.preset.risk_actions[RiskLevel(level)]

    def classify(self, path: str) -> RiskFinding:
        normalized = _normalize_path(path)
        matches = tuple(
            rule
            for rule in self.risk.all_rules()
            if _glob_matches(normalized, rule.match)
        )
        level = self.risk.default
        if matches:
            level = max(
                (rule.level for rule in matches),
                key=_RISK_STRENGTH.__getitem__,
            )
        action = self.action_for(level)
        if matches:
            reasons = "; ".join(
                f"{rule.match}: {rule.reason}"
                for rule in matches
                if rule.level is level
            )
            explanation = (
                f"{normalized} is {level.value} risk because it matched {reasons}; "
                f"policy {self.name} selects {action.value}."
            )
        else:
            explanation = (
                f"{normalized} uses the project default {level.value} risk; "
                f"policy {self.name} selects {action.value}."
            )
        return RiskFinding(normalized, level, action, matches, explanation)

    def classify_many(self, paths: Iterable[str]) -> dict[str, Any]:
        findings = tuple(self.classify(path) for path in sorted(set(paths)))
        highest = (
            max((item.level for item in findings), key=_RISK_STRENGTH.__getitem__)
            if findings
            else self.risk.default
        )
        actions = {item.action for item in findings}
        if self.preset.human_gate:
            final_action = PolicyAction.REVIEW_REQUIRED
        elif PolicyAction.DENY in actions:
            final_action = PolicyAction.DENY
        elif PolicyAction.REVIEW_REQUIRED in actions:
            final_action = PolicyAction.REVIEW_REQUIRED
        elif PolicyAction.REPORT in actions:
            final_action = PolicyAction.REPORT
        else:
            final_action = PolicyAction.ALLOW
        return {
            "protocol": RISK_CLASSIFICATION_PROTOCOL,
            "policy": self.name,
            "policy_digest": self.digest(),
            "highest_risk": highest.value,
            "final_action": final_action.value,
            "human_gate": self.preset.human_gate,
            "findings": [item.to_dict() for item in findings],
            "reason_codes": sorted(
                {
                    f"risk_{item.level.value}_{item.action.value.casefold()}"
                    for item in findings
                }
                | ({"policy_human_gate"} if self.preset.human_gate else set())
            ),
        }


def resolve_policy(
    name: str,
    *,
    risk: Mapping[str, Any] | RiskPolicy | None = None,
    source: str = "explicit",
    metadata: Mapping[str, Any] | None = None,
) -> EffectivePolicy:
    """Resolve a stable preset and project risk configuration."""

    risk_policy = risk if isinstance(risk, RiskPolicy) else RiskPolicy.from_config(risk)
    return EffectivePolicy(
        preset=PolicyPreset.named(name),
        risk=risk_policy,
        source=source,
        metadata=dict(metadata or {}),
    )
