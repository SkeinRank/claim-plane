"""Small runtime-progress primitives for long operator-facing subprocesses."""

from __future__ import annotations

import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence, TextIO


@dataclass(frozen=True, slots=True)
class StreamingProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    interrupted: bool = False


class ProgressReporter:
    """Render phase changes and silence heartbeats without terminal control codes."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        stream: TextIO,
        started: float | None = None,
        prefix: str = "Claim Plane",
    ) -> None:
        self.enabled = enabled
        self.stream = stream
        self.started = time.monotonic() if started is None else started
        self.prefix = prefix

    @staticmethod
    def duration(seconds: float) -> str:
        total = max(0, int(seconds))
        minutes, second = divmod(total, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:d}h {minutes:02d}m {second:02d}s"
        return f"{minutes:d}m {second:02d}s"

    def emit(self, message: str) -> None:
        if not self.enabled:
            return
        elapsed = self.duration(time.monotonic() - self.started)
        print(f"[{elapsed}] {self.prefix}: {message}", file=self.stream, flush=True)


@contextmanager
def periodic_heartbeat(
    callback: Callable[[str], None] | None,
    message: str,
    *,
    interval: float = 15.0,
) -> Iterator[None]:
    """Emit bounded periodic progress while a synchronous phase is running."""

    if callback is None:
        yield
        return
    stopped = threading.Event()
    started = time.monotonic()

    def pulse() -> None:
        while not stopped.wait(max(0.1, interval)):
            elapsed = ProgressReporter.duration(time.monotonic() - started)
            callback(f"{message} still running · {elapsed}")

    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass


def run_streaming_process(
    command: Sequence[str],
    *,
    cwd: str | Path,
    env: Mapping[str, str] | None = None,
    timeout: float,
    heartbeat_seconds: float = 15.0,
    on_output: Callable[[str, str], None] | None = None,
    on_heartbeat: Callable[[float], None] | None = None,
) -> StreamingProcessResult:
    """Run a subprocess with live output, silence heartbeats, timeout, and Ctrl-C."""

    process = subprocess.Popen(
        list(command),
        cwd=str(cwd),
        env=None if env is None else dict(env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        start_new_session=os.name == "posix",
    )
    events: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def reader(name: str, stream: TextIO) -> None:
        try:
            for line in iter(stream.readline, ""):
                events.put((name, line))
        finally:
            events.put((name, None))

    assert process.stdout is not None
    assert process.stderr is not None
    threads = (
        threading.Thread(target=reader, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=reader, args=("stderr", process.stderr), daemon=True),
    )
    for thread in threads:
        thread.start()

    started = time.monotonic()
    next_heartbeat = started + max(0.05, heartbeat_seconds)
    open_streams = 2
    stdout: list[str] = []
    stderr: list[str] = []
    timed_out = False
    interrupted = False
    try:
        while open_streams:
            now = time.monotonic()
            if now - started >= timeout:
                timed_out = True
                _terminate_process(process)
                break
            wait = min(0.5, max(0.05, next_heartbeat - now))
            try:
                name, line = events.get(timeout=wait)
            except queue.Empty:
                now = time.monotonic()
                if now >= next_heartbeat:
                    if on_heartbeat is not None:
                        on_heartbeat(now - started)
                    next_heartbeat = now + max(0.05, heartbeat_seconds)
                continue
            if line is None:
                open_streams -= 1
                continue
            if name == "stdout":
                stdout.append(line)
            else:
                stderr.append(line)
            if on_output is not None:
                on_output(name, line)
            next_heartbeat = time.monotonic() + max(0.05, heartbeat_seconds)
    except KeyboardInterrupt:
        interrupted = True
        _terminate_process(process)
    finally:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _terminate_process(process)

    return StreamingProcessResult(
        returncode=(
            130 if interrupted else 124 if timed_out else int(process.returncode or 0)
        ),
        stdout="".join(stdout),
        stderr="".join(stderr),
        timed_out=timed_out,
        interrupted=interrupted,
    )


def bounded_rmtree(path: Path, *, timeout: float = 15.0) -> bool:
    """Remove a potentially large tree without blocking the operator indefinitely."""

    if not path.exists():
        return True
    finished = threading.Event()

    def remove() -> None:
        try:
            shutil.rmtree(path, ignore_errors=True)
        finally:
            finished.set()

    threading.Thread(target=remove, daemon=True).start()
    return finished.wait(max(0.1, timeout))
