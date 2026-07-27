"""Reproducibility manifest collection for one experiment run."""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Sequence

from .identity import RunIdentity
from .models import StudySpec

_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def _git_value(repo: Path, *args: str) -> str | None:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _git_dirty(repo: Path) -> bool | None:
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return bool(completed.stdout.strip())


def _claim_plane_version(repo: Path) -> str:
    try:
        return version("claim-plane")
    except PackageNotFoundError:
        init_file = repo / "src" / "claim_plane" / "__init__.py"
        if init_file.exists():
            match = _VERSION_RE.search(init_file.read_text(encoding="utf-8"))
            if match:
                return match.group(1)
        return "unknown"


@dataclass(frozen=True, slots=True)
class RunManifest:
    schema_version: int
    run: RunIdentity
    created_at_utc: str
    claim_plane_version: str
    study_claim_plane_version: str
    python_version: str
    platform: str
    machine: str
    git_commit: str | None
    git_dirty: bool | None
    command: tuple[str, ...]
    environment_keys: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run": self.run.to_dict(),
            "created_at_utc": self.created_at_utc,
            "claim_plane_version": self.claim_plane_version,
            "study_claim_plane_version": self.study_claim_plane_version,
            "python_version": self.python_version,
            "platform": self.platform,
            "machine": self.machine,
            "git_commit": self.git_commit,
            "git_dirty": self.git_dirty,
            "command": list(self.command),
            "environment_keys": list(self.environment_keys),
        }


def collect_run_manifest(
    study: StudySpec,
    run: RunIdentity,
    *,
    repo_root: str | Path = ".",
    command: Sequence[str] | None = None,
    environment_keys: Sequence[str] = (),
) -> RunManifest:
    """Capture non-secret execution metadata.

    Only environment variable names explicitly supplied by the caller are recorded;
    values are never persisted by this helper.
    """

    repo = Path(repo_root).resolve()
    declared_keys = tuple(sorted(set(str(key) for key in environment_keys)))
    present_keys = tuple(key for key in declared_keys if key in os.environ)
    return RunManifest(
        schema_version=1,
        run=run,
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        claim_plane_version=_claim_plane_version(repo),
        study_claim_plane_version=study.claim_plane_version,
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        machine=platform.machine(),
        git_commit=_git_value(repo, "rev-parse", "HEAD"),
        git_dirty=_git_dirty(repo),
        command=tuple(command or sys.argv),
        environment_keys=present_keys,
    )
