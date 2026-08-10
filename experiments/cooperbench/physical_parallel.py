"""Physical concurrency instrumentation for CooperBench experiments.

This module deliberately separates two forms of concurrency:

* inner concurrency: two coding workers for one pair are active at the same time;
* outer concurrency: independent pair processes are executed through a bounded pool.

Outer workers are subprocesses so provider counters, trace buffers, Git worktrees, and
Claim Plane state cannot leak between benchmark pairs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

PHYSICAL_PARALLEL_PROTOCOL = "claim-plane.physical-parallel-benchmark.v2"


@dataclass(frozen=True, slots=True)
class ActivityInterval:
    """One wall-clock activity interval measured in epoch nanoseconds."""

    label: str
    started_ns: int
    finished_ns: int

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("activity interval label must not be empty")
        if self.started_ns <= 0 or self.finished_ns <= 0:
            raise ValueError("activity interval timestamps must be positive")
        if self.finished_ns < self.started_ns:
            raise ValueError("activity interval cannot finish before it starts")

    @property
    def duration_seconds(self) -> float:
        return (self.finished_ns - self.started_ns) / 1_000_000_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "started_ns": self.started_ns,
            "finished_ns": self.finished_ns,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True, slots=True)
class OverlapMetrics:
    """Measured physical overlap for two activity intervals."""

    overlap_seconds: float
    union_seconds: float
    shorter_interval_seconds: float
    overlap_fraction_of_shorter: float
    concurrent: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "overlap_seconds": self.overlap_seconds,
            "union_seconds": self.union_seconds,
            "shorter_interval_seconds": self.shorter_interval_seconds,
            "overlap_fraction_of_shorter": self.overlap_fraction_of_shorter,
            "concurrent": self.concurrent,
        }


def interval_overlap(left: ActivityInterval, right: ActivityInterval) -> OverlapMetrics:
    """Return physical overlap using wall-clock intervals, not logical latency."""

    overlap_ns = max(
        0,
        min(left.finished_ns, right.finished_ns)
        - max(left.started_ns, right.started_ns),
    )
    union_ns = max(left.finished_ns, right.finished_ns) - min(
        left.started_ns, right.started_ns
    )
    shorter_ns = min(
        left.finished_ns - left.started_ns,
        right.finished_ns - right.started_ns,
    )
    fraction = (overlap_ns / shorter_ns) if shorter_ns > 0 else 0.0
    return OverlapMetrics(
        overlap_seconds=overlap_ns / 1_000_000_000,
        union_seconds=union_ns / 1_000_000_000,
        shorter_interval_seconds=shorter_ns / 1_000_000_000,
        overlap_fraction_of_shorter=fraction,
        concurrent=overlap_ns > 0,
    )


def aggregate_worker_overlap(intervals: Sequence[ActivityInterval]) -> dict[str, Any]:
    """Summarize outer pair-process concurrency from parent-observed intervals."""

    if not intervals:
        return {
            "worker_count": 0,
            "wall_time_seconds": 0.0,
            "sum_worker_seconds": 0.0,
            "parallel_efficiency": 0.0,
            "peak_active_workers": 0,
        }

    events: list[tuple[int, int]] = []
    for interval in intervals:
        events.append((interval.started_ns, 1))
        events.append((interval.finished_ns, -1))
    # Finish before start for identical timestamps to avoid reporting a false overlap.
    events.sort(key=lambda item: (item[0], item[1]))
    active = 0
    peak = 0
    for _stamp, delta in events:
        active += delta
        peak = max(peak, active)

    wall_ns = max(item.finished_ns for item in intervals) - min(
        item.started_ns for item in intervals
    )
    sum_ns = sum(item.finished_ns - item.started_ns for item in intervals)
    efficiency = (sum_ns / wall_ns) if wall_ns > 0 else 0.0
    return {
        "worker_count": len(intervals),
        "wall_time_seconds": wall_ns / 1_000_000_000,
        "sum_worker_seconds": sum_ns / 1_000_000_000,
        # Average number of active pair workers over the batch wall-clock interval.
        "parallel_efficiency": efficiency,
        "peak_active_workers": peak,
    }


def parse_pair_indexes(value: str, *, pair_count: int) -> tuple[int, ...]:
    """Parse one-based indexes such as ``1-6,9,12`` deterministically."""

    selected: set[int] = set()
    for token in (part.strip() for part in value.split(",")):
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise ValueError(f"invalid descending pair range: {token}")
            selected.update(range(start, end + 1))
        else:
            selected.add(int(token))
    if not selected:
        raise ValueError("at least one pair index is required")
    invalid = sorted(index for index in selected if not 1 <= index <= pair_count)
    if invalid:
        raise ValueError(
            f"pair indexes must be within 1..{pair_count}; invalid: {invalid}"
        )
    return tuple(sorted(selected))


def _run_pair_process(command: Sequence[str], *, label: str) -> dict[str, Any]:
    started_ns = time.time_ns()
    env = os.environ.copy()
    project_root = Path(__file__).resolve().parents[2]
    import_roots = [str(project_root / "src"), str(project_root)]
    if env.get("PYTHONPATH"):
        import_roots.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(import_roots)
    completed = subprocess.run(
        list(command),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    finished_ns = time.time_ns()
    interval = ActivityInterval(label, started_ns, finished_ns)
    payload: dict[str, Any] | None = None
    if completed.stdout.strip():
        try:
            decoded = json.loads(completed.stdout)
            if isinstance(decoded, dict):
                payload = decoded
        except json.JSONDecodeError:
            payload = None
    return {
        "label": label,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "payload": payload,
        "interval": interval,
    }


def run_bounded_pair_processes(
    commands: Iterable[tuple[str, Sequence[str]]],
    *,
    max_parallel_pairs: int,
) -> dict[str, Any]:
    """Execute independent pair commands concurrently with strict bounded fan-out."""

    if max_parallel_pairs <= 0:
        raise ValueError("max_parallel_pairs must be positive")
    rows = tuple(commands)
    if not rows:
        raise ValueError("at least one pair command is required")

    batch_started_ns = time.time_ns()
    outcomes: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_parallel_pairs) as executor:
        pending = {
            executor.submit(_run_pair_process, command, label=label): label
            for label, command in rows
        }
        for future in as_completed(pending):
            outcomes.append(future.result())
    batch_finished_ns = time.time_ns()

    outcomes.sort(key=lambda item: item["label"])
    intervals = [item["interval"] for item in outcomes]
    summary = aggregate_worker_overlap(intervals)
    failed = [item for item in outcomes if int(item["returncode"]) != 0]
    return {
        "protocol": PHYSICAL_PARALLEL_PROTOCOL,
        "max_parallel_pairs": max_parallel_pairs,
        "batch_started_ns": batch_started_ns,
        "batch_finished_ns": batch_finished_ns,
        "batch_wall_time_seconds": (batch_finished_ns - batch_started_ns)
        / 1_000_000_000,
        "outer_concurrency": summary,
        "complete": not failed,
        "workers": [
            {
                "label": item["label"],
                "returncode": item["returncode"],
                "interval": item["interval"].to_dict(),
                "payload": item["payload"],
                "stdout_tail": str(item["stdout"])[-4000:],
                "stderr_tail": str(item["stderr"])[-4000:],
            }
            for item in outcomes
        ],
    }


def python_module_command(*args: str) -> tuple[str, ...]:
    """Build a command using the exact interpreter running the parent benchmark."""

    return (sys.executable, "-m", "experiments.cooperbench", *args)


__all__ = [
    "ActivityInterval",
    "OverlapMetrics",
    "PHYSICAL_PARALLEL_PROTOCOL",
    "aggregate_worker_overlap",
    "interval_overlap",
    "parse_pair_indexes",
    "python_module_command",
    "run_bounded_pair_processes",
]
