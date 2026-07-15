"""Adapter-driven A/B/C benchmark harness for clean-integration experiments.

Each arm is an external command so the same harness can drive worktree-only,
planner-only, and Claim Plane workflows without embedding a model provider.
Commands should print a final JSON object containing optional metrics such as
``clean``, ``input_tokens``, ``output_tokens``, ``cost_usd``, and
``human_minutes``.
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


@dataclass(frozen=True, slots=True)
class Arm:
    name: str
    command: str


@dataclass(frozen=True, slots=True)
class Run:
    arm: str
    repeat: int
    returncode: int
    duration_seconds: float
    metrics: Mapping[str, Any]
    stdout_tail: str
    stderr_tail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "repeat": self.repeat,
            "returncode": self.returncode,
            "duration_seconds": self.duration_seconds,
            "metrics": dict(self.metrics),
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


def _last_json_object(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _aggregate(runs: list[Run]) -> dict[str, Any]:
    by_arm: dict[str, list[Run]] = {}
    for run in runs:
        by_arm.setdefault(run.arm, []).append(run)
    output: dict[str, Any] = {}
    for arm, items in by_arm.items():
        durations = [item.duration_seconds for item in items]
        clean = [bool(item.metrics.get("clean")) for item in items]
        output[arm] = {
            "runs": len(items),
            "success_rate": sum(item.returncode == 0 for item in items) / len(items),
            "clean_rate": sum(clean) / len(clean),
            "mean_duration_seconds": sum(durations) / len(durations),
            "total_input_tokens": sum(
                int(item.metrics.get("input_tokens", 0)) for item in items
            ),
            "total_output_tokens": sum(
                int(item.metrics.get("output_tokens", 0)) for item in items
            ),
            "total_cost_usd": sum(
                float(item.metrics.get("cost_usd", 0.0)) for item in items
            ),
            "total_human_minutes": sum(
                float(item.metrics.get("human_minutes", 0.0)) for item in items
            ),
            "total_failed_ci_cycles": sum(
                int(item.metrics.get("failed_ci_cycles", 0)) for item in items
            ),
            "total_repair_attempts": sum(
                int(item.metrics.get("repair_attempts", 0)) for item in items
            ),
            "clean_first_attempt_rate": sum(
                bool(item.metrics.get("clean_first_attempt")) for item in items
            )
            / len(items),
        }
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("spec")
    parser.add_argument("--out", default="benchmark-results.json")
    args = parser.parse_args()
    spec_path = Path(args.spec).resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    repeats = int(spec.get("repeats", 3))
    timeout = int(spec.get("timeout_seconds", 3600))
    cwd = Path(spec.get("cwd") or spec_path.parent).resolve()
    arms = [Arm(str(item["name"]), str(item["command"])) for item in spec["arms"]]
    runs: list[Run] = []
    for repeat in range(1, repeats + 1):
        for arm in arms:
            env = {
                **os.environ,
                "CLAIM_PLANE_BENCH_ARM": arm.name,
                "CLAIM_PLANE_BENCH_REPEAT": str(repeat),
                "CLAIM_PLANE_BENCH_SPEC": str(spec_path),
            }
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    arm.command,
                    cwd=cwd,
                    shell=True,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    env=env,
                    check=False,
                )
                returncode = completed.returncode
                stdout = completed.stdout
                stderr = completed.stderr
            except subprocess.TimeoutExpired as exc:
                returncode = 124
                stdout = str(exc.stdout or "")
                stderr = str(exc.stderr or "") + f"\nTimed out after {timeout}s"
            runs.append(
                Run(
                    arm=arm.name,
                    repeat=repeat,
                    returncode=returncode,
                    duration_seconds=time.monotonic() - started,
                    metrics=_last_json_object(stdout),
                    stdout_tail=stdout[-4000:],
                    stderr_tail=stderr[-4000:],
                )
            )
    payload = {
        "protocol": "claim-plane.abc-benchmark.v1",
        "spec": spec,
        "runs": [run.to_dict() for run in runs],
        "aggregate": _aggregate(runs),
    }
    Path(args.out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload["aggregate"], ensure_ascii=False, indent=2))
    return 0 if all(run.returncode == 0 for run in runs) else 2


if __name__ == "__main__":
    raise SystemExit(main())
