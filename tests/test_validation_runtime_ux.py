from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from claim_plane import validation
from claim_plane.dogfood import DogfoodArm, load_dogfood_plan
from claim_plane.runtime_progress import run_streaming_process


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def _repo(root: Path) -> tuple[Path, str]:
    root.mkdir(parents=True)
    _git(root, "init")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "src").mkdir()
    (root / "src" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _cooperbench(tmp_path: Path, repo: Path, base: str) -> Path:
    source = tmp_path / "CooperBench"
    families = (
        "pallets_jinja_task",
        "pallets_click_task",
        "samuelcolvin_dirty_equals_task",
        "dspy_task",
        "llama_index_task",
        "pillow_task",
    )
    for index, family in enumerate(families, start=1):
        task = source / "dataset" / family / f"task{index}"
        task.mkdir(parents=True)
        (task / "setup.sh").write_text(
            f'BASE_COMMIT="{base}"\ngit clone file://{repo} workspace\n',
            encoding="utf-8",
        )
        (task / "run_tests.sh").write_text(
            "#!/usr/bin/env bash\nset -eu\nexit 0\n", encoding="utf-8"
        )
        (task / "run_tests.sh").chmod(0o755)
        feature = task / "feature1"
        feature.mkdir()
        (feature / "feature.md").write_text("Update value.", encoding="utf-8")
        (feature / "feature.patch").write_text(
            """diff --git a/src/value.py b/src/value.py
--- a/src/value.py
+++ b/src/value.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
""",
            encoding="utf-8",
        )
        (feature / "tests.patch").write_text("", encoding="utf-8")
    _git(source, "init")
    _git(source, "config", "user.name", "Test")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "dataset")
    return source


def _initialized(tmp_path: Path) -> tuple[Path, Any]:
    repo, base = _repo(tmp_path / "repo")
    source = _cooperbench(tmp_path, repo, base)
    root = tmp_path / "validation"
    validation.initialize_validation(
        root=root,
        profile="preview",
        cooperbench=source,
        task_count=6,
        minimum_repositories=6,
        allow_source_drift=True,
    )
    plan = load_dogfood_plan(root / "plan.json")
    bare = next(entry for entry in plan.entries if entry.arm is DogfoodArm.BARE_CODEX)
    return root, bare


def test_preview_dry_run_defaults_to_five_minute_acceptance(tmp_path: Path) -> None:
    root, bare = _initialized(tmp_path)

    payload = validation.run_validation_execution(
        bare.execution_id, root=root, dry_run=True, progress=False
    )

    assert payload["acceptance_timeout_seconds"] == 300.0
    assert payload["position"] >= 1
    assert payload["execution_count"] == 18


def test_legacy_candidate_is_resumable_without_rerunning_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bare = _initialized(tmp_path)
    manifest = validation.prepare_validation_execution(bare.execution_id, root=root)
    workspace = Path(manifest["workspace"])
    (workspace / "src" / "value.py").write_text("VALUE = 2\n", encoding="utf-8")

    status = validation.validation_status(root)
    assert status["active_execution"]["phase"] == "LEGACY_CANDIDATE"

    monkeypatch.setattr(validation, "run_oss_pilot_acceptance", lambda *a, **k: 0)
    monkeypatch.setattr(
        validation,
        "latest_oss_pilot_reverification",
        lambda workspace: {"classification": "PASS"},
    )

    class Result:
        def to_dict(self) -> dict[str, object]:
            return {"evaluation_complete": True, "task_success": True}

    monkeypatch.setattr(
        validation, "collect_validation_execution", lambda *a, **k: Result()
    )
    payload = validation.resume_validation_execution(
        bare.execution_id,
        root=root,
        agent_wall_time_seconds=178,
        progress=False,
    )

    assert payload["interrupted"] is False
    assert payload["result"]["task_success"] is True
    metadata = json.loads(
        (workspace / ".claim-plane" / "validation-execution.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["phase"] == "COMPLETED"
    assert metadata["agent_wall_time_seconds"] == 178.0


def test_streaming_process_emits_live_output_and_heartbeat(tmp_path: Path) -> None:
    output: list[tuple[str, str]] = []
    heartbeats: list[float] = []
    result = run_streaming_process(
        (
            sys.executable,
            "-c",
            "import time; print('started', flush=True); time.sleep(0.2); print('done')",
        ),
        cwd=tmp_path,
        timeout=2,
        heartbeat_seconds=0.05,
        on_output=lambda stream, line: output.append((stream, line)),
        on_heartbeat=heartbeats.append,
    )

    assert result.returncode == 0
    assert "started" in result.stdout
    assert "done" in result.stdout
    assert any("started" in line for _, line in output)
    assert heartbeats


def test_interrupted_acceptance_preserves_resumable_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bare = _initialized(tmp_path)
    manifest = validation.prepare_validation_execution(bare.execution_id, root=root)
    workspace = Path(manifest["workspace"])
    (workspace / "src" / "value.py").write_text("VALUE = 2\n", encoding="utf-8")

    monkeypatch.setattr(validation, "run_oss_pilot_acceptance", lambda *a, **k: 130)
    monkeypatch.setattr(
        validation,
        "latest_oss_pilot_reverification",
        lambda workspace: {"classification": "INTERRUPTED"},
    )

    payload = validation.resume_validation_execution(
        bare.execution_id,
        root=root,
        agent_wall_time_seconds=20,
        progress=False,
    )

    assert payload["interrupted"] is True
    metadata = json.loads(
        (workspace / ".claim-plane" / "validation-execution.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["phase"] == "INTERRUPTED"
    assert not (root / "results" / f"{bare.execution_id}.json").exists()


def test_timeout_remains_resumable_and_does_not_complete_matrix_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bare = _initialized(tmp_path)
    manifest = validation.prepare_validation_execution(bare.execution_id, root=root)
    workspace = Path(manifest["workspace"])
    (workspace / "src" / "value.py").write_text("VALUE = 2\n", encoding="utf-8")

    monkeypatch.setattr(validation, "run_oss_pilot_acceptance", lambda *a, **k: 124)
    monkeypatch.setattr(
        validation,
        "latest_oss_pilot_reverification",
        lambda workspace: {"classification": "TIMEOUT"},
    )

    payload = validation.resume_validation_execution(
        bare.execution_id,
        root=root,
        agent_wall_time_seconds=20,
        progress=False,
    )

    assert payload["retryable"] is True
    metadata = json.loads(
        (workspace / ".claim-plane" / "validation-execution.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["phase"] == "RETRYABLE_ERROR"
    assert not (root / "results" / f"{bare.execution_id}.json").exists()
    assert validation.validation_status(root)["active_execution"]["phase"] == (
        "RETRYABLE_ERROR"
    )
