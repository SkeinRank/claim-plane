from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

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
        feature = task / "feature1"
        feature.mkdir(parents=True)
        (task / "setup.sh").write_text(
            f'BASE_COMMIT="{base}"\ngit clone file://{repo} workspace\n',
            encoding="utf-8",
        )
        (task / "run_tests.sh").write_text(
            "#!/usr/bin/env bash\nset -eu\necho RUNNING_TESTS...\nexit 0\n",
            encoding="utf-8",
        )
        (task / "run_tests.sh").chmod(0o755)
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
        (feature / "tests.patch").write_text(
            """diff --git a/tests/test_hidden.py b/tests/test_hidden.py
new file mode 100644
--- /dev/null
+++ b/tests/test_hidden.py
@@ -0,0 +1 @@
+assert True
""",
            encoding="utf-8",
        )
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


def test_validation_state_and_workspace_do_not_expose_evaluator_paths(
    tmp_path: Path,
) -> None:
    root, bare = _initialized(tmp_path)
    state = json.loads((root / "validation.json").read_text(encoding="utf-8"))
    assert "source_root" not in state
    assert state["benchmark_isolation"] == "private-evaluator-vault"

    manifest = validation.prepare_validation_execution(bare.execution_id, root=root)
    workspace = Path(manifest["workspace"])
    assert root not in workspace.parents
    serialized = json.dumps(manifest)
    assert "task_dir" not in serialized
    assert "feature_dir" not in serialized
    assert "tests.patch" not in serialized
    assert "feature.patch" not in serialized
    assert manifest["acceptance"] == (
        "python -m claim_plane.oss_pilot_acceptance --repo ."
    )
    status = validation.validation_status(root)
    assert root not in Path(status["environment_cache"]).parents


def test_private_evaluator_assets_are_supplied_only_to_outer_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bare = _initialized(tmp_path)
    manifest = validation.prepare_validation_execution(bare.execution_id, root=root)
    workspace = Path(manifest["workspace"])
    (workspace / "src" / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_acceptance(*args: Any, **kwargs: Any) -> int:
        captured.update(kwargs)
        acceptance = workspace / ".claim-plane" / "oss-pilot" / "acceptance"
        acceptance.mkdir(parents=True, exist_ok=True)
        (acceptance / "latest.json").write_text(
            json.dumps({"classification": "PASS"}), encoding="utf-8"
        )
        return 0

    monkeypatch.setattr(validation, "run_oss_pilot_acceptance", fake_acceptance)

    class Result:
        def to_dict(self) -> dict[str, object]:
            return {"evaluation_complete": True, "task_success": True}

    monkeypatch.setattr(
        validation, "collect_validation_execution", lambda *a, **k: Result()
    )
    payload = validation.resume_validation_execution(
        bare.execution_id,
        root=root,
        agent_wall_time_seconds=1,
        progress=False,
    )

    assert payload["result"]["task_success"] is True
    assert Path(captured["runner_path"]).name == "run_tests.sh"
    assert Path(captured["tests_patch_path"]).name == "tests.patch"
    assert str(captured["runner_path"]) not in json.dumps(manifest)


def test_contamination_audit_detects_reference_patch_access(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    session_root = workspace / ".claim-plane" / "runs"
    session_root.mkdir(parents=True)
    before = validation._audit_snapshot((session_root,))
    transcript = session_root / "events.ndjson"
    transcript.write_text(
        '{"message":"Explored: Read feature.patch, tests.patch"}\n',
        encoding="utf-8",
    )

    audit = validation._scan_contamination(
        workspace,
        before=before,
        forbidden_paths=(tmp_path / "vault" / "tests.patch",),
    )

    assert audit["contaminated"] is True
    assert audit["matches"]


def test_frozen_plan_uses_path_resolved_python_not_host_interpreter(
    tmp_path: Path,
) -> None:
    root, _ = _initialized(tmp_path)
    plan = load_dogfood_plan(root / "plan.json")
    assert all(
        entry.acceptance == ("python -m claim_plane.oss_pilot_acceptance --repo .",)
        for entry in plan.entries
    )


def test_legacy_managed_source_is_vaulted_and_removed(tmp_path: Path) -> None:
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
    state_path = root / "validation.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    vault = validation._vault_root(root, state)
    shutil.rmtree(vault)
    managed_source = root / "_source" / "CooperBench"
    managed_source.parent.mkdir(parents=True)
    shutil.copytree(source, managed_source)
    unsigned = {
        key: value
        for key, value in state.items()
        if key
        not in {
            "digest",
            "benchmark_isolation",
            "evaluator_vault_id",
        }
    }
    unsigned["source_root"] = str(managed_source)
    legacy_state = {**unsigned, "digest": validation._digest(unsigned)}
    validation._write_json(state_path, legacy_state)

    migrated_vault, migrated_state = validation._ensure_benchmark_isolation(root)

    assert migrated_vault.exists()
    assert not (root / "_source").exists()
    assert "source_root" not in migrated_state
    assert migrated_state["benchmark_isolation"] == "private-evaluator-vault"


def test_comparative_codex_session_disables_web_and_shell_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bare = _initialized(tmp_path)
    validation.prepare_validation_execution(bare.execution_id, root=root)
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        validation,
        "prepare_validation_environment",
        lambda *a, **k: {
            "identity_digest": "env",
            "cache_hit": True,
            "python": "/tmp/venv/bin/python",
            "cache_dir": "/tmp/cache",
        },
    )
    monkeypatch.setattr(
        validation,
        "activate_task_environment",
        lambda *a, **k: {"PATH": "/tmp/venv/bin", "VIRTUAL_ENV": "/tmp/venv"},
    )
    monkeypatch.setattr(
        validation,
        "preflight_task_environment",
        lambda *a, **k: {
            "python": "/tmp/venv/bin/python",
            "pytest_available": True,
            "test_modules_available": [],
        },
    )
    monkeypatch.setattr(
        validation,
        "codex_environment_config_overrides",
        lambda env: ("allow_login_shell=false",),
    )

    def fake_command(*args: Any, **kwargs: Any) -> tuple[str, ...]:
        captured["codex_config"] = kwargs.get("codex_config")
        return ("/usr/bin/true",)

    monkeypatch.setattr(validation, "oss_pilot_command", fake_command)
    monkeypatch.setattr(
        validation,
        "_scan_contamination",
        lambda *a, **k: {
            "protocol": validation.VALIDATION_CONTAMINATION_PROTOCOL,
            "contaminated": False,
            "checked_at": "now",
            "matches": [],
        },
    )
    monkeypatch.setattr(
        validation,
        "_run_acceptance_for_execution",
        lambda *a, **k: {"result": {"evaluation_complete": True, "task_success": True}},
    )

    payload = validation.run_validation_execution(
        bare.execution_id,
        root=root,
        progress=False,
    )

    assert payload["result"]["task_success"] is True
    assert 'web_search="disabled"' in captured["codex_config"]
    assert "sandbox_workspace_write.network_access=false" in captured["codex_config"]


def test_contaminated_acceptance_is_recorded_without_task_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, bare = _initialized(tmp_path)
    manifest = validation.prepare_validation_execution(bare.execution_id, root=root)
    workspace = Path(manifest["workspace"])
    (workspace / "src" / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
    acceptance = workspace / ".claim-plane" / "oss-pilot" / "acceptance"
    acceptance.mkdir(parents=True)
    (acceptance / "latest.json").write_text(
        json.dumps(
            {
                "classification": "CONTAMINATED",
                "evidence_digest": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(validation, "_latest_report", lambda workspace: None)

    result = validation.collect_validation_execution(
        bare.execution_id,
        root=root,
        wall_time_seconds=2.0,
        runtime_returncode=0,
    )

    assert result.outcome == "CONTAMINATED"
    assert result.evaluation_complete is True
    assert result.task_success is False
    assert result.accepted_delivery is False
