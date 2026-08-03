from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claim_plane import cli
from claim_plane.connectors import codex
from claim_plane.project import (
    PROJECT_CONFIG_PROTOCOL,
    doctor_project,
    init_project,
    load_project_config,
    reset_project,
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
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    scripts = repo / "scripts"
    scripts.mkdir()
    check = scripts / "check.sh"
    check.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    check.chmod(0o755)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def test_init_creates_stable_project_config_and_detects_acceptance(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)

    first = init_project(repo)
    config_before = (repo / ".claim-plane/config.yaml").read_bytes()
    state_before = (repo / ".claim-plane/project.json").read_bytes()
    second = init_project(repo)

    config = load_project_config(repo)
    assert config["protocol"] == PROJECT_CONFIG_PROTOCOL
    assert config["project"]["id"].startswith("cp_")
    assert config["repository"]["default_branch"] == "main"
    assert config["acceptance"]["commands"] == ["./scripts/check.sh"]
    assert first["project_id"] == second["project_id"]
    assert first["created"] is True
    assert second["created"] is False
    assert config_before == (repo / ".claim-plane/config.yaml").read_bytes()
    assert state_before == (repo / ".claim-plane/project.json").read_bytes()
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        == ""
    )


def test_connect_records_runtime_sandbox_and_enables_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    init_project(repo)
    monkeypatch.setattr(
        codex, "_codex_version", lambda: ("/opt/codex/bin/codex", "codex 1.2.3")
    )
    monkeypatch.setattr(
        codex,
        "_codex_sandbox_status",
        lambda: ("warning", "project-local test boundary"),
    )

    result = codex.connect_codex(repo)

    state = json.loads((repo / ".claim-plane/codex.json").read_text())
    config = load_project_config(repo)
    assert result["runtime_version"] == "codex 1.2.3"
    assert result["sandbox_detail"] == "project-local test boundary"
    assert state["runtime"]["version"] == "codex 1.2.3"
    assert state["sandbox"]["status"] == "warning"
    assert config["adapters"]["codex"]["enabled"] is True


def test_project_doctor_reports_git_state_acceptance_and_secret_hygiene(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    init_project(repo)

    healthy = doctor_project(repo)
    assert healthy.ready is True
    by_name = {item["name"]: item for item in healthy.checks}
    assert by_name["project_config"]["status"] == "ok"
    assert by_name["acceptance_commands"]["status"] == "ok"
    assert by_name["secret_redaction"]["status"] == "ok"

    config_path = repo / ".claim-plane/config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + 'token: "sk-secret-material-1234567890"\n',
        encoding="utf-8",
    )
    unhealthy = doctor_project(repo)
    secret = next(
        item for item in unhealthy.checks if item["name"] == "secret_redaction"
    )
    assert unhealthy.ready is False
    assert secret["status"] == "error"
    assert "sk-secret" not in secret["detail"]


def test_doctor_can_run_without_explicit_codex_subcommand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path)
    init_project(repo)
    codex.connect_codex(repo)
    monkeypatch.setattr(
        codex, "_codex_version", lambda: ("/usr/bin/codex", "codex 1.2.3")
    )
    monkeypatch.setattr(
        codex,
        "_codex_auth_status",
        lambda executable: ("ok", "authentication available"),
    )

    result = cli.main(["doctor", "--repo", str(repo)])

    output = capsys.readouterr().out
    assert result == 0
    assert "project_config" in output
    assert "codex_authentication" in output
    assert "Status: ready" in output


def test_reset_preserves_repository_config_and_foreign_codex_hooks(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    init_project(repo)
    codex.connect_codex(repo)
    hooks_path = repo / ".codex/hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks["hooks"]["Stop"].append(
        {"hooks": [{"type": "command", "command": "./tools/foreign-hook"}]}
    )
    hooks_path.write_text(json.dumps(hooks), encoding="utf-8")

    disconnected = codex.disconnect_codex(repo)
    result = reset_project(repo)

    remaining = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert disconnected["removed_handlers"] == len(codex.CODEX_HOOK_EVENTS)
    assert remaining["hooks"]["Stop"] == [
        {"hooks": [{"type": "command", "command": "./tools/foreign-hook"}]}
    ]
    assert result["config_preserved"] is True
    assert (repo / ".claim-plane/config.yaml").is_file()
    assert (repo / "README.md").read_text(encoding="utf-8") == "# fixture\n"


def test_reset_remove_config_is_explicit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    init_project(repo)

    result = reset_project(repo, remove_config=True)

    assert result["config_preserved"] is False
    assert not (repo / ".claim-plane").exists()
    assert (repo / "scripts/check.sh").is_file()
