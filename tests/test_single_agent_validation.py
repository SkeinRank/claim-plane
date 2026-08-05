from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claim_plane import validation
from claim_plane.dogfood import DogfoodArm, load_dogfood_plan


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


def _feature_patch(path: str) -> str:
    return f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""


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
        for feature_number in (1, 2):
            feature = task / f"feature{feature_number}"
            feature.mkdir()
            (feature / "feature.md").write_text(
                f"Update value for {family} feature {feature_number}.",
                encoding="utf-8",
            )
            (feature / "feature.patch").write_text(
                _feature_patch("src/value.py"), encoding="utf-8"
            )
            (feature / "tests.patch").write_text("", encoding="utf-8")
    _git(source, "init")
    _git(source, "config", "user.name", "Test")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "dataset")
    return source


def test_discovery_and_selection_are_repository_diverse(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    source = _cooperbench(tmp_path, repo, base)
    catalog = validation.discover_validation_catalog(source)
    selected = validation.select_validation_tasks(
        catalog,
        task_count=6,
        minimum_repositories=6,
        selection_seed=42,
    )

    assert len(catalog) == 12
    assert len(selected) == 6
    assert len({item["repository_family"] for item in selected}) == 6
    assert selected == validation.select_validation_tasks(
        catalog,
        task_count=6,
        minimum_repositories=6,
        selection_seed=42,
    )


def test_initialize_freezes_three_arm_matrix_without_local_paths(
    tmp_path: Path,
) -> None:
    repo, base = _repo(tmp_path / "repo")
    source = _cooperbench(tmp_path, repo, base)
    root = tmp_path / "validation"

    payload = validation.initialize_validation(
        root=root,
        profile="preview",
        cooperbench=source,
        task_count=6,
        minimum_repositories=6,
        allow_source_drift=True,
    )

    assert payload["task_count"] == 6
    assert payload["execution_count"] == 18
    selection = json.loads((root / "selection.json").read_text(encoding="utf-8"))
    serialized = json.dumps(selection)
    assert '"task_dir"' not in serialized
    assert '"feature_dir"' not in serialized
    assert '"root"' not in json.dumps(selection["source"])
    plan = load_dogfood_plan(root / "plan.json")
    assert {entry.arm for entry in plan.entries} == {
        DogfoodArm.BARE_CODEX,
        DogfoodArm.OBSERVE,
        DogfoodArm.GUARDED,
    }
    status = validation.validation_status(root)
    assert status["pending_count"] == 18
    assert status["next_execution"] is not None


def test_prepare_execution_uses_exact_base_and_arm_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    guarded = next(entry for entry in plan.entries if entry.arm is DogfoodArm.GUARDED)

    monkeypatch.setattr(validation, "connect_codex", lambda path: {"root": str(path)})

    class Registry:
        def pin(self, *args: object, **kwargs: object) -> tuple[object, Path]:
            return object(), tmp_path / "pin.json"

    monkeypatch.setattr(validation, "build_adapter_registry", Registry)
    manifest = validation.prepare_validation_execution(guarded.execution_id, root=root)
    workspace = Path(manifest["workspace"])

    assert _git(workspace, "rev-parse", "HEAD") == base
    assert manifest["arm"] == "guarded"
    assert manifest["validation"]["execution_id"] == guarded.execution_id
    assert manifest["initial_scope"] == ["src/value.py"]
    config = (workspace / ".claim-plane" / "config.yaml").read_text(encoding="utf-8")
    assert 'policy: "guarded"' in config


def test_collect_binds_measured_acceptance_to_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    manifest = validation.prepare_validation_execution(bare.execution_id, root=root)
    workspace = Path(manifest["workspace"])
    (workspace / "src" / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
    acceptance_dir = workspace / ".claim-plane" / "oss-pilot" / "acceptance"
    acceptance_dir.mkdir(parents=True)
    (acceptance_dir / "latest.json").write_text(
        json.dumps(
            {
                "classification": "PASS",
                "evidence_digest": "a" * 64,
                "candidate": {"digest": "b" * 64},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(validation, "_latest_report", lambda workspace: None)

    result = validation.collect_validation_execution(
        bare.execution_id,
        root=root,
        wall_time_seconds=12.5,
        runtime_returncode=0,
    )

    assert result.execution_id == bare.execution_id
    assert result.task_success is True
    assert result.accepted_delivery is True
    assert result.files_changed == 1
    assert result.wall_time_seconds == 12.5
    assert (root / "results" / f"{bare.execution_id}.json").exists()


def test_report_and_bundle_preserve_incomplete_matrix(tmp_path: Path) -> None:
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

    report = validation.build_validation_report(root)
    assert report["summary"]["completeness"]["complete"] is False
    assert report["gate"]["status"] == "INCOMPLETE"
    bundle = validation.build_validation_bundle(tmp_path / "bundle.zip", root=root)
    assert Path(bundle["output"]).exists()
    assert len(bundle["sha256"]) == 64
    with validation.zipfile.ZipFile(bundle["output"]) as archive:
        assert "bundle.json" in archive.namelist()
        assert "summary.md" in archive.namelist()


def test_validation_gate_blocks_guarded_friction_and_missing_evidence() -> None:
    summary = {
        "protocol": "claim-plane.dogfood-summary.v1",
        "digest": "a" * 64,
        "completeness": {"complete": True},
        "arms": {
            "bare-codex": {
                "task_success_rate": 1.0,
                "accepted_delivery_rate": 1.0,
            },
            "claim-plane-observe": {
                "task_success_rate": 1.0,
                "accepted_delivery_rate": 1.0,
            },
            "claim-plane-guarded": {
                "evaluated_count": 2,
                "task_success_rate": 1.0,
                "accepted_delivery_rate": 1.0,
                "missed_mutations": 0,
                "false_blocks": 1,
            },
        },
    }
    result = validation.evaluate_validation_release_gate(summary, ())

    assert result["status"] == "BLOCKED"
    assert result["release_allowed"] is False
    assert any(
        item["code"] == "guarded_friction_above_threshold"
        for item in result["findings"]
    )
