from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claim_plane import controlled_run, oss_pilot


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def _init_repo(root: Path) -> str:
    root.mkdir(parents=True)
    _git(root, "init")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "src" / "jinja2").mkdir(parents=True)
    (root / "src" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "src" / "jinja2" / "loaders.py").write_text(
        "LOADER = 1\n", encoding="utf-8"
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    return _git(root, "rev-parse", "HEAD")


def _cooperbench_fixture(tmp_path: Path, repo: Path, base: str) -> Path:
    source = tmp_path / "CooperBench"
    task = source / "dataset" / "pallets_jinja_task" / "task1621"
    feature = task / "feature1"
    feature.mkdir(parents=True)
    (task / "setup.sh").write_text(
        f'BASE_COMMIT="{base}"\ngit clone file://{repo} workspace\n',
        encoding="utf-8",
    )
    (task / "run_tests.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'test "$(basename "$(dirname "$1")")" = agent_workspace\n'
        'git -C "$1" apply "$2"\n'
        "grep -q 'VALUE = 2' \"$1/src/value.py\"\n"
        'test -f "$1/tests/hidden.txt"\n',
        encoding="utf-8",
    )
    (task / "run_tests.sh").chmod(0o755)
    (feature / "feature.md").write_text("Change VALUE from 1 to 2.", encoding="utf-8")
    (feature / "feature.patch").write_text("unused\n", encoding="utf-8")
    (feature / "tests.patch").write_text(
        """diff --git a/tests/hidden.txt b/tests/hidden.txt
new file mode 100644
--- /dev/null
+++ b/tests/hidden.txt
@@ -0,0 +1 @@
+hidden acceptance
""",
        encoding="utf-8",
    )
    _git(source, "init")
    _git(source, "config", "user.name", "Test")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "dataset")
    return source


def test_frozen_selection_has_three_distinct_tasks() -> None:
    payload = oss_pilot.oss_pilot_selection()
    assert payload["protocol"] == oss_pilot.OSS_PILOT_SELECTION_PROTOCOL
    assert len(payload["tasks"]) == 3
    assert len({item["task_id"] for item in payload["tasks"]}) == 3
    assert len(payload["digest"]) == 64


def test_prepare_bare_workspace_at_exact_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    source = _cooperbench_fixture(tmp_path, repo, base)

    payload = oss_pilot.prepare_oss_pilot_workspace(
        "jinja-loader-local",
        arm="bare",
        workspace_root=tmp_path / "pilot",
        cooperbench=source,
        allow_source_drift=True,
    )

    workspace = Path(payload["workspace"])
    assert _git(workspace, "rev-parse", "HEAD") == base
    assert payload["repository"]["base_commit"] == base
    assert payload["prompt_sha256"] == oss_pilot._sha256(payload["prompt"])
    assert not (workspace / ".codex" / "hooks.json").exists()
    assert oss_pilot.load_oss_pilot_manifest(workspace)["digest"] == payload["digest"]


def test_prepare_guarded_enrolls_connector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    source = _cooperbench_fixture(tmp_path, repo, base)
    calls: list[Path] = []

    def fake_connect(root: str | Path) -> dict[str, object]:
        resolved = Path(root).resolve()
        calls.append(resolved)
        hooks = resolved / ".codex" / "hooks.json"
        hooks.parent.mkdir(parents=True, exist_ok=True)
        hooks.write_text("{}\n", encoding="utf-8")
        return {"root": str(resolved)}

    class Registry:
        def pin(self, *args: object, **kwargs: object) -> tuple[object, Path]:
            return object(), tmp_path / "pin.json"

    monkeypatch.setattr(oss_pilot, "connect_codex", fake_connect)
    monkeypatch.setattr(oss_pilot, "build_adapter_registry", Registry)

    payload = oss_pilot.prepare_oss_pilot_workspace(
        "jinja-loader-local",
        arm="guarded",
        workspace_root=tmp_path / "pilot",
        cooperbench=source,
        allow_source_drift=True,
    )

    workspace = Path(payload["workspace"])
    config = (workspace / ".claim-plane" / "config.yaml").read_text(encoding="utf-8")
    assert calls == [workspace]
    assert 'policy: "guarded"' in config
    assert "oss_pilot_acceptance" in config
    assert (workspace / ".codex" / "hooks.json").exists()
    latest_dir = workspace / ".claim-plane" / "oss-pilot" / "acceptance"
    latest_dir.mkdir(parents=True)
    (latest_dir / "latest.json").write_text(
        json.dumps(
            {
                "classification": "PASS",
                "detail": "official OSS task tests passed",
                "log_dir": ".claim-plane/oss-pilot/acceptance/attempt-1",
            }
        ),
        encoding="utf-8",
    )
    status = oss_pilot.oss_pilot_status(
        "jinja-loader-local",
        arm="guarded",
        workspace_root=tmp_path / "pilot",
    )
    assert status["latest_acceptance"]["classification"] == "PASS"


def test_command_uses_scope_and_selected_policy(tmp_path: Path) -> None:
    manifest = {
        "workspace": str(tmp_path),
        "prompt": "Do the task.",
        "arm": "observe",
        "initial_scope": ["src/value.py"],
    }
    command = oss_pilot.oss_pilot_command(manifest, model="gpt-test")
    assert command[:4] == (
        oss_pilot.sys.executable,
        "-m",
        "claim_plane",
        "codex",
    )
    assert "observe" in command
    assert command[-2:] == ("--scope", "src/value.py")


def test_acceptance_runs_in_isolated_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    source = _cooperbench_fixture(tmp_path, repo, base)
    task_dir = source / "dataset" / "pallets_jinja_task" / "task1621"
    feature_dir = task_dir / "feature1"
    manifest_unsigned = {
        "protocol": oss_pilot.OSS_PILOT_WORKSPACE_PROTOCOL,
        "selection_digest": "selection",
        "task": {"task_id": "fixture"},
        "arm": "guarded",
        "workspace": str(repo),
        "source": {
            "root": str(source),
            "revision": _git(source, "rev-parse", "HEAD"),
            "task_dir": str(task_dir),
            "feature_dir": str(feature_dir),
        },
        "repository": {
            "clone_url": f"file://{repo}",
            "base_commit": base,
            "head": base,
        },
        "prompt": "fixture",
        "prompt_sha256": oss_pilot._sha256("fixture"),
        "initial_scope": ["src/value.py"],
        "acceptance": "fixture",
    }
    manifest = {**manifest_unsigned, "digest": oss_pilot._sha256(manifest_unsigned)}
    manifest_path = repo / ".claim-plane" / "oss-pilot.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (repo / "src" / "value.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert oss_pilot.run_oss_pilot_acceptance(repo) == 0
    assert (repo / "src" / "value.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert _git(repo, "rev-parse", "HEAD") == base


def test_manifest_digest_rejects_tampering(tmp_path: Path) -> None:
    path = tmp_path / ".claim-plane" / "oss-pilot.json"
    path.parent.mkdir(parents=True)
    payload = {
        "protocol": oss_pilot.OSS_PILOT_WORKSPACE_PROTOCOL,
        "digest": "0" * 64,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(oss_pilot.OssPilotError, match="digest mismatch"):
        oss_pilot.load_oss_pilot_manifest(tmp_path)


def _write_manifest(repo: Path, source: Path, base: str) -> None:
    task_dir = source / "dataset" / "pallets_jinja_task" / "task1621"
    feature_dir = task_dir / "feature1"
    manifest_unsigned = {
        "protocol": oss_pilot.OSS_PILOT_WORKSPACE_PROTOCOL,
        "selection_digest": "selection",
        "task": {"task_id": "fixture"},
        "arm": "guarded",
        "workspace": str(repo),
        "source": {
            "root": str(source),
            "revision": _git(source, "rev-parse", "HEAD"),
            "task_dir": str(task_dir),
            "feature_dir": str(feature_dir),
        },
        "repository": {
            "clone_url": f"file://{repo}",
            "base_commit": base,
            "head": base,
        },
        "prompt": "fixture",
        "prompt_sha256": oss_pilot._sha256("fixture"),
        "initial_scope": ["src/value.py"],
        "acceptance": "fixture",
    }
    manifest = {**manifest_unsigned, "digest": oss_pilot._sha256(manifest_unsigned)}
    manifest_path = repo / ".claim-plane" / "oss-pilot.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_acceptance_accepts_official_tests_already_in_candidate(tmp_path: Path) -> None:
    repo = tmp_path / "repo-already-present"
    base = _init_repo(repo)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_feature.py"
    test_file.write_text("BASE = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add test file")
    base = _git(repo, "rev-parse", "HEAD")
    source = _cooperbench_fixture(tmp_path, repo, base)
    feature = source / "dataset" / "pallets_jinja_task" / "task1621" / "feature1"
    original = test_file.read_text(encoding="utf-8")
    test_file.write_text(original + "OFFICIAL = 1\n", encoding="utf-8")
    official_patch = subprocess.run(
        ["git", "diff", "--", "tests/test_feature.py"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    test_file.write_text(original, encoding="utf-8")
    (feature / "tests.patch").write_text(official_patch, encoding="utf-8")
    runner = feature.parent / "run_tests.sh"
    runner.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'test "$(basename "$(dirname "$1")")" = agent_workspace\n'
        'git -C "$1" apply "$2"\n'
        "grep -q 'OFFICIAL = 1' \"$1/tests/test_feature.py\"\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    _write_manifest(repo, source, base)
    test_file.write_text(original + "OFFICIAL = 1\n", encoding="utf-8")

    assert oss_pilot.run_oss_pilot_acceptance(repo) == 0
    latest = json.loads(
        (repo / ".claim-plane" / "oss-pilot" / "acceptance" / "latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert latest["classification"] == "PASS"


def test_acceptance_classifies_candidate_test_conflict(tmp_path: Path) -> None:
    repo = tmp_path / "repo-conflict"
    base = _init_repo(repo)
    source = _cooperbench_fixture(tmp_path, repo, base)
    feature = source / "dataset" / "pallets_jinja_task" / "task1621" / "feature1"
    value_path = repo / "src" / "value.py"
    original = value_path.read_text(encoding="utf-8")
    value_path.write_text("VALUE = 3\n", encoding="utf-8")
    conflicting_patch = subprocess.run(
        ["git", "diff", "--", "src/value.py"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    value_path.write_text(original, encoding="utf-8")
    (feature / "tests.patch").write_text(conflicting_patch, encoding="utf-8")
    _write_manifest(repo, source, base)
    value_path.write_text("VALUE = 2\n", encoding="utf-8")

    assert oss_pilot.run_oss_pilot_acceptance(repo) == 72
    latest = json.loads(
        (repo / ".claim-plane" / "oss-pilot" / "acceptance" / "latest.json").read_text(
            encoding="utf-8"
        )
    )
    assert latest["classification"] == "OFFICIAL_TEST_CONFLICT"
    assert "conflict" in latest["detail"]


def test_acceptance_summary_surfaces_pilot_classification(tmp_path: Path) -> None:
    repo = tmp_path / "summary-repo"
    _init_repo(repo)
    oss_pilot.init_project(repo)
    marker = oss_pilot.OSS_PILOT_ACCEPTANCE_RESULT_MARKER + json.dumps(
        {
            "classification": "TEST_FAILED",
            "detail": "official tests failed",
            "log_dir": ".claim-plane/oss-pilot/acceptance/attempt-1",
        }
    )
    completion = {
        "acceptance_passed": False,
        "acceptance_duration_ms": 12,
        "errors": 1,
        "warnings": 0,
        "acceptance_results": [
            {
                "command": "python -m evaluator",
                "returncode": 70,
                "passed": False,
                "duration_ms": 12,
                "stdout_tail": marker,
                "stderr_tail": "one failed test",
            }
        ],
    }
    summary = controlled_run._acceptance_summary(repo, completion)
    assert summary["classification"] == "TEST_FAILED"
    assert summary["log_dir"].endswith("attempt-1")
    assert summary["results"][0]["detail"] == "official tests failed"
