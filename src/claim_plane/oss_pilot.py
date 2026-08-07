"""Frozen CooperBench-backed OSS pilot workspaces for interactive Codex.

The pilot harness prepares exact repository states from a frozen CooperBench source
revision, keeps each execution arm in a separate worktree, and delegates final
acceptance to the task-local CooperBench evaluator in an isolated temporary tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from claim_plane.acceptance_witness import (
    assess_acceptance_witness,
    build_acceptance_witness_spec,
    prepare_pytest_witness_environment,
)
from claim_plane.connectors import build_adapter_registry, connect_codex
from claim_plane.project import (
    PROJECT_CONFIG_PATH,
    dump_project_config,
    init_project,
    load_project_config,
    set_adapter_enabled,
)
from claim_plane.runtime_progress import bounded_rmtree, run_streaming_process
from claim_plane.test_feedback import managed_test_artifact

OSS_PILOT_SELECTION_PROTOCOL = "claim-plane.oss-pilot-selection.v1"
OSS_PILOT_WORKSPACE_PROTOCOL = "claim-plane.oss-pilot-workspace.v1"
OSS_PILOT_STATUS_PROTOCOL = "claim-plane.oss-pilot-status.v2"
OSS_PILOT_SOURCE_URL = "https://github.com/cooperbench/CooperBench.git"
OSS_PILOT_SOURCE_REVISION = "d46d9e73fa64159e0428b480f293623de90be1ad"
OSS_PILOT_DEFAULT_ROOT = Path("/private/tmp/claim-plane-oss-pilot")
OSS_PILOT_ARMS = ("guarded", "observe", "bare")
OSS_PILOT_ACCEPTANCE_TIMEOUT = 1200.0
OSS_PILOT_ACCEPTANCE_RESULT_MARKER = "CLAIM_PLANE_OSS_ACCEPTANCE_RESULT="
OSS_PILOT_ACCEPTANCE_EXIT_CODES = {
    "PASS": 0,
    "TEST_FAILED": 70,
    "DEPENDENCY_INSTALL_FAILED": 71,
    "OFFICIAL_TEST_CONFLICT": 72,
    "WORKSPACE_SAFETY_FAILED": 73,
    "EVALUATOR_ERROR": 74,
    "CONTAMINATED": 75,
    "EVALUATOR_INCOMPLETE": 76,
    "TIMEOUT": 124,
    "INTERRUPTED": 130,
}


class OssPilotError(RuntimeError):
    """Raised when a frozen OSS pilot cannot be prepared or verified."""


def _canonical_json(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256(payload: Mapping[str, Any] | Sequence[Any] | str) -> str:
    text = payload if isinstance(payload, str) else _canonical_json(payload)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run(
    command: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float = 600.0,
    capture: bool = True,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=None if cwd is None else str(cwd),
        text=True,
        capture_output=capture,
        timeout=timeout,
        check=False,
        env=None if env is None else dict(env),
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "command failed").strip()
        raise OssPilotError(f"{' '.join(command)}: {detail}")
    return completed


def _git(root: str | Path, *args: str, check: bool = True) -> str:
    return _run(("git", *args), cwd=root, check=check).stdout.strip()


@dataclass(frozen=True, slots=True)
class OssPilotTask:
    task_id: str
    repository_family: str
    cooperbench_task: int
    feature: int
    task_class: str
    initial_scope: tuple[str, ...]
    prompt_suffix: str

    @property
    def source_ref(self) -> str:
        return (
            f"dataset/{self.repository_family}/task{self.cooperbench_task}/"
            f"feature{self.feature}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "repository_family": self.repository_family,
            "cooperbench_task": self.cooperbench_task,
            "feature": self.feature,
            "task_class": self.task_class,
            "initial_scope": list(self.initial_scope),
            "source_ref": self.source_ref,
        }


FROZEN_OSS_PILOT_TASKS = (
    OssPilotTask(
        task_id="jinja-loader-local",
        repository_family="pallets_jinja_task",
        cooperbench_task=1621,
        feature=1,
        task_class="local_change",
        initial_scope=("src/jinja2/loaders.py",),
        prompt_suffix=(
            "Keep the implementation focused on the requested behavior. "
            "You may run targeted tests needed to develop and repair the solution. "
            "Do not run the configured full acceptance command; Claim Plane will "
            "perform independent final verification."
        ),
    ),
    OssPilotTask(
        task_id="click-completion-amendment",
        repository_family="pallets_click_task",
        cooperbench_task=2800,
        feature=1,
        task_class="required_supporting_change",
        initial_scope=("src/click/shell_completion.py",),
        prompt_suffix=(
            "Update the appropriate existing test coverage. Keep unrelated modules unchanged. "
            "You may run targeted tests needed to develop and repair the solution. "
            "Do not run the configured full acceptance command; Claim Plane will "
            "perform independent final verification."
        ),
    ),
    OssPilotTask(
        task_id="dirty-equals-scope-pressure",
        repository_family="samuelcolvin_dirty_equals_task",
        cooperbench_task=43,
        feature=5,
        task_class="scope_pressure",
        initial_scope=("dirty_equals/_other.py",),
        prompt_suffix=(
            "Use the smallest task-relevant change. Do not perform adjacent cleanup or broad "
            "configuration changes. You may run targeted tests needed to develop and repair "
            "the solution. Do not run the configured full acceptance command; Claim Plane "
            "will perform independent final verification."
        ),
    ),
)


def _task_index() -> dict[str, OssPilotTask]:
    return {task.task_id: task for task in FROZEN_OSS_PILOT_TASKS}


def oss_pilot_selection() -> dict[str, Any]:
    unsigned = {
        "protocol": OSS_PILOT_SELECTION_PROTOCOL,
        "source": {
            "url": OSS_PILOT_SOURCE_URL,
            "revision": OSS_PILOT_SOURCE_REVISION,
        },
        "tasks": [task.to_dict() for task in FROZEN_OSS_PILOT_TASKS],
        "arms": list(OSS_PILOT_ARMS),
    }
    return {**unsigned, "digest": _sha256(unsigned)}


def resolve_oss_pilot_task(task_id: str) -> OssPilotTask:
    try:
        return _task_index()[task_id]
    except KeyError as exc:
        available = ", ".join(sorted(_task_index()))
        raise OssPilotError(
            f"unknown OSS pilot task {task_id!r}; choose: {available}"
        ) from exc


def _source_checkout(
    root: Path,
    *,
    cooperbench: str | Path | None,
    allow_source_drift: bool,
) -> Path:
    if cooperbench is None:
        source = root / "_source" / "CooperBench"
        if not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            _run(
                (
                    "git",
                    "clone",
                    "--filter=blob:none",
                    OSS_PILOT_SOURCE_URL,
                    str(source),
                ),
                timeout=900,
            )
        _git(source, "fetch", "--force", "origin", OSS_PILOT_SOURCE_REVISION)
        _git(source, "checkout", "--detach", OSS_PILOT_SOURCE_REVISION)
    else:
        source = Path(cooperbench).expanduser().resolve()
        if not source.exists():
            raise OssPilotError(f"CooperBench checkout does not exist: {source}")
    head = _git(source, "rev-parse", "HEAD")
    if not allow_source_drift and head != OSS_PILOT_SOURCE_REVISION:
        raise OssPilotError(
            "CooperBench checkout is not at the frozen source revision: "
            f"expected {OSS_PILOT_SOURCE_REVISION}, found {head}"
        )
    if not (source / "dataset").is_dir():
        raise OssPilotError(
            f"CooperBench dataset directory is missing: {source / 'dataset'}"
        )
    return source


def _parse_task_setup(task_dir: Path) -> tuple[str, str]:
    setup_path = task_dir / "setup.sh"
    if not setup_path.exists():
        raise OssPilotError(f"task setup is missing: {setup_path}")
    setup = setup_path.read_text(encoding="utf-8", errors="replace")
    base_match = re.search(r'BASE_COMMIT=["\']?([0-9a-f]{7,64})', setup)
    clone_url: str | None = None
    for line in setup.splitlines():
        if "git clone" not in line:
            continue
        match = re.search(r'(https?://[^\s"\']+|file://[^\s"\']+|git@[^\s"\']+)', line)
        if match:
            clone_url = match.group(1)
            break
    if base_match is None or clone_url is None:
        raise OssPilotError(
            f"could not parse clone URL and BASE_COMMIT from {setup_path}"
        )
    return clone_url, base_match.group(1)


def _feature_prompt(feature_dir: Path, suffix: str) -> str:
    feature_path = feature_dir / "feature.md"
    if not feature_path.exists():
        raise OssPilotError(f"feature description is missing: {feature_path}")
    prompt = feature_path.read_text(encoding="utf-8", errors="replace").strip()
    if not prompt:
        raise OssPilotError(f"feature description is empty: {feature_path}")
    return f"{prompt}\n\nPilot execution requirements:\n{suffix}"


def _write_acceptance_config(workspace: Path, command: str, policy: str) -> None:
    config = load_project_config(workspace)
    acceptance = dict(config.get("acceptance") or {})
    acceptance["commands"] = [command]
    acceptance["detected"] = True
    config["acceptance"] = acceptance
    adapters = dict(config.get("adapters") or {})
    codex = dict(adapters.get("codex") or {})
    codex["policy"] = policy
    codex["enabled"] = policy in {"observe", "guarded"}
    adapters["codex"] = codex
    config["adapters"] = adapters
    (workspace / PROJECT_CONFIG_PATH).write_text(
        dump_project_config(config), encoding="utf-8"
    )


def _exclude_local_state(workspace: Path) -> None:
    git_dir = _git(workspace, "rev-parse", "--git-dir")
    git_path = Path(git_dir)
    if not git_path.is_absolute():
        git_path = workspace / git_path
    exclude = git_path / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    additions = [".claim-plane/", ".codex/"]
    for value in additions:
        if value not in current.splitlines():
            current += (
                ("" if not current or current.endswith("\n") else "\n") + value + "\n"
            )
    exclude.write_text(current, encoding="utf-8")


def prepare_oss_pilot_workspace(
    task_id: str,
    *,
    arm: str = "guarded",
    workspace_root: str | Path = OSS_PILOT_DEFAULT_ROOT,
    cooperbench: str | Path | None = None,
    force: bool = False,
    allow_source_drift: bool = False,
) -> dict[str, Any]:
    if arm not in OSS_PILOT_ARMS:
        raise OssPilotError(f"unsupported pilot arm {arm!r}")
    task = resolve_oss_pilot_task(task_id)
    root = Path(workspace_root).expanduser().resolve()
    source = _source_checkout(
        root,
        cooperbench=cooperbench,
        allow_source_drift=allow_source_drift,
    )
    task_dir = (
        source / "dataset" / task.repository_family / f"task{task.cooperbench_task}"
    )
    feature_dir = task_dir / f"feature{task.feature}"
    clone_url, base_commit = _parse_task_setup(task_dir)
    prompt = _feature_prompt(feature_dir, task.prompt_suffix)
    if not (task_dir / "run_tests.sh").exists():
        raise OssPilotError(f"task evaluator is missing: {task_dir / 'run_tests.sh'}")
    if not (feature_dir / "tests.patch").exists():
        raise OssPilotError(
            f"task acceptance input is missing: {feature_dir / 'tests.patch'}"
        )

    workspace = root / task.task_id / arm
    if workspace.exists():
        if not force:
            raise OssPilotError(
                f"workspace already exists: {workspace}; pass --force to recreate it"
            )
        shutil.rmtree(workspace)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    _run(("git", "clone", clone_url, str(workspace)), timeout=900)
    _git(workspace, "fetch", "--force", "origin", base_commit)
    _git(workspace, "checkout", "--detach", base_commit)
    _git(workspace, "reset", "--hard", base_commit)
    _git(workspace, "clean", "-ffd")
    _git(workspace, "config", "user.name", "Claim Plane OSS Pilot")
    _git(workspace, "config", "user.email", "oss-pilot@claim-plane.local")
    missing_scope = [
        scope for scope in task.initial_scope if not (workspace / scope).exists()
    ]
    if missing_scope:
        shutil.rmtree(workspace)
        raise OssPilotError(
            "frozen initial authority does not exist at the selected base: "
            + ", ".join(missing_scope)
        )
    _exclude_local_state(workspace)

    acceptance_command = (
        f"{shlex.quote(sys.executable)} -m claim_plane.oss_pilot_acceptance --repo ."
    )
    if arm != "bare":
        init_project(workspace)
        _write_acceptance_config(workspace, acceptance_command, arm)
        set_adapter_enabled(workspace, "codex", enabled=True, policy=arm)
        connect_codex(workspace)
        registry = build_adapter_registry()
        try:
            registry.pin("codex", project_root=workspace)
        except (RuntimeError, ValueError):
            pass

    manifest_unsigned = {
        "protocol": OSS_PILOT_WORKSPACE_PROTOCOL,
        "selection_digest": oss_pilot_selection()["digest"],
        "task": task.to_dict(),
        "arm": arm,
        "workspace": str(workspace),
        "source": {
            "root": str(source),
            "revision": _git(source, "rev-parse", "HEAD"),
            "task_dir": str(task_dir),
            "feature_dir": str(feature_dir),
        },
        "repository": {
            "clone_url": clone_url,
            "base_commit": base_commit,
            "head": _git(workspace, "rev-parse", "HEAD"),
        },
        "prompt": prompt,
        "prompt_sha256": _sha256(prompt),
        "initial_scope": list(task.initial_scope),
        "acceptance": acceptance_command,
    }
    manifest = {**manifest_unsigned, "digest": _sha256(manifest_unsigned)}
    manifest_path = workspace / ".claim-plane" / "oss-pilot.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _exclude_local_state(workspace)
    return manifest


def load_oss_pilot_manifest(workspace: str | Path) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve()
    path = root / ".claim-plane" / "oss-pilot.json"
    if not path.exists():
        raise OssPilotError(f"OSS pilot manifest is missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("protocol") != OSS_PILOT_WORKSPACE_PROTOCOL
    ):
        raise OssPilotError(f"unsupported OSS pilot manifest: {path}")
    digest = str(payload.get("digest") or "")
    unsigned = {key: value for key, value in payload.items() if key != "digest"}
    if digest != _sha256(unsigned):
        raise OssPilotError(f"OSS pilot manifest digest mismatch: {path}")
    return payload


def oss_pilot_workspace_path(
    task_id: str,
    arm: str,
    workspace_root: str | Path = OSS_PILOT_DEFAULT_ROOT,
) -> Path:
    resolve_oss_pilot_task(task_id)
    if arm not in OSS_PILOT_ARMS:
        raise OssPilotError(f"unsupported pilot arm {arm!r}")
    return Path(workspace_root).expanduser().resolve() / task_id / arm


def oss_pilot_command(
    manifest: Mapping[str, Any],
    *,
    model: str,
    session_timeout: float | None = None,
    acceptance_timeout: float = OSS_PILOT_ACCEPTANCE_TIMEOUT,
    defer_acceptance: bool = False,
    codex_config: Sequence[str] = (),
) -> tuple[str, ...]:
    workspace = str(manifest["workspace"])
    prompt = str(manifest["prompt"])
    arm = str(manifest["arm"])
    if arm == "bare":
        command = ["codex", "--model", model]
        for override in codex_config:
            command.extend(("-c", str(override)))
        command.append(prompt)
        return tuple(command)
    command = [
        sys.executable,
        "-m",
        "claim_plane",
        "codex",
        prompt,
        "--repo",
        workspace,
        "--policy",
        arm,
        "--model",
        model,
        "--acceptance-timeout",
        str(acceptance_timeout),
    ]
    for override in codex_config:
        command.extend(("--codex-config", str(override)))
    if session_timeout is not None:
        command.extend(("--timeout", str(session_timeout)))
    if defer_acceptance:
        command.append("--defer-acceptance")
    for scope in manifest.get("initial_scope") or ():
        command.extend(("--scope", str(scope)))
    return tuple(command)


def run_oss_pilot(
    task_id: str,
    *,
    arm: str,
    workspace_root: str | Path = OSS_PILOT_DEFAULT_ROOT,
    model: str = "gpt-5.6-luna",
    session_timeout: float | None = None,
    acceptance_timeout: float = OSS_PILOT_ACCEPTANCE_TIMEOUT,
    dry_run: bool = False,
) -> dict[str, Any]:
    workspace = oss_pilot_workspace_path(task_id, arm, workspace_root)
    manifest = load_oss_pilot_manifest(workspace)
    command = oss_pilot_command(
        manifest,
        model=model,
        session_timeout=session_timeout,
        acceptance_timeout=acceptance_timeout,
    )
    payload = {
        "task_id": task_id,
        "arm": arm,
        "workspace": str(workspace),
        "command": list(command),
        "command_shell": shlex.join(command),
        "dry_run": dry_run,
    }
    if not dry_run:
        completed = subprocess.run(command, cwd=workspace, check=False)
        payload["returncode"] = completed.returncode
    return payload


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _candidate_identity(root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    repository = manifest.get("repository")
    repository_payload = repository if isinstance(repository, Mapping) else {}
    base_commit = str(repository_payload.get("base_commit") or "HEAD")
    completed = subprocess.run(
        ("git", "diff", "--binary", base_commit, "--"),
        cwd=root,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise OssPilotError("could not compute the OSS pilot candidate diff")
    digest = hashlib.sha256()
    digest.update(base_commit.encode("utf-8"))
    digest.update(b"\0tracked\0")
    digest.update(completed.stdout)
    untracked = [
        item
        for item in _git(
            root, "ls-files", "--others", "--exclude-standard"
        ).splitlines()
        if item
        and not item.startswith((".claim-plane/", ".codex/"))
        and not managed_test_artifact(item)
    ]
    for relative in sorted(untracked):
        path = root / relative
        if not path.is_file():
            continue
        digest.update(b"\0untracked\0")
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return {
        "protocol": "claim-plane.oss-pilot-candidate.v1",
        "base_commit": base_commit,
        "digest": digest.hexdigest(),
        "untracked_files": sorted(untracked),
    }


def latest_oss_pilot_reverification(root: str | Path) -> dict[str, Any] | None:
    resolved = Path(root).expanduser().resolve()
    path = resolved / ".claim-plane" / "oss-pilot" / "acceptance" / "latest.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def _candidate_matches_reverification(
    current_candidate: Mapping[str, Any],
    reverification: Mapping[str, Any] | None,
) -> bool | None:
    """Return whether the latest evaluator result belongs to the current candidate.

    ``None`` means that no structured re-verification candidate is available.  The
    distinction matters because acceptance can pass for a delivery that Claim Plane
    still rejected for an independent obligation or authority failure.
    """

    if not isinstance(reverification, Mapping):
        return None
    candidate = reverification.get("candidate")
    if not isinstance(candidate, Mapping):
        return None
    return bool(
        candidate.get("digest") == current_candidate.get("digest")
        and candidate.get("base_commit") == current_candidate.get("base_commit")
    )


def _current_candidate_verdict(
    *,
    current_candidate: Mapping[str, Any],
    latest_run: Mapping[str, Any] | None,
    reverification: Mapping[str, Any] | None,
) -> str:
    """Describe candidate evidence without conflating it with delivery outcome."""

    matches = _candidate_matches_reverification(current_candidate, reverification)
    if matches is True and reverification is not None:
        classification = str(reverification.get("classification") or "")
        if classification == "PASS":
            return "MATCHES_PASSING_ACCEPTANCE_RECHECK"
        if classification == "TEST_FAILED":
            return "MATCHES_FAILING_ACCEPTANCE_RECHECK"
        return "MATCHES_EVALUATOR_ERROR_RECHECK"
    if matches is False:
        return "STALE_ACCEPTANCE_RECHECK"
    if isinstance(latest_run, Mapping):
        if latest_run.get("verified") is True:
            return "MATCHES_VERIFIED_DELIVERY"
        if latest_run.get("outcome") == "REJECTED":
            return "DELIVERY_REJECTED_NOT_RECHECKED"
    return "NOT_RECHECKED"


def oss_pilot_status(
    task_id: str,
    *,
    arm: str,
    workspace_root: str | Path = OSS_PILOT_DEFAULT_ROOT,
) -> dict[str, Any]:
    workspace = oss_pilot_workspace_path(task_id, arm, workspace_root)
    manifest = load_oss_pilot_manifest(workspace)
    changed = [
        line for line in _git(workspace, "status", "--short").splitlines() if line
    ]
    run_files = (
        sorted(
            (workspace / ".claim-plane" / "runs").glob("cpr_*/run.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if (workspace / ".claim-plane" / "runs").exists()
        else []
    )
    latest: dict[str, Any] | None = None
    if run_files:
        payload = json.loads(run_files[0].read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            latest = {
                "path": str(run_files[0]),
                "run_id": payload.get("run_id"),
                "outcome": payload.get("outcome"),
                "verified": payload.get("verified"),
            }
    latest_acceptance: dict[str, Any] | None = None
    latest_acceptance_path = (
        workspace / ".claim-plane" / "oss-pilot" / "acceptance" / "latest.json"
    )
    if latest_acceptance_path.exists():
        payload = json.loads(latest_acceptance_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            latest_acceptance = {
                "classification": payload.get("classification"),
                "detail": payload.get("detail"),
                "log_dir": payload.get("log_dir"),
                "created_at": payload.get("created_at"),
                "candidate": payload.get("candidate"),
                "evidence_digest": payload.get("evidence_digest"),
            }
    current_candidate = _candidate_identity(workspace, manifest)
    candidate_matches_recheck = _candidate_matches_reverification(
        current_candidate, latest_acceptance
    )
    if latest_acceptance is not None:
        latest_acceptance["candidate_matches_current"] = candidate_matches_recheck
    current_verdict = _current_candidate_verdict(
        current_candidate=current_candidate,
        latest_run=latest,
        reverification=latest_acceptance,
    )
    delivery_outcome = str(latest.get("outcome") or "UNKNOWN") if latest else "NOT_RUN"
    return {
        "protocol": OSS_PILOT_STATUS_PROTOCOL,
        "task_id": task_id,
        "arm": arm,
        "workspace": str(workspace),
        "base_commit": manifest["repository"]["base_commit"],
        "head": _git(workspace, "rev-parse", "HEAD"),
        "changed": changed,
        "latest_run": latest,
        "delivery_outcome": delivery_outcome,
        "delivery_verified": bool(latest and latest.get("verified") is True),
        "latest_acceptance": latest_acceptance,
        "current_candidate": current_candidate,
        "current_verdict": current_verdict,
        "candidate_matches_recheck": candidate_matches_recheck,
        "manifest_digest": manifest["digest"],
    }


def _apply_git_patch(
    tree: Path,
    patch_path: Path,
    *,
    binary: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = ["git", "apply"]
    if binary:
        command.append("--binary")
    command.extend(("--ignore-whitespace", "--ignore-space-change", str(patch_path)))
    completed = _run(command, cwd=tree, check=False)
    if completed.returncode == 0:
        return completed
    fallback = ["git", "apply", "--3way"]
    if binary:
        fallback.append("--binary")
    fallback.append(str(patch_path))
    return _run(fallback, cwd=tree, check=False)


def _runner_input_patch(path: Path) -> None:
    path.write_text(
        """diff --git a/.claim-plane-evaluator-marker b/.claim-plane-evaluator-marker
new file mode 100644
--- /dev/null
+++ b/.claim-plane-evaluator-marker
@@ -0,0 +1 @@
+claim-plane evaluator input prepared
""",
        encoding="utf-8",
    )


def _acceptance_artifact_dir(root: Path) -> Path:
    attempt_id = f"attempt-{time.time_ns()}"
    path = root / ".claim-plane" / "oss-pilot" / "acceptance" / attempt_id
    path.mkdir(parents=True, exist_ok=False)
    return path


def _finish_oss_pilot_acceptance(
    root: Path,
    *,
    classification: str,
    returncode: int,
    stdout: str = "",
    stderr: str = "",
    detail: str = "",
    emit_logs: bool = True,
    acceptance_witness: Mapping[str, Any] | None = None,
) -> int:
    manifest = load_oss_pilot_manifest(root)
    candidate = _candidate_identity(root, manifest)
    artifact_dir = _acceptance_artifact_dir(root)
    (artifact_dir / "stdout.log").write_text(stdout, encoding="utf-8")
    (artifact_dir / "stderr.log").write_text(stderr, encoding="utf-8")
    relative_dir = artifact_dir.relative_to(root).as_posix()
    unsigned = {
        "protocol": "claim-plane.oss-pilot-reverification.v1",
        "attempt_id": artifact_dir.name,
        "created_at": _utc_now(),
        "classification": classification,
        "returncode": returncode,
        "detail": detail,
        "manifest_digest": manifest.get("digest"),
        "candidate": candidate,
        "log_dir": relative_dir,
        "stdout_log": f"{relative_dir}/stdout.log",
        "stderr_log": f"{relative_dir}/stderr.log",
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
        "acceptance_witness": (
            dict(acceptance_witness) if acceptance_witness is not None else None
        ),
    }
    result = {**unsigned, "evidence_digest": _sha256(unsigned)}
    (artifact_dir / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    latest = root / ".claim-plane" / "oss-pilot" / "acceptance" / "latest.json"
    latest.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if emit_logs and stdout:
        print(stdout, end="" if stdout.endswith("\n") else "\n")
    if emit_logs and stderr:
        print(stderr, end="" if stderr.endswith("\n") else "\n", file=sys.stderr)
    print(OSS_PILOT_ACCEPTANCE_RESULT_MARKER + _canonical_json(result))
    return OSS_PILOT_ACCEPTANCE_EXIT_CODES[classification]


def _classify_runner_failure(stdout: str, stderr: str) -> tuple[str, str]:
    combined = f"{stdout}\n{stderr}"
    if "Repository safety check failed" in combined:
        return (
            "WORKSPACE_SAFETY_FAILED",
            "evaluator rejected the temporary workspace layout",
        )
    if "RUNNING_TESTS..." in combined:
        return "TEST_FAILED", "the official OSS task tests did not pass"
    if "INSTALLING_DEPENDENCIES..." in combined:
        return (
            "DEPENDENCY_INSTALL_FAILED",
            "dependency or test-environment setup failed",
        )
    if any(
        marker in combined
        for marker in (
            "patch does not apply",
            "does not match index",
            "patch failed:",
            "with conflicts",
        )
    ):
        return (
            "OFFICIAL_TEST_CONFLICT",
            "the official tests could not be combined with the candidate changes",
        )
    return (
        "EVALUATOR_ERROR",
        "the official evaluator exited before producing a test verdict",
    )


def _git_blob(root: Path, revision: str, relative: str) -> bytes | None:
    completed = subprocess.run(
        ("git", "show", f"{revision}:{relative}"),
        cwd=root,
        capture_output=True,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def _official_test_paths(tree: Path) -> tuple[str, ...]:
    changed = [item for item in _git(tree, "diff", "--name-only").splitlines() if item]
    untracked_output = _git(
        tree,
        "ls-files",
        "--others",
        "--exclude-standard",
    )
    untracked = [item for item in untracked_output.splitlines() if item]
    return tuple(dict.fromkeys((*changed, *untracked)))


def _merge_text_file(
    *,
    candidate: bytes,
    base: bytes,
    official: bytes,
    temp_parent: Path,
) -> bytes | None:
    merge_dir = temp_parent / f"merge-{time.time_ns()}"
    merge_dir.mkdir(parents=True)
    candidate_path = merge_dir / "candidate"
    base_path = merge_dir / "base"
    official_path = merge_dir / "official"
    candidate_path.write_bytes(candidate)
    base_path.write_bytes(base)
    official_path.write_bytes(official)
    completed = subprocess.run(
        (
            "git",
            "merge-file",
            "-p",
            str(candidate_path),
            str(base_path),
            str(official_path),
        ),
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout


def _merge_official_tests(
    *,
    base_root: Path,
    candidate_tree: Path,
    official_tree: Path,
    temp_parent: Path,
) -> tuple[bool, str]:
    for relative in _official_test_paths(official_tree):
        base = _git_blob(base_root, "HEAD", relative)
        candidate_path = candidate_tree / relative
        official_path = official_tree / relative
        candidate = candidate_path.read_bytes() if candidate_path.is_file() else None
        official = official_path.read_bytes() if official_path.is_file() else None

        if candidate == official:
            continue
        if official == base:
            continue
        if candidate == base:
            if official is None:
                candidate_path.unlink(missing_ok=True)
            else:
                candidate_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(official_path, candidate_path)
            continue
        if base is None:
            if candidate is None and official is not None:
                candidate_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(official_path, candidate_path)
                continue
            return False, relative
        if candidate is None or official is None:
            return False, relative
        if b"\0" in base or b"\0" in candidate or b"\0" in official:
            return False, relative
        merged = _merge_text_file(
            candidate=candidate,
            base=base,
            official=official,
            temp_parent=temp_parent,
        )
        if merged is None:
            return False, relative
        candidate_path.write_bytes(merged)
    return True, ""


def _timeout_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def run_oss_pilot_acceptance(
    workspace: str | Path,
    *,
    timeout: float = OSS_PILOT_ACCEPTANCE_TIMEOUT,
    progress: Callable[[str], None] | None = None,
    stream_output: bool = False,
    heartbeat_seconds: float = 15.0,
    cleanup_timeout: float = 15.0,
    cache_dir: str | Path | None = None,
    runner_path: str | Path | None = None,
    tests_patch_path: str | Path | None = None,
) -> int:
    root = Path(workspace).expanduser().resolve()
    manifest = load_oss_pilot_manifest(root)
    if runner_path is not None or tests_patch_path is not None:
        if runner_path is None or tests_patch_path is None:
            raise OssPilotError(
                "runner_path and tests_patch_path must be provided together"
            )
        runner = Path(runner_path).expanduser().resolve()
        tests_patch = Path(tests_patch_path).expanduser().resolve()
    else:
        source = manifest.get("source")
        if not isinstance(source, Mapping):
            source = {}
        task_dir_value = source.get("task_dir")
        feature_dir_value = source.get("feature_dir")
        if task_dir_value is None or feature_dir_value is None:
            return _finish_oss_pilot_acceptance(
                root,
                classification="EVALUATOR_ERROR",
                returncode=OSS_PILOT_ACCEPTANCE_EXIT_CODES["EVALUATOR_ERROR"],
                stderr="private evaluator assets were not supplied\n",
                detail="private evaluator assets were not supplied",
            )
        task_dir = Path(str(task_dir_value))
        feature_dir = Path(str(feature_dir_value))
        runner = task_dir / "run_tests.sh"
        tests_patch = feature_dir / "tests.patch"
    if not runner.exists() or not tests_patch.exists():
        return _finish_oss_pilot_acceptance(
            root,
            classification="EVALUATOR_ERROR",
            returncode=OSS_PILOT_ACCEPTANCE_EXIT_CODES["EVALUATOR_ERROR"],
            stderr="frozen CooperBench acceptance assets are missing\n",
            detail="frozen CooperBench acceptance assets are missing",
        )

    patch = _run(("git", "diff", "--binary", "HEAD"), cwd=root).stdout
    untracked_output = _git(
        root,
        "ls-files",
        "--others",
        "--exclude-standard",
    )
    untracked = [
        item
        for item in untracked_output.splitlines()
        if item and not item.startswith((".claim-plane/", ".codex/"))
    ]
    temp_parent = Path(tempfile.mkdtemp(prefix="claim-plane-oss-acceptance-"))
    tree = temp_parent / "agent_workspace" / "repository"
    official_tree = temp_parent / "official_tests" / "repository"
    tree.parent.mkdir(parents=True, exist_ok=True)
    official_tree.parent.mkdir(parents=True, exist_ok=True)
    preparation_stdout: list[str] = []
    preparation_stderr: list[str] = []
    if progress is not None:
        progress("Preparing isolated evaluator workspace")
    try:
        _run(("git", "worktree", "add", "--detach", str(tree), "HEAD"), cwd=root)
        _run(
            ("git", "worktree", "add", "--detach", str(official_tree), "HEAD"),
            cwd=root,
        )
        if progress is not None:
            progress("Merging official task tests")
        official_result = _apply_git_patch(official_tree, tests_patch)
        preparation_stdout.append(official_result.stdout)
        preparation_stderr.append(official_result.stderr)
        if official_result.returncode != 0:
            return _finish_oss_pilot_acceptance(
                root,
                classification="OFFICIAL_TEST_CONFLICT",
                returncode=official_result.returncode,
                stdout="".join(preparation_stdout),
                stderr="".join(preparation_stderr),
                detail="the official tests patch does not apply to the frozen base",
            )

        witness_spec = build_acceptance_witness_spec(
            base_root=root,
            official_tree=official_tree,
            tests_patch=tests_patch,
        )

        if patch:
            patch_path = temp_parent / "candidate.diff"
            patch_path.write_text(patch, encoding="utf-8")
            candidate_result = _apply_git_patch(tree, patch_path, binary=True)
            preparation_stdout.append(candidate_result.stdout)
            preparation_stderr.append(candidate_result.stderr)
            if candidate_result.returncode != 0:
                return _finish_oss_pilot_acceptance(
                    root,
                    classification="EVALUATOR_ERROR",
                    returncode=candidate_result.returncode,
                    stdout="".join(preparation_stdout),
                    stderr="".join(preparation_stderr),
                    detail=(
                        "candidate changes could not be materialized in the "
                        "evaluator workspace"
                    ),
                )

        for relative_text in untracked:
            relative = Path(relative_text)
            source_path = root / relative
            target = tree / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source_path.is_dir():
                shutil.copytree(source_path, target, dirs_exist_ok=True)
            else:
                shutil.copy2(source_path, target)

        merged, conflict_path = _merge_official_tests(
            base_root=root,
            candidate_tree=tree,
            official_tree=official_tree,
            temp_parent=temp_parent,
        )
        if not merged:
            return _finish_oss_pilot_acceptance(
                root,
                classification="OFFICIAL_TEST_CONFLICT",
                returncode=OSS_PILOT_ACCEPTANCE_EXIT_CODES["OFFICIAL_TEST_CONFLICT"],
                stdout="".join(preparation_stdout),
                stderr="".join(preparation_stderr),
                detail=(
                    "the official OSS tests conflict with candidate changes in "
                    f"{conflict_path}"
                ),
            )
        preparation_stdout.append(
            "Official OSS tests were merged into the candidate evaluation tree.\n"
        )

        runner_patch = temp_parent / "runner-input.patch"
        _runner_input_patch(runner_patch)
        witness_path = temp_parent / "acceptance-witness.json"
        env = prepare_pytest_witness_environment(
            plugin_dir=temp_parent / "pytest-witness-plugin",
            witness_path=witness_path,
            spec=witness_spec,
            source_env=os.environ,
        )
        env.pop("UV_SYSTEM_PYTHON", None)
        if cache_dir is not None:
            resolved_cache = Path(cache_dir).expanduser().resolve()
            resolved_cache.mkdir(parents=True, exist_ok=True)
            env["UV_CACHE_DIR"] = str(resolved_cache)
        evaluator_command = (
            "bash",
            str(runner.resolve()),
            str(tree.resolve()),
            str(runner_patch.resolve()),
        )
        if progress is not None:
            progress(f"Running official evaluator · timeout {int(timeout)}s")
        if stream_output:
            streamed = run_streaming_process(
                evaluator_command,
                cwd=tree,
                env=env,
                timeout=timeout,
                heartbeat_seconds=heartbeat_seconds,
                on_output=lambda name, line: print(
                    line,
                    end="",
                    file=sys.stderr if name == "stderr" else sys.stdout,
                    flush=True,
                ),
                on_heartbeat=(
                    None
                    if progress is None
                    else lambda elapsed: progress(
                        f"Official evaluator still running · {int(elapsed)}s elapsed"
                    )
                ),
            )
            completed_returncode = streamed.returncode
            completed_stdout = streamed.stdout
            completed_stderr = streamed.stderr
            if streamed.interrupted:
                return _finish_oss_pilot_acceptance(
                    root,
                    classification="INTERRUPTED",
                    returncode=OSS_PILOT_ACCEPTANCE_EXIT_CODES["INTERRUPTED"],
                    stdout="".join(preparation_stdout) + completed_stdout,
                    stderr="".join(preparation_stderr) + completed_stderr,
                    detail="official evaluator interrupted by the operator",
                    emit_logs=False,
                )
            if streamed.timed_out:
                return _finish_oss_pilot_acceptance(
                    root,
                    classification="TIMEOUT",
                    returncode=OSS_PILOT_ACCEPTANCE_EXIT_CODES["TIMEOUT"],
                    stdout="".join(preparation_stdout) + completed_stdout,
                    stderr=(
                        "".join(preparation_stderr)
                        + completed_stderr
                        + f"\nTimed out after {timeout}s\n"
                    ),
                    detail=f"official evaluator timed out after {timeout}s",
                    emit_logs=False,
                )
        else:
            try:
                completed = subprocess.run(
                    evaluator_command,
                    cwd=tree,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                    env=env,
                )
            except subprocess.TimeoutExpired as exc:
                stdout = _timeout_output(exc.stdout)
                stderr = _timeout_output(exc.stderr)
                stderr += f"\nTimed out after {timeout}s\n"
                return _finish_oss_pilot_acceptance(
                    root,
                    classification="TIMEOUT",
                    returncode=OSS_PILOT_ACCEPTANCE_EXIT_CODES["TIMEOUT"],
                    stdout="".join(preparation_stdout) + stdout,
                    stderr="".join(preparation_stderr) + stderr,
                    detail=f"official evaluator timed out after {timeout}s",
                )
            completed_returncode = completed.returncode
            completed_stdout = completed.stdout
            completed_stderr = completed.stderr

        stdout = "".join(preparation_stdout) + completed_stdout
        stderr = "".join(preparation_stderr) + completed_stderr
        witness = assess_acceptance_witness(witness_spec, witness_path)
        witness_state = str(witness.get("state") or "INCOMPLETE")
        if witness_state == "INCOMPLETE":
            details = witness.get("details") or ()
            detail = "private acceptance tests were not fully witnessed"
            if details:
                detail += ": " + json.dumps(details, ensure_ascii=False)[:1000]
            return _finish_oss_pilot_acceptance(
                root,
                classification="EVALUATOR_INCOMPLETE",
                returncode=OSS_PILOT_ACCEPTANCE_EXIT_CODES["EVALUATOR_INCOMPLETE"],
                stdout=stdout,
                stderr=stderr,
                detail=detail,
                emit_logs=not stream_output,
                acceptance_witness=witness,
            )
        if completed_returncode == 0 and witness_state in {"VERIFIED", "NOT_REQUIRED"}:
            return _finish_oss_pilot_acceptance(
                root,
                classification="PASS",
                returncode=0,
                stdout=stdout,
                stderr=stderr,
                detail=(
                    "official OSS task tests passed with complete acceptance witnesses"
                ),
                emit_logs=not stream_output,
                acceptance_witness=witness,
            )
        classification, detail = _classify_runner_failure(stdout, stderr)
        return _finish_oss_pilot_acceptance(
            root,
            classification=classification,
            returncode=completed_returncode,
            stdout=stdout,
            stderr=stderr,
            detail=detail,
            emit_logs=not stream_output,
            acceptance_witness=witness,
        )
    finally:
        if progress is not None:
            progress("Cleaning evaluator workspace")
        for worktree in (tree, official_tree):
            try:
                _run(
                    ("git", "worktree", "remove", "--force", str(worktree)),
                    cwd=root,
                    timeout=cleanup_timeout,
                    check=False,
                )
            except (OssPilotError, subprocess.TimeoutExpired, KeyboardInterrupt):
                pass
        removed = bounded_rmtree(temp_parent, timeout=cleanup_timeout)
        if not removed and progress is not None:
            progress(f"Cleanup continuing asynchronously: {temp_parent}")
