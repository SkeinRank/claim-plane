"""Fail-closed witnesses for private pytest acceptance tests.

The evaluator owns hidden tests and their execution environment.  This module derives
only test identities and optional runtime prerequisites from the private test input,
loads a tiny external pytest plugin, and verifies that every hidden test was collected,
executed, and passed before an OSS acceptance result may be classified as ``PASS``.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

ACCEPTANCE_WITNESS_SPEC_PROTOCOL = "claim-plane.acceptance-witness-spec.v1"
ACCEPTANCE_WITNESS_PROTOCOL = "claim-plane.acceptance-witness.v1"
_PYTEST_PLUGIN_MODULE = "claim_plane_private_acceptance_witness"
_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@"
)

_OPTIONAL_TEST_DEPENDENCIES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("require_pil", "Pillow", "PIL", (r"\bimport\s+PIL(?:\.|\b)", r"\bfrom\s+PIL\b")),
    (
        "require_soundfile",
        "soundfile",
        "soundfile",
        (r"\bimport\s+soundfile\b", r"\bfrom\s+soundfile\b"),
    ),
    (
        "require_librosa",
        "librosa",
        "librosa",
        (r"\bimport\s+librosa\b", r"\bfrom\s+librosa\b"),
    ),
    (
        "require_torchvision",
        "torchvision",
        "torchvision",
        (r"\bimport\s+torchvision\b", r"\bfrom\s+torchvision\b"),
    ),
)


class AcceptanceWitnessError(RuntimeError):
    """Raised when private acceptance witness inputs are malformed."""


def _read_patch(value: str | Path) -> str:
    if isinstance(value, Path):
        return value.read_text(encoding="utf-8", errors="replace")
    text = str(value)
    if "\n" not in text:
        path = Path(text)
        try:
            if path.exists():
                return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return text


def infer_optional_test_dependencies(value: str | Path) -> tuple[dict[str, str], ...]:
    """Infer explicit optional test prerequisites without exposing test content."""

    patch = _read_patch(value)
    added = "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    dependencies: list[dict[str, str]] = []
    for marker, package, module, import_patterns in _OPTIONAL_TEST_DEPENDENCIES:
        if marker in added or any(
            re.search(pattern, added) for pattern in import_patterns
        ):
            dependencies.append(
                {"marker": marker, "package": package, "module": module}
            )
    return tuple(dependencies)


def _test_definitions(source: str, relative: str) -> dict[str, tuple[int, int]]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise AcceptanceWitnessError(
            f"cannot parse private Python test input {relative}: {exc}"
        ) from exc

    found: dict[str, tuple[int, int]] = {}

    def function_span(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int, int]:
        starts = [node.lineno]
        starts.extend(decorator.lineno for decorator in node.decorator_list)
        return min(starts), int(getattr(node, "end_lineno", node.lineno))

    for node in tree.body:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.name.startswith("test"):
            found[f"{relative}::{node.name}"] = function_span(node)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and child.name.startswith("test"):
                    found[f"{relative}::{node.name}::{child.name}"] = function_span(
                        child
                    )
    return found


def _git_blob(root: Path, revision: str, relative: str) -> str:
    completed = subprocess.run(
        ("git", "show", f"{revision}:{relative}"),
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else ""


def _added_lines(diff: str) -> set[int]:
    result: set[int] = set()
    new_line = 0
    in_hunk = False
    for line in diff.splitlines():
        match = _HUNK_HEADER.match(line)
        if match:
            new_line = int(match.group("new"))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            result.add(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        elif line.startswith("\\"):
            continue
        else:
            new_line += 1
    return result


def _changed_python_test_paths(official_tree: Path) -> tuple[str, ...]:
    completed = subprocess.run(
        ("git", "diff", "--name-only", "HEAD"),
        cwd=official_tree,
        text=True,
        capture_output=True,
        check=True,
    )
    untracked = subprocess.run(
        ("git", "ls-files", "--others", "--exclude-standard"),
        cwd=official_tree,
        text=True,
        capture_output=True,
        check=True,
    )
    paths = []
    for item in (*completed.stdout.splitlines(), *untracked.stdout.splitlines()):
        path = Path(item)
        if path.suffix != ".py":
            continue
        if "tests" in path.parts or path.name.startswith("test_"):
            paths.append(path.as_posix())
    return tuple(dict.fromkeys(paths))


def build_acceptance_witness_spec(
    *,
    base_root: Path,
    official_tree: Path,
    tests_patch: str | Path,
) -> dict[str, Any]:
    """Bind private Python tests added or modified by the official test input."""

    test_paths = _changed_python_test_paths(official_tree)
    targets: set[str] = set()
    discovery_errors: list[str] = []
    for relative in test_paths:
        official_path = official_tree / relative
        if not official_path.is_file():
            continue
        try:
            official_defs = _test_definitions(
                official_path.read_text(encoding="utf-8", errors="replace"),
                relative,
            )
            base_defs = _test_definitions(
                _git_blob(base_root, "HEAD", relative), relative
            )
        except AcceptanceWitnessError as exc:
            discovery_errors.append(str(exc))
            continue
        targets.update(set(official_defs) - set(base_defs))
        completed = subprocess.run(
            ("git", "diff", "--unified=0", "HEAD", "--", relative),
            cwd=official_tree,
            text=True,
            capture_output=True,
            check=True,
        )
        touched = _added_lines(completed.stdout)
        for nodeid, (start, end) in official_defs.items():
            if any(start <= line <= end for line in touched):
                targets.add(nodeid)

    required = bool(targets) or bool(discovery_errors)
    return {
        "protocol": ACCEPTANCE_WITNESS_SPEC_PROTOCOL,
        "required": required,
        "targets": sorted(targets),
        "changed_test_files": list(test_paths),
        "optional_dependencies": list(infer_optional_test_dependencies(tests_patch)),
        "discovery_errors": discovery_errors,
    }


_PYTEST_PLUGIN_SOURCE = r"""from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_PROTOCOL = "claim-plane.pytest-witness-session.v1"
_COLLECTED = []
_REPORTS = {}
_BOOTSTRAP = []
_BOOTSTRAP_ATTEMPTED = False


def _worker(config):
    return hasattr(config, "workerinput")


def _dependencies():
    raw = os.environ.get("CLAIM_PLANE_ACCEPTANCE_OPTIONAL_DEPENDENCIES", "[]")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _module_available(module):
    importlib.invalidate_caches()
    if importlib.util.find_spec(module) is None:
        return False
    try:
        __import__(module)
    except Exception:
        return False
    return True


def _bootstrap_optional_dependencies(phase):
    global _BOOTSTRAP_ATTEMPTED
    if _BOOTSTRAP_ATTEMPTED:
        return
    _BOOTSTRAP_ATTEMPTED = True
    for item in _dependencies():
        if not isinstance(item, dict):
            continue
        package = str(item.get("package") or "")
        module = str(item.get("module") or "")
        record = {
            "package": package,
            "module": module,
            "installed": False,
            "phase": phase,
        }
        if not package or not module:
            record["available"] = False
            record["error"] = "invalid optional dependency declaration"
            _BOOTSTRAP.append(record)
            continue
        if _module_available(module):
            record["available"] = True
            _BOOTSTRAP.append(record)
            continue
        uv = shutil.which("uv")
        command = (
            [uv, "pip", "install", "--python", sys.executable, package]
            if uv
            else [sys.executable, "-m", "pip", "install", package]
        )
        try:
            completed = subprocess.run(
                command, text=True, capture_output=True, check=False, timeout=300
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            record.update(
                {
                    "available": False,
                    "error": f"optional dependency installation failed: {exc}",
                }
            )
            _BOOTSTRAP.append(record)
            continue
        record.update(
            {
                "returncode": completed.returncode,
                "stdout_tail": completed.stdout[-2000:],
                "stderr_tail": completed.stderr[-2000:],
            }
        )
        if completed.returncode == 0:
            record["installed"] = True
            record["available"] = _module_available(module)
            if not record["available"]:
                record["error"] = "optional dependency import failed after installation"
        else:
            record["available"] = False
            record["error"] = "optional dependency installation failed"
        _BOOTSTRAP.append(record)


def pytest_load_initial_conftests(early_config, parser, args):
    # This hook runs before repository conftest.py files are imported. Projects often
    # cache optional-dependency availability at conftest import time, so installing in
    # pytest_configure is already too late.
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    _bootstrap_optional_dependencies("pre-collection")


def pytest_configure(config):
    if _worker(config):
        return
    # Safety fallback for pytest launchers that do not invoke the early hook for an
    # externally loaded plugin. Fail-closed witness assessment still rejects any test
    # skipped because this fallback was too late.
    _bootstrap_optional_dependencies("configure-fallback")


def pytest_collection_finish(session):
    if _worker(session.config):
        return
    _COLLECTED[:] = [item.nodeid for item in session.items]


def pytest_runtest_logreport(report):
    if report.nodeid not in _COLLECTED:
        _COLLECTED.append(report.nodeid)
    phases = _REPORTS.setdefault(report.nodeid, {})
    item = {"outcome": report.outcome}
    if hasattr(report, "wasxfail"):
        item["wasxfail"] = str(report.wasxfail)
    if report.skipped:
        item["longrepr"] = str(report.longrepr)
    phases[report.when] = item


def pytest_sessionfinish(session, exitstatus):
    if _worker(session.config):
        return
    target = os.environ.get("CLAIM_PLANE_ACCEPTANCE_WITNESS_PATH")
    if not target:
        return
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"protocol": "claim-plane.pytest-witness-log.v1", "sessions": []}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(current, dict) and isinstance(current.get("sessions"), list):
                payload = current
        except (OSError, json.JSONDecodeError):
            pass
    payload["sessions"].append(
        {
            "protocol": _PROTOCOL,
            "exitstatus": int(exitstatus),
            "collected": list(_COLLECTED),
            "reports": dict(_REPORTS),
            "optional_dependencies": list(_BOOTSTRAP),
        }
    )
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
"""


def prepare_pytest_witness_environment(
    *,
    plugin_dir: Path,
    witness_path: Path,
    spec: Mapping[str, Any],
    source_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return evaluator environment values that activate the external plugin."""

    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / f"{_PYTEST_PLUGIN_MODULE}.py").write_text(
        _PYTEST_PLUGIN_SOURCE, encoding="utf-8"
    )
    env = dict(os.environ if source_env is None else source_env)
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(plugin_dir) + (
        os.pathsep + current_pythonpath if current_pythonpath else ""
    )
    current_plugins = env.get("PYTEST_PLUGINS", "")
    env["PYTEST_PLUGINS"] = ",".join(
        item for item in (_PYTEST_PLUGIN_MODULE, current_plugins) if item
    )
    env["CLAIM_PLANE_ACCEPTANCE_WITNESS_PATH"] = str(witness_path)
    env["CLAIM_PLANE_ACCEPTANCE_OPTIONAL_DEPENDENCIES"] = json.dumps(
        spec.get("optional_dependencies") or [], separators=(",", ":")
    )
    return env


def _target_matches(target: str, nodeid: str) -> bool:
    return nodeid == target or nodeid.startswith(target + "[")


def assess_acceptance_witness(
    spec: Mapping[str, Any], witness_path: Path
) -> dict[str, Any]:
    """Return a fail-closed verdict for the private test identities."""

    required = bool(spec.get("required"))
    targets = [str(item) for item in spec.get("targets") or ()]
    discovery_errors = [str(item) for item in spec.get("discovery_errors") or ()]
    base: dict[str, Any] = {
        "protocol": ACCEPTANCE_WITNESS_PROTOCOL,
        "required": required,
        "targets": targets,
        "changed_test_files": list(spec.get("changed_test_files") or ()),
        "optional_dependencies": list(spec.get("optional_dependencies") or ()),
        "target_count": len(targets),
    }
    if not required:
        return {
            **base,
            "state": "NOT_REQUIRED",
            "verified": True,
            "collected": 0,
            "executed": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "missing": 0,
            "details": [],
        }
    if discovery_errors or not targets:
        return {
            **base,
            "state": "INCOMPLETE",
            "verified": False,
            "collected": 0,
            "executed": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "missing": len(targets),
            "details": discovery_errors or ["no private pytest targets identified"],
        }
    if not witness_path.is_file():
        return {
            **base,
            "state": "INCOMPLETE",
            "verified": False,
            "collected": 0,
            "executed": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "missing": len(targets),
            "details": ["pytest witness file was not produced"],
        }
    try:
        payload = json.loads(witness_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            **base,
            "state": "INCOMPLETE",
            "verified": False,
            "collected": 0,
            "executed": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "missing": len(targets),
            "details": [f"pytest witness is unreadable: {exc}"],
        }
    sessions = payload.get("sessions") if isinstance(payload, Mapping) else None
    if not isinstance(sessions, list):
        sessions = []
    collected_nodes: set[str] = set()
    reports: dict[str, dict[str, Mapping[str, Any]]] = {}
    dependency_records: list[Mapping[str, Any]] = []
    for session in sessions:
        if not isinstance(session, Mapping):
            continue
        collected_nodes.update(str(item) for item in session.get("collected") or ())
        raw_reports = session.get("reports")
        if isinstance(raw_reports, Mapping):
            for nodeid, phases in raw_reports.items():
                if isinstance(phases, Mapping):
                    bucket = reports.setdefault(str(nodeid), {})
                    for phase, report in phases.items():
                        if isinstance(report, Mapping):
                            bucket[str(phase)] = report
        dependency_records.extend(
            item
            for item in session.get("optional_dependencies") or ()
            if isinstance(item, Mapping)
        )

    details: list[dict[str, Any]] = []
    totals = {
        "collected": 0,
        "executed": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "missing": 0,
    }
    for target in targets:
        matches = sorted(
            nodeid for nodeid in collected_nodes if _target_matches(target, nodeid)
        )
        target_detail: dict[str, Any] = {"target": target, "nodeids": matches}
        if not matches:
            totals["missing"] += 1
            target_detail["state"] = "NOT_COLLECTED"
            details.append(target_detail)
            continue
        totals["collected"] += len(matches)
        states: list[str] = []
        for nodeid in matches:
            phases = reports.get(nodeid, {})
            phase_outcomes = {
                phase: str(report.get("outcome") or "")
                for phase, report in phases.items()
            }
            if "skipped" in phase_outcomes.values():
                state = "SKIPPED"
                totals["skipped"] += 1
            elif "failed" in phase_outcomes.values():
                state = "FAILED"
                totals["failed"] += 1
                totals["executed"] += int("call" in phases)
            elif phase_outcomes.get("call") == "passed" and not phases.get(
                "call", {}
            ).get("wasxfail"):
                state = "PASSED"
                totals["passed"] += 1
                totals["executed"] += 1
            else:
                state = "NOT_EXECUTED"
            states.append(state)
        target_detail["states"] = states
        if all(state == "PASSED" for state in states):
            target_detail["state"] = "PASSED"
        elif any(state == "FAILED" for state in states):
            target_detail["state"] = "FAILED"
        elif any(state == "SKIPPED" for state in states):
            target_detail["state"] = "SKIPPED"
        else:
            target_detail["state"] = "NOT_EXECUTED"
        details.append(target_detail)

    failed = totals["failed"] > 0
    incomplete = (
        totals["missing"] > 0
        or totals["skipped"] > 0
        or totals["passed"] != totals["collected"]
        or totals["collected"] == 0
    )
    state = "FAILED" if failed else "INCOMPLETE" if incomplete else "VERIFIED"
    return {
        **base,
        "state": state,
        "verified": state == "VERIFIED",
        **totals,
        "details": details,
        "dependency_bootstrap": dependency_records,
    }


def install_optional_test_dependencies(
    python_path: Path,
    dependencies: Sequence[Mapping[str, str]],
    *,
    cache_dir: Path | None = None,
) -> tuple[dict[str, Any], ...]:
    """Install explicit private-test prerequisites into a prepared task runtime."""

    results: list[dict[str, Any]] = []
    for item in dependencies:
        package = str(item.get("package") or "")
        module = str(item.get("module") or "")
        record: dict[str, Any] = {"package": package, "module": module}
        if not package or not module:
            record.update({"installed": False, "error": "invalid dependency"})
            results.append(record)
            continue
        probe = subprocess.run(
            (str(python_path), "-c", f"import {module}"),
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        if probe.returncode == 0:
            record.update({"available": True, "installed": False})
            results.append(record)
            continue
        uv = shutil.which("uv")
        command: list[str]
        if uv:
            command = [uv, "pip", "install", "--python", str(python_path), package]
        else:
            command = [str(python_path), "-m", "pip", "install", package]
        env = os.environ.copy()
        if cache_dir is not None:
            env["UV_CACHE_DIR"] = str(cache_dir)
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=300,
            env=env,
        )
        record.update(
            {
                "installed": completed.returncode == 0,
                "returncode": completed.returncode,
                "stdout_tail": completed.stdout[-2000:],
                "stderr_tail": completed.stderr[-2000:],
            }
        )
        if completed.returncode != 0:
            raise AcceptanceWitnessError(
                f"could not install optional test dependency {package}: "
                + (
                    completed.stderr or completed.stdout or "installation failed"
                ).strip()
            )
        results.append(record)
    return tuple(results)
