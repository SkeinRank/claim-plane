from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISSUE_DIR = ROOT / ".github" / "ISSUE_TEMPLATE"


def test_issue_forms_and_chooser_are_present() -> None:
    expected = {
        "bug.yml",
        "feature.yml",
        "research.yml",
        "reproducibility.yml",
        "technical-preview.yml",
        "config.yml",
    }
    assert expected <= {path.name for path in ISSUE_DIR.iterdir() if path.is_file()}

    config = (ISSUE_DIR / "config.yml").read_text()
    assert "blank_issues_enabled: false" in config
    assert "https://github.com/SkeinRank/claim-plane/discussions" in config


def test_issue_forms_use_public_taxonomy() -> None:
    expected_labels = {
        "bug.yml": ['"type: bug"', '"status: needs-triage"'],
        "feature.yml": ['"type: feature"', '"status: needs-triage"'],
        "research.yml": ['"type: research"', '"status: needs-triage"'],
        "reproducibility.yml": [
            '"type: research"',
            '"area: cooperbench"',
            '"status: needs-triage"',
        ],
        "technical-preview.yml": [
            '"type: bug"',
            '"area: cli"',
            '"status: needs-triage"',
        ],
    }
    for filename, labels in expected_labels.items():
        text = (ISSUE_DIR / filename).read_text()
        for label in labels:
            assert label in text


def test_issue_area_automation_is_whitelisted() -> None:
    workflow = (ROOT / ".github" / "workflows" / "issue-intake.yml").read_text()
    assert "issues: write" in workflow
    assert "actions/github-script@v9" in workflow
    for area in ("core", "broker", "verification", "cli", "cooperbench", "tooling"):
        assert f'"{area}"' in workflow
    assert 'startsWith("area: ")' in workflow


def test_label_setup_is_idempotent_and_matches_forms() -> None:
    script = (ROOT / "scripts" / "setup-github-labels.sh").read_text()
    assert "gh label create" in script
    assert "--force" in script
    for label in (
        "type: bug",
        "type: feature",
        "type: docs",
        "type: research",
        "type: integration",
        "type: performance",
        "area: core",
        "area: broker",
        "area: verification",
        "area: cli",
        "area: cooperbench",
        "area: tooling",
        "status: needs-triage",
        "status: blocked",
        "help wanted",
        "good first issue",
        "duplicate",
        "invalid",
        "wontfix",
    ):
        assert f'ensure_label "{label}"' in script
