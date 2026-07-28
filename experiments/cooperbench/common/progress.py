"""Terminal progress reporting for long-running research studies.

Progress is intentionally written to stderr so stdout remains available for stable,
machine-readable CLI payloads.
"""

from __future__ import annotations

import os
import statistics
import sys
import threading
import time
from dataclasses import dataclass
from typing import Iterable, TextIO


@dataclass(frozen=True)
class ProgressUnit:
    """One durable execution unit shown by the research progress reporter."""

    unit_id: str
    label: str
    arm: str


class ResearchProgress:
    """Render resumable progress, elapsed time, and a conservative ETA."""

    def __init__(
        self,
        title: str,
        units: Iterable[ProgressUnit],
        *,
        completed_units: Iterable[str] = (),
        historical_durations: dict[str, float] | None = None,
        stream: TextIO | None = None,
        heartbeat_seconds: float = 1.0,
        unit_noun: str = "executions",
        historical_costs: dict[str, float] | None = None,
    ) -> None:
        self.title = title
        self.units = tuple(units)
        self.stream = stream or sys.stderr
        self.heartbeat_seconds = max(float(heartbeat_seconds), 0.2)
        self.unit_noun = unit_noun.strip() or "executions"
        self._completed = set(completed_units)
        self._durations_by_arm: dict[str, list[float]] = {}
        self._duration_by_unit: dict[str, float] = {}
        self._cost_by_unit: dict[str, float] = {}
        self._started_at = time.monotonic()
        self._current: ProgressUnit | None = None
        self._current_started_at: float | None = None
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_render_len = 0
        self._enabled = os.getenv("CLAIM_PLANE_PROGRESS", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self._tty = bool(getattr(self.stream, "isatty", lambda: False)())

        for unit_id, seconds in (historical_durations or {}).items():
            unit = self._unit_by_id(unit_id)
            if unit is None or seconds <= 0:
                continue
            self._remember_duration(unit, float(seconds))
        for unit_id, cost in (historical_costs or {}).items():
            if self._unit_by_id(unit_id) is None or cost < 0:
                continue
            self._cost_by_unit[unit_id] = float(cost)

    @property
    def total(self) -> int:
        return len(self.units)

    @property
    def completed(self) -> int:
        return sum(1 for unit in self.units if unit.unit_id in self._completed)

    def start(self) -> None:
        if not self._enabled:
            return
        resume = f" · resume {self.completed}/{self.total}" if self.completed else ""
        self._write_line(
            f"Claim Plane research · {self.title} · {self.total} {self.unit_noun}{resume}"
        )
        self._write_line(self._summary_line())

    def phase(
        self, index: int, total: int, name: str, *, detail: str | None = None
    ) -> None:
        if not self._enabled:
            return
        suffix = f" · {detail}" if detail else ""
        self._write_line(f"[stage {index}/{total}] {name}{suffix}")

    def activity(self, category: str, current: int, total: int, label: str) -> None:
        if not self._enabled:
            return
        self._write_line(f"[{category} {current}/{total}] {label}")

    def start_unit(self, unit_id: str) -> None:
        unit = self._unit_by_id(unit_id)
        if unit is None:
            raise ValueError(f"unknown progress unit: {unit_id}")
        self._stop_heartbeat()
        with self._lock:
            self._current = unit
            self._current_started_at = time.monotonic()
        if not self._enabled:
            return
        if self._tty:
            self._render_current()
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._heartbeat_loop,
                name="claim-plane-progress",
                daemon=True,
            )
            self._thread.start()
        else:
            self._write_line(self._summary_line(current=unit, current_elapsed=0.0))

    def complete_unit(
        self,
        unit_id: str,
        *,
        duration_seconds: float | None = None,
        result: str | None = None,
        cost: float | None = None,
    ) -> float:
        unit = self._unit_by_id(unit_id)
        if unit is None:
            raise ValueError(f"unknown progress unit: {unit_id}")
        with self._lock:
            started = self._current_started_at
        duration = (
            float(duration_seconds)
            if duration_seconds is not None
            else max(0.0, time.monotonic() - started)
            if started is not None
            else 0.0
        )
        self._stop_heartbeat()
        self._remember_duration(unit, duration)
        if cost is not None:
            self._cost_by_unit[unit_id] = float(cost)
        self._completed.add(unit_id)
        with self._lock:
            self._current = None
            self._current_started_at = None

        if self._enabled:
            extra: list[str] = []
            if result:
                extra.append(f"result {result}")
            if cost is not None:
                extra.append(f"cost ${cost:.4f}")
            extra.append(f"unit {format_duration(duration)}")
            suffix = " · " + " · ".join(extra) if extra else ""
            self._write_line(self._summary_line() + suffix)
        return duration

    def fail_unit(self, unit_id: str, error: BaseException) -> None:
        self._stop_heartbeat()
        unit = self._unit_by_id(unit_id)
        if self._enabled:
            label = unit.label if unit is not None else unit_id
            self._write_line(f"[failed] {label} · {type(error).__name__}: {error}")
        with self._lock:
            self._current = None
            self._current_started_at = None

    def finish(self, *, detail: str | None = None) -> None:
        self._stop_heartbeat()
        if not self._enabled:
            return
        elapsed = time.monotonic() - self._started_at
        suffix = f" · {detail}" if detail else ""
        self._write_line(
            f"[complete] {self.completed}/{self.total} · elapsed {format_duration(elapsed)}{suffix}"
        )

    def close(self) -> None:
        self._stop_heartbeat()

    def _unit_by_id(self, unit_id: str) -> ProgressUnit | None:
        for unit in self.units:
            if unit.unit_id == unit_id:
                return unit
        return None

    def _remember_duration(self, unit: ProgressUnit, seconds: float) -> None:
        if seconds <= 0:
            return
        self._duration_by_unit[unit.unit_id] = seconds
        bucket = self._durations_by_arm.setdefault(unit.arm, [])
        if not bucket or seconds not in bucket:
            bucket.append(seconds)

    def _heartbeat_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.wait(self.heartbeat_seconds):
            self._render_current()

    def _render_current(self) -> None:
        if not self._enabled:
            return
        with self._lock:
            unit = self._current
            started = self._current_started_at
        if unit is None:
            return
        elapsed = max(0.0, time.monotonic() - started) if started is not None else 0.0
        line = self._summary_line(current=unit, current_elapsed=elapsed)
        if self._tty:
            padding = " " * max(0, self._last_render_len - len(line))
            self.stream.write("\r" + line + padding)
            self.stream.flush()
            self._last_render_len = len(line)
        else:
            self._write_line(line)

    def _stop_heartbeat(self) -> None:
        stop = self._stop_event
        thread = self._thread
        if stop is not None:
            stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(self.heartbeat_seconds * 2, 1.0))
        if self._tty and self._last_render_len:
            self.stream.write("\r" + " " * self._last_render_len + "\r")
            self.stream.flush()
        self._stop_event = None
        self._thread = None
        self._last_render_len = 0

    def _summary_line(
        self,
        *,
        current: ProgressUnit | None = None,
        current_elapsed: float = 0.0,
    ) -> str:
        completed = self.completed
        total = self.total
        percent = (100.0 * completed / total) if total else 100.0
        bar = progress_bar(completed, total)
        elapsed = time.monotonic() - self._started_at
        eta = self._estimate_eta(current=current, current_elapsed=current_elapsed)
        eta_text = (
            f"ETA ~{format_duration(eta)}" if eta is not None else "ETA calculating"
        )
        line = (
            f"[{completed:>2}/{total}] {bar} {percent:5.1f}%"
            f" · elapsed {format_duration(elapsed)} · {eta_text}"
        )
        completed_cost = sum(
            self._cost_by_unit.get(unit.unit_id, 0.0)
            for unit in self.units
            if unit.unit_id in self._completed
        )
        if self._cost_by_unit:
            line += f" · spent ${completed_cost:.4f}"
        if current is not None:
            line += f" · {current.label} · running {format_duration(current_elapsed)}"
        return line

    def _estimate_eta(
        self,
        *,
        current: ProgressUnit | None,
        current_elapsed: float,
    ) -> float | None:
        known = [seconds for seconds in self._duration_by_unit.values() if seconds > 0]
        if not known:
            return None
        global_mean = statistics.fmean(known)
        remaining = [unit for unit in self.units if unit.unit_id not in self._completed]
        estimate = 0.0
        for unit in remaining:
            arm_samples = self._durations_by_arm.get(unit.arm, [])
            prediction = statistics.fmean(arm_samples) if arm_samples else global_mean
            if current is not None and unit.unit_id == current.unit_id:
                prediction = max(0.0, prediction - current_elapsed)
            estimate += prediction
        return max(0.0, estimate)

    def _write_line(self, text: str) -> None:
        if self._tty and self._last_render_len:
            self.stream.write("\r" + " " * self._last_render_len + "\r")
            self._last_render_len = 0
        self.stream.write(text + "\n")
        self.stream.flush()


def progress_bar(completed: int, total: int, *, width: int = 20) -> str:
    """Return a fixed-width Unicode progress bar."""
    if width <= 0:
        raise ValueError("progress bar width must be positive")
    ratio = 1.0 if total <= 0 else min(max(completed / total, 0.0), 1.0)
    filled = int(ratio * width)
    return "[" + ("█" * filled) + ("░" * (width - filled)) + "]"


def format_duration(seconds: float | None) -> str:
    """Format a duration as HH:MM:SS for stable terminal output."""
    if seconds is None:
        return "--:--:--"
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
