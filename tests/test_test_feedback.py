from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claim_plane.connectors import codex_completion
from claim_plane.test_feedback import managed_test_artifact


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


def test_managed_test_artifact_patterns_are_bounded() -> None:
    assert managed_test_artifact(".pytest_cache/v/cache/nodeids")
    assert managed_test_artifact("src/pkg/__pycache__/mod.cpython-310.pyc")
    assert managed_test_artifact("htmlcov/index.html")
    assert managed_test_artifact("target/debug/app")
    assert not managed_test_artifact("tests/test_app.py")
    assert not managed_test_artifact("pyproject.toml")
    assert not managed_test_artifact("snapshots/result.json")


def test_completion_filter_ignores_only_untracked_managed_artifacts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    (repo / "tracked.py").write_text("VALUE = 2\n", encoding="utf-8")
    cache = repo / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True)
    (cache / "nodeids").write_text("[]\n", encoding="utf-8")

    class Region:
        def __init__(self, path: str) -> None:
            self.path = path

    class Artifact:
        def __init__(self, path: str) -> None:
            self.path = path

    class Manifest:
        changed_files = ("tracked.py", ".pytest_cache/v/cache/nodeids")
        changed_regions = (
            Region("tracked.py"),
            Region(".pytest_cache/v/cache/nodeids"),
        )
        artifacts = (Artifact("tracked.py"), Artifact(".pytest_cache/v/cache/nodeids"))
        metadata: dict[str, object] = {}

    @dataclass(frozen=True)
    class RealManifest:
        changed_files: tuple[str, ...]
        changed_regions: tuple[Any, ...]
        artifacts: tuple[Any, ...]
        metadata: dict[str, Any] = field(default_factory=dict)

    filtered = codex_completion._without_managed_test_artifacts(
        RealManifest(
            Manifest.changed_files,
            Manifest.changed_regions,
            Manifest.artifacts,
        ),
        root=repo,
    )
    assert filtered.changed_files == ("tracked.py",)
    assert filtered.metadata["managed_test_artifacts_ignored"] == [
        ".pytest_cache/v/cache/nodeids"
    ]
