from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from claim_plane import __version__, cli
from claim_plane.exit_codes import ExitCode, exit_code_manifest
from claim_plane.preview import technical_preview_manifest
from claim_plane.project import (
    LEGACY_PROJECT_CONFIG_PROTOCOL,
    PROJECT_CONFIG_PROTOCOL,
    dump_project_config,
    init_project,
    load_project_config,
    migrate_project_config,
    project_config_status,
    reset_project,
)
from claim_plane.resources import export_schemas, list_schemas

ROOT = Path(__file__).resolve().parents[1]


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
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    return repo


def test_preview_version_and_public_contract_are_consistent() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = technical_preview_manifest(ROOT)

    assert __version__ == "0.46.0"
    assert project["project"]["version"] == __version__
    assert manifest["version"] == __version__
    assert manifest["channel"] == "single-agent-codex"
    assert manifest["python"]["supported"] is True
    assert all(item["present"] is True for item in manifest["documentation"])
    assert "claim-plane codex" in manifest["stable_commands"]
    assert "claim-plane run" in manifest["stable_commands"]
    assert [item["code"] for item in exit_code_manifest()["codes"]] == [
        0,
        1,
        2,
        3,
        4,
        124,
        130,
    ]
    assert int(ExitCode.CANCELLED) == 130


def test_wheel_resources_match_repository_schemas(tmp_path: Path) -> None:
    packaged = {item["name"]: item for item in list_schemas()}
    source = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (ROOT / "schemas").glob("*.json")
    }

    assert set(packaged) == set(source)
    assert {name: item["sha256"] for name, item in packaged.items()} == source

    exported = export_schemas(tmp_path / "schemas")
    assert exported["count"] == len(source)
    for name, digest in source.items():
        exported_digest = hashlib.sha256(
            (tmp_path / "schemas" / name).read_bytes()
        ).hexdigest()
        assert exported_digest == digest


def test_supported_legacy_config_migrates_atomically(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    init_project(repo)
    current = load_project_config(repo)
    legacy = {
        "protocol": LEGACY_PROJECT_CONFIG_PROTOCOL,
        "project": current["project"],
        "repository": current["repository"],
        "acceptance": current["acceptance"],
        "codex": {"enabled": True, "policy": "guarded"},
    }
    config_path = repo / ".claim-plane/config.yaml"
    legacy_text = dump_project_config(legacy)
    config_path.write_text(legacy_text, encoding="utf-8")

    status = project_config_status(repo)
    dry_run = migrate_project_config(repo, dry_run=True)

    assert status["status"] == "migration_required"
    assert status["migration_available"] is True
    assert dry_run["changed"] is True
    assert config_path.read_text(encoding="utf-8") == legacy_text
    assert not Path(dry_run["backup"]).exists()

    result = migrate_project_config(repo)
    migrated = load_project_config(repo)

    assert result["source_protocol"] == LEGACY_PROJECT_CONFIG_PROTOCOL
    assert result["target_protocol"] == PROJECT_CONFIG_PROTOCOL
    assert Path(result["backup"]).read_text(encoding="utf-8") == legacy_text
    assert migrated["protocol"] == PROJECT_CONFIG_PROTOCOL
    assert migrated["adapters"]["codex"] == {
        "enabled": True,
        "policy": "guarded",
    }
    assert project_config_status(repo)["status"] == "current"


def test_init_performs_supported_reenrollment_migration(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    initialized = init_project(repo)
    current = load_project_config(repo)
    legacy = {
        "protocol": LEGACY_PROJECT_CONFIG_PROTOCOL,
        "project": current["project"],
        "repository": current["repository"],
        "acceptance": current["acceptance"],
        "codex": current["adapters"]["codex"],
    }
    (repo / ".claim-plane/config.yaml").write_text(
        dump_project_config(legacy), encoding="utf-8"
    )

    reenrolled = init_project(repo)

    assert reenrolled["project_id"] == initialized["project_id"]
    assert reenrolled["migration"]["source_protocol"] == LEGACY_PROJECT_CONFIG_PROTOCOL
    assert load_project_config(repo)["protocol"] == PROJECT_CONFIG_PROTOCOL


def test_full_reset_removes_config_and_migration_backup(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    init_project(repo)
    current = load_project_config(repo)
    legacy = {
        "protocol": LEGACY_PROJECT_CONFIG_PROTOCOL,
        "project": current["project"],
        "repository": current["repository"],
        "acceptance": current["acceptance"],
        "codex": current["adapters"]["codex"],
    }
    config_path = repo / ".claim-plane/config.yaml"
    config_path.write_text(dump_project_config(legacy), encoding="utf-8")
    migration = migrate_project_config(repo)

    result = reset_project(repo, remove_config=True)

    assert result["config_preserved"] is False
    assert not config_path.exists()
    assert not Path(migration["backup"]).exists()
    assert (repo / "README.md").read_text(encoding="utf-8") == "# fixture\n"


def test_preview_config_and_schema_cli_are_machine_readable(
    tmp_path: Path, capsys
) -> None:
    repo = _repo(tmp_path)
    init_project(repo)

    assert cli.main(["preview", "--repo", str(ROOT), "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["protocol"] == "claim-plane.technical-preview.v1"

    assert cli.main(["exit-codes", "--json"]) == 0
    codes = json.loads(capsys.readouterr().out)
    assert codes["protocol"] == "claim-plane.exit-codes.v1"

    assert cli.main(["config", "status", "--repo", str(repo), "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "current"

    destination = tmp_path / "exported"
    assert cli.main(["schemas", "export", str(destination), "--json"]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["count"] == len(list_schemas())


def test_preview_support_files_and_release_validation_are_present() -> None:
    required = (
        "docs/QUICKSTART.md",
        "docs/CLI_REFERENCE.md",
        "docs/GUARANTEES.md",
        "docs/TROUBLESHOOTING.md",
        "docs/UPGRADING.md",
        "examples/technical-preview/TASK.md",
        "scripts/technical-preview-demo.sh",
        "scripts/check-technical-preview.sh",
        "scripts/check-interactive-safety.sh",
        ".github/ISSUE_TEMPLATE/technical-preview.yml",
        ".github/PULL_REQUEST_TEMPLATE.md",
        "MANIFEST.in",
    )
    assert all((ROOT / relative).is_file() for relative in required)
    gate = (ROOT / "scripts/check.sh").read_text(encoding="utf-8")
    assert "./scripts/check-technical-preview.sh" in gate
    assert "./scripts/check-interactive-safety.sh" in gate


def test_interactive_codex_command_parses_without_manual_scope() -> None:
    parser = cli.build_parser()
    automatic = parser.parse_args(["codex"])
    guided = parser.parse_args(
        ["codex", "Fix timeout handling", "--scope", "src/connector.py"]
    )

    assert automatic.task is None
    assert automatic.scope is None
    assert guided.task == "Fix timeout handling"
    assert guided.scope == ["src/connector.py"]


def test_release_build_backend_is_provisioned_explicitly() -> None:
    gate = (ROOT / "scripts/check-technical-preview.sh").read_text(encoding="utf-8")
    publish = (ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    releasing = (ROOT / "docs/RELEASING.md").read_text(encoding="utf-8")

    assert "setuptools>=77.0.3 is required for release builds" in gate
    assert "--no-build-isolation" in gate
    assert '"setuptools>=77.0.3" wheel build twine' in publish
    assert "python -m build --no-isolation" in publish
    assert '"setuptools>=77.0.3" wheel build twine' in ci
    assert "python -m build --no-isolation" in ci
    assert '"setuptools>=77.0.3" wheel build twine' in releasing
