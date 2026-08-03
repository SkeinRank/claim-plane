"""Stable public CLI exit-code contract for the technical preview."""

from __future__ import annotations

from enum import IntEnum
from typing import Any

EXIT_CODE_PROTOCOL = "claim-plane.exit-codes.v1"


class ExitCode(IntEnum):
    """Public process outcomes shared by technical-preview commands."""

    OK = 0
    ERROR = 1
    ACTION_REQUIRED = 2
    INCOMPLETE = 3
    BLOCKED = 4
    TIMED_OUT = 124
    CANCELLED = 130


_EXIT_CODE_DETAILS: tuple[tuple[ExitCode, str, str], ...] = (
    (
        ExitCode.OK,
        "ok",
        "The command completed and its requested verification passed.",
    ),
    (
        ExitCode.ERROR,
        "error",
        "Input, configuration, compatibility, or an unexpected execution "
        "error prevented a result.",
    ),
    (
        ExitCode.ACTION_REQUIRED,
        "action_required",
        "The result is valid but needs review, remediation, or a compatible "
        "runtime boundary.",
    ),
    (
        ExitCode.INCOMPLETE,
        "incomplete",
        "Required measured inputs or evidence are missing; no passing claim "
        "was produced.",
    ),
    (
        ExitCode.BLOCKED,
        "blocked",
        "A deterministic policy, verification, or release gate rejected the "
        "requested outcome.",
    ),
    (
        ExitCode.TIMED_OUT,
        "timed_out",
        "The bounded operation exceeded its configured wall-time limit and "
        "authority was revoked.",
    ),
    (
        ExitCode.CANCELLED,
        "cancelled",
        "The user cancelled the bounded operation and unfinished authority "
        "was revoked.",
    ),
)


def exit_code_manifest() -> dict[str, Any]:
    """Return the machine-readable stable exit-code contract."""

    return {
        "protocol": EXIT_CODE_PROTOCOL,
        "codes": [
            {"code": int(code), "name": name, "meaning": meaning}
            for code, name, meaning in _EXIT_CODE_DETAILS
        ],
    }
