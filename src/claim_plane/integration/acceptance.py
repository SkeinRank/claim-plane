"""Explicit acceptance command execution with optional OS-level sandboxing."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from claim_plane.core.models import AcceptanceResult
from claim_plane.integration.sandbox import (
    SandboxPolicy,
    resolve_sandbox_command,
    sanitized_environment,
)


class AcceptanceRunner:
    def __init__(
        self,
        *,
        timeout_seconds: int = 300,
        output_tail_chars: int = 4000,
        sandbox_policy: SandboxPolicy | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.output_tail_chars = output_tail_chars
        self.sandbox_policy = sandbox_policy or SandboxPolicy()

    def run(
        self, commands: tuple[str, ...], repo_path: str | Path
    ) -> tuple[AcceptanceResult, ...]:
        results: list[AcceptanceResult] = []
        cwd = Path(repo_path).resolve()
        for command in commands:
            started = time.monotonic()
            try:
                sandbox = resolve_sandbox_command(command, cwd, self.sandbox_policy)
                completed = subprocess.run(
                    sandbox.argv,
                    cwd=cwd,
                    shell=False,
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    env=sanitized_environment(
                        allowlist=self.sandbox_policy.environment_allowlist
                    ),
                )
                returncode = completed.returncode
                stdout = completed.stdout
                stderr = completed.stderr
                backend = sandbox.backend
                enforced = sandbox.enforced
            except subprocess.TimeoutExpired as exc:
                returncode = 124
                stdout = _as_text(exc.stdout)
                stderr = _as_text(exc.stderr) + (
                    f"\nTimed out after {self.timeout_seconds}s"
                )
                backend = self.sandbox_policy.backend
                enforced = False
            except RuntimeError as exc:
                returncode = 125
                stdout = ""
                stderr = str(exc)
                backend = self.sandbox_policy.backend
                enforced = False
            duration_ms = int((time.monotonic() - started) * 1000)
            results.append(
                AcceptanceResult(
                    command=command,
                    returncode=returncode,
                    duration_ms=duration_ms,
                    stdout_tail=stdout[-self.output_tail_chars :],
                    stderr_tail=stderr[-self.output_tail_chars :],
                    sandbox_backend=backend,
                    sandbox_enforced=enforced,
                )
            )
            if returncode != 0:
                break
        return tuple(results)


def _as_text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
