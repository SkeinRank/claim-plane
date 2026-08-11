from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from experiments.cooperbench.confirmatory_30x3.ablation import (
    AblationProfile,
    DETERMINISTIC_ABLATION_PROTOCOL,
    deterministic_ablation_verdict,
    parse_ablation_profiles,
)


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "ablation@example.test")
    _git(root, "config", "user.name", "ablation")
    for name, source in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    return root, _git(root, "rev-parse", "HEAD")


def test_parse_ablation_profiles_is_stable_and_deduplicated() -> None:
    assert parse_ablation_profiles("full_v2,file_region_baseline,full_v2") == (
        AblationProfile.FULL_V2,
        AblationProfile.FILE_REGION_BASELINE,
    )
    with pytest.raises(ValueError):
        parse_ablation_profiles("")
    with pytest.raises(ValueError):
        parse_ablation_profiles("not-a-profile")


def test_dependency_ablation_changes_cross_file_ordering(tmp_path: Path) -> None:
    repo, base = _repo(
        tmp_path,
        {
            "producer.py": "def value():\n    return 1\n",
            "consumer.py": (
                "from producer import value\n\ndef use():\n    return value()\n"
            ),
        },
    )
    plan_a = {
        "files": [
            {
                "path": "producer.py",
                "action": "modify",
                "line_start": 1,
                "line_end": 2,
            }
        ]
    }
    plan_b = {
        "files": [
            {
                "path": "consumer.py",
                "action": "modify",
                "line_start": 3,
                "line_end": 4,
            }
        ]
    }

    full = deterministic_ablation_verdict(
        repo,
        base_commit=base,
        plan_a=plan_a,
        plan_b=plan_b,
        profile=AblationProfile.FULL_V2,
    )
    baseline = deterministic_ablation_verdict(
        repo,
        base_commit=base,
        plan_a=plan_a,
        plan_b=plan_b,
        profile=AblationProfile.FILE_REGION_BASELINE,
    )

    assert full["serialized"] is True
    assert full["kind"] == "ordered"
    assert full["serial_order"] == "A->B"
    assert baseline["serialized"] is False
    assert baseline["kind"] == "parallel"
    assert full["ablation_evidence"]["protocol"] == DETERMINISTIC_ABLATION_PROTOCOL
    assert full["ablation_evidence"]["semantic_graph_fingerprint"]
    assert baseline["ablation_evidence"]["semantic_graph_fingerprint"] is None


def test_contract_propagation_ablation_removes_inheritance_order(
    tmp_path: Path,
) -> None:
    repo, base = _repo(
        tmp_path,
        {
            "base.py": "class Base:\n    pass\n",
            "child.py": "from base import Base\n\nclass Child(Base):\n    pass\n",
        },
    )
    plan_a = {
        "files": [
            {
                "path": "base.py",
                "action": "modify",
                "line_start": 1,
                "line_end": 1,
            }
        ]
    }
    plan_b = {
        "files": [
            {
                "path": "child.py",
                "action": "modify",
                "line_start": 3,
                "line_end": 3,
            }
        ]
    }

    full = deterministic_ablation_verdict(
        repo,
        base_commit=base,
        plan_a=plan_a,
        plan_b=plan_b,
        profile="full_v2",
    )
    no_contracts = deterministic_ablation_verdict(
        repo,
        base_commit=base,
        plan_a=plan_a,
        plan_b=plan_b,
        profile="no_contract_propagation",
    )

    assert full["kind"] == "ordered"
    assert no_contracts["kind"] == "parallel"
    assert (
        full["ablation_evidence"]["semantic_graph_fingerprint"]
        != (no_contracts["ablation_evidence"]["semantic_graph_fingerprint"])
    )


def test_symbols_without_dependencies_keeps_graph_nodes_but_removes_edges(
    tmp_path: Path,
) -> None:
    repo, base = _repo(
        tmp_path,
        {
            "app.py": "def first():\n    return 1\n\ndef second():\n    return first()\n",
        },
    )
    plan_a = {
        "files": [
            {"path": "app.py", "action": "modify", "line_start": 1, "line_end": 2}
        ]
    }
    plan_b = {
        "files": [
            {"path": "app.py", "action": "modify", "line_start": 4, "line_end": 5}
        ]
    }
    verdict = deterministic_ablation_verdict(
        repo,
        base_commit=base,
        plan_a=plan_a,
        plan_b=plan_b,
        profile="symbols_without_dependencies",
    )
    evidence = verdict["ablation_evidence"]
    assert evidence["semantic_graph_mode"] == "nodes_only"
    assert evidence["semantic_graph_fingerprint"]
    assert evidence["concurrency_plan"]["metadata"]["semantic_graph_fingerprint"]


def test_full_v2_keeps_contingent_scope_out_of_initial_authority(
    tmp_path: Path,
) -> None:
    repo, base = _repo(
        tmp_path,
        {"app.py": ("def first():\n    return 1\n\ndef second():\n    return 2\n")},
    )
    plan_a = {
        "files": [
            {
                "path": "app.py",
                "action": "modify",
                "commitment": "committed",
                "line_start": 1,
                "line_end": 2,
            },
            {
                "path": "app.py",
                "action": "modify",
                "commitment": "contingent",
                "line_start": 4,
                "line_end": 5,
            },
        ]
    }
    plan_b = {
        "files": [
            {
                "path": "app.py",
                "action": "modify",
                "commitment": "committed",
                "line_start": 4,
                "line_end": 5,
            }
        ]
    }

    verdict = deterministic_ablation_verdict(
        repo,
        base_commit=base,
        plan_a=plan_a,
        plan_b=plan_b,
        profile="full_v2",
    )

    assert verdict["serialized"] is False
    assert verdict["kind"] == "parallel"


def test_full_v2_uses_qualified_symbol_identity_for_same_named_methods(
    tmp_path: Path,
) -> None:
    repo, base = _repo(
        tmp_path,
        {
            "app.py": (
                "class First:\n"
                "    def run(self):\n"
                "        return 1\n"
                "\n"
                "class Second:\n"
                "    def run(self):\n"
                "        return 2\n"
            )
        },
    )
    plan_a = {
        "files": [
            {
                "path": "app.py",
                "action": "modify",
                "line_start": 2,
                "line_end": 3,
            }
        ]
    }
    plan_b = {
        "files": [
            {
                "path": "app.py",
                "action": "modify",
                "line_start": 6,
                "line_end": 7,
            }
        ]
    }

    verdict = deterministic_ablation_verdict(
        repo,
        base_commit=base,
        plan_a=plan_a,
        plan_b=plan_b,
        profile="full_v2",
    )

    assert verdict["serialized"] is False
    assert verdict["kind"] == "parallel"


def test_full_v2_anchors_explicit_intent_symbol_when_line_coordinates_drift(
    tmp_path: Path,
) -> None:
    repo, base = _repo(
        tmp_path,
        {
            "src/click/core.py": (
                "def helper_a():\n"
                "    return 1\n"
                "\n"
                "class Option:\n"
                "    def __init__(self, value=None):\n"
                "        self.value = value\n"
                "\n"
                "def helper_b():\n"
                "    return 2\n"
            )
        },
    )
    plan_a = {
        "files": [
            {
                "path": "src/click/core.py",
                "action": "modify",
                "commitment": "committed",
                "line_start": 1,
                "line_end": 2,
                "what": "Add required_if parameter to Option.__init__",
            }
        ]
    }
    plan_b = {
        "files": [
            {
                "path": "src/click/core.py",
                "action": "modify",
                "commitment": "committed",
                "line_start": 8,
                "line_end": 9,
                "what": "Add cache parameter to Option.__init__ and store it",
            }
        ]
    }

    full = deterministic_ablation_verdict(
        repo,
        base_commit=base,
        plan_a=plan_a,
        plan_b=plan_b,
        profile="full_v2",
    )
    baseline = deterministic_ablation_verdict(
        repo,
        base_commit=base,
        plan_a=plan_a,
        plan_b=plan_b,
        profile="file_region_baseline",
    )

    assert full["serialized"] is True
    assert full["kind"] == "ordered"
    assert baseline["serialized"] is False
    assert baseline["kind"] == "parallel"

    admissions = full["ablation_evidence"]["concurrency_plan"]["metadata"][
        "same_file_admissions"
    ]
    assert len(admissions) == 1
    admission = admissions[0]
    assert admission["action"] == "serialize"
    assert admission["reason"] == "semantic_conflicting"
    assert admission["semantic_kind"] == "conflicting"
    anchors = full["ablation_evidence"]["intent_ast_anchors"]
    assert anchors["A"][0]["qualified_identifier"] == "Option.__init__"
    assert anchors["A"][0]["change_kind"] == "contract"
    assert anchors["B"][0]["qualified_identifier"] == "Option.__init__"
    assert anchors["B"][0]["change_kind"] == "contract"
    assert "symbol:src/click/core.py#Option.__init__" in admission["left_changes"]
    assert "symbol:src/click/core.py#Option.__init__" in admission["right_changes"]


def test_full_v2_does_not_order_stable_contract_caller_callee_edits(
    tmp_path: Path,
) -> None:
    repo, base = _repo(
        tmp_path,
        {
            "src/jinja2/loaders.py": (
                "def split_template_path(template):\n"
                "    return template.split('/')\n"
                "\n"
                "class FileSystemLoader:\n"
                "    def get_source(self, template):\n"
                "        return split_template_path(template)\n"
                "\n"
                "class PackageLoader:\n"
                "    def get_source(self, template):\n"
                "        return split_template_path(template)\n"
            )
        },
    )
    plan_a = {
        "files": [
            {
                "path": "src/jinja2/loaders.py",
                "action": "modify",
                "commitment": "committed",
                "line_start": 6,
                "line_end": 6,
                "what": (
                    "Normalize template before split_template_path in "
                    "FileSystemLoader.get_source"
                ),
            },
            {
                "path": "src/jinja2/loaders.py",
                "action": "modify",
                "commitment": "committed",
                "line_start": 10,
                "line_end": 10,
                "what": (
                    "Normalize template before split_template_path in "
                    "PackageLoader.get_source"
                ),
            },
        ]
    }
    plan_b = {
        "files": [
            {
                "path": "src/jinja2/loaders.py",
                "action": "modify",
                "commitment": "committed",
                "line_start": 2,
                "line_end": 2,
                "what": "Validate template inside split_template_path",
            }
        ]
    }

    verdict = deterministic_ablation_verdict(
        repo,
        base_commit=base,
        plan_a=plan_a,
        plan_b=plan_b,
        profile="full_v2",
    )

    assert verdict["serialized"] is False
    assert verdict["kind"] == "parallel"
    admissions = verdict["ablation_evidence"]["concurrency_plan"]["metadata"][
        "same_file_admissions"
    ]
    assert len(admissions) == 1
    admission = admissions[0]
    assert admission["action"] == "parallel"
    assert admission["reason"] == "semantic_independent"
    assert admission["semantic_kind"] == "independent"
    anchors = verdict["ablation_evidence"]["intent_ast_anchors"]
    assert {item["qualified_identifier"] for item in anchors["A"]} == {
        "FileSystemLoader.get_source",
        "PackageLoader.get_source",
    }
