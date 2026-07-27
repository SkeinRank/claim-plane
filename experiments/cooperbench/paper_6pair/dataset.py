"""CooperBench dataset discovery and repository preparation for the paper study."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..common import PairRef
from .config import FROZEN_PAIRS, PAIR_SELECTION_SEED, REPOSITORIES


@dataclass(frozen=True, slots=True)
class TaskInfo:
    repo: str
    task_id: int
    directory: Path
    clone_url: str
    base_commit: str
    features: dict[int, Path]


def sh(
    command: str,
    cwd: str | Path | None = None,
    timeout: int = 300,
) -> tuple[int, str, str]:
    completed = subprocess.run(
        command,
        shell=True,
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def q(value: object) -> str:
    return shlex.quote(str(value))


def parse_task_setup(task_dir: Path) -> tuple[str, str] | None:
    setup_file = task_dir / "setup.sh"
    if not setup_file.exists():
        return None
    setup = setup_file.read_text(encoding="utf-8", errors="replace")
    base_match = re.search(r'BASE_COMMIT=["\']?([0-9a-f]+)', setup)

    clone_url: str | None = None
    for line in setup.splitlines():
        if "git clone" not in line:
            continue
        url_match = re.search(r'(https?://[^\s"\']+|git@[^\s"\']+)', line)
        if url_match:
            clone_url = url_match.group(1)
            break

    if not (base_match and clone_url):
        return None
    return clone_url, base_match.group(1)


def load_tasks(
    dataset: str | Path,
    repos: Iterable[str] = REPOSITORIES,
) -> dict[tuple[str, int], TaskInfo]:
    root = Path(dataset).resolve()
    if not root.exists():
        raise FileNotFoundError(f"CooperBench dataset not found: {root}")

    allowed = set(repos)
    task_map: dict[tuple[str, int], TaskInfo] = {}
    for repo_dir in sorted(root.glob("*_task")):
        if repo_dir.name not in allowed:
            continue
        for task_dir in sorted(repo_dir.glob("task*")):
            parsed = parse_task_setup(task_dir)
            if parsed is None:
                continue
            clone_url, base_commit = parsed
            features: dict[int, Path] = {}
            for feature_dir in sorted(task_dir.glob("feature*")):
                if (
                    not (feature_dir / "feature.md").exists()
                    or not (feature_dir / "feature.patch").exists()
                ):
                    continue
                try:
                    feature_id = int(feature_dir.name.removeprefix("feature"))
                except ValueError:
                    continue
                features[feature_id] = feature_dir
            if len(features) < 2:
                continue
            try:
                task_id = int(task_dir.name.removeprefix("task"))
            except ValueError:
                continue
            task_map[(repo_dir.name, task_id)] = TaskInfo(
                repo=repo_dir.name,
                task_id=task_id,
                directory=task_dir,
                clone_url=clone_url,
                base_commit=base_commit,
                features=features,
            )
    if not task_map:
        raise RuntimeError("No compatible CooperBench tasks were found")
    return task_map


def validate_frozen_pairs(
    dataset: str | Path,
    pairs: Iterable[PairRef] = FROZEN_PAIRS,
) -> dict[tuple[str, int], TaskInfo]:
    tasks = load_tasks(dataset)
    missing: list[str] = []
    for pair in pairs:
        task = tasks.get((pair.repo, pair.task_id))
        if task is None:
            missing.append(f"{pair.repo}/task{pair.task_id}")
            continue
        for feature_id in (pair.feature_a, pair.feature_b):
            if feature_id not in task.features:
                missing.append(f"{pair.repo}/task{pair.task_id}/feature{feature_id}")
    if missing:
        raise RuntimeError(
            "CooperBench checkout does not contain all frozen paper inputs: "
            + ", ".join(sorted(set(missing)))
        )
    return tasks


def read_gold_conflicts(dataset: str | Path) -> set[tuple[str, int, frozenset[int]]]:
    path = Path(dataset) / "gold_conflict_report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        (
            str(item["repo"]),
            int(item["task_id"]),
            frozenset((int(item["f1"]), int(item["f2"]))),
        )
        for item in payload.get("conflict_pairs", [])
    }


def verify_pair_labels(
    dataset: str | Path,
    pairs: Iterable[PairRef] = FROZEN_PAIRS,
) -> None:
    conflicts = read_gold_conflicts(dataset)
    mismatches: list[str] = []
    for pair in pairs:
        actual = (
            pair.repo,
            pair.task_id,
            frozenset((pair.feature_a, pair.feature_b)),
        ) in conflicts
        if pair.gold_conflict is not None and actual != pair.gold_conflict:
            mismatches.append(
                f"{pair.key}: expected {pair.gold_conflict}, dataset reports {actual}"
            )
    if mismatches:
        raise RuntimeError(
            "Frozen pair labels differ from this CooperBench checkout: "
            + "; ".join(mismatches)
        )


def get_repo(clone_url: str, base_commit: str, repo_cache: str | Path) -> Path:
    cache = Path(repo_cache).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    repo_name = clone_url.rstrip("/").split("/")[-1].removesuffix(".git")
    path = cache / repo_name
    if not path.exists():
        rc, _out, err = sh(f"git clone -q {q(clone_url)} {q(path)}", timeout=600)
        if rc != 0:
            raise RuntimeError(f"clone failed for {clone_url}: {err[-1000:]}")

    sh(f"git -C {q(path)} worktree prune")
    sh(f"git -C {q(path)} fetch -q origin {q(base_commit)}", timeout=300)
    rc, out, err = sh(f"git -C {q(path)} checkout -q {q(base_commit)}", timeout=120)
    if rc != 0:
        raise RuntimeError(f"cannot checkout base {base_commit}: {(out + err)[-1000:]}")
    rc, out, err = sh(f"git -C {q(path)} reset --hard -q {q(base_commit)}")
    if rc != 0:
        raise RuntimeError((out + err)[-1000:])
    rc, out, err = sh(f"git -C {q(path)} clean -qfdx")
    if rc != 0:
        raise RuntimeError((out + err)[-1000:])

    # The coding harness creates local commits; keep identity local to the cache.
    sh(f"git -C {q(path)} config user.email agent@cooperbench.local")
    sh(f"git -C {q(path)} config user.name claim-plane-benchmark")
    return path


def stable_seed(pair: PairRef, repetition: int, role: str, phase: str) -> int:
    raw = (
        f"{PAIR_SELECTION_SEED}|{pair.repo}|{pair.task_id}|{pair.feature_a}|"
        f"{pair.feature_b}|{repetition}|{role}|{phase}"
    )
    return int(hashlib.sha256(raw.encode()).hexdigest()[:8], 16) % 2_000_000_000
