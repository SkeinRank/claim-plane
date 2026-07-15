"""Run a task-paired A/B/C experiment for parallel coding-agent integration.

Adapter commands receive task/arm/repeat metadata through environment variables
and must print one final JSON object.  The harness validates required metrics,
keeps raw evidence paths, and computes paired deltas rather than mixing unlike
repository tasks into one aggregate.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


REQUIRED_METRICS = {
    "clean",
    "clean_first_attempt",
    "input_tokens",
    "output_tokens",
    "failed_ci_cycles",
    "repair_attempts",
    "human_minutes",
    "unsafe_admission",
    "false_serialization",
}


@dataclass(frozen=True, slots=True)
class Result:
    task_id: str
    arm: str
    repeat: int
    returncode: int
    duration_seconds: float
    metrics: Mapping[str, Any]
    stdout_tail: str
    stderr_tail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "arm": self.arm,
            "repeat": self.repeat,
            "returncode": self.returncode,
            "duration_seconds": self.duration_seconds,
            "metrics": dict(self.metrics),
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


def _last_json(text: str) -> dict[str, Any]:
    for line in reversed([item.strip() for item in text.splitlines() if item.strip()]):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _validate_metrics(metrics: Mapping[str, Any]) -> list[str]:
    missing = sorted(REQUIRED_METRICS - set(metrics))
    errors = [f"missing metric: {name}" for name in missing]
    for name in (
        "input_tokens",
        "output_tokens",
        "failed_ci_cycles",
        "repair_attempts",
    ):
        if name in metrics and not isinstance(metrics[name], int):
            errors.append(f"{name} must be an integer")
    for name in (
        "clean",
        "clean_first_attempt",
        "unsafe_admission",
        "false_serialization",
    ):
        if name in metrics and not isinstance(metrics[name], bool):
            errors.append(f"{name} must be a boolean")
    return errors


def _aggregate(results: list[Result]) -> dict[str, Any]:
    groups: dict[str, list[Result]] = {}
    for result in results:
        groups.setdefault(result.arm, []).append(result)
    aggregate: dict[str, Any] = {}
    for arm, items in groups.items():
        count = len(items)
        aggregate[arm] = {
            "runs": count,
            "success_rate": sum(item.returncode == 0 for item in items) / count,
            "clean_rate": sum(bool(item.metrics.get("clean")) for item in items)
            / count,
            "clean_first_attempt_rate": sum(
                bool(item.metrics.get("clean_first_attempt")) for item in items
            )
            / count,
            "unsafe_admission_rate": sum(
                bool(item.metrics.get("unsafe_admission")) for item in items
            )
            / count,
            "false_serialization_rate": sum(
                bool(item.metrics.get("false_serialization")) for item in items
            )
            / count,
            "mean_duration_seconds": sum(item.duration_seconds for item in items)
            / count,
            "total_tokens": sum(
                int(item.metrics.get("input_tokens", 0))
                + int(item.metrics.get("output_tokens", 0))
                for item in items
            ),
            "failed_ci_cycles": sum(
                int(item.metrics.get("failed_ci_cycles", 0)) for item in items
            ),
            "repair_attempts": sum(
                int(item.metrics.get("repair_attempts", 0)) for item in items
            ),
            "human_minutes": sum(
                float(item.metrics.get("human_minutes", 0.0)) for item in items
            ),
        }
    return aggregate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("spec")
    parser.add_argument("--out", default="frontier-pack-results.json")
    args = parser.parse_args()
    spec_path = Path(args.spec).resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    cwd = Path(spec.get("cwd") or spec_path.parent).resolve()
    task_pack_path = Path(spec["task_pack"])
    if not task_pack_path.is_absolute():
        task_pack_path = spec_path.parent / task_pack_path
    task_pack = json.loads(task_pack_path.read_text(encoding="utf-8"))
    tasks = task_pack["tasks"]
    arms = spec["arms"]
    repeats = int(spec.get("repeats", 3))
    timeout = int(spec.get("timeout_seconds", 3600))
    results: list[Result] = []
    validation_errors: list[dict[str, Any]] = []

    for task in tasks:
        for repeat in range(1, repeats + 1):
            for arm in arms:
                env = {
                    **os.environ,
                    "CLAIM_PLANE_BENCH_TASK_ID": str(task["id"]),
                    "CLAIM_PLANE_BENCH_TASK_JSON": json.dumps(task, sort_keys=True),
                    "CLAIM_PLANE_BENCH_ARM": str(arm["name"]),
                    "CLAIM_PLANE_BENCH_REPEAT": str(repeat),
                    "CLAIM_PLANE_BENCH_SPEC": str(spec_path),
                }
                started = time.monotonic()
                try:
                    completed = subprocess.run(
                        str(arm["command"]),
                        cwd=cwd,
                        shell=True,
                        text=True,
                        capture_output=True,
                        timeout=timeout,
                        env=env,
                        check=False,
                    )
                    returncode = completed.returncode
                    stdout, stderr = completed.stdout, completed.stderr
                except subprocess.TimeoutExpired as exc:
                    returncode = 124
                    stdout = str(exc.stdout or "")
                    stderr = str(exc.stderr or "") + f"\nTimed out after {timeout}s"
                metrics = _last_json(stdout)
                errors = _validate_metrics(metrics)
                if errors:
                    validation_errors.append(
                        {
                            "task_id": task["id"],
                            "arm": arm["name"],
                            "repeat": repeat,
                            "errors": errors,
                        }
                    )
                results.append(
                    Result(
                        task_id=str(task["id"]),
                        arm=str(arm["name"]),
                        repeat=repeat,
                        returncode=returncode,
                        duration_seconds=time.monotonic() - started,
                        metrics=metrics,
                        stdout_tail=stdout[-4000:],
                        stderr_tail=stderr[-4000:],
                    )
                )

    payload = {
        "protocol": "claim-plane.frontier-benchmark.v1",
        "spec": spec,
        "task_pack_sha256": __import__("hashlib")
        .sha256(task_pack_path.read_bytes())
        .hexdigest(),
        "validation_errors": validation_errors,
        "results": [item.to_dict() for item in results],
        "aggregate": _aggregate(results),
    }
    Path(args.out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["aggregate"], indent=2))
    return (
        0
        if not validation_errors and all(item.returncode == 0 for item in results)
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
