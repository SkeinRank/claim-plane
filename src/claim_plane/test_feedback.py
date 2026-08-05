"""Bounded test-feedback helpers for interactive single-agent runs."""

from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath

TEST_FEEDBACK_PROTOCOL = "claim-plane.test-feedback.v1"

_MANAGED_TEST_ARTIFACT_PATTERNS = (
    ".pytest_cache/**",
    "**/.pytest_cache/**",
    "__pycache__/**",
    "**/__pycache__/**",
    ".mypy_cache/**",
    "**/.mypy_cache/**",
    ".ruff_cache/**",
    "**/.ruff_cache/**",
    ".coverage",
    ".coverage.*",
    "coverage.xml",
    "htmlcov/**",
    ".tox/**",
    ".nox/**",
    ".venv/**",
    "venv/**",
    "build/**",
    "dist/**",
    "*.egg-info/**",
    "**/*.egg-info/**",
    "node_modules/**",
    "target/**",
    ".gradle/**",
    ".breeze/**",
)


def managed_test_artifact(path: str) -> bool:
    """Return whether an untracked path is a bounded generated test artifact."""

    normalized = PurePosixPath(path.replace("\\", "/")).as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return any(
        fnmatch.fnmatchcase(normalized, pattern)
        for pattern in _MANAGED_TEST_ARTIFACT_PATTERNS
    )
