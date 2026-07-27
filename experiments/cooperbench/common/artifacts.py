"""Canonical artifact layout for reproducible CooperBench executions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .checkpoint import Checkpoint, CheckpointStore
from .identity import RunIdentity, build_run_identity
from .manifest import collect_run_manifest
from .models import ShardSpec, StudySpec


@dataclass(frozen=True, slots=True)
class ArtifactLayout:
    root: Path
    study_dir: Path
    run_dir: Path
    declarations_dir: Path
    plans_dir: Path
    results_dir: Path
    traces_dir: Path
    logs_dir: Path
    manifest_file: Path
    checkpoint_file: Path

    @classmethod
    def for_run(cls, root: str | Path, run: RunIdentity) -> "ArtifactLayout":
        base = Path(root)
        study_dir = base / run.study_id / run.study_fingerprint[:12]
        run_dir = study_dir / "runs" / run.run_id
        return cls(
            root=base,
            study_dir=study_dir,
            run_dir=run_dir,
            declarations_dir=run_dir / "declarations",
            plans_dir=run_dir / "plans",
            results_dir=run_dir / "results",
            traces_dir=run_dir / "traces",
            logs_dir=run_dir / "logs",
            manifest_file=run_dir / "manifest.json",
            checkpoint_file=run_dir / "checkpoint.json",
        )

    def create(self) -> None:
        for directory in (
            self.study_dir,
            self.declarations_dir,
            self.plans_dir,
            self.results_dir,
            self.traces_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _write_immutable_json(path: Path, payload: Any) -> None:
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != payload:
            raise RuntimeError(
                f"immutable artifact already exists with different content: {path}"
            )
        return
    _atomic_json(path, payload)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_run(
    study: StudySpec,
    *,
    coder_seed: int,
    artifact_root: str | Path = ".claim-plane/experiments",
    shard: ShardSpec | None = None,
    repo_root: str | Path = ".",
) -> tuple[RunIdentity, ArtifactLayout]:
    """Create a deterministic run directory and its initial provenance records."""

    run = build_run_identity(study, coder_seed=coder_seed, shard=shard)
    layout = ArtifactLayout.for_run(artifact_root, run)
    layout.create()

    _write_immutable_json(layout.study_dir / "study.json", study.to_dict())
    _write_immutable_json(
        layout.study_dir / "pairs.json", [pair.to_dict() for pair in study.pairs]
    )

    if layout.manifest_file.exists():
        manifest = json.loads(layout.manifest_file.read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("run", {}).get("run_id") != run.run_id
        ):
            raise RuntimeError("existing manifest belongs to a different run")
    else:
        _atomic_json(
            layout.manifest_file,
            collect_run_manifest(study, run, repo_root=repo_root).to_dict(),
        )

    store = CheckpointStore(layout.checkpoint_file)
    if store.path.exists():
        existing = store.load()
        if existing.run_id != run.run_id:
            raise RuntimeError("existing checkpoint belongs to a different run")
    else:
        store.save(Checkpoint(run_id=run.run_id))

    return run, layout
