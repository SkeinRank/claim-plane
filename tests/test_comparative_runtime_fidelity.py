from __future__ import annotations

import subprocess
from pathlib import Path

from claim_plane import validation
from claim_plane.connectors.codex_guard import classify_tool_call
from claim_plane.dogfood import DogfoodArm, load_dogfood_plan
from claim_plane.oss_pilot import oss_pilot_command
from claim_plane.validation_environment import (
    _dependency_prefix,
    activate_task_environment,
    prepare_task_environment,
)


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
        feature = task / "feature1"
        feature.mkdir()
        (feature / "feature.md").write_text("Update value.", encoding="utf-8")
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


def _classify(root: Path, command: str) -> str:
    classification, mutations = classify_tool_call(
        root,
        {
            "tool_name": "exec_command",
            "tool_input": {"command": command},
        },
    )
    assert mutations == ()
    return classification


def test_remaining_real_world_inspection_surfaces_are_bounded(tmp_path: Path) -> None:
    source = tmp_path / "src.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")

    assert _classify(tmp_path, 'rg -n "require_decoding|class Features" src.py') == (
        "read_only"
    )
    assert _classify(tmp_path, "git remote -v") == "read_only"
    assert _classify(tmp_path, "git remote get-url origin") == "read_only"
    assert (
        _classify(
            tmp_path,
            "PYTHONDONTWRITEBYTECODE=1 python -m py_compile src.py",
        )
        == "test_feedback"
    )

    classification, mutations = classify_tool_call(
        tmp_path,
        {"tool_name": "webrun", "tool_input": {"query": "project docs"}},
    )
    assert classification == "read_only"
    assert mutations == ()


def test_dependency_prefix_keeps_test_dependency_installs_but_stops_before_tests(
    tmp_path: Path,
) -> None:
    runner = tmp_path / "run_tests.sh"
    runner.write_text(
        """#!/usr/bin/env bash
set -e
trap cleanup EXIT INT TERM
uv pip install pytest pytest-xdist
echo RUNNING_TESTS...
pytest -q tests/features/test_features.py
""",
        encoding="utf-8",
    )

    prefix = _dependency_prefix(runner)

    assert prefix is not None
    assert "uv pip install pytest pytest-xdist" in prefix
    assert "RUNNING_TESTS" not in prefix
    assert "pytest -q" not in prefix
    assert "trap cleanup" not in prefix
    assert "CLAIM_PLANE_DEPENDENCIES_READY" in prefix


def test_prefetch_passes_valid_nonempty_patch_arguments(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    runner = task_dir / "run_tests.sh"
    runner.write_text(
        """#!/usr/bin/env bash
set -e
REPO_PATH="$1"
TEST_PATCH="$2"
FEATURE_PATCH="$3"
cd "$REPO_PATH"
echo APPLYING_FEATURE_PATCH
git apply "$FEATURE_PATCH"
git apply "$TEST_PATCH"
python -m venv .venv
echo RUNNING_TESTS...
exit 91
""",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    task = {
        "task_id": "empty-feature-patch-regression",
        "clone_url": f"file://{repo}",
        "base_commit": base,
        "task_dir": str(task_dir),
    }

    prepared = prepare_task_environment(
        validation_root=tmp_path / "validation",
        task=task,
        source_revision="fixture-revision",
        stream_output=False,
    )

    assert prepared["cache_hit"] is False
    assert Path(prepared["python"]).is_file()
    prepared_repo = Path(prepared["repository"])
    assert not (prepared_repo / ".claim-plane-feature-marker").exists()
    assert not (prepared_repo / ".claim-plane-evaluator-marker").exists()


def test_task_environment_is_reused_and_activated_for_each_arm(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path / "repo")
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    runner = task_dir / "run_tests.sh"
    runner.write_text(
        """#!/usr/bin/env bash
set -e
REPO_PATH="$1"
cd "$REPO_PATH"
python -m venv .venv
echo RUNNING_TESTS...
exit 91
""",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    task = {
        "task_id": "fixture-task",
        "clone_url": f"file://{repo}",
        "base_commit": base,
        "task_dir": str(task_dir),
    }
    validation_root = tmp_path / "validation"

    first = prepare_task_environment(
        validation_root=validation_root,
        task=task,
        source_revision="fixture-revision",
        stream_output=False,
    )
    second = prepare_task_environment(
        validation_root=validation_root,
        task=task,
        source_revision="fixture-revision",
        stream_output=False,
    )

    assert first["identity_digest"] == second["identity_digest"]
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    env = activate_task_environment(second, workspace=repo)
    assert env["VIRTUAL_ENV"] == second["venv"]
    assert env["CLAIM_PLANE_VALIDATION_ENVIRONMENT"] == second["identity_digest"]
    assert Path(second["python"]).is_file()


def test_observe_and_guarded_commands_defer_internal_acceptance() -> None:
    manifest = {
        "workspace": "/tmp/workspace",
        "prompt": "Update value.",
        "arm": "guarded",
        "initial_scope": ["src/value.py"],
    }

    deferred = oss_pilot_command(
        manifest,
        model="gpt-test",
        acceptance_timeout=300,
        defer_acceptance=True,
    )
    regular = oss_pilot_command(
        manifest,
        model="gpt-test",
        acceptance_timeout=300,
        defer_acceptance=False,
    )
    bare = oss_pilot_command(
        {**manifest, "arm": "bare"},
        model="gpt-test",
        defer_acceptance=True,
    )

    assert "--defer-acceptance" in deferred
    assert "--defer-acceptance" not in regular
    assert "--defer-acceptance" not in bare


def test_validation_dry_run_declares_one_authoritative_acceptance(
    tmp_path: Path,
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
    observe = next(entry for entry in plan.entries if entry.arm is DogfoodArm.OBSERVE)
    guarded = next(entry for entry in plan.entries if entry.arm is DogfoodArm.GUARDED)
    bare = next(entry for entry in plan.entries if entry.arm is DogfoodArm.BARE_CODEX)

    observe_payload = validation.run_validation_execution(
        observe.execution_id, root=root, dry_run=True, progress=False
    )
    guarded_payload = validation.run_validation_execution(
        guarded.execution_id, root=root, dry_run=True, progress=False
    )
    bare_payload = validation.run_validation_execution(
        bare.execution_id, root=root, dry_run=True, progress=False
    )

    assert observe_payload["authoritative_acceptance_runs"] == 1
    assert guarded_payload["authoritative_acceptance_runs"] == 1
    assert observe_payload["internal_acceptance_deferred"] is True
    assert guarded_payload["internal_acceptance_deferred"] is True
    assert "--defer-acceptance" in observe_payload["command"]
    assert "--defer-acceptance" in guarded_payload["command"]
    assert bare_payload["internal_acceptance_deferred"] is False
    assert "--defer-acceptance" not in bare_payload["command"]


def test_reset_task_removes_all_three_diagnostic_cells_but_keeps_cache(
    tmp_path: Path,
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
    task_id = plan.entries[0].task_id
    entries = [entry for entry in plan.entries if entry.task_id == task_id]
    assert len(entries) == 3
    for entry in entries:
        (root / "results" / f"{entry.execution_id}.json").write_text(
            "{}\n", encoding="utf-8"
        )
        workspace = validation.validation_workspace(root, entry.execution_id)
        workspace.mkdir(parents=True)
    cache_marker = root / "cache" / "uv" / "fixture"
    cache_marker.parent.mkdir(parents=True, exist_ok=True)
    cache_marker.write_text("cached\n", encoding="utf-8")

    payload = validation.reset_validation_task(task_id, root=root)

    assert payload["removed_results"] == 3
    assert payload["removed_workspaces"] == 3
    assert cache_marker.exists()
    assert all(
        not (root / "results" / f"{entry.execution_id}.json").exists()
        for entry in entries
    )


def test_codex_environment_overrides_pin_non_login_shell_and_runtime() -> None:
    from claim_plane.validation_environment import codex_environment_config_overrides

    env = {
        "PATH": "/tmp/task/.venv/bin:/usr/bin",
        "VIRTUAL_ENV": "/tmp/task/.venv",
        "UV_CACHE_DIR": "/tmp/cache",
        "UV_PROJECT_ENVIRONMENT": "/tmp/task/.venv",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "/tmp/workspace/src",
        "CLAIM_PLANE_VALIDATION_ENVIRONMENT": "digest",
        "CLAIM_PLANE_VALIDATION_PYTHON": "/tmp/task/.venv/bin/python",
    }

    overrides = codex_environment_config_overrides(env)

    assert "allow_login_shell=false" in overrides
    assert 'shell_environment_policy.inherit="all"' in overrides
    assert (
        'shell_environment_policy.set.PATH="/tmp/task/.venv/bin:/usr/bin"' in overrides
    )
    assert 'shell_environment_policy.set.VIRTUAL_ENV="/tmp/task/.venv"' in overrides
    assert (
        "shell_environment_policy.set.CLAIM_PLANE_VALIDATION_PYTHON="
        '"/tmp/task/.venv/bin/python"' in overrides
    )


def test_oss_pilot_commands_forward_codex_environment_config() -> None:
    manifest = {
        "workspace": "/tmp/workspace",
        "prompt": "Update value.",
        "arm": "guarded",
        "initial_scope": ["src/value.py"],
    }
    overrides = (
        "allow_login_shell=false",
        'shell_environment_policy.set.PATH="/tmp/venv/bin:/usr/bin"',
    )

    controlled = oss_pilot_command(
        manifest,
        model="gpt-test",
        defer_acceptance=True,
        codex_config=overrides,
    )
    bare = oss_pilot_command(
        {**manifest, "arm": "bare"},
        model="gpt-test",
        codex_config=overrides,
    )

    assert controlled.count("--codex-config") == len(overrides)
    for override in overrides:
        assert override in controlled
        index = bare.index(override)
        assert bare[index - 1] == "-c"
    assert bare[-1] == "Update value."


def test_environment_preflight_uses_exact_python_and_detects_test_imports(
    tmp_path: Path,
) -> None:
    import os
    import sys

    from claim_plane.validation_environment import preflight_task_environment

    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests" / "conftest.py").write_text(
        "import json\nimport pytest\n", encoding="utf-8"
    )
    env = os.environ.copy()
    env["VIRTUAL_ENV"] = sys.prefix
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    environment = {
        "python": sys.executable,
        "venv": sys.prefix,
    }

    payload = preflight_task_environment(
        environment,
        workspace=workspace,
        env=env,
    )

    assert Path(payload["python"]).resolve() == Path(sys.executable).resolve()
    assert payload["pytest_available"] is True
    assert "pytest" in payload["test_modules_available"]


def test_environment_preflight_fails_before_codex_when_test_import_is_missing(
    tmp_path: Path,
) -> None:
    import os
    import sys

    import pytest

    from claim_plane.validation_environment import (
        ValidationEnvironmentError,
        preflight_task_environment,
    )

    workspace = tmp_path / "workspace"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests" / "conftest.py").write_text(
        "import claim_plane_dependency_that_does_not_exist\n", encoding="utf-8"
    )
    env = os.environ.copy()
    env["VIRTUAL_ENV"] = sys.prefix
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")

    with pytest.raises(ValidationEnvironmentError, match="missing test imports"):
        preflight_task_environment(
            {"python": sys.executable, "venv": sys.prefix},
            workspace=workspace,
            env=env,
        )


def test_claim_plane_codex_parser_accepts_repeated_one_run_config() -> None:
    from claim_plane.cli import build_parser

    args = build_parser().parse_args(
        [
            "codex",
            "Update value.",
            "--codex-config",
            "allow_login_shell=false",
            "--codex-config",
            'shell_environment_policy.set.PATH="/tmp/venv/bin:/usr/bin"',
        ]
    )

    assert args.codex_config == [
        "allow_login_shell=false",
        'shell_environment_policy.set.PATH="/tmp/venv/bin:/usr/bin"',
    ]


def test_validation_environment_removes_python_launcher_redirects(
    tmp_path: Path,
) -> None:
    import os
    import sys

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = {
        "venv": sys.prefix,
        "python": sys.executable,
        "cache_dir": str(tmp_path / "cache"),
        "identity_digest": "fixture-environment",
    }
    original = {
        "__PYVENV_LAUNCHER__": os.environ.get("__PYVENV_LAUNCHER__"),
        "PYTHONEXECUTABLE": os.environ.get("PYTHONEXECUTABLE"),
        "PYTHONHOME": os.environ.get("PYTHONHOME"),
    }
    try:
        os.environ["__PYVENV_LAUNCHER__"] = "/tmp/wrong-python"
        os.environ["PYTHONEXECUTABLE"] = "/tmp/wrong-python"
        os.environ["PYTHONHOME"] = "/tmp/wrong-home"
        env = activate_task_environment(environment, workspace=workspace)
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert "__PYVENV_LAUNCHER__" not in env
    assert "PYTHONEXECUTABLE" not in env
    assert "PYTHONHOME" not in env
    assert env["VIRTUAL_ENV"] == sys.prefix


def test_environment_preflight_reports_exact_virtual_environment_prefix(
    tmp_path: Path,
) -> None:
    import os
    import sys

    from claim_plane.validation_environment import preflight_task_environment

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = os.environ.copy()
    env["VIRTUAL_ENV"] = sys.prefix
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["__PYVENV_LAUNCHER__"] = "/tmp/claim-plane-parent-python"

    payload = preflight_task_environment(
        {"python": sys.executable, "venv": sys.prefix},
        workspace=workspace,
        env=env,
    )

    assert Path(payload["prefix"]).resolve() == Path(sys.prefix).resolve()
    assert payload["pytest_available"] is True
    assert payload["pytest_version"]


def test_dependency_digest_ignores_seed_and_editable_candidate_packages(
    tmp_path: Path,
) -> None:
    import json
    import sys

    from claim_plane.validation_environment import _dependency_digest

    venv = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        check=True,
        text=True,
        capture_output=True,
    )
    python_path = venv / "bin" / "python"
    site_query = subprocess.run(
        [
            str(python_path),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    site_packages = Path(site_query.stdout.strip())
    dependency = site_packages / "fixture_dependency-1.0.dist-info"
    dependency.mkdir()
    (dependency / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: fixture-dependency\nVersion: 1.0\n",
        encoding="utf-8",
    )
    project = site_packages / "fixture_project-1.0.dist-info"
    project.mkdir()
    (project / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: fixture-project\nVersion: 1.0\n",
        encoding="utf-8",
    )
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    (project / "direct_url.json").write_text(
        json.dumps({"url": seed_root.as_uri(), "dir_info": {"editable": False}}),
        encoding="utf-8",
    )

    seed_digest = _dependency_digest(python_path, project_root=seed_root)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (project / "direct_url.json").write_text(
        json.dumps({"url": workspace.as_uri(), "dir_info": {"editable": True}}),
        encoding="utf-8",
    )
    editable_digest = _dependency_digest(python_path, project_root=seed_root)

    assert editable_digest == seed_digest
