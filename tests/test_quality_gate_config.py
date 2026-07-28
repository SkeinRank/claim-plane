from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RUFF_SPEC = "ruff==0.15.21"


def test_pre_commit_uses_offline_system_ruff() -> None:
    config = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")

    assert config.count("./scripts/ruff-tool.sh") == 2
    assert config.count("language: system") == 2
    assert "additional_dependencies" not in config
    assert "language: python" not in config


def test_quality_gate_and_project_share_the_ruff_pin() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    wrapper = (ROOT / "scripts" / "ruff-tool.sh").read_text(encoding="utf-8")
    check_script = (ROOT / "scripts" / "check.sh").read_text(encoding="utf-8")

    assert f'"{EXPECTED_RUFF_SPEC}"' in pyproject
    assert 'EXPECTED_RUFF_VERSION="0.15.21"' in wrapper
    assert "./scripts/ruff-tool.sh check" in check_script
    assert "./scripts/ruff-tool.sh format --check" in check_script
