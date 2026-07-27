"""Pinned research-environment metadata and runtime diagnostics."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

_LOCK_FILE = Path(__file__).resolve().parent / "docker" / "environment.lock.json"


def load_environment_lock() -> dict[str, Any]:
    payload = json.loads(_LOCK_FILE.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("research environment lock must contain one JSON object")
    return payload


def _command_version(*command: str) -> str | None:
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    text = completed.stdout.strip() or completed.stderr.strip()
    return text.splitlines()[0] if text else None


def runtime_environment() -> dict[str, Any]:
    """Return non-secret toolchain diagnostics for a research execution."""

    return {
        "lock": load_environment_lock(),
        "runtime": {
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "git_version": _command_version("git", "--version"),
            "uv_version": _command_version("uv", "--version"),
            "container_environment": os.environ.get("CLAIM_PLANE_RESEARCH_ENVIRONMENT"),
            "container_base_image": os.environ.get("CLAIM_PLANE_RESEARCH_BASE_IMAGE"),
            "claim_plane_git_commit": os.environ.get("CLAIM_PLANE_RESEARCH_GIT_COMMIT"),
            "openrouter_api_key_present": "OPENROUTER_API_KEY" in os.environ,
        },
    }
