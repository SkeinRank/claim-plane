from __future__ import annotations

import json
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

from claim_plane.acceptance_witness import (
    ACCEPTANCE_WITNESS_SPEC_PROTOCOL,
    assess_acceptance_witness,
    build_acceptance_witness_spec,
    infer_optional_test_dependencies,
    prepare_pytest_witness_environment,
)


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", *args), cwd=root, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def _repo(root: Path) -> Path:
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_value.py").write_text(
        "def test_existing():\n    assert True\n", encoding="utf-8"
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    return root


def test_build_spec_binds_added_private_pytest_node_and_pillow(tmp_path: Path) -> None:
    base = _repo(tmp_path / "repo")
    official = tmp_path / "official"
    _git(base, "worktree", "add", "--detach", str(official), "HEAD")
    test_file = official / "tests" / "test_value.py"
    test_file.write_text(
        test_file.read_text(encoding="utf-8")
        + (
            "\n@require_pil\ndef test_hidden_image():\n"
            "    import PIL.Image\n    assert PIL.Image is not None\n"
        ),
        encoding="utf-8",
    )
    patch = subprocess.run(
        ("git", "diff", "--", "tests/test_value.py"),
        cwd=official,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    patch_path = tmp_path / "tests.patch"
    patch_path.write_text(patch, encoding="utf-8")

    spec = build_acceptance_witness_spec(
        base_root=base, official_tree=official, tests_patch=patch_path
    )

    assert spec["protocol"] == ACCEPTANCE_WITNESS_SPEC_PROTOCOL
    assert spec["required"] is True
    assert spec["targets"] == ["tests/test_value.py::test_hidden_image"]
    assert spec["optional_dependencies"] == [
        {"marker": "require_pil", "package": "Pillow", "module": "PIL"}
    ]


def _run_witnessed_pytest(tmp_path: Path, test_source: str) -> dict[str, object]:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_hidden.py").write_text(test_source, encoding="utf-8")
    witness_path = tmp_path / "witness.json"
    spec = {
        "protocol": ACCEPTANCE_WITNESS_SPEC_PROTOCOL,
        "required": True,
        "targets": ["tests/test_hidden.py::test_private"],
        "changed_test_files": ["tests/test_hidden.py"],
        "optional_dependencies": [],
        "discovery_errors": [],
    }
    env = prepare_pytest_witness_environment(
        plugin_dir=tmp_path / "plugin",
        witness_path=witness_path,
        spec=spec,
    )
    subprocess.run(
        (sys.executable, "-m", "pytest", "tests/test_hidden.py", "-q"),
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return assess_acceptance_witness(spec, witness_path)


def test_witness_requires_private_test_to_execute_and_pass(tmp_path: Path) -> None:
    witness = _run_witnessed_pytest(tmp_path, "def test_private():\n    assert True\n")

    assert witness["state"] == "VERIFIED"
    assert witness["verified"] is True
    assert witness["collected"] == 1
    assert witness["executed"] == 1
    assert witness["passed"] == 1
    assert witness["skipped"] == 0


def test_witness_rejects_skipped_private_test(tmp_path: Path) -> None:
    witness = _run_witnessed_pytest(
        tmp_path,
        (
            "import pytest\n\n"
            "@pytest.mark.skip(reason='missing optional dependency')\n"
            "def test_private():\n    assert True\n"
        ),
    )

    assert witness["state"] == "INCOMPLETE"
    assert witness["verified"] is False
    assert witness["collected"] == 1
    assert witness["executed"] == 0
    assert witness["skipped"] == 1


def test_witness_rejects_missing_pytest_session(tmp_path: Path) -> None:
    spec = {
        "required": True,
        "targets": ["tests/test_hidden.py::test_private"],
        "changed_test_files": ["tests/test_hidden.py"],
        "optional_dependencies": [],
        "discovery_errors": [],
    }

    witness = assess_acceptance_witness(spec, tmp_path / "missing.json")

    assert witness["state"] == "INCOMPLETE"
    assert witness["missing"] == 1


def test_optional_dependency_inference_ignores_context_lines() -> None:
    patch = """diff --git a/tests/test_image.py b/tests/test_image.py
--- a/tests/test_image.py
+++ b/tests/test_image.py
@@ -1 +1 @@
-import PIL.Image
+assert True
"""

    assert infer_optional_test_dependencies(patch) == ()


def _minimal_wheel(root: Path, *, distribution: str, module: str) -> Path:
    wheel = root / f"{distribution}-0.0.0-py3-none-any.whl"
    dist_info = f"{distribution}-0.0.0.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{module}/__init__.py", "AVAILABLE = True\n")
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: 0.0.0\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: claim-plane-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    return wheel


def test_optional_dependency_is_available_before_initial_conftest_import(
    tmp_path: Path,
) -> None:
    module = "claim_plane_early_optional_" + uuid.uuid4().hex
    wheel = _minimal_wheel(
        tmp_path,
        distribution=module,
        module=module,
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tmp_path / "conftest.py").write_text(
        "import importlib.util\n"
        "OPTIONAL_AVAILABLE_AT_IMPORT = "
        f"importlib.util.find_spec('{module}') is not None\n",
        encoding="utf-8",
    )
    (tests / "test_hidden.py").write_text(
        "import pytest\n"
        "from conftest import OPTIONAL_AVAILABLE_AT_IMPORT\n\n"
        "@pytest.mark.skipif(not OPTIONAL_AVAILABLE_AT_IMPORT, reason='late bootstrap')\n"
        "def test_private():\n"
        f"    import {module}\n"
        f"    assert {module}.AVAILABLE is True\n",
        encoding="utf-8",
    )
    witness_path = tmp_path / "witness.json"
    spec = {
        "protocol": ACCEPTANCE_WITNESS_SPEC_PROTOCOL,
        "required": True,
        "targets": ["tests/test_hidden.py::test_private"],
        "changed_test_files": ["tests/test_hidden.py"],
        "optional_dependencies": [
            {
                "marker": "require_test_optional",
                "package": str(wheel),
                "module": module,
            }
        ],
        "discovery_errors": [],
    }
    env = prepare_pytest_witness_environment(
        plugin_dir=tmp_path / "plugin",
        witness_path=witness_path,
        spec=spec,
    )
    completed = subprocess.run(
        (sys.executable, "-m", "pytest", "tests/test_hidden.py", "-q"),
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    witness = assess_acceptance_witness(spec, witness_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert witness["state"] == "VERIFIED"
    assert witness["passed"] == 1
    assert witness["skipped"] == 0
    bootstrap = witness["dependency_bootstrap"]
    assert len(bootstrap) == 1
    assert bootstrap[0]["available"] is True
    assert bootstrap[0]["installed"] is True
    assert bootstrap[0]["module"] == module
    assert bootstrap[0]["package"] == str(wheel)
    assert bootstrap[0]["phase"] == "pre-collection"
    assert bootstrap[0]["returncode"] == 0


def test_witness_json_is_serializable(tmp_path: Path) -> None:
    witness = _run_witnessed_pytest(tmp_path, "def test_private():\n    assert True\n")
    json.dumps(witness, sort_keys=True)
