"""SCIP index generation and revision-aware artifact caching.

This module deliberately treats ``index.scip`` as an opaque artifact.  Parsing SCIP
symbols into Claim Plane Semantic Resource IR is a separate boundary; keeping indexing
independent makes cache correctness and indexer overhead measurable on their own.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

SCIP_INDEX_ARTIFACT_PROTOCOL = "claim-plane.scip-index-artifact.v1"
SCIP_INDEX_CACHE_PROTOCOL = "claim-plane.scip-index-cache.v1"
SCIP_INDEX_CACHE_SCHEMA = "1"
DEFAULT_SCIP_PYTHON_COMMAND = ("scip-python",)
DEFAULT_SCIP_ENVIRONMENT_PROBE_COMMAND = (
    "python",
    "-m",
    "pip",
    "list",
    "--format=freeze",
    "--disable-pip-version-check",
)
DEFAULT_SCIP_INDEX_TIMEOUT_SECONDS = 300.0


class ScipIndexError(RuntimeError):
    """Base error for SCIP index generation and cache handling."""


class ScipIndexerUnavailable(ScipIndexError):
    """Raised when the configured SCIP indexer executable cannot be started."""


class ScipIndexerFailed(ScipIndexError):
    """Raised when a SCIP indexer exits unsuccessfully or emits no index."""


class ScipRevisionMismatch(ScipIndexError):
    """Raised when a requested source revision is not the checked-out revision."""


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _default_claim_plane_cache_root() -> Path:
    override = os.environ.get("CLAIM_PLANE_CACHE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return (Path(xdg).expanduser() / "claim-plane").resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library/Caches/claim-plane").resolve()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return (Path(local) / "claim-plane/Cache").resolve()
    return (Path.home() / ".cache/claim-plane").resolve()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_git(root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ("git", "-C", str(root), *args),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ScipIndexError("git is required for revision-aware SCIP indexing") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ScipIndexError(
            f"git {' '.join(args)} failed for {root}: {detail or result.returncode}"
        )
    return result.stdout


def _repository_root(path: str | Path) -> Path:
    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ScipIndexError(f"repository root is not a directory: {root}")
    top = _run_git(root, "rev-parse", "--show-toplevel").decode().strip()
    resolved = Path(top).resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ScipIndexError(f"git reported an invalid repository root: {resolved}")
    return resolved


def _changed_paths(root: Path) -> tuple[str, ...]:
    tracked = _run_git(root, "diff", "--name-only", "-z", "HEAD", "--").split(b"\0")
    untracked = _run_git(
        root, "ls-files", "--others", "--exclude-standard", "-z"
    ).split(b"\0")
    values = {
        item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for item in (*tracked, *untracked)
        if item
    }
    return tuple(sorted(values))


def _working_tree_fingerprint(root: Path) -> tuple[str, bool]:
    paths = _changed_paths(root)
    digest = hashlib.sha256()
    digest.update(b"claim-plane.scip-worktree.v1\0")
    for relative in paths:
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        path = root / relative
        if not path.exists() and not path.is_symlink():
            digest.update(b"deleted\0")
            continue
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise ScipIndexError(f"cannot inspect changed path {relative}: {exc}") from exc
        if stat.S_ISLNK(mode):
            digest.update(b"symlink\0")
            digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
        elif stat.S_ISREG(mode):
            digest.update(b"file\0")
            digest.update(str(mode & 0o777).encode("ascii"))
            digest.update(b"\0")
            try:
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                raise ScipIndexError(f"cannot hash changed path {relative}: {exc}") from exc
            digest.update(b"\0")
        else:
            digest.update(b"other\0")
    return digest.hexdigest(), bool(paths)


@dataclass(frozen=True, slots=True)
class ScipRepositoryState:
    """Git-bound repository identity used to make SCIP cache entries source-safe."""

    repository_root: Path
    revision: str
    workspace_fingerprint: str
    dirty: bool

    @property
    def repository_id(self) -> str:
        return hashlib.sha256(str(self.repository_root).encode("utf-8")).hexdigest()


def capture_scip_repository_state(
    repository_root: str | Path,
    *,
    revision: str | None = None,
) -> ScipRepositoryState:
    """Capture HEAD plus a content fingerprint for any working-tree deviations.

    An explicit revision is an assertion about the current checkout, not a request to
    index another commit.  Mismatches fail closed instead of producing mislabeled cache
    artifacts.
    """

    root = _repository_root(repository_root)
    head = _run_git(root, "rev-parse", "HEAD").decode().strip()
    if revision is not None:
        requested = str(revision).strip()
        if not requested:
            raise ScipRevisionMismatch("requested SCIP revision must not be empty")
        resolved = _run_git(root, "rev-parse", requested).decode().strip()
        if resolved != head:
            raise ScipRevisionMismatch(
                f"requested SCIP revision {resolved} is not checked out HEAD {head}"
            )
    workspace_fingerprint, dirty = _working_tree_fingerprint(root)
    return ScipRepositoryState(
        repository_root=root,
        revision=head,
        workspace_fingerprint=workspace_fingerprint,
        dirty=dirty,
    )


@dataclass(frozen=True, slots=True)
class ScipIndexerConfig:
    """Configuration for an external SCIP-producing indexer command."""

    command: tuple[str, ...] = DEFAULT_SCIP_PYTHON_COMMAND
    version_args: tuple[str, ...] = ("--version",)
    indexer_version: str | None = None
    project_name: str | None = None
    extra_args: tuple[str, ...] = ()
    environment_fingerprint: str | None = None
    environment_probe_command: tuple[str, ...] = DEFAULT_SCIP_ENVIRONMENT_PROBE_COMMAND
    timeout_seconds: float = DEFAULT_SCIP_INDEX_TIMEOUT_SECONDS
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        command = tuple(str(item).strip() for item in self.command if str(item).strip())
        if not command:
            raise ValueError("SCIP indexer command must not be empty")
        version_args = tuple(str(item) for item in self.version_args)
        version = (
            None
            if self.indexer_version is None
            else str(self.indexer_version).strip() or None
        )
        project_name = (
            None if self.project_name is None else str(self.project_name).strip() or None
        )
        environment_fingerprint = (
            None
            if self.environment_fingerprint is None
            else str(self.environment_fingerprint).strip() or None
        )
        environment_probe_command = tuple(
            str(item).strip()
            for item in self.environment_probe_command
            if str(item).strip()
        )
        if environment_fingerprint is None and not environment_probe_command:
            raise ValueError(
                "SCIP environment probe command is required without an explicit fingerprint"
            )
        timeout = float(self.timeout_seconds)
        if timeout <= 0:
            raise ValueError("SCIP index timeout must be positive")
        object.__setattr__(self, "command", command)
        object.__setattr__(self, "version_args", version_args)
        object.__setattr__(self, "indexer_version", version)
        object.__setattr__(self, "project_name", project_name)
        extra_args = tuple(str(item) for item in self.extra_args)
        managed_options = ("--cwd", "--output", "--project-name", "--project-version")
        for item in extra_args:
            if any(item == option or item.startswith(f"{option}=") for option in managed_options):
                raise ValueError(
                    f"SCIP indexer option {item!r} is managed by Claim Plane"
                )
        object.__setattr__(self, "extra_args", extra_args)
        object.__setattr__(self, "environment_fingerprint", environment_fingerprint)
        object.__setattr__(self, "environment_probe_command", environment_probe_command)
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(
            self,
            "environment",
            {str(key): str(value) for key, value in self.environment.items()},
        )


@dataclass(frozen=True, slots=True)
class ScipIndexArtifact:
    """One validated opaque ``index.scip`` artifact."""

    index_path: Path
    cache_key: str
    repository_root: Path
    revision: str
    workspace_fingerprint: str
    dirty: bool
    indexer_id: str
    indexer_version: str
    environment_fingerprint: str
    project_name: str
    project_version: str
    sha256: str
    size_bytes: int
    cache_hit: bool
    protocol: str = SCIP_INDEX_ARTIFACT_PROTOCOL

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "cache_key": self.cache_key,
            "repository_root": str(self.repository_root),
            "revision": self.revision,
            "workspace_fingerprint": self.workspace_fingerprint,
            "dirty": self.dirty,
            "indexer_id": self.indexer_id,
            "indexer_version": self.indexer_version,
            "environment_fingerprint": self.environment_fingerprint,
            "project_name": self.project_name,
            "project_version": self.project_version,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "cache_hit": self.cache_hit,
            "index_path": str(self.index_path),
        }


class ScipRevisionCache:
    """Content-validated cache for source-bound SCIP index artifacts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def entry_dir(self, cache_key: str) -> Path:
        key = str(cache_key).strip().casefold()
        if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
            raise ValueError("SCIP cache key must be a SHA-256 hex digest")
        return self.root / key[:2] / key

    def load(self, cache_key: str) -> ScipIndexArtifact | None:
        entry = self.entry_dir(cache_key)
        index_path = entry / "index.scip"
        metadata_path = entry / "metadata.json"
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            if payload.get("protocol") != SCIP_INDEX_CACHE_PROTOCOL:
                return None
            if payload.get("cache_schema") != SCIP_INDEX_CACHE_SCHEMA:
                return None
            if payload.get("cache_key") != cache_key:
                return None
            if not index_path.is_file():
                return None
            digest = _sha256_path(index_path)
            size = index_path.stat().st_size
            if digest != payload.get("sha256") or size != int(payload.get("size_bytes", -1)):
                return None
            return ScipIndexArtifact(
                index_path=index_path,
                cache_key=cache_key,
                repository_root=Path(payload["repository_root"]),
                revision=str(payload["revision"]),
                workspace_fingerprint=str(payload["workspace_fingerprint"]),
                dirty=bool(payload["dirty"]),
                indexer_id=str(payload["indexer_id"]),
                indexer_version=str(payload["indexer_version"]),
                environment_fingerprint=str(payload["environment_fingerprint"]),
                project_name=str(payload["project_name"]),
                project_version=str(payload["project_version"]),
                sha256=digest,
                size_bytes=size,
                cache_hit=True,
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def store(self, artifact: ScipIndexArtifact) -> ScipIndexArtifact:
        entry = self.entry_dir(artifact.cache_key)
        entry.mkdir(parents=True, exist_ok=True)
        index_path = entry / "index.scip"
        metadata_path = entry / "metadata.json"

        with tempfile.NamedTemporaryFile(
            prefix="index.scip.", dir=entry, delete=False
        ) as handle:
            temp_index = Path(handle.name)
        try:
            shutil.copyfile(artifact.index_path, temp_index)
            os.replace(temp_index, index_path)
        finally:
            temp_index.unlink(missing_ok=True)

        digest = _sha256_path(index_path)
        size = index_path.stat().st_size
        payload = {
            "protocol": SCIP_INDEX_CACHE_PROTOCOL,
            "cache_schema": SCIP_INDEX_CACHE_SCHEMA,
            "cache_key": artifact.cache_key,
            "repository_root": str(artifact.repository_root),
            "revision": artifact.revision,
            "workspace_fingerprint": artifact.workspace_fingerprint,
            "dirty": artifact.dirty,
            "indexer_id": artifact.indexer_id,
            "indexer_version": artifact.indexer_version,
            "environment_fingerprint": artifact.environment_fingerprint,
            "project_name": artifact.project_name,
            "project_version": artifact.project_version,
            "sha256": digest,
            "size_bytes": size,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="metadata.", suffix=".json", dir=entry,
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            temp_metadata = Path(handle.name)
        try:
            os.replace(temp_metadata, metadata_path)
        finally:
            temp_metadata.unlink(missing_ok=True)

        return replace(
            artifact,
            index_path=index_path,
            sha256=digest,
            size_bytes=size,
            cache_hit=False,
        )


class ScipIndexManager:
    """Generate SCIP indexes once per repository revision/workspace/indexer identity."""

    def __init__(self, config: ScipIndexerConfig | None = None) -> None:
        self.config = config or ScipIndexerConfig()
        self._resolved_indexer_version: str | None = self.config.indexer_version
        self._resolved_environment_fingerprint: str | None = (
            self.config.environment_fingerprint
        )

    def index_repository(
        self,
        repository_root: str | Path,
        *,
        revision: str | None = None,
        cache_root: str | Path | None = None,
        force: bool = False,
    ) -> ScipIndexArtifact:
        state = capture_scip_repository_state(repository_root, revision=revision)
        indexer_version = self._indexer_version(state.repository_root)
        environment_fingerprint = self._environment_fingerprint(state.repository_root)
        project_name = self.config.project_name or state.repository_root.name or "project"
        project_version = state.revision
        indexer_id = Path(self.config.command[0]).name
        key_payload = {
            "cache_schema": SCIP_INDEX_CACHE_SCHEMA,
            "repository_id": state.repository_id,
            "revision": state.revision,
            "workspace_fingerprint": state.workspace_fingerprint,
            "indexer_id": indexer_id,
            "indexer_version": indexer_version,
            "environment_fingerprint": environment_fingerprint,
            "environment_probe_command": list(self.config.environment_probe_command),
            "command": list(self.config.command),
            "extra_args": list(self.config.extra_args),
            "project_name": project_name,
            "project_version": project_version,
        }
        cache_key = hashlib.sha256(_canonical_json(key_payload)).hexdigest()
        cache = ScipRevisionCache(
            cache_root
            if cache_root is not None
            else _default_claim_plane_cache_root() / "code-intelligence/scip"
        )
        if not force:
            cached = cache.load(cache_key)
            if cached is not None:
                return cached

        with tempfile.TemporaryDirectory(prefix="claim-plane-scip-") as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            generated = temp_dir / "index.scip"
            command = (
                *self.config.command,
                "index",
                f"--cwd={state.repository_root}",
                f"--output={generated}",
                f"--project-name={project_name}",
                f"--project-version={project_version}",
                *self.config.extra_args,
            )
            result = self._run(
                command,
                cwd=temp_dir,
                timeout=self.config.timeout_seconds,
            )
            if result.returncode != 0:
                raise ScipIndexerFailed(
                    self._failure_message("SCIP indexer failed", command, result)
                )
            if not generated.is_file():
                raise ScipIndexerFailed(
                    "SCIP indexer completed without producing index.scip in its "
                    "working directory"
                )
            digest = _sha256_path(generated)
            size = generated.stat().st_size
            artifact = ScipIndexArtifact(
                index_path=generated,
                cache_key=cache_key,
                repository_root=state.repository_root,
                revision=state.revision,
                workspace_fingerprint=state.workspace_fingerprint,
                dirty=state.dirty,
                indexer_id=indexer_id,
                indexer_version=indexer_version,
                environment_fingerprint=environment_fingerprint,
                project_name=project_name,
                project_version=project_version,
                sha256=digest,
                size_bytes=size,
                cache_hit=False,
            )
            return cache.store(artifact)

    def _environment_fingerprint(self, repository_root: Path) -> str:
        if self._resolved_environment_fingerprint is not None:
            return self._resolved_environment_fingerprint
        command = self.config.environment_probe_command
        environment = os.environ.copy()
        environment.update(self.config.environment)
        try:
            result = subprocess.run(
                command,
                cwd=repository_root,
                env=environment,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=min(60.0, self.config.timeout_seconds),
            )
        except FileNotFoundError as exc:
            raise ScipIndexerFailed(
                f"SCIP environment probe is unavailable: {command[0]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ScipIndexerFailed(
                "SCIP environment probe timed out before cache identity could be established"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-4000:]
            raise ScipIndexerFailed(
                "SCIP environment probe failed: "
                + (detail or f"exit {result.returncode}")
            )
        lines = sorted(
            line.strip() for line in result.stdout.splitlines() if line.strip()
        )
        payload = {
            "protocol": "claim-plane.scip-environment.v1",
            "probe_command": list(command),
            "packages": lines,
            "virtual_env": environment.get("VIRTUAL_ENV"),
            "conda_prefix": environment.get("CONDA_PREFIX"),
            "python_path": environment.get("PYTHONPATH"),
        }
        fingerprint = hashlib.sha256(_canonical_json(payload)).hexdigest()
        self._resolved_environment_fingerprint = fingerprint
        return fingerprint

    def _indexer_version(self, repository_root: Path) -> str:
        if self._resolved_indexer_version is not None:
            return self._resolved_indexer_version
        command = (*self.config.command, *self.config.version_args)
        result = self._run(
            command,
            cwd=repository_root,
            timeout=min(30.0, self.config.timeout_seconds),
        )
        if result.returncode != 0:
            raise ScipIndexerFailed(
                self._failure_message("SCIP indexer version probe failed", command, result)
            )
        output = (result.stdout or result.stderr).strip()
        first_line = output.splitlines()[0].strip() if output else ""
        if not first_line:
            raise ScipIndexerFailed("SCIP indexer version probe returned no version")
        self._resolved_indexer_version = first_line
        return first_line

    def _run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(self.config.environment)
        try:
            return subprocess.run(
                tuple(command),
                cwd=cwd,
                env=environment,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise ScipIndexerUnavailable(
                f"SCIP indexer executable is unavailable: {self.config.command[0]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ScipIndexerFailed(
                f"SCIP indexer timed out after {timeout:.1f}s: {' '.join(command)}"
            ) from exc

    @staticmethod
    def _failure_message(
        prefix: str,
        command: Sequence[str],
        result: subprocess.CompletedProcess[str],
    ) -> str:
        stderr = result.stderr.strip()[-4000:]
        stdout = result.stdout.strip()[-4000:]
        detail = stderr or stdout or f"exit {result.returncode}"
        return f"{prefix}: {' '.join(command)}: {detail}"
