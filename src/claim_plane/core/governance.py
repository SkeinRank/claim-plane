"""Governance policy for intent admission and integration execution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GovernancePolicy:
    """Controls fail-open versus fail-closed project behavior.

    ``governed`` is the default. It requires an immutable base commit before
    an intent can be admitted. ``exploratory`` preserves an explicitly looser
    workflow for local experiments.
    """

    mode: str = "governed"
    require_base_commit: bool = True

    def __post_init__(self) -> None:
        if self.mode not in {"governed", "exploratory"}:
            raise ValueError("governance mode must be 'governed' or 'exploratory'")
        if self.mode == "governed" and not self.require_base_commit:
            raise ValueError("governed mode requires base_commit admission pinning")

    @classmethod
    def governed(cls) -> "GovernancePolicy":
        return cls("governed", True)

    @classmethod
    def exploratory(cls) -> "GovernancePolicy":
        return cls("exploratory", False)

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "require_base_commit": self.require_base_commit,
        }
