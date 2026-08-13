from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claim_plane.code_intelligence import (
    SCIP_INDEX_ARTIFACT_PROTOCOL,
    ScipIndexManager,
    ScipIndexerConfig,
    ScipIndexerFailed,
    ScipIndexerUnavailable,
    ScipRevisionCache,
    ScipRevisionMismatch,
    capture_scip_repository_state,
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "claim-plane@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Claim Plane Tests"],
        cwd=repo,
        check=True,
    )
    (repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def _head(repo: Path) -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def _fake_indexer(tmp_path: Path) -> Path:
    script = tmp_path / "scip-python"
    script.write_text(
        """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

log = os.environ.get("SCIP_FAKE_LOG")
if log:
    with pathlib.Path(log).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")

if sys.argv[1:] == ["--version"]:
    print("scip-python 9.9.9-test")
    raise SystemExit(0)

if not sys.argv[1:] or sys.argv[1] != "index":
    print("unexpected invocation", file=sys.stderr)
    raise SystemExit(2)

if "__FAIL__" in sys.argv:
    print("synthetic index failure", file=sys.stderr)
    raise SystemExit(7)

def option(prefix):
    for value in sys.argv[2:]:
        if value.startswith(prefix):
            return value.split("=", 1)[1]
    raise RuntimeError(f"missing option {prefix}")

target = pathlib.Path(option("--cwd=")).resolve()
output = pathlib.Path(option("--output=")).resolve()
payload = (target / "app.py").read_bytes()
if "__NO_INDEX__" not in sys.argv:
    output.write_bytes(b"SCIP" + bytes([0]) + payload)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _manager(indexer: Path, log: Path, *, extra_args: tuple[str, ...] = ()) -> ScipIndexManager:
    return ScipIndexManager(
        ScipIndexerConfig(
            command=(str(indexer),),
            extra_args=extra_args,
            environment_fingerprint="fixture-environment-v1",
            environment={"SCIP_FAKE_LOG": str(log)},
        )
    )


def _invocations(log: Path) -> list[dict[str, object]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_scip_index_is_cached_by_revision_and_workspace(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    log = tmp_path / "scip.log"
    indexer = _fake_indexer(tmp_path)
    manager = _manager(indexer, log)
    cache = tmp_path / "cache"

    cold = manager.index_repository(repo, cache_root=cache)
    warm = manager.index_repository(repo, cache_root=cache)

    assert cold.protocol == SCIP_INDEX_ARTIFACT_PROTOCOL
    assert cold.cache_hit is False
    assert warm.cache_hit is True
    assert cold.cache_key == warm.cache_key
    assert cold.sha256 == warm.sha256
    assert warm.index_path == cold.index_path
    assert warm.index_path.is_file()
    assert not (repo / "index.scip").exists()
    calls = _invocations(log)
    assert [item["argv"] for item in calls] == [
        ["--version"],
        [
            "index",
            f"--cwd={repo.resolve()}",
            calls[1]["argv"][2],
            f"--project-name={repo.name}",
            f"--project-version={_head(repo)}",
        ],
    ]
    assert str(calls[1]["argv"][2]).startswith("--output=/")


def test_new_commit_creates_a_new_cache_entry(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    log = tmp_path / "scip.log"
    manager = _manager(_fake_indexer(tmp_path), log)
    cache = tmp_path / "cache"

    first = manager.index_repository(repo, cache_root=cache)
    (repo / "app.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "change"], cwd=repo, check=True)
    second = manager.index_repository(repo, cache_root=cache)

    assert second.cache_hit is False
    assert first.revision != second.revision
    assert first.cache_key != second.cache_key
    assert first.sha256 != second.sha256
    assert len([item for item in _invocations(log) if item["argv"][0] == "index"]) == 2


def test_dirty_workspace_has_content_bound_cache_identity(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    log = tmp_path / "scip.log"
    manager = _manager(_fake_indexer(tmp_path), log)
    cache = tmp_path / "cache"

    clean = manager.index_repository(repo, cache_root=cache)
    (repo / "app.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    dirty = manager.index_repository(repo, cache_root=cache)
    dirty_warm = manager.index_repository(repo, cache_root=cache)
    (repo / "app.py").write_text("def run():\n    return 3\n", encoding="utf-8")
    dirty_changed = manager.index_repository(repo, cache_root=cache)

    assert clean.dirty is False
    assert dirty.dirty is True
    assert dirty_warm.cache_hit is True
    assert dirty.cache_key == dirty_warm.cache_key
    assert clean.cache_key != dirty.cache_key
    assert dirty.cache_key != dirty_changed.cache_key
    assert dirty.workspace_fingerprint != dirty_changed.workspace_fingerprint


def test_untracked_content_changes_workspace_fingerprint(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = capture_scip_repository_state(repo)
    (repo / "new.py").write_text("VALUE = 1\n", encoding="utf-8")
    second = capture_scip_repository_state(repo)
    (repo / "new.py").write_text("VALUE = 2\n", encoding="utf-8")
    third = capture_scip_repository_state(repo)

    assert first.dirty is False
    assert second.dirty is True
    assert second.workspace_fingerprint != first.workspace_fingerprint
    assert third.workspace_fingerprint != second.workspace_fingerprint


def test_environment_fingerprint_participates_in_cache_identity(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    log = tmp_path / "scip.log"
    indexer = _fake_indexer(tmp_path)
    first = ScipIndexManager(
        ScipIndexerConfig(
            command=(str(indexer),),
            environment_fingerprint="environment-a",
            environment={"SCIP_FAKE_LOG": str(log)},
        )
    ).index_repository(repo, cache_root=tmp_path / "cache")
    second = ScipIndexManager(
        ScipIndexerConfig(
            command=(str(indexer),),
            environment_fingerprint="environment-b",
            environment={"SCIP_FAKE_LOG": str(log)},
        )
    ).index_repository(repo, cache_root=tmp_path / "cache")

    assert first.environment_fingerprint == "environment-a"
    assert second.environment_fingerprint == "environment-b"
    assert first.cache_key != second.cache_key
    assert second.cache_hit is False


def test_explicit_revision_must_match_checked_out_head(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    old = _head(repo)
    (repo / "app.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "change"], cwd=repo, check=True)

    with pytest.raises(ScipRevisionMismatch, match="not checked out HEAD"):
        capture_scip_repository_state(repo, revision=old)


def test_missing_indexer_fails_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    manager = ScipIndexManager(
        ScipIndexerConfig(command=(str(tmp_path / "missing-scip-python"),))
    )

    with pytest.raises(ScipIndexerUnavailable, match="unavailable"):
        manager.index_repository(repo, cache_root=tmp_path / "cache")


def test_indexer_failure_and_missing_output_are_errors(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    log = tmp_path / "scip.log"
    indexer = _fake_indexer(tmp_path)

    failing = _manager(indexer, log, extra_args=("__FAIL__",))
    with pytest.raises(ScipIndexerFailed, match="synthetic index failure"):
        failing.index_repository(repo, cache_root=tmp_path / "cache-fail")

    no_output = _manager(indexer, log, extra_args=("__NO_INDEX__",))
    with pytest.raises(ScipIndexerFailed, match="without producing index.scip"):
        no_output.index_repository(repo, cache_root=tmp_path / "cache-empty")


def test_corrupt_cached_index_is_rebuilt(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    log = tmp_path / "scip.log"
    manager = _manager(_fake_indexer(tmp_path), log)
    cache_root = tmp_path / "cache"

    first = manager.index_repository(repo, cache_root=cache_root)
    first.index_path.write_bytes(b"corrupt")
    rebuilt = manager.index_repository(repo, cache_root=cache_root)

    assert rebuilt.cache_hit is False
    assert rebuilt.sha256 != ""
    assert rebuilt.index_path.read_bytes().startswith(b"SCIP\x00")
    index_calls = [item for item in _invocations(log) if item["argv"][0] == "index"]
    assert len(index_calls) == 2


def test_default_cache_stays_outside_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    log = tmp_path / "scip.log"
    user_cache = tmp_path / "user-cache"
    monkeypatch.setenv("CLAIM_PLANE_CACHE_DIR", str(user_cache))
    manager = _manager(_fake_indexer(tmp_path), log)

    artifact = manager.index_repository(repo)

    expected = user_cache / "code-intelligence/scip"
    assert artifact.index_path.is_relative_to(expected)
    assert not artifact.index_path.is_relative_to(repo)
    status = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=repo, text=True
    )
    assert status == ""


def test_cache_loader_rejects_metadata_digest_mismatch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    log = tmp_path / "scip.log"
    manager = _manager(_fake_indexer(tmp_path), log)
    cache_root = tmp_path / "cache"
    artifact = manager.index_repository(repo, cache_root=cache_root)
    cache = ScipRevisionCache(cache_root)

    metadata = artifact.index_path.parent / "metadata.json"
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    payload["sha256"] = "0" * 64
    metadata.write_text(json.dumps(payload), encoding="utf-8")

    assert cache.load(artifact.cache_key) is None


def test_managed_scip_options_cannot_be_overridden() -> None:
    for option in (
        "--cwd=/tmp/other",
        "--output=/tmp/other.scip",
        "--project-name=other",
        "--project-version=other",
    ):
        with pytest.raises(ValueError, match="managed by Claim Plane"):
            ScipIndexerConfig(
                extra_args=(option,),
                environment_fingerprint="fixture-environment-v1",
            )


def test_environment_probe_is_stable_and_memoized(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    indexer = _fake_indexer(tmp_path)
    log = tmp_path / "scip.log"
    probe_log = tmp_path / "probe.log"
    probe = tmp_path / "env-probe"
    probe.write_text(
        """#!/usr/bin/env python3
import os
import pathlib

pathlib.Path(os.environ["SCIP_ENV_PROBE_LOG"]).open("a", encoding="utf-8").write("probe\\n")
print("z-package==2")
print("a-package==1")
""",
        encoding="utf-8",
    )
    probe.chmod(0o755)
    manager = ScipIndexManager(
        ScipIndexerConfig(
            command=(str(indexer),),
            environment_probe_command=(str(probe),),
            environment={
                "SCIP_FAKE_LOG": str(log),
                "SCIP_ENV_PROBE_LOG": str(probe_log),
            },
        )
    )

    first = manager.index_repository(repo, cache_root=tmp_path / "cache")
    second = manager.index_repository(repo, cache_root=tmp_path / "cache")

    assert len(first.environment_fingerprint) == 64
    assert first.environment_fingerprint == second.environment_fingerprint
    assert second.cache_hit is True
    assert probe_log.read_text(encoding="utf-8").splitlines() == ["probe"]
