"""Reusable development environments for comparative validation tasks.

The official CooperBench evaluator remains isolated and authoritative.  This module
prepares the dependency prefix of the frozen evaluator once per repository/task
contract, then exposes the same environment to Bare, Observe, and Guarded Codex so
all arms can run targeted tests during development.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from claim_plane.runtime_progress import run_streaming_process

VALIDATION_ENVIRONMENT_PROTOCOL = "claim-plane.validation-environment.v2"
_ENVIRONMENT_FILE = "environment.json"
_TEST_COMMAND = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=[^\s]+\s+)*"
    r"(?:(?:uv|poetry|pipenv)\s+run\s+)?"
    r"(?:python(?:3(?:\.\d+)?)?\s+-m\s+pytest|pytest|py\.test|tox|nox|"
    r"cargo\s+test|go\s+test|npm\s+(?:run\s+)?test|"
    r"pnpm\s+(?:run\s+)?test|yarn\s+(?:run\s+)?test|"
    r"bun\s+(?:run\s+)?test|breeze\s+testing\s+tests)(?:\s|$)",
    re.IGNORECASE,
)
_EXIT_TRAP = re.compile(r"^\s*trap\b.*(?:EXIT|\b0\b)")

_CODEX_ENVIRONMENT_KEYS = (
    "PATH",
    "VIRTUAL_ENV",
    "UV_CACHE_DIR",
    "UV_PROJECT_ENVIRONMENT",
    "PYTHONNOUSERSITE",
    "PYTHONPATH",
    "CLAIM_PLANE_VALIDATION_ENVIRONMENT",
    "CLAIM_PLANE_VALIDATION_PYTHON",
)

# CPython's macOS framework launcher consults these variables before resolving
# virtual-environment paths.  A Claim Plane process started from pyenv can
# otherwise leak its own launcher into the prepared task interpreter.
_PYTHON_LAUNCHER_ENVIRONMENT_KEYS = (
    "__PYVENV_LAUNCHER__",
    "PYTHONEXECUTABLE",
    "PYTHONHOME",
)


def _sanitized_python_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment that cannot redirect a prepared Python runtime."""

    env = dict(os.environ if source is None else source)
    for key in _PYTHON_LAUNCHER_ENVIRONMENT_KEYS:
        env.pop(key, None)
    return env


class ValidationEnvironmentError(RuntimeError):
    """Raised when a frozen development environment cannot be prepared safely."""


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256(value: bytes | str | Mapping[str, Any]) -> str:
    if isinstance(value, Mapping):
        data = _canonical_json(value).encode("utf-8")
    elif isinstance(value, str):
        data = value.encode("utf-8")
    else:
        data = value
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValidationEnvironmentError(f"expected a JSON object: {path}")
    return payload


def _run_git(
    root: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ("git", *args),
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=900,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git command failed").strip()
        raise ValidationEnvironmentError(detail)
    return completed


def _environment_identity(
    *, task: Mapping[str, Any], runner: Path, source_revision: str
) -> dict[str, Any]:
    runner_bytes = runner.read_bytes()
    return {
        "clone_url": str(task["clone_url"]),
        "base_commit": str(task["base_commit"]),
        "runner_sha256": _sha256(runner_bytes),
        "source_revision": source_revision,
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform": platform.system().lower(),
        "machine": platform.machine().lower(),
    }


def _dependency_prefix(runner: Path) -> str | None:
    """Return the frozen evaluator prefix ending before official tests begin."""

    lines = runner.read_text(encoding="utf-8", errors="replace").splitlines()
    boundary: int | None = None
    for index, line in enumerate(lines):
        if "RUNNING_TESTS" in line or _TEST_COMMAND.match(line):
            boundary = index
            break
    if boundary is None:
        # A runner without a recognizable test boundary cannot be truncated safely.
        # Dependency-free fixtures use a trivial script and receive a minimal venv.
        meaningful = [
            line.strip()
            for line in lines
            if line.strip()
            and not line.lstrip().startswith("#")
            and line.strip() not in {"set -e", "set -eu", "set -euo pipefail", "exit 0"}
        ]
        if meaningful:
            raise ValidationEnvironmentError(
                "frozen evaluator has no recognizable test boundary; "
                "refusing to execute the full acceptance script during prefetch"
            )
        return None
    retained = [line for line in lines[:boundary] if not _EXIT_TRAP.search(line)]
    if retained and retained[0].startswith("#!"):
        retained[0] = "#!/usr/bin/env bash"
    else:
        retained.insert(0, "#!/usr/bin/env bash")
    retained.extend(("", "echo CLAIM_PLANE_DEPENDENCIES_READY", ""))
    return "\n".join(retained)


def _marker_patch(path: str, content: str) -> str:
    """Return a minimal valid patch for evaluator bootstrap arguments."""

    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1 @@\n"
        f"+{content}\n"
    )


def _minimal_venv(path: Path) -> None:
    completed = subprocess.run(
        (sys.executable, "-m", "venv", str(path)),
        text=True,
        capture_output=True,
        check=False,
        timeout=300,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr or completed.stdout or "venv creation failed"
        ).strip()
        raise ValidationEnvironmentError(detail)


def _dependency_digest(python_path: Path, *, project_root: Path | None = None) -> str:
    inventory_code = """
import importlib.metadata as metadata
import json
from pathlib import Path
import sys
from urllib.parse import unquote, urlparse

project_root = Path(sys.argv[1]).resolve() if sys.argv[1] else None
items = []
for distribution in metadata.distributions():
    direct_url = distribution.read_text("direct_url.json")
    if direct_url:
        try:
            direct = json.loads(direct_url)
            editable = bool(direct.get("dir_info", {}).get("editable"))
            url = str(direct.get("url") or "")
        except (AttributeError, json.JSONDecodeError):
            editable = False
            url = ""
        if editable:
            continue
        if project_root is not None and url.startswith("file://"):
            source = Path(unquote(urlparse(url).path)).resolve()
            if source == project_root:
                continue
    name = distribution.metadata.get("Name")
    if name:
        items.append((name.casefold(), distribution.version))
print(json.dumps(sorted(items), separators=(",", ":")))
"""
    completed = subprocess.run(
        (
            str(python_path),
            "-c",
            inventory_code,
            str(project_root.resolve()) if project_root is not None else "",
        ),
        env=_sanitized_python_environment(),
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr or completed.stdout or "dependency inventory failed"
        ).strip()
        raise ValidationEnvironmentError(detail)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationEnvironmentError(
            "dependency inventory did not return JSON"
        ) from exc
    if not isinstance(payload, list):
        raise ValidationEnvironmentError("dependency inventory is not a JSON list")
    return _sha256(json.dumps(payload, separators=(",", ":")))


def _python_runtime_snapshot(python_path: Path) -> dict[str, Any]:
    probe = r"""
import importlib.util
import json
from pathlib import Path
import site
import sys
import sysconfig

paths = []
for candidate in (
    *getattr(site, "getsitepackages", lambda: [])(),
    sysconfig.get_paths().get("purelib"),
    sysconfig.get_paths().get("platlib"),
):
    if candidate and candidate not in paths:
        paths.append(candidate)
print(json.dumps({
    "python": str(Path(sys.executable)),
    "prefix": str(Path(sys.prefix).resolve()),
    "base_prefix": str(Path(sys.base_prefix).resolve()),
    "site_packages": paths,
    "pytest_available": importlib.util.find_spec("pytest") is not None,
}, sort_keys=True))
"""
    completed = subprocess.run(
        (str(python_path), "-c", probe),
        env=_sanitized_python_environment(),
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr or completed.stdout or "Python runtime probe failed"
        ).strip()
        raise ValidationEnvironmentError(detail)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationEnvironmentError(
            "Python runtime probe did not return JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ValidationEnvironmentError("Python runtime probe is not a JSON object")
    return payload


def _valid_environment(marker: Mapping[str, Any], *, digest: str) -> bool:
    if marker.get("protocol") != VALIDATION_ENVIRONMENT_PROTOCOL:
        return False
    if marker.get("identity_digest") != digest:
        return False
    python_path = Path(str(marker.get("python") or ""))
    if not python_path.is_file():
        return False
    expected_dependencies = str(marker.get("dependency_digest") or "")
    if not expected_dependencies:
        return False
    repository_value = str(marker.get("repository") or "")
    project_root = Path(repository_value) if repository_value else None
    try:
        return (
            _dependency_digest(python_path, project_root=project_root)
            == expected_dependencies
        )
    except ValidationEnvironmentError:
        return False


def prepare_task_environment(
    *,
    validation_root: Path,
    task: Mapping[str, Any],
    source_revision: str,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
    stream_output: bool = True,
) -> dict[str, Any]:
    """Prepare or reuse one task-level dependency environment."""

    task_dir = Path(str(task["task_dir"])).expanduser().resolve()
    runner = task_dir / "run_tests.sh"
    if not runner.is_file():
        raise ValidationEnvironmentError(f"frozen evaluator is missing: {runner}")
    identity = _environment_identity(
        task=task, runner=runner, source_revision=source_revision
    )
    identity_digest = _sha256(identity)
    environment_root = validation_root / "environments" / identity_digest[:24]
    repository = environment_root / "repository"
    marker_path = environment_root / _ENVIRONMENT_FILE
    cache_dir = validation_root / "cache" / "uv"
    cache_dir.mkdir(parents=True, exist_ok=True)

    if marker_path.exists() and not force:
        marker = _read_json(marker_path)
        if _valid_environment(marker, digest=identity_digest):
            return {**marker, "cache_hit": True}
    if environment_root.exists():
        shutil.rmtree(environment_root)
    environment_root.mkdir(parents=True, exist_ok=True)

    if progress is not None:
        progress(f"Preparing dependency environment for {task['task_id']}")
    completed = subprocess.run(
        ("git", "clone", str(task["clone_url"]), str(repository)),
        text=True,
        capture_output=True,
        check=False,
        timeout=900,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git clone failed").strip()
        raise ValidationEnvironmentError(detail)
    base_commit = str(task["base_commit"])
    _run_git(repository, "fetch", "--force", "origin", base_commit)
    _run_git(repository, "checkout", "--detach", base_commit)
    _run_git(repository, "reset", "--hard", base_commit)
    _run_git(repository, "clean", "-ffd")

    prefix = _dependency_prefix(runner)
    venv = repository / ".venv"
    bootstrap_stdout = ""
    bootstrap_stderr = ""
    if prefix is None:
        _minimal_venv(venv)
    else:
        prefix_path = environment_root / "dependency-prefix.sh"
        prefix_path.write_text(prefix, encoding="utf-8")
        prefix_path.chmod(0o755)
        patch_path = environment_root / "runner-input.patch"
        patch_path.write_text(
            _marker_patch(
                ".claim-plane-evaluator-marker",
                "claim-plane evaluator input prepared",
            ),
            encoding="utf-8",
        )
        feature_patch_path = environment_root / "feature-input.patch"
        feature_patch_path.write_text(
            _marker_patch(
                ".claim-plane-feature-marker",
                "claim-plane feature input prepared",
            ),
            encoding="utf-8",
        )
        env = _sanitized_python_environment()
        env.pop("UV_SYSTEM_PYTHON", None)
        env["UV_CACHE_DIR"] = str(cache_dir)
        command = (
            "bash",
            "-c",
            prefix,
            str(runner),
            str(repository),
            str(patch_path),
            str(feature_patch_path),
        )
        streamed = run_streaming_process(
            command,
            cwd=repository,
            env=env,
            timeout=900,
            heartbeat_seconds=15.0,
            on_output=(
                (
                    lambda name, line: print(
                        line,
                        end="",
                        file=sys.stderr if name == "stderr" else sys.stdout,
                        flush=True,
                    )
                )
                if stream_output
                else None
            ),
            on_heartbeat=(
                None
                if progress is None
                else lambda elapsed: progress(
                    f"Dependency environment still preparing · {int(elapsed)}s elapsed"
                )
            ),
        )
        bootstrap_stdout = streamed.stdout
        bootstrap_stderr = streamed.stderr
        if streamed.interrupted:
            raise KeyboardInterrupt
        if streamed.timed_out:
            raise ValidationEnvironmentError(
                "dependency environment preparation timed out after 900 seconds"
            )
        if streamed.returncode != 0:
            detail = (
                streamed.stderr or streamed.stdout or "dependency bootstrap failed"
            ).strip()
            raise ValidationEnvironmentError(detail)
        if not venv.is_dir():
            raise ValidationEnvironmentError(
                "frozen evaluator dependency prefix did not create .venv"
            )

    python_path = venv / "bin" / "python"
    if not python_path.is_file():
        python_path = venv / "Scripts" / "python.exe"
    if not python_path.is_file():
        raise ValidationEnvironmentError("prepared environment has no Python runtime")

    # Keep the reusable environment while restoring the seed repository itself.
    (repository / ".claim-plane-evaluator-marker").unlink(missing_ok=True)
    _run_git(repository, "reset", "--hard", base_commit)
    _run_git(repository, "clean", "-ffd", "-e", ".venv/")
    runtime = _python_runtime_snapshot(python_path)
    marker = {
        "protocol": VALIDATION_ENVIRONMENT_PROTOCOL,
        "created_at": (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
        "task_id": str(task["task_id"]),
        "identity": identity,
        "identity_digest": identity_digest,
        "environment_root": str(environment_root),
        "repository": str(repository),
        "venv": str(venv),
        "python": str(python_path),
        "python_prefix": runtime["prefix"],
        "site_packages": list(runtime.get("site_packages") or ()),
        "pytest_available": bool(runtime.get("pytest_available")),
        "cache_dir": str(cache_dir),
        "dependency_digest": _dependency_digest(python_path, project_root=repository),
        "bootstrap_stdout_sha256": _sha256(bootstrap_stdout),
        "bootstrap_stderr_sha256": _sha256(bootstrap_stderr),
    }
    _write_json(marker_path, marker)
    return {**marker, "cache_hit": False}


def activate_task_environment(
    environment: Mapping[str, Any],
    *,
    workspace: Path,
    progress: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """Bind the reusable dependency environment to one arm workspace."""

    venv = Path(str(environment["venv"]))
    python_path = Path(str(environment["python"]))
    bin_dir = python_path.parent
    if not python_path.is_file():
        raise ValidationEnvironmentError("validation environment disappeared")

    installable = any(
        (workspace / name).exists()
        for name in ("pyproject.toml", "setup.py", "setup.cfg")
    )
    if installable:
        if progress is not None:
            progress("Binding editable candidate source to shared dependencies")
        uv = shutil.which("uv")
        command: tuple[str, ...]
        if uv is not None:
            command = (
                uv,
                "pip",
                "install",
                "--python",
                str(python_path),
                "--no-deps",
                "-e",
                str(workspace),
            )
        else:
            command = (
                str(python_path),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "-e",
                str(workspace),
            )
        install_env = _sanitized_python_environment()
        install_env["UV_CACHE_DIR"] = str(environment["cache_dir"])
        install_env["VIRTUAL_ENV"] = str(venv)
        install_env["UV_PROJECT_ENVIRONMENT"] = str(venv)
        completed = subprocess.run(
            command,
            cwd=workspace,
            text=True,
            capture_output=True,
            check=False,
            timeout=300,
            env=install_env,
        )
        if completed.returncode != 0:
            detail = (
                completed.stderr or completed.stdout or "editable install failed"
            ).strip()
            raise ValidationEnvironmentError(detail)

    env = _sanitized_python_environment()
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["UV_CACHE_DIR"] = str(environment["cache_dir"])
    env["UV_PROJECT_ENVIRONMENT"] = str(venv)
    env["PYTHONNOUSERSITE"] = "1"
    env["CLAIM_PLANE_VALIDATION_ENVIRONMENT"] = str(environment["identity_digest"])
    env["CLAIM_PLANE_VALIDATION_PYTHON"] = str(python_path)
    python_paths: list[str] = []
    src = workspace / "src"
    if src.is_dir():
        python_paths.append(str(src))
    for item in environment.get("site_packages") or ():
        path = Path(str(item))
        if path.is_dir() and str(path) not in python_paths:
            python_paths.append(str(path))
    current = env.get("PYTHONPATH", "")
    if current:
        python_paths.append(current)
    if python_paths:
        env["PYTHONPATH"] = os.pathsep.join(python_paths)
    return env


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def codex_environment_config_overrides(env: Mapping[str, str]) -> tuple[str, ...]:
    """Return one-run Codex config overrides that pin the development runtime.

    Codex applies ``shell_environment_policy`` to shell tools independently of the
    environment inherited by the TUI process. Explicit ``set`` values plus a
    non-login shell prevent the user's profile from replacing the prepared PATH.
    """

    missing = [key for key in ("PATH", "VIRTUAL_ENV") if not env.get(key)]
    if missing:
        raise ValidationEnvironmentError(
            "validation environment is missing Codex shell values: "
            + ", ".join(missing)
        )
    overrides = [
        "allow_login_shell=false",
        'shell_environment_policy.inherit="all"',
    ]
    for key in _CODEX_ENVIRONMENT_KEYS:
        value = env.get(key)
        if value:
            overrides.append(
                f"shell_environment_policy.set.{key}={_toml_string(value)}"
            )
    return tuple(overrides)


def _top_level_test_imports(workspace: Path) -> tuple[str, ...]:
    conftest = workspace / "tests" / "conftest.py"
    if not conftest.is_file():
        return ()
    try:
        tree = ast.parse(conftest.read_text(encoding="utf-8"), filename=str(conftest))
    except (OSError, SyntaxError):
        return ()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.partition(".")[0])
    external = {
        name
        for name in names
        if not (workspace / name).exists()
        and not (workspace / f"{name}.py").exists()
        and not (workspace / "src" / name).exists()
    }
    return tuple(sorted(external))


def preflight_task_environment(
    environment: Mapping[str, Any],
    *,
    workspace: Path,
    env: Mapping[str, str],
) -> dict[str, Any]:
    """Prove that targeted tests will use the prepared Python environment."""

    python_path = Path(str(environment["python"])).expanduser().absolute()
    venv = Path(str(environment["venv"])).expanduser().resolve()
    if not python_path.is_file():
        raise ValidationEnvironmentError("validation environment Python disappeared")
    test_imports = _top_level_test_imports(workspace)
    probe = r"""
import importlib
import importlib.metadata as metadata
import importlib.util
import json
import os
from pathlib import Path
import sys
from urllib.parse import unquote, urlparse

workspace = Path(sys.argv[1]).resolve()
requested = json.loads(sys.argv[2])
stdlib = getattr(sys, "stdlib_module_names", set())
missing = []
available = []
for name in requested:
    if name in stdlib:
        continue
    if importlib.util.find_spec(name) is None:
        missing.append(name)
    else:
        available.append(name)

project_modules = []
project_errors = []
for distribution in metadata.distributions():
    raw = distribution.read_text("direct_url.json")
    if not raw:
        continue
    try:
        direct = json.loads(raw)
        editable = bool(direct.get("dir_info", {}).get("editable"))
        url = str(direct.get("url") or "")
    except (AttributeError, json.JSONDecodeError):
        continue
    if not editable or not url.startswith("file://"):
        continue
    path = Path(unquote(urlparse(url).path)).resolve()
    if path != workspace:
        continue
    top_level = distribution.read_text("top_level.txt") or ""
    candidates = [line.strip() for line in top_level.splitlines() if line.strip()]
    if not candidates:
        name = distribution.metadata.get("Name")
        if name:
            candidates = [name.replace("-", "_")]
    for name in candidates:
        try:
            importlib.import_module(name)
            project_modules.append(name)
        except Exception as exc:
            project_errors.append(f"{name}: {type(exc).__name__}: {exc}")

pytest_spec = importlib.util.find_spec("pytest")
pytest_available = pytest_spec is not None
pytest_version = None
pytest_error = None
if pytest_available:
    try:
        pytest_module = importlib.import_module("pytest")
        pytest_version = getattr(pytest_module, "__version__", None)
    except Exception as exc:
        pytest_available = False
        pytest_error = f"{type(exc).__name__}: {exc}"

payload = {
    "python": str(Path(sys.executable)),
    "prefix": str(Path(sys.prefix).resolve()),
    "base_prefix": str(Path(sys.base_prefix).resolve()),
    "virtual_env": os.environ.get("VIRTUAL_ENV"),
    "pytest_available": pytest_available,
    "pytest_version": pytest_version,
    "pytest_origin": getattr(pytest_spec, "origin", None),
    "pytest_error": pytest_error,
    "test_modules_available": available,
    "test_modules_missing": missing,
    "project_modules": sorted(set(project_modules)),
    "project_import_errors": project_errors,
}
print(json.dumps(payload, sort_keys=True))
"""
    completed = subprocess.run(
        (str(python_path), "-c", probe, str(workspace), json.dumps(test_imports)),
        cwd=workspace,
        env=_sanitized_python_environment(env),
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr
            or completed.stdout
            or "development environment probe failed"
        ).strip()
        raise ValidationEnvironmentError(detail)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationEnvironmentError(
            "development environment probe did not return JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ValidationEnvironmentError(
            "development environment probe is not a JSON object"
        )
    actual_python = Path(str(payload.get("python") or "")).absolute()
    actual_prefix = Path(str(payload.get("prefix") or "")).resolve()
    actual_venv = Path(str(payload.get("virtual_env") or "")).resolve()
    if actual_python != python_path:
        raise ValidationEnvironmentError(
            f"development Python mismatch: expected {python_path}, got {actual_python}"
        )
    if actual_prefix != venv:
        raise ValidationEnvironmentError(
            f"development sys.prefix mismatch: expected {venv}, got {actual_prefix}"
        )
    if actual_venv != venv:
        raise ValidationEnvironmentError(
            f"development VIRTUAL_ENV mismatch: expected {venv}, got {actual_venv}"
        )
    installable_python = any(
        (workspace / name).exists()
        for name in ("pyproject.toml", "setup.py", "setup.cfg")
    )
    if installable_python and not payload.get("pytest_available"):
        detail = str(payload.get("pytest_error") or "not importable")
        raise ValidationEnvironmentError(
            "prepared Python environment does not provide pytest: " + detail
        )
    missing = payload.get("test_modules_missing") or []
    if missing:
        raise ValidationEnvironmentError(
            "prepared Python environment is missing test imports: "
            + ", ".join(str(item) for item in missing)
        )
    errors = payload.get("project_import_errors") or []
    if errors:
        detail = "; ".join(str(item) for item in errors)
        raise ValidationEnvironmentError(f"editable candidate import failed: {detail}")
    return payload


def environment_marker_paths(validation_root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted((validation_root / "environments").glob(f"*/{_ENVIRONMENT_FILE}"))
    )
