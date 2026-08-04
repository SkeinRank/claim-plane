"""Technical-preview packaging manifest and user-facing support contract."""

from __future__ import annotations

import platform
import sys
from pathlib import Path
from typing import Any

from claim_plane import __version__
from claim_plane.exit_codes import exit_code_manifest
from claim_plane.resources import list_schemas

TECHNICAL_PREVIEW_PROTOCOL = "claim-plane.technical-preview.v1"
TECHNICAL_PREVIEW_CHANNEL = "single-agent-codex"

_STABLE_COMMANDS = (
    "claim-plane init",
    "claim-plane connect codex",
    "claim-plane doctor",
    "claim-plane codex",
    "claim-plane run",
    "claim-plane report",
    "claim-plane replay",
    "claim-plane policy inspect",
    "claim-plane adapters inspect",
    "claim-plane adapters conformance",
    "claim-plane config status",
    "claim-plane config migrate",
    "claim-plane schemas list",
    "claim-plane schemas export",
    "claim-plane reset",
)

_DOCUMENTATION = (
    "README.md",
    "docs/QUICKSTART.md",
    "docs/CLI_REFERENCE.md",
    "docs/GUARANTEES.md",
    "docs/TROUBLESHOOTING.md",
    "docs/UPGRADING.md",
    "docs/RELEASING.md",
    "SECURITY.md",
)


def technical_preview_manifest(
    repository_root: str | Path | None = None,
) -> dict[str, Any]:
    """Describe the installed preview without claiming project readiness."""

    schemas = list_schemas()
    root = (
        None
        if repository_root is None
        else Path(repository_root).expanduser().resolve()
    )
    documentation: list[dict[str, Any]] = []
    for relative in _DOCUMENTATION:
        present = None if root is None else (root / relative).is_file()
        documentation.append({"path": relative, "present": present})

    return {
        "protocol": TECHNICAL_PREVIEW_PROTOCOL,
        "channel": TECHNICAL_PREVIEW_CHANNEL,
        "version": __version__,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "supported": sys.version_info >= (3, 10),
            "requires": ">=3.10",
        },
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
        },
        "stable_commands": list(_STABLE_COMMANDS),
        "exit_codes": exit_code_manifest(),
        "schemas": {
            "count": len(schemas),
            "digest_entries": list(schemas),
        },
        "documentation": documentation,
        "limitations": [
            "Codex is the first complete interactive and one-command adapter path.",
            (
                "Project-local runtime hooks are not a non-bypassable "
                "operating-system boundary."
            ),
            (
                "A verified result proves declared authority and evidence "
                "checks, not universal code correctness."
            ),
            "Swarm operation remains an advanced research preview.",
        ],
    }
